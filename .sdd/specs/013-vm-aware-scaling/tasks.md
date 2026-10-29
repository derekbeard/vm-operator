# VM-aware scaling tasks

**Epic**: vmop-1701. [NEEDS CLARIFICATION: allocate story tickets before merge; do not invent ticket IDs.]

- [x] T001 Add `status.utilization` to v1alpha6; regenerate deepcopy, conversions, CRD and API docs; run the separate API conversion suites.
- [x] T002 Sample quick stats into status behind `K8sWorkloadMgmtAPI`, clear when powered off, damp writes; unit tests in `pkg/providers/vsphere/vmlifecycle` (vcsim) and `pkg/util/vmopv1`.
- [x] T003 Add `SummarizeUtilization` and `ReplicaTotals` with missing, stale, powered-off, deleting and sparse-data tests.
- [x] T003a Requeue powered-on ReplicaSet VMs every refresh interval so samples stay fresh; unit-test the policy. Load not measured.
- [x] T004 Build the `custom.metrics.k8s.io` adapter (provider, binary, manifests with read-only RBAC and delegated auth); 10 unit tests pass. `cmd/vm-metrics-adapter/main_test.go` starts the real binary behind an envtest kube-apiserver configured as an aggregator (front-proxy certificates, the shipped RBAC and both APIServices, CA bundle). Through the aggregator it checks, with the HPA's own `k8s.io/metrics` client and real discovery: a 3k/7.5k MHz object metric, informer propagation, discovery of both metrics in `v1beta1`/`v1beta2`, a 403 for a caller without RBAC, a 401/403 for direct calls, and a 503 on stale samples. Two findings: the serving certificate needs the Service DNS SAN (otherwise discovery is `Stale` and the HPA cannot find the API, though the APIService shows `Available`), and the Pod needs a `/readyz` readiness probe (it serves about 100 ms before it loads the front-proxy CA). Both are fixed in `config/vm-metrics-adapter/{base,secure}`.
- [ ] T004a Release image build and fit into the product's deployment model (Supervisor namespace, cert-manager availability). Done: CA-bundle TLS via the cert-manager `secure/` overlay.
- [ ] T005 Add an E2E case and lab harness mode that scales a ReplicaSet on CPU under load using an `Object`/`AverageValue` HPA.
- [ ] T006 Live validation: scale-out, scale-in, telemetry outage hold, adapter restart; record evidence.
- [ ] T007 Decide whether to add a ReplicaSet-level status aggregate or leave aggregation in the adapter.
