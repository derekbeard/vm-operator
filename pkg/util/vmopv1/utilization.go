// © Broadcom. All Rights Reserved.
// The term "Broadcom" refers to Broadcom Inc. and/or its subsidiaries.
// SPDX-License-Identifier: Apache-2.0

package vmopv1

import (
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	vmopv1 "github.com/vmware-tanzu/vm-operator/api/v1alpha6"
)

const (
	// UtilizationRefreshInterval is how long a recorded utilization sample is
	// kept unchanged. Each status write wakes watchers, so a sample is only
	// replaced when it is older than this or has moved meaningfully.
	UtilizationRefreshInterval = 60 * time.Second

	// UtilizationMaxAge is how old a sample may be before consumers must
	// ignore it. It is a few refresh intervals so one missed reconcile does
	// not drop a replica from an aggregate.
	UtilizationMaxAge = 3 * UtilizationRefreshInterval

	// A move is meaningful when it exceeds both 10% of the previous value and
	// an absolute floor, so idle VMs do not churn status on noise.
	utilizationChangeFraction = 0.10
	utilizationCPUFloorMHz    = 50
	utilizationMemFloorMiB    = 64
)

// NextUtilization returns the sample to record for a VM: the previous sample
// when it is fresh and the new values are within noise, otherwise a new one.
func NextUtilization(
	prev *vmopv1.VirtualMachineUtilizationStatus,
	cpuMHz, memMiB int64,
	now metav1.Time) *vmopv1.VirtualMachineUtilizationStatus {

	if prev != nil &&
		now.Sub(prev.SampleTime.Time) < UtilizationRefreshInterval &&
		!movedMeaningfully(prev.CPUUsageMHz, cpuMHz, utilizationCPUFloorMHz) &&
		!movedMeaningfully(prev.GuestMemoryUsageMiB, memMiB, utilizationMemFloorMiB) {

		return prev
	}

	return &vmopv1.VirtualMachineUtilizationStatus{
		CPUUsageMHz:         cpuMHz,
		GuestMemoryUsageMiB: memMiB,
		SampleTime:          now,
	}
}

func movedMeaningfully(prev, cur, floor int64) bool {
	delta := cur - prev
	if delta < 0 {
		delta = -delta
	}
	return delta > floor && float64(delta) > utilizationChangeFraction*float64(prev)
}

// UtilizationSummary aggregates the utilization of a set of replica VMs.
type UtilizationSummary struct {
	// Replicas is the number of non-deleting VMs considered.
	Replicas int

	// Sampled is how many of them are powered on with a fresh sample.
	Sampled int

	// TotalCPUUsageMHz and TotalGuestMemoryUsageMiB sum the fresh samples.
	TotalCPUUsageMHz         int64
	TotalGuestMemoryUsageMiB int64
}

// SummarizeUtilization sums the fresh samples of the given VMs. VMs being
// deleted are ignored. A powered-off VM, or one without a sample newer than
// maxAge, counts as a replica but not as sampled, so that missing data cannot
// be mistaken for an idle VM.
func SummarizeUtilization(
	vms []vmopv1.VirtualMachine,
	now time.Time,
	maxAge time.Duration) UtilizationSummary {

	var s UtilizationSummary
	for i := range vms {
		vm := &vms[i]
		if !vm.DeletionTimestamp.IsZero() {
			continue
		}
		s.Replicas++
		u := vm.Status.Utilization
		if vm.Status.PowerState != vmopv1.VirtualMachinePowerStateOn ||
			u == nil || now.Sub(u.SampleTime.Time) > maxAge {

			continue
		}
		s.Sampled++
		s.TotalCPUUsageMHz += u.CPUUsageMHz
		s.TotalGuestMemoryUsageMiB += u.GuestMemoryUsageMiB
	}
	return s
}

// ReplicaTotals estimates fleet-wide usage for an autoscaler that divides a
// total by the replica count. Replicas without a fresh sample are assumed to
// use the average of those that have one. It returns false when fewer than
// minCoverage (0..1) of the replicas are sampled, because a decision made on
// sparse data would bias the autoscaler toward scaling in; the caller should
// report an error so the autoscaler holds its current replica count.
func (s UtilizationSummary) ReplicaTotals(
	minCoverage float64) (cpuMHz, memMiB int64, ok bool) {

	if s.Replicas == 0 || s.Sampled == 0 {
		return 0, 0, false
	}
	if float64(s.Sampled) < minCoverage*float64(s.Replicas) {
		return 0, 0, false
	}
	scale := float64(s.Replicas) / float64(s.Sampled)
	return int64(float64(s.TotalCPUUsageMHz) * scale),
		int64(float64(s.TotalGuestMemoryUsageMiB) * scale),
		true
}

// UtilizationRequeueDelay returns how long until a VM should reconcile again to
// refresh its utilization sample, or zero when no refresh is wanted.
//
// The VM watcher does not observe quick stats, and a steady powered-on VM
// otherwise reconciles only on the manager's resync period (30 minutes by
// default), far longer than UtilizationMaxAge. Without a requeue every sample
// would go stale and an autoscaler would correctly hold forever. Only powered-on
// VMs owned by a VirtualMachineReplicaSet are requeued, so the extra reconcile
// load scales with the number of autoscaled VMs, not with the whole inventory.
func UtilizationRequeueDelay(vm *vmopv1.VirtualMachine, enabled bool) time.Duration {
	if !enabled ||
		!vm.DeletionTimestamp.IsZero() ||
		vm.Status.PowerState != vmopv1.VirtualMachinePowerStateOn {

		return 0
	}
	if ref := metav1.GetControllerOfNoCopy(vm); ref == nil ||
		ref.Kind != "VirtualMachineReplicaSet" {

		return 0
	}
	return UtilizationRefreshInterval
}
