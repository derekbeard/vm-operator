// © Broadcom. All Rights Reserved.
// The term "Broadcom" refers to Broadcom Inc. and/or its subsidiaries.
// SPDX-License-Identifier: Apache-2.0

package main_test

import (
	"bytes"
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"fmt"
	"io"
	"math/big"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	authnv1 "k8s.io/api/authentication/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/labels"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/util/intstr"
	"k8s.io/client-go/discovery"
	"k8s.io/client-go/discovery/cached/memory"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"k8s.io/client-go/rest"
	"k8s.io/client-go/restmapper"
	"k8s.io/client-go/tools/clientcmd"
	clientcmdapi "k8s.io/client-go/tools/clientcmd/api"
	cmclient "k8s.io/metrics/pkg/client/custom_metrics"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/envtest"
	"sigs.k8s.io/yaml"

	vmopv1 "github.com/vmware-tanzu/vm-operator/api/v1alpha6"
	vmopv1common "github.com/vmware-tanzu/vm-operator/api/v1alpha6/common"
)

const (
	metricsGroup   = "custom.metrics.k8s.io"
	metricsVersion = "v1beta2"
	frontProxyName = "front-proxy-client"
	adapterNS      = "vmop-metrics-adapter"
	adapterName    = "vm-metrics-adapter"
)

// TestAdapterThroughAggregator builds the real binary and registers it with an
// envtest kube-apiserver configured as an aggregator: the shipped RBAC and
// APIServices, a CA bundle, front-proxy client certificates, and delegated
// authn/authz. Every metric request goes through the kube-apiserver, as the
// HPA's do, using the HPA controller's own custom-metrics client with real
// discovery. It needs KUBEBUILDER_ASSETS and is skipped otherwise.
func TestAdapterThroughAggregator(t *testing.T) {
	if os.Getenv("KUBEBUILDER_ASSETS") == "" {
		t.Skip("KUBEBUILDER_ASSETS not set")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 4*time.Minute)
	defer cancel()
	dir := t.TempDir()
	must := func(err error) {
		t.Helper()
		if err != nil {
			t.Fatal(err)
		}
	}

	// Front-proxy PKI: the kube-apiserver presents this client certificate to
	// the adapter, which trusts it through the requestheader CA that the
	// kube-apiserver publishes in kube-system/extension-apiserver-authentication.
	fpCA, fpCAKey := newCA(t, "front-proxy-ca")
	writePEM(t, filepath.Join(dir, "fp-ca.crt"), "CERTIFICATE", fpCA.Raw)
	proxyCert, proxyKey := newLeaf(t, fpCA, fpCAKey, frontProxyName, x509.ExtKeyUsageClientAuth)
	writePEM(t, filepath.Join(dir, "proxy.crt"), "CERTIFICATE", proxyCert)
	writeKey(t, filepath.Join(dir, "proxy.key"), proxyKey)

	// Adapter serving PKI. The APIServices carry this CA instead of
	// insecureSkipTLSVerify.
	srvCA, srvCAKey := newCA(t, "adapter-ca")
	srvCert, srvKey := newLeaf(t, srvCA, srvCAKey, "localhost", x509.ExtKeyUsageServerAuth)
	writePEM(t, filepath.Join(dir, "serving.crt"), "CERTIFICATE", srvCert)
	writeKey(t, filepath.Join(dir, "serving.key"), srvKey)
	srvCABundle := base64.StdEncoding.EncodeToString(
		pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: srvCA.Raw}))

	env := &envtest.Environment{
		CRDDirectoryPaths:     []string{filepath.Join("..", "..", "config", "crd", "bases")},
		ErrorIfCRDPathMissing: true,
	}
	args := env.ControlPlane.GetAPIServer().Configure()
	args.Set("requestheader-client-ca-file", filepath.Join(dir, "fp-ca.crt"))
	args.Set("requestheader-allowed-names", frontProxyName)
	args.Set("requestheader-username-headers", "X-Remote-User")
	args.Set("requestheader-group-headers", "X-Remote-Group")
	args.Set("requestheader-extra-headers-prefix", "X-Remote-Extra-")
	args.Set("proxy-client-cert-file", filepath.Join(dir, "proxy.crt"))
	args.Set("proxy-client-key-file", filepath.Join(dir, "proxy.key"))
	// APISERVER_LOG=<file> captures kube-apiserver output, where aggregator
	// discovery and TLS failures are reported.
	if f := os.Getenv("APISERVER_LOG"); f != "" {
		lf, err := os.Create(f)
		must(err)
		defer func() { _ = lf.Close() }()
		env.ControlPlane.GetAPIServer().Out = lf
		env.ControlPlane.GetAPIServer().Err = lf
		args.Set("v", "4")
	}
	cfg, err := env.Start()
	must(err)
	defer func() { _ = env.Stop() }()

	scheme := runtime.NewScheme()
	must(clientgoscheme.AddToScheme(scheme))
	must(vmopv1.AddToScheme(scheme))
	c, err := client.New(cfg, client.Options{Scheme: scheme})
	must(err)

	// Fixture: a ReplicaSet with three powered-on replicas at 1000 MHz each.
	must(c.Create(ctx, &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: "lab"}}))
	rs := &vmopv1.VirtualMachineReplicaSet{
		ObjectMeta: metav1.ObjectMeta{Name: "web", Namespace: "lab"},
		Spec: vmopv1.VirtualMachineReplicaSetSpec{
			Replicas: ptr.To[int32](3),
			Selector: &metav1.LabelSelector{MatchLabels: map[string]string{"app": "web"}},
			Template: vmopv1.VirtualMachineTemplateSpec{
				ObjectMeta: vmopv1common.ObjectMeta{Labels: map[string]string{"app": "web"}},
			},
		},
	}
	must(c.Create(ctx, rs))
	rs.Status.Selector = "app=web"
	must(c.Status().Update(ctx, rs))
	for i := range 3 {
		vm := &vmopv1.VirtualMachine{ObjectMeta: metav1.ObjectMeta{
			Name: fmt.Sprintf("web-%d", i), Namespace: "lab",
			Labels: map[string]string{"app": "web"},
			OwnerReferences: []metav1.OwnerReference{*metav1.NewControllerRef(
				rs, vmopv1.GroupVersion.WithKind("VirtualMachineReplicaSet"))},
		}}
		must(c.Create(ctx, vm))
		vm.Status.PowerState = vmopv1.VirtualMachinePowerStateOn
		vm.Status.Utilization = &vmopv1.VirtualMachineUtilizationStatus{
			CPUUsageMHz: 1000, GuestMemoryUsageMiB: 512, SampleTime: metav1.Now()}
		must(c.Status().Update(ctx, vm))
	}

	// Apply the shipped manifests except the workload itself. The adapter
	// then runs as their ServiceAccount with only their RBAC, so a missing
	// rule fails here.
	var apiServices []*unstructured.Unstructured
	for _, o := range loadManifests(t, filepath.Join("..", "..", "config", adapterName, "base", "adapter.yaml")) {
		switch o.GetKind() {
		case "Deployment", "Service":
		case "APIService":
			apiServices = append(apiServices, o)
		default:
			must(c.Create(ctx, o))
		}
	}
	if len(apiServices) == 0 {
		t.Fatal("adapter.yaml has no APIService")
	}
	adapterCfg := impersonatingKubeconfig(t, cfg, dir,
		"system:serviceaccount:"+adapterNS+":"+adapterName)

	port := freePort(t)
	bin := filepath.Join(dir, adapterName)
	build := exec.CommandContext(ctx, "go", "build", "-o", bin, ".")
	if out, err := build.CombinedOutput(); err != nil {
		t.Fatalf("build: %v\n%s", err, out)
	}
	var logs bytes.Buffer
	cmd := exec.CommandContext(ctx, bin,
		"--lister-kubeconfig", adapterCfg,
		"--authentication-kubeconfig", adapterCfg,
		"--authorization-kubeconfig", adapterCfg,
		"--secure-port", fmt.Sprint(port),
		"--bind-address", "127.0.0.1",
		"--tls-cert-file", filepath.Join(dir, "serving.crt"),
		"--tls-private-key-file", filepath.Join(dir, "serving.key"),
	)
	cmd.Stdout, cmd.Stderr = &logs, &logs
	must(cmd.Start())
	defer func() { _ = cmd.Process.Kill(); _ = cmd.Wait() }()
	fail := func(format string, a ...any) {
		t.Helper()
		t.Fatalf(format+"\n--- adapter log ---\n%s", append(a, logs.String())...)
	}

	// Gate registration on /readyz, as the Deployment's readiness probe gates
	// Service endpoints. The adapter serves about 100ms before it loads the
	// front-proxy CA, and requests in that window are forbidden.
	direct := &http.Client{Timeout: 5 * time.Second, Transport: &http.Transport{
		TLSClientConfig: &tls.Config{RootCAs: certPool(srvCA), ServerName: "localhost", MinVersion: tls.VersionTLS12}}}
	adapterURL := fmt.Sprintf("https://localhost:%d", port)
	waitFor(t, 60*time.Second, func() (bool, string) {
		req, _ := http.NewRequestWithContext(ctx, http.MethodGet, adapterURL+"/readyz", nil)
		resp, err := direct.Do(req)
		if err != nil {
			return false, err.Error()
		}
		_ = resp.Body.Close()
		return resp.StatusCode == http.StatusOK, fmt.Sprintf("readyz %d", resp.StatusCode)
	}, fail)

	// Register the manifest's APIServices with two test-only changes: a CA
	// bundle replaces insecureSkipTLSVerify, and an ExternalName Service on
	// the adapter's port lets the aggregator reach a loopback listener.
	must(c.Create(ctx, &corev1.Service{
		ObjectMeta: metav1.ObjectMeta{Name: adapterName, Namespace: adapterNS},
		Spec: corev1.ServiceSpec{
			Type: corev1.ServiceTypeExternalName, ExternalName: "localhost",
			Ports: []corev1.ServicePort{{Name: "https", Port: int32(port),
				TargetPort: intstr.FromInt32(int32(port))}},
		},
	}))
	for _, as := range apiServices {
		unstructured.RemoveNestedField(as.Object, "spec", "insecureSkipTLSVerify")
		must(unstructured.SetNestedField(as.Object, srvCABundle, "spec", "caBundle"))
		must(unstructured.SetNestedField(as.Object, int64(port), "spec", "service", "port"))
		must(c.Create(ctx, as))
	}

	// Available means the aggregator reached the adapter over TLS verified
	// against the CA bundle.
	for _, as := range apiServices {
		name := as.GetName()
		waitFor(t, 90*time.Second, func() (bool, string) {
			got := &unstructured.Unstructured{}
			got.SetGroupVersionKind(as.GroupVersionKind())
			if err := c.Get(ctx, client.ObjectKey{Name: name}, got); err != nil {
				return false, err.Error()
			}
			conds, _, _ := unstructured.NestedSlice(got.Object, "status", "conditions")
			for _, ci := range conds {
				cond, _ := ci.(map[string]any)
				if cond["type"] == "Available" {
					return cond["status"] == "True", fmt.Sprintf("%s %v: %v", name, cond["reason"], cond["message"])
				}
			}
			return false, name + ": no Available condition"
		}, fail)
	}

	// The HPA controller's client, with real discovery and REST mapping
	// through the kube-apiserver.
	dc, err := discovery.NewDiscoveryClientForConfig(cfg)
	must(err)
	mapper := restmapper.NewDeferredDiscoveryRESTMapper(memory.NewMemCacheClient(dc))
	apis := cmclient.NewAvailableAPIsGetter(dc)
	mc := cmclient.NewForConfig(cfg, mapper, apis)
	gk := schema.GroupKind{Group: vmopv1.GroupVersion.Group, Kind: "VirtualMachineReplicaSet"}
	getCPU := func() (int64, error) {
		mv, err := mc.NamespacedMetrics("lab").GetForObject(gk, "web", "vm_cpu_usage_mhz", labels.Everything())
		if err != nil {
			return 0, err
		}
		if mv.DescribedObject.Name != "web" || mv.DescribedObject.Kind != "VirtualMachineReplicaSet" {
			return 0, fmt.Errorf("described object = %+v", mv.DescribedObject)
		}
		return mv.Value.Value(), nil
	}
	// Aggregated discovery lags the Available condition briefly; the HPA
	// controller retries the same way.
	waitFor(t, 30*time.Second, func() (bool, string) {
		apis.Invalidate()
		gv, err := apis.PreferredVersion()
		return err == nil && gv.Version == metricsVersion, fmt.Sprintf("preferred %v, %v", gv, err)
	}, fail)

	// 1. Object metric through the aggregator: 3 x 1000 MHz.
	waitFor(t, 30*time.Second, func() (bool, string) {
		v, err := getCPU()
		if err != nil {
			return false, err.Error()
		}
		return v == 3000, fmt.Sprintf("value %d", v)
	}, fail)

	// 2. Discovery through the aggregator lists both metrics, in both
	// registered versions.
	hc, err := rest.HTTPClientFor(cfg)
	must(err)
	host := strings.TrimSuffix(cfg.Host, "/")
	for _, v := range []string{"v1beta1", "v1beta2"} {
		body := getRaw(ctx, t, hc, host+"/apis/"+metricsGroup+"/"+v, http.StatusOK, fail)
		for _, m := range []string{"vm_cpu_usage_mhz", "vm_guest_memory_usage_mib"} {
			if !bytes.Contains(body, []byte("virtualmachinereplicasets.vmoperator.vmware.com/"+m)) {
				fail("%s discovery missing %s: %s", v, m, body)
			}
		}
	}

	// 3. Load changes propagate through the adapter's informer cache.
	setCPU(ctx, t, c, 2500, time.Now())
	waitFor(t, 30*time.Second, func() (bool, string) {
		v, err := getCPU()
		if err != nil {
			return false, err.Error()
		}
		return v == 7500, fmt.Sprintf("value %d", v)
	}, fail)

	// 4. Delegated authz: a ServiceAccount without RBAC is authenticated by
	// the aggregator and then forbidden by the adapter's SubjectAccessReview.
	sa := &corev1.ServiceAccount{ObjectMeta: metav1.ObjectMeta{Name: "nobody", Namespace: "lab"}}
	must(c.Create(ctx, sa))
	tr := &authnv1.TokenRequest{Spec: authnv1.TokenRequestSpec{ExpirationSeconds: ptr.To[int64](3600)}}
	must(c.SubResource("token").Create(ctx, sa, tr))
	noRBAC := rest.AnonymousClientConfig(cfg)
	noRBAC.BearerToken = tr.Status.Token
	nc, err := rest.HTTPClientFor(noRBAC)
	must(err)
	objPath := host + "/apis/" + metricsGroup + "/" + metricsVersion +
		"/namespaces/lab/virtualmachinereplicasets.vmoperator.vmware.com/web/vm_cpu_usage_mhz"
	getRaw(ctx, t, nc, objPath, http.StatusForbidden, fail)

	// 5. Direct calls that bypass the aggregator, without a front-proxy
	// certificate, are rejected.
	code := getCode(ctx, t, direct, adapterURL+"/apis/"+metricsGroup+"/"+metricsVersion)
	if code != http.StatusUnauthorized && code != http.StatusForbidden {
		fail("anonymous direct request got %d, want 401/403", code)
	}

	// 6. Stale telemetry: the metric becomes unavailable, so the HPA holds
	// rather than computing a scale-in from zero.
	setCPU(ctx, t, c, 2500, time.Now().Add(-time.Hour))
	waitFor(t, 30*time.Second, func() (bool, string) {
		_, err := getCPU()
		return err != nil, fmt.Sprintf("err %v", err)
	}, fail)
	getRaw(ctx, t, hc, objPath, http.StatusServiceUnavailable, fail)
}

func setCPU(ctx context.Context, t *testing.T, c client.Client, mhz int64, at time.Time) {
	t.Helper()
	var vms vmopv1.VirtualMachineList
	if err := c.List(ctx, &vms, client.InNamespace("lab")); err != nil {
		t.Fatal(err)
	}
	for i := range vms.Items {
		vms.Items[i].Status.Utilization.CPUUsageMHz = mhz
		vms.Items[i].Status.Utilization.SampleTime = metav1.NewTime(at)
		if err := c.Status().Update(ctx, &vms.Items[i]); err != nil {
			t.Fatal(err)
		}
	}
}

// loadManifests decodes a multi-document YAML file.
func loadManifests(t *testing.T, path string) []*unstructured.Unstructured {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var out []*unstructured.Unstructured
	for _, doc := range strings.Split(string(data), "\n---\n") {
		o := &unstructured.Unstructured{}
		if err := yaml.Unmarshal([]byte(doc), &o.Object); err != nil {
			t.Fatalf("%s: %v", path, err)
		}
		if len(o.Object) != 0 {
			out = append(out, o)
		}
	}
	return out
}

// impersonatingKubeconfig returns a kubeconfig that acts as user.
func impersonatingKubeconfig(t *testing.T, cfg *rest.Config, dir, user string) string {
	t.Helper()
	path := filepath.Join(dir, "adapter.kubeconfig")
	if err := clientcmd.WriteToFile(clientcmdapi.Config{
		Clusters: map[string]*clientcmdapi.Cluster{"c": {
			Server: cfg.Host, CertificateAuthorityData: cfg.CAData}},
		AuthInfos: map[string]*clientcmdapi.AuthInfo{"u": {
			ClientCertificateData: cfg.CertData, ClientKeyData: cfg.KeyData,
			Impersonate: user}},
		Contexts:       map[string]*clientcmdapi.Context{"x": {Cluster: "c", AuthInfo: "u"}},
		CurrentContext: "x",
	}, path); err != nil {
		t.Fatal(err)
	}
	return path
}

func waitFor(t *testing.T, d time.Duration, f func() (bool, string), fail func(string, ...any)) {
	t.Helper()
	deadline := time.Now().Add(d)
	for {
		ok, last := f()
		if ok {
			return
		}
		if time.Now().After(deadline) {
			fail("timed out after %s: %s", d, last)
		}
		time.Sleep(500 * time.Millisecond)
	}
}

func getCode(ctx context.Context, t *testing.T, hc *http.Client, url string) int {
	t.Helper()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		t.Fatal(err)
	}
	resp, err := hc.Do(req)
	if err != nil {
		t.Fatalf("GET %s: %v", url, err)
	}
	_ = resp.Body.Close()
	return resp.StatusCode
}

func getRaw(ctx context.Context, t *testing.T, hc *http.Client, url string, want int, fail func(string, ...any)) []byte {
	t.Helper()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		t.Fatal(err)
	}
	resp, err := hc.Do(req)
	if err != nil {
		fail("GET %s: %v", url, err)
	}
	defer func() { _ = resp.Body.Close() }()
	b, _ := io.ReadAll(resp.Body)
	if resp.StatusCode != want {
		fail("GET %s = %d, want %d: %s", url, resp.StatusCode, want, b)
	}
	if want == http.StatusOK && !json.Valid(b) {
		fail("GET %s returned non-JSON: %s", url, b)
	}
	return b
}

func freePort(t *testing.T) int {
	t.Helper()
	l, err := (&net.ListenConfig{}).Listen(context.Background(), "tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = l.Close() }()
	return l.Addr().(*net.TCPAddr).Port
}

func newCA(t *testing.T, cn string) (*x509.Certificate, *ecdsa.PrivateKey) {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	tmpl := &x509.Certificate{
		SerialNumber: big.NewInt(1), Subject: pkix.Name{CommonName: cn},
		NotBefore: time.Now().Add(-time.Hour), NotAfter: time.Now().Add(time.Hour),
		IsCA: true, BasicConstraintsValid: true,
		KeyUsage: x509.KeyUsageCertSign | x509.KeyUsageDigitalSignature,
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, tmpl, &key.PublicKey, key)
	if err != nil {
		t.Fatal(err)
	}
	ca, err := x509.ParseCertificate(der)
	if err != nil {
		t.Fatal(err)
	}
	return ca, key
}

func newLeaf(t *testing.T, ca *x509.Certificate, caKey *ecdsa.PrivateKey, cn string, usage x509.ExtKeyUsage) ([]byte, *ecdsa.PrivateKey) {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	tmpl := &x509.Certificate{
		SerialNumber: big.NewInt(2), Subject: pkix.Name{CommonName: cn},
		NotBefore: time.Now().Add(-time.Hour), NotAfter: time.Now().Add(time.Hour),
		KeyUsage:    x509.KeyUsageDigitalSignature,
		ExtKeyUsage: []x509.ExtKeyUsage{usage},
	}
	if usage == x509.ExtKeyUsageServerAuth {
		// The aggregator verifies the adapter against its Service DNS name,
		// whatever the Service resolves to; without this SAN discovery and
		// OpenAPI fetches fail TLS and the group stays Stale.
		tmpl.DNSNames = []string{"localhost", adapterName + "." + adapterNS + ".svc"}
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, ca, &key.PublicKey, caKey)
	if err != nil {
		t.Fatal(err)
	}
	return der, key
}

func writePEM(t *testing.T, path, typ string, der []byte) {
	t.Helper()
	if err := os.WriteFile(path, pem.EncodeToMemory(&pem.Block{Type: typ, Bytes: der}), 0o600); err != nil {
		t.Fatal(err)
	}
}

func writeKey(t *testing.T, path string, key *ecdsa.PrivateKey) {
	t.Helper()
	der, err := x509.MarshalECPrivateKey(key)
	if err != nil {
		t.Fatal(err)
	}
	writePEM(t, path, "EC PRIVATE KEY", der)
}

func certPool(ca *x509.Certificate) *x509.CertPool {
	p := x509.NewCertPool()
	p.AddCert(ca)
	return p
}

// TestSecureOverlayMatchesAggregatorExpectations guards the TLS settings the
// aggregator test found to be load-bearing.
func TestSecureOverlayMatchesAggregatorExpectations(t *testing.T) {
	secure := filepath.Join("..", "..", "config", adapterName, "secure")
	want := adapterName + "." + adapterNS + ".svc"
	found := false
	for _, o := range loadManifests(t, filepath.Join(secure, "certificate.yaml")) {
		if o.GetKind() != "Certificate" {
			continue
		}
		names, _, _ := unstructured.NestedStringSlice(o.Object, "spec", "dnsNames")
		for _, n := range names {
			found = found || n == want
		}
	}
	if !found {
		t.Fatalf("secure/certificate.yaml does not cover %s", want)
	}
	k, err := os.ReadFile(filepath.Join(secure, "kustomization.yaml"))
	if err != nil {
		t.Fatal(err)
	}
	for _, s := range []string{"/spec/insecureSkipTLSVerify", "cert-manager.io/inject-ca-from"} {
		if !bytes.Contains(k, []byte(s)) {
			t.Fatalf("secure/kustomization.yaml no longer patches %s", s)
		}
	}
}
