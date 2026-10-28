# Workload scaling API

`VirtualMachineReplicaSetStatus.Selector string` uses JSON `selector,omitempty` in all served versions. It is optional and controller-owned. Its value is `metav1.LabelSelectorAsSelector(spec.selector).String()`, and CRD `labelSelectorPath` maps `.status.selector` into `autoscaling/v1.Scale.status.selector`. Existing desired and observed replica paths do not change.

`VirtualMachinesAdopted=True`, reason `SuccessfulAdopt`, records that the ReplicaSet adopted at least one VM during its lifetime. The condition is historical and is never used to gate reconciliation. Conditions are retained on later reconciliations and survive version conversions.

The lab adds no product CRDs. It uses KEDA's existing `ScaledObject` and `TriggerAuthentication`, a Kubernetes Secret, and namespaced Role/RoleBinding resources. One ScaledObject owns the generated HPA for each target. Metric selectors and VM selectors are separate interfaces.
