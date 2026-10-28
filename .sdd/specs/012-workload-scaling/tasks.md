# Workload scaling tasks

**Epic**: vmop-1701. Adoption is tracked by vmop-4017. [NEEDS CLARIFICATION: allocate story tickets for the remaining implementation and lab-validation work before merge; do not invent ticket IDs.]

- [x] T001 [vmop-4017] Implement persistent adoption status, conflict-safe ownership, and regression coverage; align spec 009.
- [x] T002 Add selector status and scale mapping across served APIs; regenerate artifacts and verify conversions and older-client updates.
- [x] T003 Extend experimental Supervisor static/scale E2E cases and run focused local suites. All nine static ReplicaSet E2E cases passed on a dedicated Supervisor running Kubernetes 1.35.8 on 2026-10-23; autoscaler acceptance remains T005.
- [x] T004 Implement pinned dedicated-lab setup, namespaced autoscaler permissions, RabbitMQ VM worker, diagnostics, and owned-resource teardown.
- [ ] T005 Run three complete autoscaling cycles, failure recovery, namespace isolation, and the supported Supervisor version matrix; archive evidence. The dedicated Kubernetes 1.35.8 gate passed on 2026-10-23 with recorded harness corrections: three complete cycles, 450 verified unique job completions, replacement/redelivery, quota/access recovery, isolation, restart identity preservation, and owned workload teardown. Kubernetes 1.34/1.36 and broker power/network matrix cases remain outstanding.
- [x] T006 Run API/CRD/conversion generation verification, the full root-module suite, separate API conversion suites, changed-package lint, E2E compilation, and local lab-harness checks.
- [ ] T007 Publish the three dependent PRs after the lab evidence and remaining story-ticket mapping are available. Source changes remain local; the prototype is deployed to a dedicated testbed, and no PR has been published.
- [x] T008 Provision a dedicated Supervisor testbed through tera-tools, enable the workload-management capability, deploy the prototype image, and verify the running image and scale-selector contracts against fresh deployment credentials.
