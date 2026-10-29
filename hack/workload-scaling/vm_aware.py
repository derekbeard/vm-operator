#!/usr/bin/env python3
# © Broadcom. All Rights Reserved.
# The term "Broadcom" refers to Broadcom Inc. and/or its subsidiaries.
# SPDX-License-Identifier: Apache-2.0
"""Live validation of VM-aware autoscaling (spec 013) on a dedicated Supervisor.

A VirtualMachineReplicaSet's VMs burn CPU from cloud-init until a fixed
wall-clock deadline baked into the shared bootstrap Secret, so replicas added
during the burn burn too and every replica goes idle at the same moment. A
HorizontalPodAutoscaler with an Object/AverageValue target on the adapter's
vm_cpu_usage_mhz metric must then scale out during the burn, scale back in
after it, and hold its replica count while the metrics adapter is down.

KUBECONFIG (or --kubectl) must identify the Supervisor. The VM Operator must run
with K8sWorkloadMgmtAPI and the adapter must be serving custom.metrics.k8s.io.
"""

import argparse
import copy
import json
from pathlib import Path
import shlex
import subprocess
import sys
import time

LABEL = "vmoperator.vmware.com/vm-aware-lab"
GROUP = "vmoperator.vmware.com"
METRIC = "vm_cpu_usage_mhz"
RESOURCES = {"Secret": "secrets", "VirtualMachineReplicaSet": "virtualmachinereplicasets." + GROUP,
             "Role": "roles.rbac.authorization.k8s.io", "RoleBinding": "rolebindings.rbac.authorization.k8s.io",
             "HorizontalPodAutoscaler": "horizontalpodautoscalers.autoscaling"}
METRIC_PATH = ("/apis/custom.metrics.k8s.io/v1beta2/namespaces/{ns}/"
               "virtualmachinereplicasets.vmoperator.vmware.com/{name}/" + METRIC)


def command(args, body=None, check=True):
    result = subprocess.run(args, input=json.dumps(body) if body is not None else None,
                            text=True, capture_output=True, check=False)
    if check and result.returncode:
        # Creation errors can echo Secret payloads; never print stdin.
        raise RuntimeError(f"{Path(args[0]).name} {' '.join(args[1:3])} failed "
                           f"(exit {result.returncode}): {result.stderr.strip()[:300]}")
    return result


def cloud_config(deadline):
    """Burn every vCPU until the epoch deadline, then stay idle."""
    burn = ("n=$(nproc); i=0; while [ $i -lt $n ]; do "
            "( while [ $(date +%s) -lt " + str(int(deadline)) + " ]; do :; done ) & "
            "i=$((i+1)); done; wait")
    return "#cloud-config\n" + json.dumps({"runcmd": [["sh", "-c", burn]]})


def hpa(name, namespace, run, target_mhz, max_replicas):
    return {"apiVersion": "autoscaling/v2", "kind": "HorizontalPodAutoscaler",
            "metadata": {"name": name, "namespace": namespace, "labels": {LABEL: run}},
            "spec": {
                "scaleTargetRef": {"apiVersion": GROUP + "/v1alpha6",
                                   "kind": "VirtualMachineReplicaSet", "name": name},
                "minReplicas": 1, "maxReplicas": max_replicas,
                "metrics": [{"type": "Object", "object": {
                    "describedObject": {"apiVersion": GROUP + "/v1alpha6",
                                        "kind": "VirtualMachineReplicaSet", "name": name},
                    "metric": {"name": METRIC},
                    "target": {"type": "AverageValue", "averageValue": str(target_mhz)}}}],
                # Short windows keep the run bounded; production defaults are longer.
                "behavior": {"scaleDown": {"stabilizationWindowSeconds": 60},
                             "scaleUp": {"stabilizationWindowSeconds": 0}}}}


def condition(obj, kind):
    for c in obj.get("status", {}).get("conditions", []):
        if c["type"] == kind:
            return c
    return None


def metric_unavailable(hpa_obj):
    """The HPA reports that it cannot read the metric (and so holds)."""
    c = condition(hpa_obj, "ScalingActive")
    return bool(c) and c["status"] == "False"


class VMAware:
    def __init__(self, args):
        self.args = args
        self.directory = Path(args.state).resolve()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)
        self.path = self.directory / "state.json"
        self.state = json.loads(self.path.read_text()) if self.path.exists() else {}
        if self.state and (self.state["run"] != args.run or self.state["namespace"] != args.namespace):
            raise ValueError("state belongs to a different run or namespace")
        self.name = "vmaware-" + args.run
        self.namespace = args.namespace
        self.prefix = shlex.split(args.kubectl)
        self.samples = self.directory / "samples.jsonl"

    def kubectl(self, *args, body=None, check=True):
        return command([*self.prefix, "--request-timeout=30s", *args], body, check)

    def get(self, resource, name, namespace=None):
        args = ["get", resource, name, "-o", "json"]
        if namespace:
            args += ["-n", namespace]
        return json.loads(self.kubectl(*args).stdout)

    def save(self):
        self.path.write_text(json.dumps(self.state, indent=2) + "\n")
        self.path.chmod(0o600)

    def event(self, name, **fields):
        self.state.setdefault("events", []).append({"event": name, "time": time.time(), **fields})
        self.save()
        print(f"{time.strftime('%H:%M:%S')} {name} {json.dumps(fields)}", flush=True)

    def create(self, obj):
        # create rather than apply prevents taking over an existing resource.
        created = json.loads(self.kubectl("create", "-f", "-", "-o", "json", body=obj).stdout)
        self.state.setdefault("objects", []).append({"apiVersion": obj["apiVersion"],
            "kind": obj["kind"], "name": obj["metadata"]["name"],
            "namespace": obj["metadata"].get("namespace"), "uid": created["metadata"]["uid"]})
        self.save()
        return created

    def obj(self, api, kind, name, **fields):
        return {"apiVersion": api, "kind": kind,
                "metadata": {"name": name, "namespace": self.namespace, "labels": {LABEL: self.args.run}},
                **fields}

    def sample(self):
        rs = self.get("virtualmachinereplicasets." + GROUP, self.name, self.namespace)
        vms = json.loads(self.kubectl("get", "virtualmachines." + GROUP, "-n", self.namespace,
                                      "-l", LABEL + "=" + self.args.run, "-o", "json").stdout)["items"]
        hpa_obj = self.get("horizontalpodautoscalers.autoscaling", self.name, self.namespace) \
            if self.state.get("hpa_created") else {}
        raw = self.kubectl("get", "--raw", METRIC_PATH.format(ns=self.namespace, name=self.name), check=False)
        metric = None
        if raw.returncode == 0:
            items = json.loads(raw.stdout).get("items", [])
            metric = items[0]["value"] if items else None
        s = {"time": time.time(), "spec_replicas": rs["spec"].get("replicas"),
             "status_replicas": rs.get("status", {}).get("replicas"),
             "ready_replicas": rs.get("status", {}).get("readyReplicas"),
             "metric": metric, "metric_error": None if raw.returncode == 0 else raw.stderr.strip()[:200],
             "hpa_desired": hpa_obj.get("status", {}).get("desiredReplicas"),
             "hpa_current": hpa_obj.get("status", {}).get("currentReplicas"),
             "hpa_scaling_active": (condition(hpa_obj, "ScalingActive") or {}).get("status"),
             "vms": [{"name": v["metadata"]["name"], "power": v.get("status", {}).get("powerState"),
                      "utilization": v.get("status", {}).get("utilization")} for v in vms]}
        with self.samples.open("a") as f:
            f.write(json.dumps(s) + "\n")
        return s

    def wait(self, description, predicate, timeout):
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            last = self.sample()
            if predicate(last):
                return last
            time.sleep(self.args.interval)
        raise TimeoutError(f"{description} not reached in {timeout}s; last sample {json.dumps(last)[:600]}")

    def setup(self):
        if self.state:
            raise ValueError("state already exists; use a fresh state directory or teardown")
        template = json.loads(Path(self.args.template).read_text())
        if template.get("kind") != "VirtualMachine" or template.get("apiVersion") != GROUP + "/v1alpha6":
            raise ValueError("template must be a disposable v1alpha6 VirtualMachine manifest in JSON")
        spec = copy.deepcopy(template["spec"])
        if spec.get("bootstrap") or spec.get("volumes"):
            raise ValueError("use a VM template without bootstrap or volumes")
        for key in ("imageName", "className", "storageClass"):
            if not spec.get(key):
                raise ValueError("VM template must set " + key)
        self.state = {"run": self.args.run, "namespace": self.namespace,
                      "burn_until": time.time() + self.args.burn_minutes * 60}
        self.save()
        self.create(self.obj("v1", "Secret", self.name + "-bootstrap", type="Opaque",
                             stringData={"user-data": cloud_config(self.state["burn_until"])}))
        spec["bootstrap"] = {"cloudInit": {"rawCloudConfig": {"name": self.name + "-bootstrap", "key": "user-data"}}}
        spec["powerState"] = "PoweredOn"
        self.create(self.obj(GROUP + "/v1alpha6", "VirtualMachineReplicaSet", self.name,
            spec={"replicas": 1, "selector": {"matchLabels": {LABEL: self.args.run}},
                  "template": {"metadata": {"labels": {LABEL: self.args.run}}, "spec": spec}}))
        if self.args.hpa_subject:
            # Supervisor's HPA controller may lack cluster-wide rights on VM
            # Operator scale subresources; grant them only in this namespace.
            subject = json.loads(Path(self.args.hpa_subject).read_text())
            self.create(self.obj("rbac.authorization.k8s.io/v1", "Role", self.name,
                rules=[{"apiGroups": [GROUP], "resources": ["virtualmachinereplicasets"], "verbs": ["get", "list", "watch"]},
                       {"apiGroups": [GROUP], "resources": ["virtualmachinereplicasets/scale"],
                        "verbs": ["get", "update", "patch"]},
                       {"apiGroups": ["custom.metrics.k8s.io"], "resources": ["*"], "verbs": ["get"]}]))
            self.create(self.obj("rbac.authorization.k8s.io/v1", "RoleBinding", self.name,
                roleRef={"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": self.name},
                subjects=[subject]))
        self.event("setup", burn_until=self.state["burn_until"])

    def run(self):
        if not self.state.get("burn_until"):
            raise ValueError("run setup first")
        s = self.wait("first replica reporting utilization",
                      lambda s: any(v["utilization"] and v["power"] == "PoweredOn" for v in s["vms"]), 1800)
        self.event("telemetry", vms=s["vms"])
        s = self.wait("adapter metric", lambda s: s["metric"] is not None, 600)
        self.event("metric", value=s["metric"])
        if time.time() > self.state["burn_until"] - 300:
            raise RuntimeError("burn window nearly over before the HPA exists; use a longer --burn-minutes")
        self.create(hpa(self.name, self.namespace, self.args.run, self.args.target_mhz, self.args.max_replicas))
        self.state["hpa_created"] = True
        self.save()

        s = self.wait("scale-out under load",
                      lambda s: (s["spec_replicas"] or 0) >= 2 and (s["hpa_desired"] or 0) >= 2,
                      max(60, self.state["burn_until"] - time.time()))
        self.event("scale_out", spec_replicas=s["spec_replicas"], metric=s["metric"], hpa_desired=s["hpa_desired"])
        peak = s["spec_replicas"]

        if self.args.adapter_deployment and self.args.outage_during_burn:
            # The strong case: replicas are above minimum and the load ends
            # while the adapter is down. Treating missing data as zero would
            # scale in; holding keeps every replica until metrics return.
            s = self.wait("scale-out to max", lambda s: s["spec_replicas"] == self.args.max_replicas, 1200)
            hold = self.state["burn_until"] - time.time() + self.args.outage_hold_seconds
            if hold < self.args.outage_hold_seconds:
                raise RuntimeError("burn ended before the outage could start; use a longer --burn-minutes")
            self.outage(hold)
            peak = max(peak, self.args.max_replicas)

        remaining = self.state["burn_until"] - time.time()
        if remaining > 0:
            self.event("waiting_for_burn_end", seconds=int(remaining))
            while time.time() < self.state["burn_until"]:
                s = self.sample()
                peak = max(peak, s["spec_replicas"] or 0)
                time.sleep(self.args.interval)
        s = self.wait("scale-in after load", lambda s: s["spec_replicas"] == 1 and s["hpa_desired"] == 1, 1800)
        self.event("scale_in", peak=peak, metric=s["metric"])

        if self.args.adapter_deployment and not self.args.outage_during_burn:
            self.outage(self.args.outage_hold_seconds)
        self.state["passed"] = True
        self.save()
        print("vm-aware autoscaling run passed", flush=True)

    def outage(self, hold_seconds):
        """Stopping the adapter must leave the replica count alone."""
        ns, name = self.args.adapter_namespace, self.args.adapter_deployment
        original = self.get("deployment.apps", name, ns)["spec"]["replicas"]
        before = self.sample()["spec_replicas"]
        self.event("outage_start", adapter_replicas=original, spec_replicas=before)
        self.kubectl("scale", "deployment/" + name, "-n", ns, "--replicas=0")
        try:
            self.wait("HPA reports metric unavailable",
                      lambda s: s["hpa_scaling_active"] == "False", 600)
            end = time.time() + hold_seconds
            while time.time() < end:
                s = self.sample()
                if s["spec_replicas"] != before:
                    raise RuntimeError(f"replicas changed during adapter outage: {before} -> {s['spec_replicas']}")
                time.sleep(self.args.interval)
            self.event("outage_held", seconds=int(hold_seconds), spec_replicas=before,
                       burn_over=time.time() > self.state["burn_until"])
        finally:
            self.kubectl("scale", "deployment/" + name, "-n", ns, f"--replicas={original}")
        self.wait("HPA recovers", lambda s: s["hpa_scaling_active"] == "True", 600)
        self.event("outage_recovered")

    def teardown(self):
        # HPA first, so nothing rescales the target while it is being removed.
        for o in reversed(self.state.get("objects", [])):
            ref = RESOURCES[o["kind"]]
            got = self.kubectl("get", ref, o["name"], "-n", o["namespace"], "-o", "json", check=False)
            if got.returncode:
                continue
            if json.loads(got.stdout)["metadata"]["uid"] != o["uid"]:
                print(f"skip {o['kind']}/{o['name']}: UID changed", file=sys.stderr)
                continue
            # Foreground deletion keeps the bootstrap Secret until the VMs are gone.
            self.kubectl("delete", ref, o["name"], "-n", o["namespace"], "--cascade=foreground",
                         "--wait=true", "--timeout=15m")
        self.event("teardown")


def parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("operation", choices=("setup", "run", "sample", "teardown"))
    p.add_argument("--run", required=True)
    p.add_argument("--namespace", required=True, help="Existing Supervisor namespace for the VMs")
    p.add_argument("--state", required=True, help="Private evidence directory; never commit it")
    p.add_argument("--template", help="v1alpha6 disposable Linux VirtualMachine JSON (setup)")
    p.add_argument("--hpa-subject", help="JSON RBAC subject of the HPA controller identity (setup)")
    p.add_argument("--burn-minutes", type=int, default=30)
    p.add_argument("--target-mhz", type=int, default=1000, help="AverageValue target per replica")
    p.add_argument("--max-replicas", type=int, default=3)
    p.add_argument("--adapter-namespace", default="vmop-metrics-adapter")
    p.add_argument("--adapter-deployment", help="Adapter Deployment to stop for the outage check")
    p.add_argument("--outage-hold-seconds", type=int, default=180)
    p.add_argument("--outage-during-burn", action="store_true",
                   help="Stop the adapter at max replicas and keep it down until after the burn ends")
    p.add_argument("--interval", type=int, default=15)
    p.add_argument("--kubectl", default="kubectl", help="Kubectl command or quoted prefix, run without a shell")
    return p


def main():
    args = parser().parse_args()
    lab = VMAware(args)
    if args.operation == "sample":
        print(json.dumps(lab.sample(), indent=2))
    else:
        getattr(lab, args.operation)()


if __name__ == "__main__":
    main()
