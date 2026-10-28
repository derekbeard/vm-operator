# VirtualMachineReplicaSet functional test plan

## Current implementation and acceptance

The observable contract is defined by the 38 scenarios in [tds.md](tds.md). Controller unit tests, envtest integration tests, admission tests, and experimental Supervisor E2E cases are present. Negative replica validation, readiness counting, and the persistent adoption condition required by SC19 are implemented. All local tests must pass; no intentionally red assertion is waived.

A VM without a configured readiness probe is implicitly ready. This does not certify application health. The autoscaling PoC in [spec 012](../012-workload-scaling/spec.md) uses an explicit GuestInfo application-readiness probe.

## Test surfaces

| Surface | Contract |
| --- | --- |
| Controller unit tests | Desired-count convergence, zero/back, fresh reconciler convergence, repeated edits, template/selector semantics, status and conditions, failures and recovery, ownership conflicts, and absence of rollout behavior. |
| Controller envtest integration | Actual API admission/defaulting, scale subresource behavior, ownership and adoption, replacement, namespace isolation, readiness, and blocked finalizers. Real garbage collection and vSphere provisioning require Supervisor E2E. |
| Admission webhook tests | Negative replicas, selector/template consistency, and mutation behavior. |
| Supervisor E2E | Powered-on creation, scale up/down, zero/back, `/scale` including its selector, adoption visibility, fresh replacement, and cascading deletion. New/changed cases retain `experimental` until verified on hardware. |

Controller tests live together in `controllers/virtualmachinereplicaset/virtualmachinereplicaset_controller_test.go`; labels distinguish unit and envtest infrastructure. Reuse the existing builder, fake provider, readiness helpers, and Ginkgo/Gomega asynchronous assertions. Do not introduce sleeps or weaken assertions to match a broken implementation.

The controller creates Kubernetes VM resources and never calls vSphere directly. Provider behavior is covered by existing provider vcsim suites; real Supervisor E2E remains necessary to establish actual provisioning, power, readiness, deletion, and restart behavior.

## Workload scaling regressions

Spec 012 adds canonical `status.selector` exposed by `/scale`, conversion round trips across served API versions, reconstruction after older status writers omit that field, selector availability during quota failures, and optimistic-lock ownership conflict retries. A successful adoption sets a historical `VirtualMachinesAdopted=True` condition with reason `SuccessfulAdopt`; subsequent scale-in does not erase it.

The dedicated-lab runner validates controller rollout/restart with VM identities held stable, live KEDA/HPA scaling without matching Pods, metric/broker access failures, quota recovery, and tenant namespace authorization. These results cannot be inferred from envtest or from compilation.

## Verification

```sh
# KUBEBUILDER_ASSETS must point to installed etcd/kube-apiserver binaries.
go test ./controllers/virtualmachinereplicaset/... ./webhooks/virtualmachinereplicaset/...
go -C api/test test ./...
# Hardware: configure the Supervisor test environment per test/e2e/README.md.
make test-e2e TEST_FOCUS=VirtualMachineReplicaSet LABEL_FILTER=experimental
```

Run focused suites after affected changes, then the required repository checks. Update the spec, plan, and tasks alongside any discovered implementation deviation. Record exact cluster versions and hardware results before removing experimental labels. Missing capability or cluster access is an incomplete acceptance gate, not a successful test.
