# VM-aware scaling tasks

**Epic**: vmop-1701. [NEEDS CLARIFICATION: allocate story tickets before merge; do not invent ticket IDs.]

- [x] T001 Add `status.utilization` to v1alpha6; regenerate deepcopy, conversions, CRD and API docs; run the separate API conversion suites.
- [x] T002 Sample quick stats into status behind `K8sWorkloadMgmtAPI`, clear when powered off, damp writes; unit tests in `pkg/providers/vsphere/vmlifecycle` (vcsim) and `pkg/util/vmopv1`.
- [x] T003 Add `SummarizeUtilization` and `ReplicaTotals` with missing, stale, powered-off, deleting and sparse-data tests.
- [x] T003a Requeue powered-on ReplicaSet VMs every refresh interval so samples stay fresh; unit-test the policy. Load not measured.
- [ ] T004 Build the `custom.metrics.k8s.io` adapter (stateless Deployment, aggregated APIService, delegated auth, read-only RBAC).
- [ ] T005 Add an E2E case and lab harness mode that scales a ReplicaSet on CPU under load using an `Object`/`AverageValue` HPA.
- [ ] T006 Live validation: scale-out, scale-in, telemetry outage hold, adapter restart; record evidence.
- [ ] T007 Decide whether to add a ReplicaSet-level status aggregate or leave aggregation in the adapter.
