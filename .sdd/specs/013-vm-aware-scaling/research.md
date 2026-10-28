# VM-aware scaling research

## Findings

- A steady powered-on VM is not reconciled between resyncs (the watcher ignores quick stats, async signalling disables the IP requeue, default resync is 30 minutes), so sampling in status alone would have produced stale data. Found by reading the controller, not by running it; the requeue added for it is untested against a live manager.

- VM Operator had no utilization telemetry: no use of quick stats or the performance manager in `api/`, `pkg/` or `controllers/`. The VM summary, including `quickStats`, is already retrieved on every reconcile.
- The HPA resource-metric path (CPU/memory utilization) relies on Pod metrics and Pods matching the selector, so it cannot serve VMs. That is why spec 012 uses external metrics.
- `external.metrics.k8s.io` can have one provider; KEDA holds it in the lab. A second provider cannot coexist, so a VM-aware adapter uses `custom.metrics.k8s.io`.
- From [replica_calculator.go at v1.35.8](https://github.com/kubernetes/kubernetes/blob/v1.35.8/pkg/controller/podautoscaler/replica_calculator.go): `GetObjectPerPodMetricReplicas` (the `AverageValue` path) uses status replicas and does not list Pods; `GetObjectMetricReplicas` (the `Value` path) calls `getReadyPodsCount` when current replicas are nonzero and the ratio is outside tolerance, and that errors when no Pods match. This was read from a fetched summary of the source and should be re-verified against the exact code before the adapter is built.

## Verification so far

Local only. The new unit tests pass: three vcsim-backed status cases, plus NextUtilization, SummarizeUtilization, ReplicaTotals and UtilizationRequeueDelay cases in `pkg/util/vmopv1`. The `vmlifecycle` and VM controller packages pass in full. The API conversion suites for v1alpha1 through v1alpha5 pass. The adapter's provider has 10 passing unit tests with a fake client, and `golangci-lint` is clean on the new packages. The adapter binary builds but has never been started, so aggregated-API registration, TLS, delegated auth and the HPA's real request shape are unverified. No HPA has scaled on this signal and no run has touched a live Supervisor.

## Risks and open questions

- The adapter library adds a direct dependency on `sigs.k8s.io/custom-metrics-apiserver` and `k8s.io/metrics`, about 70 lines of go.mod/go.sum churn, and the binary is about 134 MB.
- The demonstration APIService skips TLS verification; it is not acceptable beyond a lab.

- Status write amplification from sampling is bounded by damping but not measured at scale.
- Samples refresh only on reconcile. The controller now requeues powered-on ReplicaSet VMs every 60 seconds, but the added reconcile load and the resulting freshness have not been measured at scale, and each reconcile's cost is unmeasured; 180 seconds may still be too tight under load.
- Utilization scaling suits stateless, replaceable VMs poorly if they are long-lived or bursty; boot time and quota are not modeled.
- CPU in MHz needs a per-VM target the user must choose; a percentage would need each VM's CPU capacity.
- Whether the aggregate belongs in a ReplicaSet status field instead of the adapter (T007).
