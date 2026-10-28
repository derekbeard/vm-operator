# Workload scaling implementation

## Static scaling

Extend the existing controller orchestration with an optimistic-lock adoption patch and persistent adoption condition. Reuse the existing condition patch helper and controller feature gate. Keep replica semantics and readiness fallback unchanged; explicit application readiness is required for the PoC. Keep business logic in reusable packages and all vSphere calls inside the provider.

## Scale interface and compatibility

Add a string `selector` to ReplicaSet status in v1alpha3 through v1alpha6 and set the scale marker's selectorpath to `.status.selector`. Populate it before replica operations. It is optional, contains no desired state, and is reconstructed on reconciliation. Existing object updates cannot overwrite status through the main-resource endpoint. Older status writers may drop it transiently; the controller restores it. Regenerate CRDs and code and exercise actual older-version API writes, not just fake clients. This additive change is authorized for the gated API; it does not assert that additive changes to shipped APIs are generally safe.

## Lab integration

Use KEDA's RabbitMQ scaler and generated HPA against the Supervisor VMReplicaSet. RabbitMQ runs on a dedicated service VM reachable from KEDA and workers. Cloud-init installs the worker as a systemd service. Namespaced RBAC binds explicit KEDA and HPA identities; no broad cluster permissions are added to VM Operator. Pin the Helm chart and images using a compatibility-checked lock file. Preflight checks capability, API discovery, permissions, external metrics ownership, connectivity, and VM inputs. Retain evidence and require three repeatable cycles before accepting the PoC.

## Verification and rollout

Run focused controller/admission and API conversion tests, generation checks, then required repository checks. Add experimental real-Supervisor cases in the existing E2E package and provide a standalone autoscaling lab runner. Do not remove experimental labels without hardware evidence or enable the capability in production. Rollback removes the ScaledObject first and waits for its HPA to disappear before manual scaling. Dependency removal is restricted to the dedicated installation created by this run.

Supervisor-specific lab provisioning uses pre-created, vCenter-registered workload namespaces for the target and KEDA. The harness verifies a common kube-system UID for contextual kubectl and Helm, discovers worker OS placement, and adapts only the three hardcoded selectors in a checksum-verified KEDA chart for CRX workers. Preserve both upstream and derived chart evidence. Minimal worker images may use an embedded, hash-verified pure-Python wheel in a --without-pip environment; smoke-test actual guest application readiness and broker connectivity before timed cycles. Outage observation must exceed the configured cooldown and distinguish scaler errors from normal inactive conditions.
