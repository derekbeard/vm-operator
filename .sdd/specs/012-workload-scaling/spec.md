# VM Service workload scaling

**Status**: In Progress
**Epic**: vmop-1701

VMReplicaSet provides static scaling of interchangeable VirtualMachines. External autoscalers own desired replica count through `/scale`; the VM controller retains provisioning and lifecycle ownership. This feature remains gated by `K8sWorkloadMgmtAPI`.

## Acceptance criteria

1. Static creation, scale up/down, zero/back, replacement, restart convergence, quota recovery, and ownership isolation retain the contract in spec 009. Successful adoption records the persistent `VirtualMachinesAdopted` condition (vmop-4017). Concurrent ownership changes cannot be overwritten.
2. Every served ReplicaSet API exposes optional `status.selector` and maps it to `Scale.status.selector`. The controller reconstructs it from the actual selector, including when replica synchronization fails. Old-client updates cannot change desired count accidentally or permanently remove selector status.
3. A real Supervisor HPA scales VM replicas using external `AverageValue` metrics without matching Pods. KEDA uses the same namespace and API server as its target.
4. Dedicated-lab RabbitMQ workers demonstrate activation from zero, bounded scale-out, return to zero, worker replacement/redelivery, metric outage/recovery, and quota exhaustion/recovery. Three consecutive successful cycles are required for acceptance.
5. Credentials remain in Secrets. Setup detects external-metrics conflicts. Teardown removes only this run's resources. Cluster failures, skipped tests, and missing access are incomplete gates, never successful validation.

## Defaults and exclusions

Use RabbitMQ queue length, 10 messages per replica, activation threshold 0, min/max replicas 0/3, polling 15 seconds, cooldown and downscale stabilization 300 seconds, and an HPA `Pods: 1` scaling policy with `periodSeconds: 60`. KEDA writes separate zero-to-one activation and final scale-to-zero transitions; an HPA increase can follow activation within 60 seconds, and recent HPA downscale history can permit a faster rebound, so this is not a global write-rate limit. Worker VMs use explicit readiness probes, manual acknowledgement, prefetch 1, and idempotent processing with redelivery. These are lab defaults, not production settings. General application draining, CPU metrics, rollout controllers, Karpenter, and production enablement are excluded.
