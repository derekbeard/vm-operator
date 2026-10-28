# Workload scaling research and verification

## References

- [Kubernetes scale subresource](https://kubernetes.io/docs/tasks/extend-kubernetes/custom-resources/custom-resource-definitions/#scale-subresource): HPA requires a serialized nonempty selector.
- [HPA implementation](https://github.com/kubernetes/kubernetes/blob/master/pkg/controller/podautoscaler/horizontal.go): selector validation precedes metric computation.
- [External metric calculation](https://github.com/kubernetes/kubernetes/blob/master/pkg/controller/podautoscaler/replica_calculator.go): `AverageValue` uses target status replicas; resource metrics and several value-based paths assume Pods.
- [KEDA custom resources](https://keda.sh/docs/2.21/concepts/scaling-deployments/): KEDA activates from zero and delegates ordinary scaling to HPA.
- [RabbitMQ scaler](https://keda.sh/docs/2.21/scalers/rabbitmq-queue/): queue-length scaling supports per-instance targets.

## Baseline

The controller and static E2E tests already exist on main. The adoption test contradicts spec 009 by omitting its required persistent condition. The scale CRD lacks labelSelectorPath. Replica readiness without a configured probe is implicit and is not an application-health guarantee.

## Cluster evidence

A dedicated vSphere/Supervisor testbed was provisioned through tera-tools on 2026-10-23. The workload-management capability is activated, and the prototype image is running in both VM Operator deployments. Fresh credentials retrieved from that deployment are attached to the vm-operator checkout through tera-tools. All nine experimental static ReplicaSet E2E cases passed against Kubernetes 1.35.8: powered-on creation, readiness, scale up/down, zero/back, `/scale`, adoption, replacement, and cascading deletion. The running CRD exposes `.status.selector` through `/scale` in all four served versions.

Live KEDA 2.21.0/HPA integration passed on Kubernetes 1.35.8: three complete zero-to-three-to-zero cycles, 270 cycle jobs plus 180 failure-workload jobs (450 unique completions), worker replacement, 12 completed redelivered jobs, native object-count quota rejection and automatic recovery, tenant isolation, and controller restart convergence with all three VM UIDs unchanged. Explicit metrics-credential and broker-permission errors were observed for 363 and 362 seconds; desired replicas stayed at least three and two respectively. Owned workload/autoscaler teardown passed, and all six Supervisor nodes and the prototype remained healthy. The Kubernetes 1.34/1.36 matrix and broker power/network outages remain unverified. Dependency compatibility remains a preflight gate. The first static E2E attempt failed in shared setup at a platform quota admission webhook; its report was retained separately, and the successful retry followed webhook recovery.

## Implementation verification

- Controller/admission focused unit and envtest integration suites pass using local Kubernetes 1.36 assets. Regression coverage includes optimistic-lock ownership conflicts, canonical scale selector, quota failure/recovery, served-version conversion round trips, and older-client updates.
- API conversion suites for v1alpha1 through v1alpha5 pass in the separate `api/test` module.
- The experimental Supervisor ReplicaSet E2E package compiles, and all nine cases pass on the dedicated live Supervisor described above.
- Twenty-one local Python safety/delivery/provenance tests pass. Shell syntax checks pass. The checksum-pinned KEDA chart renders all three container images with the locked digests, and custom-resource cluster-wide scaling permissions are disabled.
- Changed-package Go lint passes for the controller, API types/conversions, and E2E package. CRD/object/conversion generators have been rerun. API reference changes retain the generated ReplicaSet status section; unrelated documentation-generator drift is excluded.
- The broad `make generate-go` target reaches an unrelated cloud-init schema step requiring unavailable npm. API object generation itself completes using controller-gen; no cloud-init or network schema changes are part of this work.

The dedicated-lab runner implements `setup`, `cycles`, `restart`, `failures`, `isolation`, `diagnostics`, and `teardown`. Broker authorization failure exercises both scaler and worker access loss; it is not evidence of broker-VM power or network-failure behavior. The full root-module `go test -p 4 ./...` suite passes with Kubernetes 1.36 envtest assets. Focused controller/admission integration tests were rerun successfully after adding direct old-version status-update coverage. The prototype is deployed to the dedicated testbed; source changes remain local and no PRs have been published.

Live validation on 2026-10-23 confirmed that KEDA zero-to-one activation is independent of HPA scale policies: the first observed transition from 1 to 2 followed activation by about five seconds, followed by the HPA-rate-limited increase to 3. Document the policy scope explicitly; the configuration does not implement a global replica-write rate limit.

On the immediately repeated burst, the observed desired count changed from 1 to 3. The [upstream Kubernetes 1.35.8 scale-up budget](https://github.com/kubernetes/kubernetes/blob/v1.35.8/pkg/controller/podautoscaler/horizontal.go#L1231-L1257) incorporates both recent additions and deletions when calculating the period-start count. This accounts for faster rebound after a recent downscale (an inference matching the observed trace). The configured Pods:1/60s policy therefore does not promise one VM per individual write; a strict global VM creation rate would require separate coordination.

Live validation retained failed and superseded attempts. The KEDA chart required a checksum-recorded CRX placement adaptation. A smoke VM verified offline loading of the pinned pure-Python wheel in a --without-pip environment because guest repository access and ensurepip were unavailable. Native quota removal briefly exposed inconsistent storage reservation accounting; the platform reconciled automatically without raised limits, status edits, or restarts. Changing the metrics Secret alone did not invalidate the cached client within the observation setup, so scoped server-side credential revocation supplied the actual fault. A loaded assertion requiring three recovered workers was deliberately stopped only after both outage windows, restored access, and healthy current HPA demand of two were recorded; corrected shared recovery/drain code completed the remaining checks. This is staged hardware validation, not a claim that every original process passed or that the final driver was replayed from a fresh setup. A server-version metadata shadowing bug was fixed with regression coverage; the report uses a fresh live API version query and preserves the original state. Subsequent phases snapshot driver inputs.

The broker container and its remote credential file were removed. The dedicated broker VM remains under its recorded lease because tera-tools resource deletion could not verify its Nimbus identity; no guessed VM deletion was attempted. Pre-provisioned namespace/tenant RBAC fixtures and the Supervisor prototype were preserved. Running KEDA rootfs layers matched hash-verified pinned OCI manifests; the CRX runtime does not expose the full image-config digest through Pod status. These limits and the operational gaps are recorded in private evidence and a tera-tools improvement prompt.

## Red-team corrections and evidence caveats

A post-hoc review of the harness (2026-10-28) found that several recorded 2026-10-23 live results were produced by weaker assertions than the research text implies. The harness now corrects them, but the corrected code has **not** been replayed on a live Supervisor, so the results above predate these corrections and should not be read as validating them.

- The restart case only checked that VM identities stayed unchanged for 45 seconds, which a controller doing nothing also satisfies. It now also deletes one replica and requires a replacement. It requires `--allow-operator-restart` because it restarts the named VM Operator deployment for every tenant.
- The outage case failed only if desired replicas reached zero, so a drop from three to one would have passed. It now fails if desired falls below its starting value. The recorded "at least three and two" minimums came from the earlier assertion; the 12 redelivered jobs are not attributable to the VM-kill step alone, since worker disconnects, scale-in and broker outages also cause redelivery.
- `cycles` reused deterministic job IDs and did not reset `successful_cycles`, so leftovers from an aborted run could count toward a later one. It now purges both queues, resets acceptance state, and adds a per-invocation nonce. Nothing aggregates the acceptance flags; the operator must confirm each phase succeeded in one uninterrupted run.
- Raw results are now persisted before validation, because the broker drain is destructive.
- Teardown now tolerates a missing KEDA namespace or broker objects, and waits for owned VMs to be garbage collected before removing their Secrets and RBAC.
- Evidence artifacts are private and not in this repository. The counts above (450 completions, three cycles) are the author's recorded results, not independently reproducible from the repo.

Known remaining gaps: the isolation check uses `auth can-i --as` without groups and makes no real `/scale` call; worker boot packages (apt, pip) and the guest image are not pinned; broker traffic is plain HTTP/AMQP; Helm leaves cluster-wide CRDs behind, so the harness supports one install per cluster; the preflight API-group check does not verify discovery; the supported Kubernetes range (1.34–1.36) exceeds the single validated 1.35.8.
