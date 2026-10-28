// © Broadcom. All Rights Reserved.
// The term "Broadcom" refers to Broadcom Inc. and/or its subsidiaries.
// SPDX-License-Identifier: Apache-2.0

package vmlifecycle

import (
	"github.com/vmware/govmomi/object"
	vimtypes "github.com/vmware/govmomi/vim25/types"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	ctrlclient "sigs.k8s.io/controller-runtime/pkg/client"

	pkgcfg "github.com/vmware-tanzu/vm-operator/pkg/config"
	pkgctx "github.com/vmware-tanzu/vm-operator/pkg/context"
	vmopv1util "github.com/vmware-tanzu/vm-operator/pkg/util/vmopv1"
)

// reconcileStatusUtilization records the hypervisor's quick stats for a
// powered-on VM. The stats are already part of the VM summary fetched for
// every reconcile, so this adds no vSphere calls.
func reconcileStatusUtilization(
	vmCtx pkgctx.VirtualMachineContext,
	_ ctrlclient.Client,
	_ *object.VirtualMachine,
	_ ReconcileStatusData) []error { //nolint:unparam

	if !pkgcfg.FromContext(vmCtx).Features.K8sWorkloadMgmtAPI {
		return nil
	}

	if vmCtx.MoVM.Runtime.PowerState != vimtypes.VirtualMachinePowerStatePoweredOn {
		// A powered-off VM has no usage; stale numbers would mislead an
		// autoscaler that sums them.
		vmCtx.VM.Status.Utilization = nil
		return nil
	}

	qs := vmCtx.MoVM.Summary.QuickStats
	vmCtx.VM.Status.Utilization = vmopv1util.NextUtilization(
		vmCtx.VM.Status.Utilization,
		int64(qs.OverallCpuUsage),
		int64(qs.GuestMemoryUsage),
		metav1.Now())

	return nil
}
