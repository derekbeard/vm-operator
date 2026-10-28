# VM-aware scaling signals

**Status**: Draft
**Epic**: vmop-1701 (extends spec 012). [NEEDS CLARIFICATION: allocate a story ticket before merge; do not invent ticket IDs.]

Spec 012 makes a VirtualMachineReplicaSet scalable by an external autoscaler, but supplies no scaling signal: the user brings a metric such as queue depth. This spec adds a bottom-up, platform-provided signal, VM CPU and memory usage sampled from the hypervisor, so an HPA can scale replicas with no user-supplied metric. It is a demonstration of what is possible, behind the `K8sWorkloadMgmtAPI` capability.

## Acceptance criteria

1. A powered-on VM's `status.utilization` records hypervisor CPU (MHz) and guest memory (MiB) with a sample time, with no added vSphere calls. A powered-off VM, or a disabled capability, leaves it unset.
2. Status writes are bounded: an unchanged-within-noise sample is not rewritten more than once per refresh interval (60 seconds).
3. A ReplicaSet's usage is derived only from fresh samples of powered-on, non-deleting VMs. Missing or stale data is never treated as idle, and sparse data (under the minimum coverage) yields an error so an autoscaler holds its replica count.
4. [NOT BUILT] A metrics adapter serves the aggregate through `custom.metrics.k8s.io` as an `Object` metric on the ReplicaSet, and a real HPA scales replicas on it using an `AverageValue` target. It coexists with KEDA.
5. [NOT BUILT] Live evidence on a Supervisor: scale-out under CPU load, scale-in after load stops, a telemetry outage that holds replicas, and an adapter restart.

## Defaults and exclusions

Refresh interval 60 seconds, maximum sample age 180 seconds, minimum coverage 50 percent. These are demonstration defaults, not tuned values. Excluded: scale-to-zero on utilization (no samples exist at zero replicas), per-VM network or disk signals, predictive scaling, scale-in draining, and quota or capacity awareness. Guest memory requires VMware Tools.
