# VM-aware scaling implementation

## Telemetry

`VirtualMachineStatus.Utilization` (v1alpha6, optional, controller-owned) is set from `summary.quickStats` (`overallCpuUsage`, `guestMemoryUsage`). The VM summary is already fetched for every reconcile (`VMUpdatePropertiesSelector` includes `summary`), so no vSphere call is added and nothing leaves `pkg/providers/vsphere/`. Older served versions carry the field through the existing whole-status restore, as other v1alpha6-only status fields do. The write is gated by `K8sWorkloadMgmtAPI`.

Quick stats change roughly every 20 seconds, and every status write wakes the ReplicaSet controller and any other watcher. `NextUtilization` therefore keeps the previous sample unless it is older than the refresh interval or has moved by more than 10 percent and an absolute floor. The VM watcher's property list does not include `summary.quickStats`, and with async signalling enabled a steady powered-on VM is not requeued, so it reconciles only on the manager resync period (30 minutes by default). Left alone, every sample would exceed the 180 second maximum age and an autoscaler would correctly hold forever. `UtilizationRequeueDelay` therefore requeues a powered-on VM every refresh interval, only when it is controlled by a VirtualMachineReplicaSet and the capability is enabled, so the added reconcile load tracks the number of autoscaled VMs, not the whole inventory. The VM controller takes the shorter of this and its existing requeue delay.

## Aggregation

`SummarizeUtilization` sums fresh samples of powered-on, non-deleting VMs and reports how many replicas had one. `ReplicaTotals` extrapolates unsampled replicas from the sampled average and refuses sparse data. The adapter calls these and holds no state of its own.

## Metric serving [BUILT, NOT DEPLOYED]

An HPA can read a ReplicaSet's usage without Pods only through the external or custom metrics APIs. Kubernetes allows one APIService per group, and KEDA already claims `external.metrics.k8s.io` (the lab preflight refuses to take it over). The adapter therefore serves `custom.metrics.k8s.io` and the HPA uses an `Object` metric whose `describedObject` is the VirtualMachineReplicaSet.

Target type matters. In the HPA replica calculator (v1.35.8), `Object` with a `Value` target reaches `getReadyPodsCount` and fails when no Pods match the selector, unless current replicas are zero. `Object` with an `AverageValue` target uses status replicas as the divisor and never lists Pods. The adapter returns the fleet total (for example, total CPU MHz) and the user sets `AverageValue` to the per-VM target.

`pkg/metricsadapter` implements the provider and `cmd/vm-metrics-adapter` wires it to the `sigs.k8s.io/custom-metrics-apiserver` library (v1.37.0), which supplies the aggregated API server, serving certificate and delegated authentication. It reads through an informer cache so HPA polling is not API server load, counts only VMs the ReplicaSet controls, and returns 503 (so the HPA holds) when too few replicas have a fresh sample or the selector is empty or unsupported. `config/vm-metrics-adapter/` has demonstration manifests with a placeholder image and an example HPA.

The adapter must be a separate, stateless Deployment with read-only access to VMs and ReplicaSets, not part of the leader-elected manager, so it serves while the manager restarts. It uses an aggregated APIService with delegated authentication.

## Verification

Unit and envtest suites for the util and status packages, API conversion suites, and generation checks. The adapter and live scaling need a Supervisor and are not covered here.
