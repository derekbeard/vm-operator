// © Broadcom. All Rights Reserved.
// The term "Broadcom" refers to Broadcom Inc. and/or its subsidiaries.
// SPDX-License-Identifier: Apache-2.0

package vmopv1_test

import (
	"time"

	. "github.com/onsi/ginkgo/v2"
	. "github.com/onsi/gomega"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	vmopv1 "github.com/vmware-tanzu/vm-operator/api/v1alpha6"
	"github.com/vmware-tanzu/vm-operator/pkg/constants/testlabels"
	vmopv1util "github.com/vmware-tanzu/vm-operator/pkg/util/vmopv1"
)

var _ = Describe("Utilization", Label(testlabels.API), func() {
	now := metav1.NewTime(time.Date(2026, 10, 28, 12, 0, 0, 0, time.UTC))

	sample := func(cpu, mem int64, age time.Duration) *vmopv1.VirtualMachineUtilizationStatus {
		return &vmopv1.VirtualMachineUtilizationStatus{
			CPUUsageMHz:         cpu,
			GuestMemoryUsageMiB: mem,
			SampleTime:          metav1.NewTime(now.Add(-age)),
		}
	}

	Describe("NextUtilization", func() {
		It("records the first sample", func() {
			got := vmopv1util.NextUtilization(nil, 500, 1024, now)
			Expect(got).To(Equal(sample(500, 1024, 0)))
		})

		It("keeps a fresh sample when values are within noise", func() {
			prev := sample(1000, 2048, 10*time.Second)
			Expect(vmopv1util.NextUtilization(prev, 1060, 2060, now)).To(BeIdenticalTo(prev))
		})

		It("replaces a fresh sample when CPU moves meaningfully", func() {
			prev := sample(1000, 2048, 10*time.Second)
			Expect(vmopv1util.NextUtilization(prev, 1500, 2048, now).CPUUsageMHz).To(Equal(int64(1500)))
		})

		It("replaces a fresh sample when memory moves meaningfully", func() {
			prev := sample(1000, 2048, 10*time.Second)
			Expect(vmopv1util.NextUtilization(prev, 1000, 4096, now).GuestMemoryUsageMiB).To(Equal(int64(4096)))
		})

		It("does not churn an idle VM on small absolute changes", func() {
			prev := sample(10, 100, 10*time.Second)
			Expect(vmopv1util.NextUtilization(prev, 40, 150, now)).To(BeIdenticalTo(prev))
		})

		It("refreshes an old sample even when values are unchanged", func() {
			prev := sample(1000, 2048, vmopv1util.UtilizationRefreshInterval+time.Second)
			got := vmopv1util.NextUtilization(prev, 1000, 2048, now)
			Expect(got).ToNot(BeIdenticalTo(prev))
			Expect(got.SampleTime).To(Equal(now))
		})
	})

	Describe("SummarizeUtilization", func() {
		vm := func(on bool, u *vmopv1.VirtualMachineUtilizationStatus) vmopv1.VirtualMachine {
			v := vmopv1.VirtualMachine{}
			v.Status.PowerState = vmopv1.VirtualMachinePowerStateOff
			if on {
				v.Status.PowerState = vmopv1.VirtualMachinePowerStateOn
			}
			v.Status.Utilization = u
			return v
		}

		It("sums fresh samples of powered-on VMs", func() {
			s := vmopv1util.SummarizeUtilization([]vmopv1.VirtualMachine{
				vm(true, sample(100, 10, time.Second)),
				vm(true, sample(300, 30, time.Second)),
			}, now.Time, vmopv1util.UtilizationMaxAge)
			Expect(s).To(Equal(vmopv1util.UtilizationSummary{
				Replicas: 2, Sampled: 2, TotalCPUUsageMHz: 400, TotalGuestMemoryUsageMiB: 40}))
		})

		It("counts missing, stale and powered-off VMs as replicas but not as sampled", func() {
			s := vmopv1util.SummarizeUtilization([]vmopv1.VirtualMachine{
				vm(true, sample(100, 10, time.Second)),
				vm(true, nil),
				vm(true, sample(999, 99, vmopv1util.UtilizationMaxAge+time.Second)),
				vm(false, sample(999, 99, time.Second)),
			}, now.Time, vmopv1util.UtilizationMaxAge)
			Expect(s.Replicas).To(Equal(4))
			Expect(s.Sampled).To(Equal(1))
			Expect(s.TotalCPUUsageMHz).To(Equal(int64(100)))
		})

		It("ignores VMs that are being deleted", func() {
			deleting := vm(true, sample(500, 50, time.Second))
			deleting.DeletionTimestamp = &now
			s := vmopv1util.SummarizeUtilization([]vmopv1.VirtualMachine{
				vm(true, sample(100, 10, time.Second)), deleting,
			}, now.Time, vmopv1util.UtilizationMaxAge)
			Expect(s.Replicas).To(Equal(1))
		})
	})

	Describe("ReplicaTotals", func() {
		It("extrapolates unsampled replicas from the sampled average", func() {
			s := vmopv1util.UtilizationSummary{
				Replicas: 4, Sampled: 2, TotalCPUUsageMHz: 600, TotalGuestMemoryUsageMiB: 200}
			cpu, mem, ok := s.ReplicaTotals(0.5)
			Expect(ok).To(BeTrue())
			Expect(cpu).To(Equal(int64(1200)))
			Expect(mem).To(Equal(int64(400)))
		})

		It("refuses sparse data so the autoscaler holds", func() {
			s := vmopv1util.UtilizationSummary{Replicas: 4, Sampled: 1, TotalCPUUsageMHz: 100}
			_, _, ok := s.ReplicaTotals(0.5)
			Expect(ok).To(BeFalse())
		})

		It("refuses an empty or fully unsampled set", func() {
			_, _, ok := vmopv1util.UtilizationSummary{}.ReplicaTotals(0)
			Expect(ok).To(BeFalse())
			_, _, ok = vmopv1util.UtilizationSummary{Replicas: 3}.ReplicaTotals(0)
			Expect(ok).To(BeFalse())
		})
	})
	Describe("UtilizationRequeueDelay", func() {
		owned := func(kind string, on bool) *vmopv1.VirtualMachine {
			vm := &vmopv1.VirtualMachine{}
			vm.Status.PowerState = vmopv1.VirtualMachinePowerStateOff
			if on {
				vm.Status.PowerState = vmopv1.VirtualMachinePowerStateOn
			}
			vm.OwnerReferences = []metav1.OwnerReference{{
				Kind: kind, Name: "owner", UID: "uid", Controller: func() *bool { b := true; return &b }()}}
			return vm
		}

		It("requeues powered-on VMs owned by a ReplicaSet", func() {
			Expect(vmopv1util.UtilizationRequeueDelay(
				owned("VirtualMachineReplicaSet", true), true)).To(Equal(vmopv1util.UtilizationRefreshInterval))
		})

		It("does not requeue when the capability is disabled", func() {
			Expect(vmopv1util.UtilizationRequeueDelay(owned("VirtualMachineReplicaSet", true), false)).To(BeZero())
		})

		It("does not requeue powered-off, foreign-owned, unowned or deleting VMs", func() {
			Expect(vmopv1util.UtilizationRequeueDelay(owned("VirtualMachineReplicaSet", false), true)).To(BeZero())
			Expect(vmopv1util.UtilizationRequeueDelay(owned("Deployment", true), true)).To(BeZero())
			unowned := owned("VirtualMachineReplicaSet", true)
			unowned.OwnerReferences = nil
			Expect(vmopv1util.UtilizationRequeueDelay(unowned, true)).To(BeZero())
			deleting := owned("VirtualMachineReplicaSet", true)
			now := metav1.Now()
			deleting.DeletionTimestamp = &now
			Expect(vmopv1util.UtilizationRequeueDelay(deleting, true)).To(BeZero())
		})
	})
})
