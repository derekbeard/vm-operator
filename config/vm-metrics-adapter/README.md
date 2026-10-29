# VM metrics adapter

Serves `custom.metrics.k8s.io` so a HorizontalPodAutoscaler can scale a `VirtualMachineReplicaSet` on the CPU or guest memory its VMs use. See spec 013.

Status: the real binary is tested behind a real kube-apiserver aggregator (envtest, `cmd/vm-metrics-adapter/main_test.go`), using the shipped RBAC and APIServices and the HPA controller's own client. It has **not run in a cluster**, and no HPA controller has acted on its metrics yet.

- Metrics: `vm_cpu_usage_mhz` and `vm_guest_memory_usage_mib`, totals across the ReplicaSet's replicas, on `virtualmachinereplicasets.vmoperator.vmware.com`. Served at `v1beta2` (preferred by the HPA) and `v1beta1`.
- The VM controller must run with `K8sWorkloadMgmtAPI` enabled, which populates `status.utilization` and refreshes it every 60 seconds for ReplicaSet-owned VMs.
- If fewer than `--min-coverage` of the replicas have a fresh sample, the API returns 503 and the HPA holds its current replica count.
- The image `REGISTRY/vm-metrics-adapter:TAG` is a placeholder; no release image build exists. Build the binary with `go build ./cmd/vm-metrics-adapter`.
- It cannot be installed next to another `custom.metrics.k8s.io` provider. It does not conflict with KEDA, which serves `external.metrics.k8s.io`.
- `hpa-example.yaml` shows the required `Object` metric with an `AverageValue` target.

## Layouts

- `base/`: demonstration only. A self-signed certificate and `insecureSkipTLSVerify` on the APIServices.
- `secure/`: requires cert-manager. Issues a serving certificate for the Service DNS name, mounts it, and injects its CA into both APIServices.

`kubectl apply -k config/vm-metrics-adapter/secure`

## TLS requirements found in testing

- The serving certificate must include `vm-metrics-adapter.vmop-metrics-adapter.svc` as a SAN. The aggregator verifies against that name. Without it the APIService still reports `Available`, but aggregated discovery marks the group `Stale`, so the HPA reports "no known available metric versions" and never scales.
- The adapter serves about 100 ms before it has loaded the front-proxy CA, and requests in that window are forbidden. The readiness probe on `/readyz` keeps the Pod out of the Service until then.
