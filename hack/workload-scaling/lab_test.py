# © Broadcom. All Rights Reserved.
# The term "Broadcom" refers to Broadcom Inc. and/or its subsidiaries.
# SPDX-License-Identifier: Apache-2.0
"""Safety and delivery guarantees of the lab harness; no live services required."""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import tarfile
import io
import types
import unittest
from unittest import mock

import lab


class LabTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        args = lab.parser().parse_args(["cycles", "--run", "test", "--namespace", "workloads", "--state", self.directory.name])
        self.lab = lab.Lab(args)

    def test_refuses_foreign_state(self):
        self.lab.state = {"run": "another", "namespace": "workloads"}
        self.lab.save()
        with self.assertRaisesRegex(ValueError, "different run"):
            lab.Lab(self.lab.args)

    def test_refuses_cluster_switch(self):
        self.lab.state = {"kube_system_uid": "original"}
        with mock.patch.object(self.lab, "get", return_value={"metadata": {"uid": "other"}}):
            with self.assertRaisesRegex(ValueError, "cluster"):
                self.lab.check_cluster()

    def test_refuses_helm_context_on_another_cluster(self):
        with mock.patch.dict("os.environ", {"KUBECONFIG": "/private/explicit-config"}), \
                mock.patch("lab.command", return_value=json.dumps({"metadata": {"uid": "another-cluster"}})):
            with self.assertRaisesRegex(ValueError, "different clusters"):
                self.lab.check_helm_context("selected-cluster")

    def test_never_deletes_replaced_resource(self):
        self.lab.state = {"objects": [{"kind": "Secret", "name": "auth", "namespace": "workloads", "uid": "original"}]}
        with mock.patch.object(self.lab, "check_cluster"), mock.patch.object(self.lab, "kubectl", return_value=json.dumps(
                {"metadata": {"uid": "replaced", "labels": {lab.LABEL: "test"}}})) as kubectl:
            with self.assertRaisesRegex(ValueError, "foreign"):
                self.lab.teardown()
            self.assertFalse(any("delete" in call.args for call in kubectl.call_args_list))

    def test_auth_denial_is_not_a_command_failure(self):
        with mock.patch("subprocess.run", return_value=subprocess.CompletedProcess([], 1, "no\n", "")):
            self.assertEqual(self.lab.can_i("patch", "foreign", "tenant"), "no")
        with mock.patch("subprocess.run", return_value=subprocess.CompletedProcess([], 1, "", "Unauthorized")):
            with self.assertRaises(RuntimeError):
                self.lab.can_i("patch", "foreign", "tenant")

    def test_contextual_kubectl_prefix_preserves_arguments(self):
        args = lab.parser().parse_args(["cycles", "--run", "test", "--namespace", "workloads",
            "--state", self.directory.name, "--kubectl",
            'tbenv kubectl --worktree "/tmp/check out" supervisor --'])
        target = lab.Lab(args)
        with mock.patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, "yes\n", "")) as run:
            self.assertEqual(target.can_i("patch", "workloads", "tenant"), "yes")
        self.assertEqual(run.call_args.args[0][:7],
            ["tbenv", "kubectl", "--worktree", "/tmp/check out", "supervisor", "--", "--request-timeout=30s"])

    def test_teardown_preserves_preprovisioned_supervisor_namespace(self):
        self.lab.state = {"objects": [], "helm_install_attempted": True,
            "keda_namespace_uid": "owned", "keda_namespace_created": False}
        namespace = {"metadata": {"uid": "owned", "labels": {lab.LABEL: "test"}}}
        with mock.patch.dict("os.environ", {"KUBECONFIG": "/private/explicit-config"}), \
                mock.patch.object(self.lab, "check_cluster"), \
                mock.patch.object(self.lab, "kubectl", return_value=json.dumps(namespace)) as kubectl, \
                mock.patch("lab.command") as command:
            self.lab.teardown()
        command.assert_called_once()
        self.assertFalse(any("delete" in call.args for call in kubectl.call_args_list))
        self.assertTrue(self.lab.state["teardown_complete"])

    def test_teardown_is_rerunnable_after_namespace_and_broker_cleanup(self):
        self.lab.state = {"objects": [], "helm_install_attempted": True, "broker_created": True,
            "keda_namespace_uid": "owned", "keda_namespace_created": True}
        with mock.patch.object(self.lab, "check_cluster"), \
                mock.patch.object(self.lab, "kubectl", return_value="") as kubectl, \
                mock.patch.object(self.lab, "broker", return_value=None) as broker, \
                mock.patch("lab.command") as command:
            self.lab.teardown()
        command.assert_not_called()
        self.assertTrue(all(call.kwargs.get("allow_missing") for call in broker.call_args_list))
        self.assertEqual(len(broker.call_args_list), 2)
        self.assertTrue(self.lab.state["teardown_complete"])

    def test_broker_missing_object_is_tolerated_only_on_request(self):
        error = lab.urllib.error.HTTPError("http://broker", 404, "missing", {}, None)
        env = {"BROKER_HTTP_URL": "http://broker", "BROKER_ADMIN_USER": "u", "BROKER_ADMIN_PASSWORD": "p"}
        with mock.patch.dict("os.environ", env), mock.patch("urllib.request.urlopen", side_effect=error):
            self.assertIsNone(self.lab.broker("DELETE", "users/test", allow_missing=True))
            with self.assertRaises(RuntimeError):
                self.lab.broker("DELETE", "users/test")

    def test_restart_requires_explicit_operator_disruption_consent(self):
        args = lab.parser().parse_args(["restart", "--run", "test", "--namespace", "workloads",
            "--state", self.directory.name, "--operator-namespace", "ns", "--operator-deployment", "dep"])
        with mock.patch.object(self.lab, "check_cluster"):
            self.lab.args = args
            with self.assertRaisesRegex(ValueError, "allow-operator-restart"):
                self.lab.restart()

    def test_raw_results_are_persisted_before_validation(self):
        good = {"id": "ok", "sha256": lab.hashlib.sha256(b"ok").hexdigest()}
        bad = {"id": "bad", "sha256": "forged"}
        messages = [{"payload": json.dumps(bad)}, {"payload": json.dumps(good)}]
        with mock.patch.object(self.lab, "broker", return_value=messages):
            with self.assertRaisesRegex(AssertionError, "invalid result"):
                self.lab.drain_results(set())
        raw = (self.lab.directory / "results.raw.jsonl").read_text().splitlines()
        self.assertEqual(len(raw), 2)

    def test_reloads_preprovisioned_keda_namespace_from_state(self):
        self.lab.state = {"run": "test", "namespace": "workloads", "keda_namespace": "supervisor-service-lab"}
        self.lab.save()
        restored = lab.Lab(self.lab.args)
        self.assertEqual(restored.keda_ns, "supervisor-service-lab")

    def test_corrupt_chart_fails_closed(self):
        (Path(self.directory.name) / ("keda-" + lab.CHART_VERSION + ".tgz")).write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "checksum"):
            self.lab.chart()

    def test_crx_chart_changes_only_three_pinned_selectors(self):
        upstream = Path(self.directory.name) / "upstream.tar"
        entries = {"keda/templates/" + part + "/deployment.yaml":
                   b"nodeSelector:\n        kubernetes.io/os: linux\n"
                   for part in ("manager", "metrics-server", "webhooks")}
        entries["keda/templates/secret.yaml"] = b"secret unchanged\n"
        with tarfile.open(upstream, "w") as output:
            for name, data in entries.items():
                member = tarfile.TarInfo(name)
                member.size = len(data)
                output.addfile(member, io.BytesIO(data))
        adapted = self.lab.worker_chart(upstream)
        with tarfile.open(adapted) as output:
            for name, original in entries.items():
                expected = original.replace(b"os: linux", b"os: CRX") if name.endswith("deployment.yaml") else original
                self.assertEqual(output.extractfile(name).read(), expected)
        self.assertEqual(self.lab.state["adapted_chart_sha256"], lab.hashlib.sha256(adapted.read_bytes()).hexdigest())

    def test_crx_chart_refuses_missing_templates(self):
        upstream = Path(self.directory.name) / "empty.tar"
        with tarfile.open(upstream, "w"):
            pass
        with self.assertRaisesRegex(ValueError, "missing pinned"):
            self.lab.worker_chart(upstream)

    def test_offline_worker_dependency_rejects_unpinned_bytes(self):
        wheel = Path(self.directory.name) / "pika.whl"
        wheel.write_bytes(b"unverified dependency")
        self.lab.args.worker_wheel = str(wheel)
        with self.assertRaisesRegex(ValueError, "wheel checksum"):
            self.lab.worker_wheel()

    def test_idle_scaler_is_not_an_outage(self):
        self.assertFalse(lab.scaler_failed([
            {"type": "Ready", "status": "True"}, {"type": "Active", "status": "False"}]))
        self.assertTrue(lab.scaler_failed([{"type": "Active", "status": "Unknown"}]))
        self.assertTrue(lab.scaler_failed([{"type": "Ready", "status": "False"}]))

    def test_quota_gate_requires_the_injected_quota(self):
        failure = {"type": "ReplicaFailure", "status": "True",
                   "message": "storage policy quota admission failed"}
        self.assertFalse(lab.quota_rejected([failure], "owned-quota"))
        failure["message"] = "VM forbidden: exceeded quota: owned-quota, requested: count=1"
        self.assertTrue(lab.quota_rejected([failure], "owned-quota"))
        self.assertFalse(lab.quota_rejected([failure], "another-quota"))

    def test_recovery_follows_current_hpa_demand(self):
        with mock.patch.object(self.lab, "sample", return_value={
                "desired": 2, "status": {"replicas": 2, "readyReplicas": 2}}):
            self.assertTrue(self.lab.workers_ready())
        with mock.patch.object(self.lab, "sample", return_value={
                "desired": 3, "status": {"replicas": 3, "readyReplicas": 2}}):
            self.assertFalse(self.lab.workers_ready())
        with mock.patch.object(self.lab, "sample", return_value={
                "desired": 0, "status": {"replicas": 0, "readyReplicas": 0}}):
            self.assertFalse(self.lab.workers_ready())

    def test_preflight_records_server_version_instead_of_crd_schema(self):
        version = {"major": "1", "minor": "35", "gitVersion": "v1.35.8"}
        def get(resource, name=None, namespace=None):
            if resource == "namespace":
                return {"metadata": {"uid": "selected-cluster"}}
            if resource == "capabilities.iaas.vmware.com":
                return {"items": [{"status": {"supervisor": {
                    "supports_vm_service_k8s_workload_mgmt_api": {"activated": True}}}}]}
            if resource == "crd":
                return {"spec": {"versions": [{"name": "v1alpha6", "served": True,
                    "subresources": {"scale": {"labelSelectorPath": ".status.selector"}}}]}}
            if resource == "nodes":
                return {"items": [{"metadata": {"labels": {"kubernetes.io/os": "linux"}},
                    "status": {"conditions": [{"type": "Ready", "status": "True"}]}}]}
            return {"items": []}
        def kubectl(*args):
            return json.dumps(version) if args[:3] == ("get", "--raw", "/version") else "yes"
        env = {"KUBECONFIG": "/explicit", "BROKER_HTTP_URL": "http://fixture",
               "BROKER_ADMIN_USER": "fixture", "BROKER_ADMIN_PASSWORD": "fixture", "BROKER_AMQP_HOST": "fixture"}
        with mock.patch.dict("os.environ", env), mock.patch.object(self.lab, "get", side_effect=get), \
                mock.patch.object(self.lab, "kubectl", side_effect=kubectl), \
                mock.patch.object(self.lab, "broker", return_value=[]), \
                mock.patch.object(self.lab, "check_helm_context"):
            self.lab.preflight()
        self.assertEqual(self.lab.state["version"], version)

    def test_bounds_violation_fails_run(self):
        with mock.patch.object(self.lab, "target", return_value={"spec": {"replicas": 4}}):
            with self.assertRaisesRegex(AssertionError, "bounds"):
                self.lab.sample()

    def test_records_actual_redelivery_for_acceptance(self):
        job = {"id": "redelivered-job", "sha256": lab.hashlib.sha256(b"redelivered-job").hexdigest(),
               "redelivered": True, "worker": "replacement"}
        completed = set()
        with mock.patch.object(self.lab, "broker", return_value=[{"payload": json.dumps(job)}]):
            self.lab.drain_results(completed)
            self.lab.drain_results(completed)
        self.assertEqual(completed, {"redelivered-job"})
        self.assertEqual(self.lab.state["redelivered_job_ids"], ["redelivered-job"])

    def test_broker_failure_disconnects_only_this_run(self):
        connections = [{"user": "vmrs-test", "vhost": "vmrs-test", "name": "owned connection"},
                       {"user": "another", "vhost": "vmrs-test", "name": "foreign user"},
                       {"user": "vmrs-test", "vhost": "another", "name": "foreign vhost"}]
        with mock.patch.object(self.lab, "broker", side_effect=[connections, None]) as broker:
            self.lab.disconnect_workers()
        self.assertEqual(broker.call_args_list, [mock.call("GET", "connections"),
                         mock.call("DELETE", "connections/owned%20connection")])


class WorkerTests(unittest.TestCase):
    def setUp(self):
        # Pika is installed in the VM from a hash-pinned wheel. A small interface
        # substitute here lets us inject publish failure without a live broker.
        spec = importlib.util.spec_from_file_location("scaling_worker", Path(__file__).with_name("worker.py"))
        self.worker = importlib.util.module_from_spec(spec)
        with mock.patch.dict(sys.modules, {"pika": types.SimpleNamespace(BasicProperties=lambda **kw: kw)}):
            spec.loader.exec_module(self.worker)

    def test_confirms_result_before_acknowledging_work(self):
        channel = mock.Mock()
        method = types.SimpleNamespace(delivery_tag=7, redelivered=True)
        with mock.patch.dict("os.environ", {"RESULT_QUEUE": "results"}):
            self.worker.process(mock.Mock(), channel, method, None, b'{"id":"job","seconds":0}')
        self.assertEqual([call[0] for call in channel.mock_calls], ["basic_publish", "basic_ack"])
        result = json.loads(channel.basic_publish.call_args.args[2])
        self.assertEqual(result["id"], "job")
        self.assertTrue(result["redelivered"])

    def test_malformed_job_is_rejected_without_requeue(self):
        for body in (b"not json", b'{"seconds":0}', b"5"):
            channel = mock.Mock()
            method = types.SimpleNamespace(delivery_tag=9, redelivered=False)
            self.worker.process(mock.Mock(), channel, method, None, body)
            channel.basic_reject.assert_called_once_with(9, requeue=False)
            channel.basic_publish.assert_not_called()

    def test_scale_read_distinguishes_forbidden_from_failure(self):
        ok = subprocess.CompletedProcess([], 0, "{}", "")
        forbidden = subprocess.CompletedProcess([], 1, "", 'Error from server (Forbidden): forbidden')
        broken = subprocess.CompletedProcess([], 1, "", "connection refused")
        with tempfile.TemporaryDirectory() as directory:
            instance = lab.Lab(lab.parser().parse_args(
                ["isolation", "--run", "test", "--namespace", "workloads", "--state", directory]))
            with mock.patch("subprocess.run", return_value=ok) as run:
                self.assertTrue(instance.scale_read("ns", "user", ["g"], "name"))
                self.assertIn("--as-group=g", run.call_args.args[0])
            with mock.patch("subprocess.run", return_value=forbidden):
                self.assertFalse(instance.scale_read("ns", "user", [], "name"))
            with mock.patch("subprocess.run", return_value=broken):
                with self.assertRaises(RuntimeError):
                    instance.scale_read("ns", "user", [], "name")

    def test_failed_result_publish_leaves_job_unacknowledged(self):
        channel = mock.Mock()
        channel.basic_publish.side_effect = OSError("broker unavailable")
        method = types.SimpleNamespace(delivery_tag=7, redelivered=False)
        with mock.patch.dict("os.environ", {"RESULT_QUEUE": "results"}):
            with self.assertRaises(OSError):
                self.worker.process(mock.Mock(), channel, method, None, b'{"id":"job","seconds":0}')
        channel.basic_ack.assert_not_called()


if __name__ == "__main__":
    unittest.main()
