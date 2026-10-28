// © Broadcom. All Rights Reserved.
// The term "Broadcom" refers to Broadcom Inc. and/or its subsidiaries.
// SPDX-License-Identifier: Apache-2.0

// Package metricsadapter serves VM-aware scaling signals through the
// custom.metrics.k8s.io API so a HorizontalPodAutoscaler can scale a
// VirtualMachineReplicaSet on the CPU and memory its VMs actually use.
package metricsadapter

import (
	"context"
	"fmt"
	"time"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/labels"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/metrics/pkg/apis/custom_metrics"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/custom-metrics-apiserver/pkg/provider"

	vmopv1 "github.com/vmware-tanzu/vm-operator/api/v1alpha6"
	vmopv1util "github.com/vmware-tanzu/vm-operator/pkg/util/vmopv1"
)

const (
	// MetricVMCPUUsageMHz is the total CPU usage, in MHz, of a ReplicaSet's
	// VMs. Use it with an HPA Object metric and an AverageValue target equal
	// to the desired per-VM usage in MHz.
	MetricVMCPUUsageMHz = "vm_cpu_usage_mhz"

	// MetricVMGuestMemoryUsageMiB is the total guest memory usage, in MiB, of
	// a ReplicaSet's VMs, with the same usage pattern.
	MetricVMGuestMemoryUsageMiB = "vm_guest_memory_usage_mib"

	// DefaultMinCoverage is the fraction of replicas that must have a fresh
	// sample before a total is reported.
	DefaultMinCoverage = 0.5
)

var replicaSetGroupResource = schema.GroupResource{
	Group:    vmopv1.GroupVersion.Group,
	Resource: "virtualmachinereplicasets",
}

// Provider is a read-only provider of per-ReplicaSet VM utilization totals.
// It is stateless: every answer is computed from the VMs and ReplicaSets the
// Reader returns, which should be backed by an informer cache.
type Provider struct {
	// Reader reads VirtualMachines and VirtualMachineReplicaSets.
	Reader client.Reader

	// Now returns the current time; it defaults to time.Now.
	Now func() time.Time

	// MinCoverage is the fraction (0..1) of replicas that must have a fresh
	// sample. Below it the metric is unavailable and the HPA holds. A zero
	// value uses DefaultMinCoverage.
	MinCoverage float64
}

var _ provider.CustomMetricsProvider = &Provider{}

func (p *Provider) now() time.Time {
	if p.Now != nil {
		return p.Now()
	}
	return time.Now()
}

func (p *Provider) minCoverage() float64 {
	if p.MinCoverage > 0 {
		return p.MinCoverage
	}
	return DefaultMinCoverage
}

// ListAllMetrics implements provider.CustomMetricsProvider.
func (p *Provider) ListAllMetrics() []provider.CustomMetricInfo {
	return []provider.CustomMetricInfo{
		{GroupResource: replicaSetGroupResource, Namespaced: true, Metric: MetricVMCPUUsageMHz},
		{GroupResource: replicaSetGroupResource, Namespaced: true, Metric: MetricVMGuestMemoryUsageMiB},
	}
}

// GetMetricByName implements provider.CustomMetricsProvider.
func (p *Provider) GetMetricByName(
	ctx context.Context,
	name types.NamespacedName,
	info provider.CustomMetricInfo,
	_ labels.Selector) (*custom_metrics.MetricValue, error) {

	if err := validate(info); err != nil {
		return nil, err
	}

	var rs vmopv1.VirtualMachineReplicaSet
	if err := p.Reader.Get(ctx, name, &rs); err != nil {
		if apierrors.IsNotFound(err) {
			return nil, provider.NewMetricNotFoundForError(
				info.GroupResource, info.Metric, name.Name)
		}
		return nil, apierrors.NewInternalError(
			fmt.Errorf("failed to get VirtualMachineReplicaSet: %w", err))
	}

	return p.value(ctx, &rs, info)
}

// GetMetricBySelector implements provider.CustomMetricsProvider. The selector
// matches VirtualMachineReplicaSet labels, as for any Kubernetes object.
func (p *Provider) GetMetricBySelector(
	ctx context.Context,
	namespace string,
	selector labels.Selector,
	info provider.CustomMetricInfo,
	_ labels.Selector) (*custom_metrics.MetricValueList, error) {

	if err := validate(info); err != nil {
		return nil, err
	}
	if namespace == "" {
		return nil, provider.NewMetricNotFoundError(info.GroupResource, info.Metric)
	}

	var list vmopv1.VirtualMachineReplicaSetList
	if err := p.Reader.List(ctx, &list,
		client.InNamespace(namespace),
		client.MatchingLabelsSelector{Selector: selector}); err != nil {

		return nil, apierrors.NewInternalError(
			fmt.Errorf("failed to list VirtualMachineReplicaSets: %w", err))
	}

	out := &custom_metrics.MetricValueList{}
	for i := range list.Items {
		v, err := p.value(ctx, &list.Items[i], info)
		if err != nil {
			return nil, err
		}
		out.Items = append(out.Items, *v)
	}
	return out, nil
}

func validate(info provider.CustomMetricInfo) error {
	if info.GroupResource != replicaSetGroupResource || !info.Namespaced {
		return provider.NewMetricNotFoundError(info.GroupResource, info.Metric)
	}
	switch info.Metric {
	case MetricVMCPUUsageMHz, MetricVMGuestMemoryUsageMiB:
		return nil
	}
	return provider.NewMetricNotFoundError(info.GroupResource, info.Metric)
}

func (p *Provider) value(
	ctx context.Context,
	rs *vmopv1.VirtualMachineReplicaSet,
	info provider.CustomMetricInfo) (*custom_metrics.MetricValue, error) {

	// Use the controller's own notion of which VMs belong to the ReplicaSet.
	// An absent, empty or expression-based selector is not managed by the
	// controller, so it has no meaningful total either.
	if rs.Spec.Selector == nil {
		return nil, unavailable(rs, "has no selector")
	}
	if _, err := metav1.LabelSelectorAsMap(rs.Spec.Selector); err != nil {
		return nil, unavailable(rs, "has an unsupported selector")
	}
	selector, err := metav1.LabelSelectorAsSelector(rs.Spec.Selector)
	if err != nil || selector.Empty() {
		return nil, unavailable(rs, "has an empty or invalid selector")
	}

	var vms vmopv1.VirtualMachineList
	if err := p.Reader.List(ctx, &vms,
		client.InNamespace(rs.Namespace),
		client.MatchingLabelsSelector{Selector: selector}); err != nil {

		return nil, apierrors.NewInternalError(
			fmt.Errorf("failed to list VirtualMachines: %w", err))
	}

	// Count only VMs this ReplicaSet controls: a standalone VM matching the
	// selector is not a replica and must not skew the total.
	owned := make([]vmopv1.VirtualMachine, 0, len(vms.Items))
	for i := range vms.Items {
		if metav1.IsControlledBy(&vms.Items[i], rs) {
			owned = append(owned, vms.Items[i])
		}
	}

	now := p.now()
	cpu, mem, ok := vmopv1util.SummarizeUtilization(
		owned, now, vmopv1util.UtilizationMaxAge).ReplicaTotals(p.minCoverage())
	if !ok {
		return nil, unavailable(rs,
			"has too few VMs with a fresh utilization sample; holding replicas")
	}

	total := cpu
	if info.Metric == MetricVMGuestMemoryUsageMiB {
		total = mem
	}

	return &custom_metrics.MetricValue{
		DescribedObject: custom_metrics.ObjectReference{
			APIVersion: vmopv1.GroupVersion.String(),
			Kind:       "VirtualMachineReplicaSet",
			Namespace:  rs.Namespace,
			Name:       rs.Name,
		},
		Metric:    custom_metrics.MetricIdentifier{Name: info.Metric},
		Timestamp: metav1.NewTime(now),
		Value:     *resource.NewQuantity(total, resource.DecimalSI),
	}, nil
}

func unavailable(rs *vmopv1.VirtualMachineReplicaSet, why string) error {
	return apierrors.NewServiceUnavailable(fmt.Sprintf(
		"VirtualMachineReplicaSet %s/%s %s", rs.Namespace, rs.Name, why))
}
