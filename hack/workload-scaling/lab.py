#!/usr/bin/env python3
# © Broadcom. All Rights Reserved.
# The term "Broadcom" refers to Broadcom Inc. and/or its subsidiaries.
# SPDX-License-Identifier: Apache-2.0
"""Reproducible, dedicated-Supervisor KEDA/RabbitMQ validation harness.

KUBECONFIG must identify the Supervisor, not a guest Kubernetes cluster.
Broker management credentials are read exclusively from the environment.
"""

import argparse
import base64
import copy
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = Path(__file__).resolve().parent
LABEL = "vmoperator.vmware.com/scaling-lab"
GROUP = "vmoperator.vmware.com"
LOCK = json.loads((HERE / "dependencies.json").read_text())
CHART_VERSION = LOCK["keda"]["chartVersion"]
CHART_SHA = LOCK["keda"]["chartSHA256"]
DIGESTS = LOCK["keda"]["images"]
FAILURE_JOBS = 180


def command(args, body=None):
    result = subprocess.run(args, input=json.dumps(body) if body is not None else None,
                            text=True, capture_output=True, check=False)
    if result.returncode:
        # Creation errors can echo Secret payloads; never print stdin or command output.
        raise RuntimeError(f"{Path(args[0]).name} {args[1]} failed (exit {result.returncode})")
    return result.stdout


def scaler_failed(conditions):
    """Recognize scaler errors without treating a normally idle trigger as failed."""
    return any((c["type"] == "Ready" and c["status"] == "False") or
               (c["type"] == "Active" and c["status"] == "Unknown") for c in conditions)


def quota_rejected(conditions, name):
    """Require rejection by this injected quota, rather than an unrelated create error."""
    return any(c["type"] == "ReplicaFailure" and c["status"] == "True" and
               "exceeded quota: " + name + "," in c.get("message", "") for c in conditions)


class Lab:
    def __init__(self, args):
        self.args = args
        self.directory = Path(args.state).resolve()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)
        self.path = self.directory / "state.json"
        self.state = json.loads(self.path.read_text()) if self.path.exists() else {}
        if self.state and (self.state["run"] != args.run or self.state["namespace"] != args.namespace):
            raise ValueError("state belongs to a different run or namespace")
        self.name = "vmrs-" + args.run
        self.keda_ns = args.keda_namespace or self.state.get("keda_namespace") or self.name + "-keda"
        self.namespace = args.namespace
        self.kubectl_prefix = shlex.split(args.kubectl)
        if not self.kubectl_prefix:
            raise ValueError("kubectl command must not be empty")

    def kubectl(self, *args, body=None):
        return command([*self.kubectl_prefix, "--request-timeout=30s", *args], body)

    def get(self, resource, name=None, namespace=None):
        args = ["get", resource]
        if name:
            args.append(name)
        if namespace:
            args.extend(["-n", namespace])
        return json.loads(self.kubectl(*args, "-o", "json"))

    def can_i(self, verb, namespace, identity, groups=()):
        result = subprocess.run([*self.kubectl_prefix, "--request-timeout=30s", "auth", "can-i", verb,
            "virtualmachinereplicasets." + GROUP, "--subresource=scale", "-n", namespace,
            "--as=" + identity, *["--as-group=" + group for group in groups]],
            text=True, capture_output=True, check=False)
        if result.returncode not in (0, 1) or result.stdout.strip() not in ("yes", "no"):
            raise RuntimeError("authorization check failed")
        return result.stdout.strip()

    def save(self):
        self.path.write_text(json.dumps(self.state, indent=2) + "\n")
        self.path.chmod(0o600)

    def check_helm_context(self, cluster_uid):
        """Keep Helm's explicit kubeconfig on the same cluster as kubectl."""
        kubeconfig = os.environ.get("KUBECONFIG")
        if not kubeconfig:
            raise ValueError("KUBECONFIG is required for Helm")
        namespace = json.loads(command(["kubectl", "--kubeconfig", kubeconfig,
            "--request-timeout=30s", "get", "namespace", "kube-system", "-o", "json"]))
        if namespace["metadata"]["uid"] != cluster_uid:
            raise ValueError("Helm kubeconfig and kubectl command identify different clusters")

    def broker(self, method, path, body=None, allow_missing=False):
        url = os.environ["BROKER_HTTP_URL"].rstrip("/") + "/api/" + path
        auth = base64.b64encode((os.environ["BROKER_ADMIN_USER"] + ":" +
                                 os.environ["BROKER_ADMIN_PASSWORD"]).encode()).decode()
        request = urllib.request.Request(url, method=method,
            headers={"Authorization": "Basic " + auth, "Content-Type": "application/json"},
            data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                data = response.read()
                return json.loads(data) if data else None
        except urllib.error.HTTPError as error:
            if allow_missing and error.code == 404:
                return None
            raise RuntimeError("broker management request failed") from None
        except urllib.error.URLError:
            raise RuntimeError("broker management request failed") from None

    def object(self, api, kind, name, **fields):
        return {"apiVersion": api, "kind": kind,
                "metadata": {"name": name, "namespace": self.namespace,
                             "labels": {LABEL: self.args.run}}, **fields}

    def create(self, obj):
        # create rather than apply prevents taking over any existing resource.
        created = json.loads(self.kubectl("create", "-f", "-", "-o", "json", body=obj))
        self.state.setdefault("objects", []).append({"apiVersion": obj["apiVersion"],
            "kind": obj["kind"], "name": obj["metadata"]["name"],
            "namespace": obj["metadata"]["namespace"], "uid": created["metadata"]["uid"]})
        self.save()
        return created

    def preflight(self):
        if self.state:
            raise ValueError("state already exists")
        for key in ("KUBECONFIG", "BROKER_HTTP_URL", "BROKER_ADMIN_USER", "BROKER_ADMIN_PASSWORD", "BROKER_AMQP_HOST"):
            if not os.environ.get(key):
                raise ValueError(key + " is required")
        version = json.loads(self.kubectl("get", "--raw", "/version"))
        minor = int(re.match(r"\d+", version["minor"]).group())
        if version["major"] != "1" or minor not in LOCK["keda"]["kubernetesMinors"]:
            raise ValueError("KEDA 2.21 validation is restricted to Kubernetes 1.34–1.36")
        self.get("namespace", self.namespace)
        if self.get("virtualmachines", namespace=self.namespace)["items"]:
            raise ValueError("use an empty dedicated VM namespace")
        if self.args.keda_namespace:
            namespace = self.get("namespace", self.keda_ns)
            if namespace["metadata"].get("labels", {}).get(LABEL) != self.args.run:
                raise ValueError("existing KEDA namespace must carry this run's ownership label")
            if (self.get("all", namespace=self.keda_ns)["items"] or
                    self.get("virtualmachines", namespace=self.keda_ns)["items"]):
                raise ValueError("existing KEDA namespace must not contain workloads")
        capabilities = self.get("capabilities.iaas.vmware.com")
        if not any(item.get("status", {}).get("supervisor", {}).get("supports_vm_service_k8s_workload_mgmt_api", {}).get("activated") for item in capabilities["items"]):
            raise ValueError("Supervisor workload-management capability is not activated")
        crd = self.get("crd", "virtualmachinereplicasets." + GROUP)
        for crd_version in crd["spec"]["versions"]:
            if crd_version.get("served") and crd_version.get("subresources", {}).get("scale", {}).get("labelSelectorPath") != ".status.selector":
                raise ValueError("install the selector-enabled VM Operator CRD before setup")
        # Feature activation is proved by the controller's selector status after creation.
        for resource in ("virtualmachineimages", "virtualmachineclasses", "storageclasses"):
            self.kubectl("api-resources", "--api-group=" + (GROUP if resource != "storageclasses" else "storage.k8s.io"))
        existing = self.get("apiservices")["items"]
        if any(x["spec"].get("group") == "external.metrics.k8s.io" for x in existing):
            raise ValueError("external metrics provider already installed; use a dedicated lab")
        if any(x["metadata"]["name"].endswith((".keda.sh", ".eventing.keda.sh")) for x in self.get("crds")["items"]):
            raise ValueError("KEDA CRDs already exist; this installer never takes them over")
        for verb in ("create", "delete"):
            for resource in ("deployments.apps", "roles.rbac.authorization.k8s.io", "rolebindings.rbac.authorization.k8s.io", "secrets", "virtualmachinereplicasets." + GROUP):
                if self.kubectl("auth", "can-i", verb, resource, "-n", self.namespace).strip() != "yes":
                    raise ValueError("missing namespace permission: " + verb + " " + resource)
        if self.kubectl("auth", "can-i", "create", "apiservices.apiregistration.k8s.io").strip() != "yes":
            raise ValueError("lab setup requires APIService installation permission")
        self.broker("GET", "overview")
        for kind in ("vhosts", "users"):
            if any(item["name"] == self.name for item in self.broker("GET", kind)):
                raise ValueError("broker resource already exists; choose another run ID")
        # Validate the exact image/class/storage inputs using admission when creating the target.
        cluster_uid = self.get("namespace", "kube-system")["metadata"]["uid"]
        self.check_helm_context(cluster_uid)
        worker_os = {node["metadata"].get("labels", {}).get("kubernetes.io/os")
            for node in self.get("nodes")["items"]
            if "node-role.kubernetes.io/control-plane" not in node["metadata"].get("labels", {})
            and any(c["type"] == "Ready" and c["status"] == "True"
                    for c in node.get("status", {}).get("conditions", []))}
        if len(worker_os) != 1 or not worker_os <= {"linux", "CRX"}:
            raise ValueError("expected ready workload nodes with one supported OS label")
        self.state = {"run": self.args.run, "namespace": self.namespace, "version": version,
                      "objects": [], "keda_namespace": self.keda_ns,
                      "kube_system_uid": cluster_uid, "keda_node_os": worker_os.pop()}
        self.save()

    def chart(self):
        chart = self.directory / ("keda-" + CHART_VERSION + ".tgz")
        if not chart.exists():
            # Helm's cache is explicitly inside the run directory.
            command([self.args.helm, "pull", "keda", "--repo", "https://kedacore.github.io/charts",
                     "--version", CHART_VERSION, "--destination", str(self.directory),
                     "--repository-cache", str(self.directory / "helm-cache"),
                     "--repository-config", str(self.directory / "helm-repositories.yaml")])
        if hashlib.sha256(chart.read_bytes()).hexdigest() != CHART_SHA:
            raise ValueError("KEDA chart checksum mismatch")
        if self.state.get("keda_node_os", "linux") == "CRX":
            # This pinned chart hardcodes linux and appends user selectors. A
            # duplicate YAML key is ambiguous; adapt the three exact templates
            # before Helm renders, preserving the verified upstream archive.
            chart = self.worker_chart(chart)
        return chart

    def worker_chart(self, upstream):
        """Adapt only pinned KEDA deployment OS selectors for Supervisor CRX."""
        expected = {"keda/templates/" + part + "/deployment.yaml"
                    for part in ("manager", "metrics-server", "webhooks")}
        adapted = self.directory / ("keda-" + CHART_VERSION + "-crx.tgz")
        with tarfile.open(upstream) as source, adapted.open("wb") as raw, \
                gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as compressed, \
                tarfile.open(fileobj=compressed, mode="w") as output:
            for member in source.getmembers():
                data = source.extractfile(member).read() if member.isfile() else None
                if member.name in expected:
                    old = b"        kubernetes.io/os: linux\n"
                    if data.count(old) != 1:
                        raise ValueError("unexpected pinned KEDA node selector")
                    data = data.replace(old, b"        kubernetes.io/os: CRX\n")
                    member.size = len(data)
                    expected.remove(member.name)
                output.addfile(member, io.BytesIO(data) if data is not None else None)
        if expected:
            adapted.unlink()
            raise ValueError("missing pinned KEDA deployment templates")
        self.state["adapted_chart_sha256"] = hashlib.sha256(adapted.read_bytes()).hexdigest()
        self.save()
        return adapted

    def setup(self):
        if self.state:
            raise ValueError("state already exists; use a fresh run directory or teardown")
        wheel = self.worker_wheel()
        if self.args.prepared_worker_image and wheel is None:
            raise ValueError("prepared worker image requires a verified offline worker wheel")
        self.preflight()
        template = json.loads(Path(self.args.template).read_text())
        if template.get("kind") != "VirtualMachine" or template.get("apiVersion") != GROUP + "/v1alpha6":
            raise ValueError("template must be a new disposable v1alpha6 VirtualMachine manifest in JSON")
        spec = copy.deepcopy(template["spec"])
        if spec.get("bootstrap") or spec.get("volumes"):
            raise ValueError("use a disposable VM template without bootstrap or shared volumes")
        for key in ("imageName", "className", "storageClass"):
            if not spec.get(key):
                raise ValueError("VM template must set " + key)
        chart = self.chart()
        values = {"watchNamespace": self.namespace, "rbac": {"enabledCustomScaledRefKinds": False}, "image": {
            key: {"tag": CHART_VERSION + "@" + digest} for key, digest in DIGESTS.items()}}
        values_path = self.directory / "keda-values.json"
        values_path.write_text(json.dumps(values))
        if self.args.keda_namespace:
            # Supervisor PodVM namespaces are provisioned through vCenter before
            # Helm installation; preserve that platform-owned namespace on teardown.
            installed = self.get("namespace", self.keda_ns)
            if installed["metadata"].get("labels", {}).get(LABEL) != self.args.run:
                raise ValueError("existing KEDA namespace ownership changed")
        else:
            namespace = self.object("v1", "Namespace", self.keda_ns)
            namespace["metadata"].pop("namespace")
            installed = json.loads(self.kubectl("create", "-f", "-", "-o", "json", body=namespace))
        self.state["keda_namespace_uid"] = installed["metadata"]["uid"]
        self.state["keda_namespace_created"] = not bool(self.args.keda_namespace)
        self.state["helm_install_attempted"] = True
        self.save()
        command([self.args.helm, "install", self.name, str(chart), "-n", self.keda_ns,
                 "--kubeconfig", os.environ["KUBECONFIG"],
                 "-f", str(values_path), "--wait", "--timeout", "5m"])
        # Chart ClusterRoles are installation infrastructure; target permissions stay namespaced.
        hpa_subject = json.loads(Path(self.args.hpa_subject).read_text())
        if hpa_subject.get("kind") not in ("User", "ServiceAccount") or not hpa_subject.get("name"):
            raise ValueError("hpa-subject must describe the actual controller's User or ServiceAccount")
        self.create(self.object("rbac.authorization.k8s.io/v1", "Role", self.name,
            rules=[{"apiGroups": [GROUP], "resources": ["virtualmachinereplicasets"], "verbs": ["get", "list", "watch"]},
                   {"apiGroups": [GROUP], "resources": ["virtualmachinereplicasets/scale"], "verbs": ["get", "update", "patch"]},
                   {"apiGroups": ["external.metrics.k8s.io"], "resources": ["*"], "verbs": ["get"]}]))
        self.create(self.object("rbac.authorization.k8s.io/v1", "RoleBinding", self.name,
            roleRef={"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": self.name},
            subjects=[{"kind": "ServiceAccount", "name": "keda-operator", "namespace": self.keda_ns}, hpa_subject]))
        user, password = self.name, secrets.token_urlsafe(32)
        # Each run gets an isolated broker vhost, user, queues, and credentials.
        self.state["broker_created"] = True
        self.save()
        self.broker("PUT", "vhosts/" + self.name, {})
        self.broker("PUT", "users/" + user, {"password": password, "tags": "management"})
        self.broker("PUT", "permissions/" + self.name + "/" + user,
                    {"configure": ".*", "write": ".*", "read": ".*"})
        for queue in ("work", "results"):
            self.broker("PUT", "queues/" + self.name + "/" + queue,
                        {"durable": True, "auto_delete": False, "arguments": {}})
        amqp = "amqp://" + user + ":" + password + "@" + os.environ["BROKER_AMQP_HOST"] + "/" + self.name
        http = os.environ["BROKER_HTTP_URL"].rstrip("/")
        self.create(self.object("v1", "Secret", self.name + "-auth", type="Opaque",
                               stringData={"host": http, "username": user, "password": password}))
        # Cloud-init accepts JSON after the #cloud-config header (JSON is a YAML subset).
        env = "BROKER_AMQP_URL=" + amqp + "\nWORK_QUEUE=work\nRESULT_QUEUE=results\n"
        service = """[Unit]
Description=VMReplicaSet RabbitMQ worker
After=network-online.target
Wants=network-online.target
[Service]
EnvironmentFile=/etc/vmrs-worker.env
ExecStart=/opt/vmrs-worker/venv/bin/python /opt/vmrs-worker/worker.py
ExecStopPost=/usr/bin/vmware-rpctool 'info-set guestinfo.vmrs-ready false'
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
"""
        cloud = {"package_update": True, "packages": ["python3-venv", "open-vm-tools"],
            "write_files": [{"path": path, "permissions": mode, "content": content} for path, mode, content in (
                ("/opt/vmrs-worker/worker.py", "0755", (HERE / "worker.py").read_text()),
                ("/opt/vmrs-worker/requirements.txt", "0644", (HERE / "requirements.txt").read_text()),
                ("/etc/vmrs-worker.env", "0600", env),
                ("/etc/systemd/system/vmrs-worker.service", "0644", service))],
            "runcmd": [["python3", "-m", "venv", "/opt/vmrs-worker/venv"],
                       ["/opt/vmrs-worker/venv/bin/pip", "install", "--require-hashes", "--only-binary=:all:",
                        "-r", "/opt/vmrs-worker/requirements.txt"],
                       ["systemctl", "enable", "--now", "vmrs-worker.service"]]}
        if self.args.ssh_public_key:
            cloud["ssh_authorized_keys"] = [Path(self.args.ssh_public_key).read_text().strip()]
        if wheel is not None:
            cloud["write_files"].append(wheel)
            # Pika's pure-Python wheel is zip-importable. No pip/ensurepip or
            # package index is needed, even on minimal Ubuntu cloud images.
            verify = ["python3", "-c", "import hashlib,pathlib; "
                "assert hashlib.sha256(pathlib.Path(" + repr(wheel["path"]) +
                ").read_bytes()).hexdigest() == " + repr(LOCK["pika"]["wheelSHA256"])]
            for entry in cloud["write_files"]:
                if entry["path"] == "/etc/vmrs-worker.env":
                    entry["content"] += "PYTHONPATH=" + wheel["path"] + "\n"
            commands = [verify, ["python3", "-m", "venv", "--without-pip", "/opt/vmrs-worker/venv"],
                        ["/opt/vmrs-worker/venv/bin/python", "-c",
                         "import sys; sys.path.insert(0," + repr(wheel["path"]) + "); import pika"],
                        ["systemctl", "enable", "--now", "vmrs-worker.service"]]
            cloud["runcmd"] = [["sh", "-ec", " && ".join(shlex.join(c) for c in commands)]]
        if self.args.prepared_worker_image:
            # The caller has checked python3-venv and vmware-rpctool in this
            # image; package repositories need not be reachable from guests.
            cloud.pop("package_update")
            cloud.pop("packages")
        self.create(self.object("v1", "Secret", self.name + "-bootstrap", type="Opaque",
                               stringData={"cloud-config": "#cloud-config\n" + json.dumps(cloud)}))
        spec["bootstrap"] = {"cloudInit": {"rawCloudConfig": {"name": self.name + "-bootstrap", "key": "cloud-config"}}}
        spec["readinessProbe"] = {"guestInfo": [{"key": "vmrs-ready", "value": "^true$"}]}
        spec["powerState"] = "PoweredOn"
        self.create(self.object(GROUP + "/v1alpha6", "VirtualMachineReplicaSet", self.name,
            spec={"replicas": 0, "selector": {"matchLabels": {LABEL: self.args.run}},
                  "template": {"metadata": {"labels": {LABEL: self.args.run}}, "spec": spec}}))
        self.wait("selector status", lambda: self.target().get("status", {}).get("selector"), 120)
        self.create(self.object("keda.sh/v1alpha1", "TriggerAuthentication", self.name,
            spec={"secretTargetRef": [{"parameter": field, "name": self.name + "-auth", "key": field}
                                     for field in ("host", "username", "password")]}))
        self.create(self.object("keda.sh/v1alpha1", "ScaledObject", self.name, spec=self.scaled_spec()))
        self.wait("KEDA HPA", lambda: self.hpa_ready(), 180)
        print("Setup complete. Run the cycles command; this is not cluster acceptance yet.")

    def worker_wheel(self):
        """Verify the pinned pure-Python dependency before embedding it in cloud-init."""
        if not self.args.worker_wheel:
            return None
        data = Path(self.args.worker_wheel).read_bytes()
        if hashlib.sha256(data).hexdigest() != LOCK["pika"]["wheelSHA256"]:
            raise ValueError("worker wheel checksum mismatch")
        filename = "pika-" + LOCK["pika"]["version"] + "-py3-none-any.whl"
        return {"path": "/opt/vmrs-worker/wheels/" + filename, "permissions": "0644",
                "encoding": "b64", "content": base64.b64encode(data).decode()}

    def scaled_spec(self):
        return {"scaleTargetRef": {"apiVersion": GROUP + "/v1alpha6", "kind": "VirtualMachineReplicaSet", "name": self.name},
            "pollingInterval": 15, "cooldownPeriod": 300, "minReplicaCount": 0, "maxReplicaCount": 3,
            "advanced": {"horizontalPodAutoscalerConfig": {"behavior": {
                "scaleUp": {"policies": [{"type": "Pods", "value": 1, "periodSeconds": 60}]},
                "scaleDown": {"stabilizationWindowSeconds": 300,
                              "policies": [{"type": "Pods", "value": 1, "periodSeconds": 60}]}}}},
            "triggers": [{"type": "rabbitmq", "metricType": "AverageValue", "metadata": {
                "protocol": "http", "mode": "QueueLength", "value": "10", "activationValue": "0",
                "queueName": "work", "vhostName": self.name, "excludeUnacknowledged": "false"},
                "authenticationRef": {"name": self.name}}]}

    def target(self):
        return self.get("vmrs", self.name, self.namespace)

    def hpa_ready(self):
        obj = self.get("scaledobjects", self.name, self.namespace)
        return any(c["type"] == "Ready" and c["status"] == "True" for c in obj.get("status", {}).get("conditions", []))

    def sample(self):
        target = self.target()
        desired = target["spec"]["replicas"]
        if not 0 <= desired <= 3:
            raise AssertionError("replica count exceeded configured bounds")
        sample = {"time": time.time(), "desired": desired, "status": target.get("status", {})}
        queue = self.broker("GET", "queues/" + self.name + "/work")
        sample["queue"] = {key: queue.get(key, 0) for key in
                           ("messages", "messages_ready", "messages_unacknowledged")}
        with (self.directory / "samples.jsonl").open("a") as stream:
            stream.write(json.dumps(sample) + "\n")
        return sample

    def wait(self, description, predicate, timeout=1800):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(5)
        raise TimeoutError("Timed out waiting for " + description)

    def publish(self, prefix, count=90):
        for i in range(count):
            result = self.broker("POST", "exchanges/" + self.name + "/amq.default/publish", {
                "properties": {"delivery_mode": 2}, "routing_key": "work",
                "payload": json.dumps({"id": f"{prefix}-{i}", "seconds": 10}), "payload_encoding": "string"})
            if not result["routed"]:
                raise AssertionError("job was not routed")

    def drain_results(self, completed):
        results = self.broker("POST", "queues/" + self.name + "/results/get",
                              {"count": 1000, "ackmode": "ack_requeue_false", "encoding": "auto"})
        # Results are consumed destructively, so persist every raw payload
        # before validating any of them; a bad message must not lose the rest.
        with (self.directory / "results.raw.jsonl").open("a") as stream:
            for message in results:
                stream.write(json.dumps(message["payload"]) + "\n")
        for message in results:
            job = json.loads(message["payload"])
            if job["sha256"] != hashlib.sha256(job["id"].encode()).hexdigest():
                raise AssertionError("worker returned an invalid result")
            completed.add(job["id"])
            if job.get("redelivered") is True:
                redelivered = self.state.setdefault("redelivered_job_ids", [])
                if job["id"] not in redelivered:
                    redelivered.append(job["id"])
                    self.save()
            with (self.directory / "results.jsonl").open("a") as stream:
                stream.write(json.dumps(job) + "\n")

    def cycles(self):
        self.check_cluster()
        pods = self.get("pods", namespace=self.namespace)["items"]
        if any(pod["metadata"].get("labels", {}).get(LABEL) == self.args.run for pod in pods):
            raise AssertionError("PoC must scale without matching Pods")
        hpas = [hpa for hpa in self.get("hpa", namespace=self.namespace)["items"]
                if hpa["spec"]["scaleTargetRef"].get("name") == self.name]
        if len(hpas) != 1 or any(metric.get("type") != "External" or
                metric.get("external", {}).get("target", {}).get("type") != "AverageValue"
                for metric in hpas[0]["spec"].get("metrics", [])):
            raise AssertionError("expected exactly one HPA using external AverageValue metrics")
        completed = set()
        # Leftover jobs or results from an earlier aborted invocation must not
        # count toward this one, so reset acceptance state, purge both queues,
        # and give this invocation's job IDs a unique nonce.
        self.state["successful_cycles"] = 0
        self.save()
        self.wait("empty scale before purge", lambda: self.sample()["desired"] == 0)
        for queue in ("work", "results"):
            self.broker("DELETE", "queues/" + self.name + "/" + queue + "/contents", allow_missing=True)
        nonce = secrets.token_hex(4)
        for cycle in range(3):
            self.wait("empty scale", lambda: self.sample()["desired"] == 0)
            prefix = f"{self.args.run}-{nonce}-{cycle}"
            started = time.time()
            self.publish(prefix)
            self.wait("three ready workers", lambda: self.sample()["status"].get("readyReplicas", 0) == 3)
            with (self.directory / "latencies.jsonl").open("a") as stream:
                stream.write(json.dumps({"cycle": cycle, "activationToThreeReadySeconds": time.time() - started}) + "\n")
            if cycle == 0:
                self.wait("in-flight work", lambda: self.broker("GET", "queues/" + self.name + "/work").get("messages_unacknowledged", 0) == 3)
                vms = self.get("virtualmachines", namespace=self.namespace)["items"]
                owned = [vm for vm in vms if any(ref.get("uid") == self.target()["metadata"]["uid"] for ref in vm["metadata"].get("ownerReferences", []))]
                victim = owned[0]
                self.state["replacement_victim_uid"] = victim["metadata"]["uid"]
                self.save()
                self.kubectl("delete", "vm", victim["metadata"]["name"], "-n", self.namespace, "--wait=false")
                self.wait("replacement worker", lambda: self.replaced(victim["metadata"]["uid"]))
                self.state["worker_replacement_passed"] = True
                self.save()
            expected = {f"{prefix}-{i}" for i in range(90)}
            def done():
                self.sample()
                self.drain_results(completed)
                return expected <= completed
            self.wait("all unique jobs completed", done)
            if cycle == 0 and not expected.intersection(self.state.get("redelivered_job_ids", [])):
                raise AssertionError("worker deletion did not demonstrate an actual redelivered job")
            self.wait("queue drain", lambda: self.broker("GET", "queues/" + self.name + "/work").get("messages", 0) == 0)
            self.wait("return to zero", lambda: self.sample()["desired"] == 0 and self.target().get("status", {}).get("replicas") == 0)
            self.state["successful_cycles"] = cycle + 1
            self.save()
            self.diagnostics()
        print("Three cycles passed. Run outage, quota, and namespace-isolation checks before acceptance.")

    def failures(self):
        """Test quota recovery and failure of both worker and scaler broker access."""
        self.check_cluster()
        self.wait("zero before failure tests", lambda: self.sample()["desired"] == 0 and
                  self.target().get("status", {}).get("replicas") == 0)
        self.state["failure_phase"] = "quota"
        self.save()
        quota = self.create(self.object("v1", "ResourceQuota", self.name + "-quota",
            spec={"hard": {"count/virtualmachines." + GROUP: "0"}}))
        self.wait("quota enforcement", lambda: self.get("resourcequota", quota["metadata"]["name"],
            self.namespace).get("status", {}).get("hard", {}).get("count/virtualmachines." + GROUP) == "0", 120)
        # Keep demand present across a full-cooldown metrics outage; otherwise
        # normal completion can legitimately trigger zero during restoration.
        self.publish(self.args.run + "-failures", FAILURE_JOBS)
        try:
            self.wait("reported quota failure", lambda: quota_rejected(
                self.target().get("status", {}).get("conditions", []), quota["metadata"]["name"]), 180)
            if self.get("virtualmachines", namespace=self.namespace)["items"]:
                raise AssertionError("quota test requires an otherwise empty dedicated VM namespace")
            self.state["quota_exhaustion_observed"] = True
            self.save()
        finally:
            self.kubectl("delete", "resourcequota", quota["metadata"]["name"], "-n", self.namespace, "--wait=true")
        self.wait("quota recovery", lambda: self.sample()["status"].get("readyReplicas") == 3)
        if any(c["type"] == "ReplicaFailure" and c["status"] == "True"
               for c in self.target().get("status", {}).get("conditions", [])):
            raise AssertionError("quota recovery left a stale ReplicaFailure condition")
        self.state["quota_recovery_passed"] = True
        self.save()
        for mode in ("metrics", "broker"):
            self.state["failure_phase"] = mode + "-outage"
            self.save()
            auth = self.get("secret", self.name + "-auth", self.namespace)
            original = auth["data"]["password"]
            try:
                if mode == "metrics":
                    # Secret updates need not invalidate an already cached
                    # scaler client. Revoke the dedicated user's password at
                    # the server so every metrics HTTP request actually fails;
                    # established AMQP worker connections remain usable.
                    self.broker("PUT", "users/" + self.name,
                                {"password": secrets.token_urlsafe(32), "tags": "management"})
                else:
                    self.broker("DELETE", "permissions/" + self.name + "/" + self.name)
                    self.disconnect_workers()
                self.wait("visible " + mode + " failure", lambda: scaler_failed(
                    self.get("scaledobjects", self.name, self.namespace).get("status", {}).get("conditions", [])), 180)
                if mode == "broker":
                    self.wait("workers report broker access failure", lambda:
                              self.sample()["status"].get("readyReplicas", 0) == 0, 180)
                self.state["failure_phase"] = mode + "-observing"
                self.save()
                # Observe past the entire cooldown, so stabilization alone cannot
                # disguise an outage incorrectly being treated as an empty queue.
                deadline = time.monotonic() + self.scaled_spec()["cooldownPeriod"] + 60
                observed_start = time.monotonic()
                baseline_desired = self.sample()["desired"]
                if baseline_desired == 0:
                    raise AssertionError("outage observation needs nonzero demand at its start")
                minimum_desired = baseline_desired
                while time.monotonic() < deadline:
                    minimum_desired = min(minimum_desired, self.sample()["desired"])
                    if minimum_desired < baseline_desired:
                        raise AssertionError("metric outage scaled the target below its starting demand")
                    time.sleep(5)
                self.state.setdefault("outage_observations", {})[mode] = {
                    "seconds": time.monotonic() - observed_start,
                    "baseline_desired": baseline_desired,
                    "minimum_desired": minimum_desired, "scaler_error_observed": True}
                self.save()
                self.diagnostics()
            finally:
                if mode == "metrics":
                    self.broker("PUT", "users/" + self.name,
                                {"password": base64.b64decode(original).decode(), "tags": "management"})
                else:
                    self.broker("PUT", "permissions/" + self.name + "/" + self.name,
                                {"configure": ".*", "write": ".*", "read": ".*"})
            self.wait(mode + " recovery", self.hpa_ready, 180)
            if mode == "broker":
                self.wait("worker broker readiness recovery", self.workers_ready, 180)
        self.finish_failures()

    def workers_ready(self):
        """Check recovery against current HPA demand, which may be below the maximum."""
        current = self.sample()
        desired = current["desired"]
        return desired > 0 and current["status"].get("readyReplicas", 0) == desired and \
            current["status"].get("replicas", 0) == desired

    def finish_failures(self):
        """Verify every failure-test result and final scale-in after access recovers."""
        self.state["failure_phase"] = "drain"
        self.save()
        completed = set()
        expected = {f"{self.args.run}-failures-{i}" for i in range(FAILURE_JOBS)}
        def recovered():
            self.sample()
            self.drain_results(completed)
            return expected <= completed
        self.wait("all failure-test work completed", recovered)
        self.wait("zero after failure tests", lambda: self.sample()["desired"] == 0 and
                  self.target().get("status", {}).get("replicas") == 0)
        self.state["failure_checks_passed"] = True
        self.state["failure_phase"] = "complete"
        self.save()

    def disconnect_workers(self):
        # Permissions can be cached by an existing AMQP connection. Close only
        # this run's connections so workers must reconnect with revoked access.
        for connection in self.broker("GET", "connections"):
            if connection.get("user") == self.name and connection.get("vhost") == self.name:
                self.broker("DELETE", "connections/" + urllib.parse.quote(connection["name"], safe=""))

    def isolation(self):
        """Verify an existing tenant's positive and negative scale authorization."""
        self.check_cluster()
        if not self.args.tenant_subject or not self.args.foreign_namespace:
            raise ValueError("isolation requires --tenant-subject and --foreign-namespace")
        subject = json.loads(Path(self.args.tenant_subject).read_text())
        identity = ("system:serviceaccount:" + subject["namespace"] + ":" + subject["name"]
                    if subject["kind"] == "ServiceAccount" else subject["name"])
        groups = subject.get("groups", [])
        self.get("namespace", self.args.foreign_namespace)
        if self.args.foreign_namespace == self.namespace:
            raise ValueError("foreign namespace must differ from the target namespace")
        for namespace, expected in ((self.namespace, "yes"), (self.args.foreign_namespace, "no")):
            for verb in ("get", "update", "patch"):
                allowed = self.can_i(verb, namespace, identity, groups)
                if allowed != expected:
                    raise AssertionError("unexpected tenant /scale authorization in " + namespace)
        # can-i evaluates RBAC rules only, so also make real impersonated /scale
        # reads: the target must serve the scale object, the foreign namespace
        # must be forbidden.
        if not self.scale_read(self.namespace, identity, groups, self.name):
            raise AssertionError("tenant could not read the target /scale subresource")
        if self.scale_read(self.args.foreign_namespace, identity, groups, self.name):
            raise AssertionError("tenant could read /scale in a foreign namespace")
        self.state["namespace_isolation_passed"] = True
        self.save()

    def scale_read(self, namespace, identity, groups, name):
        """Return True when an impersonated GET of /scale succeeds, False when forbidden."""
        path = ("/apis/" + GROUP + "/v1alpha6/namespaces/" + namespace +
                "/virtualmachinereplicasets/" + name + "/scale")
        result = subprocess.run([*self.kubectl_prefix, "--request-timeout=30s", "get", "--raw", path,
            "--as=" + identity, *["--as-group=" + group for group in groups]],
            text=True, capture_output=True, check=False)
        if result.returncode == 0:
            return True
        if "forbidden" in result.stderr.lower():
            return False
        raise RuntimeError("impersonated /scale read failed for a reason other than authorization")

    def restart(self):
        """Verify cold-controller convergence in an explicitly identified dedicated lab."""
        self.check_cluster()
        if not self.args.operator_namespace or not self.args.operator_deployment:
            raise ValueError("restart requires --operator-namespace and --operator-deployment")
        if not self.args.allow_operator_restart:
            raise ValueError("restart disrupts the named VM Operator deployment for every tenant; "
                             "pass --allow-operator-restart only on a dedicated lab Supervisor")
        pause = "autoscaling.keda.sh/paused-replicas"
        self.kubectl("annotate", "scaledobject", self.name, "-n", self.namespace,
                     pause + "=3", "--overwrite")
        try:
            self.wait("paused static replicas", lambda: self.sample()["status"].get("readyReplicas") == 3)
            target_uid = self.target()["metadata"]["uid"]
            def owned_uids():
                return {vm["metadata"]["uid"] for vm in self.get("virtualmachines", namespace=self.namespace)["items"]
                        if any(ref.get("uid") == target_uid for ref in vm["metadata"].get("ownerReferences", []))}
            before = owned_uids()
            if len(before) != 3:
                raise AssertionError("expected three static replicas before restart")
            self.state["restart_vm_uids_before"] = sorted(before)
            self.save()
            self.kubectl("rollout", "restart", "deployment/" + self.args.operator_deployment,
                         "-n", self.args.operator_namespace)
            self.kubectl("rollout", "status", "deployment/" + self.args.operator_deployment,
                         "-n", self.args.operator_namespace, "--timeout=5m")
            # A metadata update ensures the restarted controller observes the target.
            self.kubectl("annotate", "vmrs", self.name, "-n", self.namespace,
                         "vmoperator.vmware.com/scaling-lab-resync=" + str(time.time()), "--overwrite")
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                self.sample()
                if owned_uids() != before:
                    raise AssertionError("controller restart changed steady-state VM identities")
                time.sleep(5)
            # A controller that does nothing also keeps identities stable, so
            # prove the restarted controller still reconciles by deleting one
            # replica and requiring a replacement while the target is paused.
            victim_uid = sorted(before)[0]
            victim = next(vm for vm in self.get("virtualmachines", namespace=self.namespace)["items"]
                          if vm["metadata"]["uid"] == victim_uid)
            self.kubectl("delete", "vm", victim["metadata"]["name"], "-n", self.namespace, "--wait=false")
            self.wait("restarted controller replaces a deleted VM", lambda: (
                self.sample() and victim_uid not in owned_uids() and len(owned_uids()) == 3), 900)
            self.state["controller_restart_passed"] = True
            self.state["restart_vm_uids_after"] = sorted(owned_uids())
            self.save()
        finally:
            self.kubectl("annotate", "scaledobject", self.name, "-n", self.namespace, pause + "-")

    def replaced(self, uid):
        self.sample()
        vms = self.get("virtualmachines", namespace=self.namespace)["items"]
        return all(vm["metadata"]["uid"] != uid for vm in vms) and self.target().get("status", {}).get("readyReplicas") == 3

    def check_cluster(self):
        if not self.state or self.get("namespace", "kube-system")["metadata"]["uid"] != self.state["kube_system_uid"]:
            raise ValueError("kubeconfig does not match the cluster recorded during setup")
        self.check_helm_context(self.state["kube_system_uid"])

    def diagnostics(self):
        self.check_cluster()
        snapshot = self.directory / "diagnostics" / str(time.time_ns())
        snapshot.mkdir(parents=True, mode=0o700)
        for resource in ("vmrs", "virtualmachines", "scaledobjects", "hpa", "events", "resourcequotas"):
            # Secrets and cloud-init contents are deliberately excluded.
            data = self.get(resource, namespace=self.namespace)
            if resource in ("virtualmachines", "vmrs"):
                for vm in data.get("items", []):
                    vm.pop("spec", None)
            if resource == "virtualmachines":
                for vm in data.get("items", []):
                    vm.pop("spec", None)
            serialized = json.dumps(data, indent=2)
            (self.directory / (resource + ".json")).write_text(serialized)
            (snapshot / (resource + ".json")).write_text(serialized)
        logs = self.kubectl("logs", "-n", self.keda_ns, "deployment/keda-operator", "--tail=500")
        (self.directory / "keda.log").write_text(logs)
        (snapshot / "keda.log").write_text(logs)

    def teardown(self):
        if self.state.get("teardown_complete"):
            return
        self.check_cluster()
        for record in reversed(self.state.get("objects", [])):
            resource = record["kind"]
            obj = json.loads(self.kubectl("get", resource, record["name"], "-n", record["namespace"],
                                         "--ignore-not-found", "-o", "json") or "null")
            if obj is None:
                continue
            if obj["metadata"]["uid"] != record["uid"] or obj["metadata"].get("labels", {}).get(LABEL) != self.args.run:
                raise ValueError("refusing to delete a changed or foreign resource")
            self.kubectl("delete", resource, record["name"], "-n", record["namespace"], "--wait=true", "--timeout=15m")
            if resource == "VirtualMachineReplicaSet":
                # Deleting the ReplicaSet returns before garbage collection
                # removes its VMs; secrets and RBAC they depend on come next.
                self.wait("owned VM deletion", lambda: not any(
                    ref.get("uid") == record["uid"]
                    for vm in self.get("virtualmachines", namespace=record["namespace"])["items"]
                    for ref in vm["metadata"].get("ownerReferences", [])), 900)
            if resource == "ScaledObject":
                self.wait("generated HPA deletion", lambda: not any(
                    h["spec"]["scaleTargetRef"].get("name") == self.name
                    for h in self.get("hpa", namespace=self.namespace)["items"]), 180)
        if self.state.get("helm_install_attempted"):
            # A rerun after a partial teardown may find the namespace already gone.
            ns = json.loads(self.kubectl("get", "namespace", self.keda_ns,
                                         "--ignore-not-found", "-o", "json") or "null")
        else:
            ns = None
        if ns is not None:
            if (ns["metadata"]["uid"] != self.state["keda_namespace_uid"] or
                    ns["metadata"].get("labels", {}).get(LABEL) != self.args.run):
                raise ValueError("KEDA namespace was replaced; refusing deletion")
            command([self.args.helm, "uninstall", self.name, "-n", self.keda_ns,
                     "--kubeconfig", os.environ["KUBECONFIG"], "--ignore-not-found", "--wait"])
            if self.state.get("keda_namespace_created", True):
                remaining = self.get("all,configmaps,secrets,serviceaccounts,roles,rolebindings,persistentvolumeclaims",
                                     namespace=self.keda_ns).get("items", [])
                remaining = [obj for obj in remaining if
                             (obj["kind"], obj["metadata"]["name"]) not in
                             (("ServiceAccount", "default"), ("ConfigMap", "kube-root-ca.crt"))]
                if remaining:
                    raise ValueError("KEDA namespace contains remaining resources; refusing namespace deletion")
                self.kubectl("delete", "namespace", self.keda_ns, "--wait=true", "--timeout=5m")
        if self.state.get("broker_created"):
            # Tolerate objects removed by an earlier, partially completed teardown.
            self.broker("DELETE", "vhosts/" + self.name, allow_missing=True)
            self.broker("DELETE", "users/" + self.name, allow_missing=True)
        self.state["teardown_complete"] = True
        self.save()


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("operation", choices=("setup", "cycles", "failures", "isolation", "restart", "diagnostics", "teardown"))
    p.add_argument("--run", required=True)
    p.add_argument("--namespace", required=True, help="Existing dedicated Supervisor namespace")
    p.add_argument("--state", required=True, help="Private evidence directory; never commit it")
    p.add_argument("--template", help="v1alpha6 disposable Linux VirtualMachine JSON")
    p.add_argument("--hpa-subject", help="JSON RBAC subject for the actual HPA controller identity")
    p.add_argument("--ssh-public-key", help="Optional fresh public key for disposable guest bootstrap diagnostics")
    p.add_argument("--worker-wheel", help="Offline Pika wheel matching dependencies.json; embedded in cloud-init")
    p.add_argument("--prepared-worker-image", action="store_true",
                   help="Skip package installation; image must contain python3-venv and vmware-rpctool")
    p.add_argument("--keda-namespace", help="Existing empty Supervisor workload namespace labeled for this run; preserved on teardown")
    p.add_argument("--tenant-subject", help="JSON RBAC subject for an existing namespace-scoped tenant")
    p.add_argument("--foreign-namespace", help="Second existing namespace for negative authorization checks")
    p.add_argument("--operator-namespace", help="Dedicated lab VM Operator namespace for restart validation")
    p.add_argument("--allow-operator-restart", action="store_true",
                   help="Acknowledge that restart validation restarts the named VM Operator deployment")
    p.add_argument("--operator-deployment", help="Explicit VM Operator deployment for restart validation")
    p.add_argument("--kubectl", default="kubectl", help="Kubectl command or quoted command prefix, executed without a shell")
    p.add_argument("--helm", default="helm")
    return p


def main():
    args = parser().parse_args()
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,19}[a-z0-9])?", args.run):
        raise ValueError("run must be a DNS label of at most 21 characters")
    if args.operation == "setup" and not (args.template and args.hpa_subject):
        raise ValueError("setup requires --template and --hpa-subject")
    lab = Lab(args)
    try:
        getattr(lab, args.operation)()
    except Exception:
        if lab.state and args.operation != "teardown":
            try:
                lab.diagnostics()
            except Exception:
                pass
        raise


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, TimeoutError, AssertionError, OSError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
