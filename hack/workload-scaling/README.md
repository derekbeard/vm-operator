# VMReplicaSet autoscaling lab

This dedicated-lab PoC exercises KEDA → HPA → VMReplicaSet with disposable RabbitMQ worker VMs. It remains behind `supports_vm_service_k8s_workload_mgmt_api`; it does not enable production autoscaling. Acceptance requires three successful cycles, failure recovery, namespace isolation, and the static Supervisor E2E suite. A local test pass does not satisfy these hardware gates.

## Prerequisites

- A dedicated Supervisor running Kubernetes 1.34–1.36, the workload-management capability activated, and the selector-enabled VM Operator controller and CRD installed. KEDA 2.21's [tested compatibility window](https://keda.sh/docs/2.21/operate/cluster/) limits this pinned PoC; a different Supervisor version requires a separately reviewed dependency lock.
- Admin kubeconfig for that Supervisor, permission to install KEDA and the aggregated external-metrics API, and no existing KEDA CRDs or external-metrics provider. The installer refuses takeover. KEDA runs on the same cluster as VMReplicaSet; installing it only in a guest cluster cannot scale the Supervisor target.
- An existing, empty, dedicated VM namespace with image, class, storage, network, and quota sufficient for three workers. The quota failure test temporarily rejects all new VM CRs in this namespace, so do not share it with other workloads.
- A second empty Supervisor workload namespace for the KEDA release, provisioned through vCenter with PodVM networking/storage and labeled `vmoperator.vmware.com/scaling-lab=RUN_ID`. Pass it as `--keda-namespace`; the harness preserves this pre-provisioned namespace on teardown. Creating a bare Kubernetes namespace does not configure Supervisor PodVM placement.
- A second existing namespace and an existing tenant identity authorized to scale VMs only in the target namespace, plus permission to impersonate that identity for authorization checks.
- A dedicated Linux broker VM with Docker, reachable on TCP 5672 from worker VMs and TCP 15672 from KEDA and the test runner. Workers need package-repository/PyPI access. Use an Ubuntu image with cloud-init and VMware Tools; `python3-venv` must be available. No shared PVCs or stable per-instance identities are supported by the disposable template.
- Python 3.9+, kubectl, and Helm. `lab.py` itself uses only the Python standard library; workers install the hash-pinned Pika wheel.

## Start the broker

Copy `broker.sh` and `dependencies.json` into the same directory on the dedicated service VM (Python 3 is required). Create a mode-0600 env file on that VM containing `RABBITMQ_DEFAULT_USER` and `RABBITMQ_DEFAULT_PASS`, using fresh lab-only credentials. Run:

```sh
bash broker.sh start example /private/path/rabbitmq.env
```

The broker image is pinned to a manifest digest. It is disposable and has no persistent host volume. Restrict its management port to the dedicated lab network; this PoC uses plain HTTP/AMQP. `broker.sh stop example` checks the run label before removing the container. The lab creates a separate broker vhost and user and never uses the admin account in worker VMs.

## Set up the target

Prepare a **new** v1alpha6 VirtualMachine manifest in JSON with the lab's `spec.imageName`, `spec.className`, `spec.storageClass`, and required network/resource-policy configuration. Do not copy instance identifiers from a running VM. Omit bootstrap, volumes, and readinessProbe; the harness adds its own bootstrap and GuestInfo readiness probe. Save it outside the repository as `worker-template.json`.

Identify the actual HPA controller identity from the Supervisor controller configuration. Save its RBAC subject in `hpa-subject.json`. A cluster using individual controller service accounts commonly uses:

```json
{"kind":"ServiceAccount","name":"horizontal-pod-autoscaler","namespace":"kube-system"}
```

For a controller using a User identity, supply the actual `kind`, `name`, and `apiGroup` instead. The installer does not guess the identity. A Role in the target namespace grants that identity and KEDA access to VMReplicaSet `/scale` and external metrics. KEDA's chart-wide custom-resource scale grant is disabled.

Set `KUBECONFIG` to one explicit cluster-extension administrator config, plus `BROKER_HTTP_URL` (for example `http://broker-host:15672`), `BROKER_AMQP_HOST` (for example `broker-host:5672`), `BROKER_ADMIN_USER`, and `BROKER_ADMIN_PASSWORD` in your environment. VM Service administration alone may not grant extension-installation privileges. Keep credentials and kubeconfig outside version control. The harness checks that Helm's config and the kubectl command identify the same cluster. Then run:

```sh
python3 hack/workload-scaling/lab.py setup \
  --run example --namespace scaling-lab --state /tmp/vmrs-example \
  --keda-namespace scaling-lab-keda \
  --template /private/path/worker-template.json \
  --hpa-subject /private/path/hpa-subject.json
```

State and evidence belong to one run and one cluster. The harness records the `kube-system` namespace UID and refuses later operations against another cluster. Choose a fresh run ID and directory for each installation. A failed setup retains state for diagnosis and teardown; do not overwrite it to retry.

`--kubectl` also accepts a quoted command prefix, executed without a shell, so an explicitly selected tera-tools context can be used directly, for example `--kubectl 'tbenv kubectl --worktree "/path/to/vm-operator" supervisor-autoscaling-admin --'`. Supply that prefix to each operation. The KEDA namespace choice is retained in run state for later commands.

The KEDA chart is checksum-pinned, all three KEDA images are digest-pinned, and RabbitMQ/Pika are pinned. Defaults are queue length 10 per VM, activation threshold 0, bounds 0–3, polling 15 seconds, cooldown/downscale stabilization 300 seconds, and at most one replica change per 60 seconds. The worker sets `guestinfo.vmrs-ready=true` only after connecting and initializing its queues. It uses prefetch 1, manual acknowledgements, and confirms a result before acknowledging a job. Hashing the job ID has no external side effects; duplicate result records are deduplicated by ID. This is an at-least-once demonstration, not a general application-draining solution.

## Run and inspect validation

```sh
python3 hack/workload-scaling/lab.py cycles --run example --namespace scaling-lab --state /tmp/vmrs-example
python3 hack/workload-scaling/lab.py restart --run example --namespace scaling-lab --state /tmp/vmrs-example \
  --operator-namespace vmoperator-namespace --operator-deployment actual-vmoperator-deployment
python3 hack/workload-scaling/lab.py failures --run example --namespace scaling-lab --state /tmp/vmrs-example
python3 hack/workload-scaling/lab.py isolation --run example --namespace scaling-lab --state /tmp/vmrs-example \
  --tenant-subject /private/path/tenant-subject.json --foreign-namespace another-lab-namespace
python3 hack/workload-scaling/lab.py diagnostics --run example --namespace scaling-lab --state /tmp/vmrs-example
```

Each of three cycles publishes 90 ten-second jobs, waits for three ready workers, verifies every unique completion, and waits for queue drain and scale-to-zero. The first cycle deletes a worker while work is in flight and requires a fresh ready replacement. Cycles can take tens of minutes because they include real provisioning and cooldowns. Every poll enforces the maximum desired replica count. Timeouts fail the run; they do not skip validation.

The failure pass uses an object-count ResourceQuota to reject VM creation and requires a visible `ReplicaFailure` naming that exact quota, then removes only that quota and verifies recovery. It next invalidates KEDA's metric credential at the RabbitMQ server (rotating only the dedicated run user's password; a Secret update alone may leave a cached scaler client usable) and removes the run user's broker permissions, separately, to verify explicit scaler errors and no false scale-to-zero beyond the full configured cooldown. Credentials and permissions are restored in `finally` blocks. It then verifies all jobs complete and replicas return to zero. This tests broker access failure, not a service-VM power outage; power/network disruption remains a hardware matrix case.

The restart pass pauses KEDA at three replicas, restarts the explicitly named VM Operator deployment, and requires VM identities to remain unchanged through resync. It always removes the pause annotation afterward. Run it only in the dedicated lab because it restarts that deployment.

The isolation pass requires positive `/scale` authorization in the target namespace and negative authorization in the second namespace for the supplied tenant. It does not alter that tenant's existing permissions.

Evidence includes desired/observed/ready replicas, activation latency, completion IDs, conditions, events, KEDA logs, and cluster version. The state flags `successful_cycles`, `failure_checks_passed`, and `namespace_isolation_passed`, and `controller_restart_passed` must all indicate success. Preserve the private evidence directory. Review logs before sharing because upstream error messages may contain environment details. Never share Secrets, cloud-init payloads, or kubeconfig.

Run the existing static ReplicaSet suite separately using `make test-e2e TEST_FOCUS=VirtualMachineReplicaSet LABEL_FILTER=experimental` after configuring the Supervisor test environment per `test/e2e/README.md`. Leave `experimental` labels until those cases have passed on hardware. Run the restart command above for controller restart coverage. Real broker power/network failures remain a hardware matrix case; do not infer them from the broker-permission failure test.

## Teardown and local checks

```sh
python3 hack/workload-scaling/lab.py teardown --run example --namespace scaling-lab --state /tmp/vmrs-example
```

Teardown deletes the ScaledObject first and waits for its HPA to disappear before removing the ReplicaSet and bootstrap credentials. It checks recorded Kubernetes UIDs and run labels before deleting resources. It uninstalls only this run's Helm release, preserves Helm-retained CRDs, and refuses deletion of a KEDA namespace containing remaining workloads. The broker VM/container remains until you explicitly run `broker.sh stop example` on that VM. Never manually scale the target while its ScaledObject/HPA still exists.

```sh
python3 -m unittest discover -s hack/workload-scaling -p '*_test.py'
bash -n hack/workload-scaling/broker.sh
```

The controller/admission suites also require envtest assets. See spec 012's research artifact for actual results and remaining acceptance gates.

The runner discovers the ready workload nodes’ OS label. On Supervisor CRX workers, it derives a chart archive from the checksum-verified upstream chart, changing only the three hardcoded KEDA deployment OS selectors. It retains the upstream archive and records the derived SHA-256 in run state; unsupported or mixed worker OS labels fail preflight. This avoids duplicate YAML selector keys and works with Helm 3 and 4.

For guests without repository access, download the wheel on the runner with `python3 -m pip download --no-deps --require-hashes --only-binary=:all: -r requirements.txt -d wheels`, then pass `--worker-wheel wheels/pika-1.3.2-py3-none-any.whl`. The runner verifies the lock-file digest, embeds it in cloud-init, verifies its digest again in the guest, and loads the pure-Python wheel through `PYTHONPATH` in a `--without-pip` virtual environment. The offline path requires neither pip nor ensurepip. If the image already contains Python 3, a working `venv --without-pip` module, and `vmware-rpctool`, pass `--prepared-worker-image` to skip apt. Verify actual environment creation and a wheel import in a smoke VM first; `venv --help` alone does not prove ensurepip is present. An optional fresh `--ssh-public-key` enables guest diagnostics without baked-in credentials.
