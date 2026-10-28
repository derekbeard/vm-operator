# VM-aware scaling tasks

**Epic**: vmop-1701. [NEEDS CLARIFICATION: allocate story tickets before merge; do not invent ticket IDs.]

- [x] T001 Add `status.utilization` to v1alpha6; regenerate deepcopy, conversions, CRD and API docs; run the separate API conversion suites.
- [x] T002 Sample quick stats into status behind `K8sWorkloadMgmtAPI`, clear when powered off, damp writes; unit tests in `pkg/providers/vsphere/vmlifecycle` (vcsim) and `pkg/util/vmopv1`.
- [x] T003 Add `SummarizeUtilization` and `ReplicaTotals` with missing, stale, powered-off, deleting and sparse-data tests.
- [x] T003a Requeue powered-on ReplicaSet VMs every refresh interval so samples stay fresh; unit-test the policy. Load not measured.
- [x] T004 Build the `custom.metrics.k8s.io` adapter (provider, binary, demonstration manifests with read-only RBAC and delegated auth); 10 unit tests pass. Not deployed: the image is a placeholder, the APIService uses `insecureSkipTLSVerify`, and no release packaging exists.
- [ ] T004a Package the adapter into a release image, replace `insecureSkipTLSVerify` with a CA bundle, decide the Supervisor namespace, and fit it into the product's deployment model.
- [ ] T005 Add an E2E case and lab harness mode that scales a ReplicaSet on CPU under load using an `Object`/`AverageValue` HPA.
- [ ] T006 Live validation: scale-out, scale-in, telemetry outage hold, adapter restart; record evidence.
- [ ] T007 Decide whether to add a ReplicaSet-level status aggregate or leave aggregation in the adapter.
