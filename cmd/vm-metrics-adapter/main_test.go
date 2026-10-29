// © Broadcom. All Rights Reserved.
// The term "Broadcom" refers to Broadcom Inc. and/or its subsidiaries.
// SPDX-License-Identifier: Apache-2.0

package main_test

import (
	"bytes"
	"context"
	"crypto/tls"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"testing"
	"time"

	authnv1 "k8s.io/api/authentication/v1"
	corev1 "k8s.io/api/core/v1"
	rbacv1 "k8s.io/api/rbac/v1"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/labels"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"k8s.io/client-go/rest"
	"k8s.io/client-go/tools/clientcmd"
	clientcmdapi "k8s.io/client-go/tools/clientcmd/api"
	cmclient "k8s.io/metrics/pkg/client/custom_metrics"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/envtest"

	vmopv1 "github.com/vmware-tanzu/vm-operator/api/v1alpha6"
	vmopv1common "github.com/vmware-tanzu/vm-operator/api/v1alpha6/common"
)

// TestAdapterServesHPAShapedRequests builds the real binary, points it at an
// envtest API server and issues the same requests the HPA controller sends. It
// needs KUBEBUILDER_ASSETS and is skipped otherwise.
func TestAdapterServesHPAShapedRequests(t *testing.T) {
	if os.Getenv("KUBEBUILDER_ASSETS") == "" {
		t.Skip("KUBEBUILDER_ASSETS not set")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 4*time.Minute)
	defer cancel()
	dir := t.TempDir()

	env := &envtest.Environment{
		CRDDirectoryPaths:     []string{filepath.Join("..", "..", "config", "crd", "bases")},
		ErrorIfCRDPathMissing: true,
	}
	cfg, err := env.Start()
	if err != nil {
		t.Fatalf("envtest: %v", err)
	}
	defer func() { _ = env.Stop() }()

	scheme := runtime.NewScheme()
	_ = clientgoscheme.AddToScheme(scheme)
	_ = vmopv1.AddToScheme(scheme)
	c, err := client.New(cfg, client.Options{Scheme: scheme})
	if err != nil {
		t.Fatal(err)
	}

	// Fixture: a ReplicaSet with three powered-on replicas.
	must := func(err error) {
		t.Helper()
		if err != nil {
			t.Fatal(err)
		}
	}
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
	for i := 0; i < 3; i++ {
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

	// Caller identity: a ServiceAccount token the adapter's delegated
	// authn/authz can verify against the API server.
	sa := &corev1.ServiceAccount{ObjectMeta: metav1.ObjectMeta{Name: "hpa", Namespace: "lab"}}
	must(c.Create(ctx, sa))
	must(c.Create(ctx, &rbacv1.ClusterRoleBinding{
		ObjectMeta: metav1.ObjectMeta{Name: "hpa-admin"},
		RoleRef:    rbacv1.RoleRef{APIGroup: rbacv1.GroupName, Kind: "ClusterRole", Name: "cluster-admin"},
		Subjects:   []rbacv1.Subject{{Kind: "ServiceAccount", Name: "hpa", Namespace: "lab"}},
	}))
	tr := &authnv1.TokenRequest{Spec: authnv1.TokenRequestSpec{ExpirationSeconds: ptr.To[int64](3600)}}
	must(c.SubResource("token").Create(ctx, sa, tr))

	kubeconfig := filepath.Join(dir, "kubeconfig")
	must(clientcmd.WriteToFile(clientcmdapi.Config{
		Clusters: map[string]*clientcmdapi.Cluster{"c": {
			Server: cfg.Host, CertificateAuthorityData: cfg.CAData}},
		AuthInfos: map[string]*clientcmdapi.AuthInfo{"u": {
			ClientCertificateData: cfg.CertData, ClientKeyData: cfg.KeyData}},
		Contexts:       map[string]*clientcmdapi.Context{"x": {Cluster: "c", AuthInfo: "u"}},
		CurrentContext: "x",
	}, kubeconfig))

	bin := filepath.Join(dir, "vm-metrics-adapter")
	build := exec.CommandContext(ctx, "go", "build", "-o", bin, ".")
	if out, err := build.CombinedOutput(); err != nil {
		t.Fatalf("build: %v\n%s", err, out)
	}

	l, err := net.Listen("tcp", "127.0.0.1:0")
	must(err)
	port := l.Addr().(*net.TCPAddr).Port
	_ = l.Close()

	var logs bytes.Buffer
	cmd := exec.CommandContext(ctx, bin,
		"--lister-kubeconfig", kubeconfig,
		"--authentication-kubeconfig", kubeconfig,
		"--authorization-kubeconfig", kubeconfig,
		"--secure-port", fmt.Sprint(port),
		"--bind-address", "127.0.0.1",
		"--cert-dir", dir,
		"--authentication-skip-lookup",
	)
	cmd.Stdout, cmd.Stderr = &logs, &logs
	must(cmd.Start())
	defer func() { _ = cmd.Process.Kill(); _ = cmd.Wait() }()

	hc := &http.Client{Timeout: 5 * time.Second, Transport: &http.Transport{
		TLSClientConfig: &tls.Config{InsecureSkipVerify: true}, //nolint:gosec // test-only self-signed cert
	}}
	get := func(path, token string) (int, []byte) {
		req, _ := http.NewRequestWithContext(ctx, http.MethodGet,
			fmt.Sprintf("https://127.0.0.1:%d%s", port, path), nil)
		if token != "" {
			req.Header.Set("Authorization", "Bearer "+token)
		}
		resp, err := hc.Do(req)
		if err != nil {
			return 0, []byte(err.Error())
		}
		defer resp.Body.Close()
		b, _ := io.ReadAll(resp.Body)
		return resp.StatusCode, b
	}

	const base = "/apis/custom.metrics.k8s.io/v1beta2"
	deadline := time.Now().Add(90 * time.Second)
	for {
		if code, _ := get(base, tr.Status.Token); code == http.StatusOK {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("adapter never became ready\n%s", logs.String())
		}
		time.Sleep(time.Second)
	}

	// 1. HPA Object-metric shape.
	path := base + "/namespaces/lab/virtualmachinereplicasets.vmoperator.vmware.com/web/vm_cpu_usage_mhz"
	code, body := get(path, tr.Status.Token)
	if code != http.StatusOK {
		t.Fatalf("GET %s = %d: %s\n%s", path, code, body, logs.String())
	}
	var list struct {
		Items []struct {
			Value  string `json:"value"`
			Metric struct{ Name string }
		}
	}
	must(json.Unmarshal(body, &list))
	if len(list.Items) != 1 || list.Items[0].Value != "3k" {
		t.Fatalf("want one item with value 3k (3 x 1000 MHz), got %s", body)
	}

	// 1b. The client library the HPA controller itself uses.
	rc := &rest.Config{
		Host:            fmt.Sprintf("https://127.0.0.1:%d", port),
		BearerToken:     tr.Status.Token,
		TLSClientConfig: rest.TLSClientConfig{Insecure: true},
	}
	mapper := meta.NewDefaultRESTMapper([]schema.GroupVersion{vmopv1.GroupVersion})
	mapper.Add(vmopv1.GroupVersion.WithKind("VirtualMachineReplicaSet"), meta.RESTScopeNamespace)
	mc := cmclient.NewForConfig(rc, mapper, fixedVersion{})
	gk := schema.GroupKind{Group: vmopv1.GroupVersion.Group, Kind: "VirtualMachineReplicaSet"}
	mv, err := mc.NamespacedMetrics("lab").GetForObject(gk, "web", "vm_cpu_usage_mhz", labels.Everything())
	if err != nil {
		t.Fatalf("HPA client GetForObject: %v\n%s", err, logs.String())
	}
	if got := mv.Value.MilliValue(); got != 3000*1000 {
		t.Fatalf("HPA client value = %d milli, want 3000000", got)
	}
	if mv.DescribedObject.Name != "web" || mv.DescribedObject.Kind != "VirtualMachineReplicaSet" {
		t.Fatalf("described object = %+v", mv.DescribedObject)
	}

	// 2. Unauthenticated callers are rejected.
	if code, _ := get(path, ""); code != http.StatusUnauthorized && code != http.StatusForbidden {
		t.Fatalf("anonymous request got %d, want 401/403", code)
	}

	// 3. Discovery lists both metrics.
	code, body = get(base, tr.Status.Token)
	if code != http.StatusOK ||
		!bytes.Contains(body, []byte("virtualmachinereplicasets.vmoperator.vmware.com/vm_cpu_usage_mhz")) ||
		!bytes.Contains(body, []byte("virtualmachinereplicasets.vmoperator.vmware.com/vm_guest_memory_usage_mib")) {
		t.Fatalf("discovery = %d: %s", code, body)
	}

	// 4. Stale telemetry holds the HPA (503) instead of reporting zero.
	var vms vmopv1.VirtualMachineList
	must(c.List(ctx, &vms, client.InNamespace("lab")))
	for i := range vms.Items {
		vms.Items[i].Status.Utilization.SampleTime = metav1.NewTime(time.Now().Add(-time.Hour))
		must(c.Status().Update(ctx, &vms.Items[i]))
	}
	stale := time.Now().Add(20 * time.Second)
	for {
		code, body = get(path, tr.Status.Token)
		if code == http.StatusServiceUnavailable {
			break
		}
		if time.Now().After(stale) {
			t.Fatalf("stale samples: got %d %s, want 503", code, body)
		}
		time.Sleep(time.Second)
	}
}

// fixedVersion stands in for discovery: in a cluster the kube-apiserver
// aggregator publishes the group, which the adapter alone does not.
type fixedVersion struct{}

func (fixedVersion) PreferredVersion() (schema.GroupVersion, error) {
	return schema.GroupVersion{Group: "custom.metrics.k8s.io", Version: "v1beta2"}, nil
}
func (fixedVersion) Invalidate() {}
