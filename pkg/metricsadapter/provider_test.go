// © Broadcom. All Rights Reserved.
// The term "Broadcom" refers to Broadcom Inc. and/or its subsidiaries.
// SPDX-License-Identifier: Apache-2.0

package metricsadapter_test

import (
	"context"
	"time"

	. "github.com/onsi/ginkgo/v2"
	. "github.com/onsi/gomega"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/labels"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/custom-metrics-apiserver/pkg/provider"

	vmopv1 "github.com/vmware-tanzu/vm-operator/api/v1alpha6"
	"github.com/vmware-tanzu/vm-operator/pkg/constants/testlabels"
	"github.com/vmware-tanzu/vm-operator/pkg/metricsadapter"
)

var _ = Describe("Provider", Label(testlabels.API), func() {
	const ns = "ns"

	var (
		ctx  = context.Background()
		now  = time.Date(2026, 10, 28, 12, 0, 0, 0, time.UTC)
		rs   *vmopv1.VirtualMachineReplicaSet
		vms  []client.Object
		prov *metricsadapter.Provider

		cpuInfo = provider.CustomMetricInfo{
			GroupResource: schema.GroupResource{
				Group: "vmoperator.vmware.com", Resource: "virtualmachinereplicasets"},
			Namespaced: true,
			Metric:     metricsadapter.MetricVMCPUUsageMHz,
		}
	)

	vm := func(name string, owned, on bool, cpu, mem int64, age time.Duration) *vmopv1.VirtualMachine {
		v := &vmopv1.VirtualMachine{ObjectMeta: metav1.ObjectMeta{
			Name: name, Namespace: ns, Labels: map[string]string{"app": "web"}}}
		if owned {
			v.OwnerReferences = []metav1.OwnerReference{*metav1.NewControllerRef(
				rs, vmopv1.GroupVersion.WithKind("VirtualMachineReplicaSet"))}
		}
		v.Status.PowerState = vmopv1.VirtualMachinePowerStateOff
		if on {
			v.Status.PowerState = vmopv1.VirtualMachinePowerStateOn
		}
		v.Status.Utilization = &vmopv1.VirtualMachineUtilizationStatus{
			CPUUsageMHz: cpu, GuestMemoryUsageMiB: mem,
			SampleTime: metav1.NewTime(now.Add(-age)),
		}
		return v
	}

	build := func() {
		scheme := runtime.NewScheme()
		Expect(vmopv1.AddToScheme(scheme)).To(Succeed())
		objs := append([]client.Object{rs}, vms...)
		prov = &metricsadapter.Provider{
			Reader: fake.NewClientBuilder().WithScheme(scheme).WithObjects(objs...).Build(),
			Now:    func() time.Time { return now },
		}
	}

	BeforeEach(func() {
		rs = &vmopv1.VirtualMachineReplicaSet{ObjectMeta: metav1.ObjectMeta{
			Name: "rs", Namespace: ns, UID: "rs-uid"}}
		rs.Spec.Selector = &metav1.LabelSelector{MatchLabels: map[string]string{"app": "web"}}
		vms = nil
	})

	It("lists both metrics for ReplicaSets", func() {
		build()
		infos := prov.ListAllMetrics()
		Expect(infos).To(HaveLen(2))
		for _, i := range infos {
			Expect(i.Namespaced).To(BeTrue())
			Expect(i.GroupResource.Resource).To(Equal("virtualmachinereplicasets"))
		}
	})

	It("totals CPU across the ReplicaSet's fresh powered-on VMs", func() {
		vms = []client.Object{
			vm("a", true, true, 600, 100, time.Second),
			vm("b", true, true, 400, 300, time.Second),
		}
		build()
		got, err := prov.GetMetricByName(ctx, types.NamespacedName{Namespace: ns, Name: "rs"}, cpuInfo, nil)
		Expect(err).ToNot(HaveOccurred())
		Expect(got.Value.Value()).To(Equal(int64(1000)))
		Expect(got.DescribedObject.Name).To(Equal("rs"))
		Expect(got.Metric.Name).To(Equal(metricsadapter.MetricVMCPUUsageMHz))
	})

	It("totals memory when asked for the memory metric", func() {
		vms = []client.Object{
			vm("a", true, true, 600, 100, time.Second),
			vm("b", true, true, 400, 300, time.Second),
		}
		build()
		info := cpuInfo
		info.Metric = metricsadapter.MetricVMGuestMemoryUsageMiB
		got, err := prov.GetMetricByName(ctx, types.NamespacedName{Namespace: ns, Name: "rs"}, info, nil)
		Expect(err).ToNot(HaveOccurred())
		Expect(got.Value.Value()).To(Equal(int64(400)))
	})

	It("ignores a standalone VM that merely matches the selector", func() {
		vms = []client.Object{
			vm("a", true, true, 500, 0, time.Second),
			vm("standalone", false, true, 9000, 0, time.Second),
		}
		build()
		got, err := prov.GetMetricByName(ctx, types.NamespacedName{Namespace: ns, Name: "rs"}, cpuInfo, nil)
		Expect(err).ToNot(HaveOccurred())
		Expect(got.Value.Value()).To(Equal(int64(500)))
	})

	It("extrapolates a replica without a fresh sample from the others", func() {
		vms = []client.Object{
			vm("a", true, true, 500, 0, time.Second),
			vm("b", true, true, 500, 0, time.Second),
			vm("stale", true, true, 9999, 0, 10*time.Minute),
		}
		build()
		got, err := prov.GetMetricByName(ctx, types.NamespacedName{Namespace: ns, Name: "rs"}, cpuInfo, nil)
		Expect(err).ToNot(HaveOccurred())
		Expect(got.Value.Value()).To(Equal(int64(1500)))
	})

	It("is unavailable, so the HPA holds, when too few replicas have samples", func() {
		vms = []client.Object{
			vm("a", true, true, 500, 0, time.Second),
			vm("stale1", true, true, 9, 0, 10*time.Minute),
			vm("stale2", true, true, 9, 0, 10*time.Minute),
			vm("off", true, false, 9, 0, time.Second),
		}
		build()
		_, err := prov.GetMetricByName(ctx, types.NamespacedName{Namespace: ns, Name: "rs"}, cpuInfo, nil)
		Expect(apierrors.IsServiceUnavailable(err)).To(BeTrue())
	})

	It("is unavailable when no replica has a sample, rather than reporting zero", func() {
		build()
		_, err := prov.GetMetricByName(ctx, types.NamespacedName{Namespace: ns, Name: "rs"}, cpuInfo, nil)
		Expect(apierrors.IsServiceUnavailable(err)).To(BeTrue())
	})

	It("refuses an empty selector instead of totalling every VM in the namespace", func() {
		rs.Spec.Selector = &metav1.LabelSelector{}
		vms = []client.Object{vm("a", true, true, 500, 0, time.Second)}
		build()
		_, err := prov.GetMetricByName(ctx, types.NamespacedName{Namespace: ns, Name: "rs"}, cpuInfo, nil)
		Expect(apierrors.IsServiceUnavailable(err)).To(BeTrue())
	})

	It("reports not found for an unknown ReplicaSet, metric, or resource", func() {
		build()
		_, err := prov.GetMetricByName(ctx, types.NamespacedName{Namespace: ns, Name: "missing"}, cpuInfo, nil)
		Expect(apierrors.IsNotFound(err)).To(BeTrue())

		info := cpuInfo
		info.Metric = "nope"
		_, err = prov.GetMetricByName(ctx, types.NamespacedName{Namespace: ns, Name: "rs"}, info, nil)
		Expect(apierrors.IsNotFound(err)).To(BeTrue())

		info = cpuInfo
		info.GroupResource.Resource = "pods"
		info.GroupResource.Group = ""
		_, err = prov.GetMetricByName(ctx, types.NamespacedName{Namespace: ns, Name: "rs"}, info, nil)
		Expect(apierrors.IsNotFound(err)).To(BeTrue())
	})

	It("answers selector queries over ReplicaSets and rejects root scope", func() {
		vms = []client.Object{vm("a", true, true, 700, 0, time.Second)}
		build()
		got, err := prov.GetMetricBySelector(ctx, ns, labels.Everything(), cpuInfo, nil)
		Expect(err).ToNot(HaveOccurred())
		Expect(got.Items).To(HaveLen(1))
		Expect(got.Items[0].Value.Value()).To(Equal(int64(700)))

		_, err = prov.GetMetricBySelector(ctx, "", labels.Everything(), cpuInfo, nil)
		Expect(apierrors.IsNotFound(err)).To(BeTrue())
	})
})
