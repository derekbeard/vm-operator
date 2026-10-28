# VM metrics adapter (demonstration)

Serves `custom.metrics.k8s.io` so a HorizontalPodAutoscaler can scale a `VirtualMachineReplicaSet` on the CPU or guest memory its VMs use. See spec 013. Status: built and unit-tested; **never deployed or run against a cluster**.

- Metrics: `vm_cpu_usage_mhz` and `vm_guest_memory_usage_mib`, totals across the ReplicaSet's replicas, on `virtualmachinereplicasets.vmoperator.vmware.com`.
- The VM controller must run with `K8sWorkloadMgmtAPI` enabled, which populates `status.utilization` and refreshes it every 60 seconds for ReplicaSet-owned VMs.
- If fewer than `--min-coverage` of the replicas have a fresh sample, the API returns 503 and the HPA holds its current replica count.
- `adapter.yaml` needs a real image (`REGISTRY/vm-metrics-adapter:TAG`); no release image build exists. Build the binary with `go build ./cmd/vm-metrics-adapter`.
- It cannot be installed next to another `custom.metrics.k8s.io` provider. It does not conflict with KEDA, which serves `external.metrics.k8s.io`.
- `hpa-example.yaml` shows the required `Object` metric with an `AverageValue` target.
