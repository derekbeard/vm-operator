# VM-aware scaling API

`VirtualMachineStatus.Utilization *VirtualMachineUtilizationStatus` (v1alpha6, optional) with `cpuUsageMHz` (int64), `guestMemoryUsageMiB` (int64) and `sampleTime` (required). `sampleTime` is when the operator read the sample, not a hypervisor timestamp: quick stats carry none. Consumers must ignore samples older than 180 seconds.

The adapter exposes one metric per resource on the VirtualMachineReplicaSet: total CPU usage in MHz and total guest memory in MiB. HPA divides by status replicas and compares with its `AverageValue` target. Metric names and the exact quantity format are decided when the adapter is built.
