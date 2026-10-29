# © Broadcom. All Rights Reserved.
# The term "Broadcom" refers to Broadcom Inc. and/or its subsidiaries.
# SPDX-License-Identifier: Apache-2.0

import json
import subprocess
import time
import unittest

import vm_aware


class CloudConfigTests(unittest.TestCase):
    @staticmethod
    def script(deadline):
        text = vm_aware.cloud_config(deadline)
        assert text.startswith("#cloud-config\n")
        return json.loads(text.split("\n", 1)[1])["runcmd"][0][2]

    def test_deadline_is_an_integer_epoch(self):
        self.assertIn("-lt 1793300000 ]", self.script(1793300000.7))

    def test_past_deadline_does_not_burn(self):
        start = time.monotonic()
        subprocess.run(["sh", "-c", self.script(0)], check=True, timeout=10)
        self.assertLess(time.monotonic() - start, 5)

    def test_burns_until_deadline(self):
        start = time.monotonic()
        subprocess.run(["sh", "-c", self.script(time.time() + 2)], check=True, timeout=10)
        self.assertGreaterEqual(time.monotonic() - start, 1)


class HPATests(unittest.TestCase):
    def test_object_average_value_on_replicaset(self):
        h = vm_aware.hpa("vmaware-r", "ns", "r", 1000, 3)
        m = h["spec"]["metrics"][0]
        self.assertEqual(m["type"], "Object")
        self.assertEqual(m["object"]["target"], {"type": "AverageValue", "averageValue": "1000"})
        self.assertEqual(m["object"]["metric"]["name"], vm_aware.METRIC)
        self.assertEqual(h["spec"]["scaleTargetRef"]["kind"], "VirtualMachineReplicaSet")
        self.assertEqual(h["spec"]["scaleTargetRef"], {
            "apiVersion": "vmoperator.vmware.com/v1alpha6", "kind": "VirtualMachineReplicaSet", "name": "vmaware-r"})
        self.assertEqual((h["spec"]["minReplicas"], h["spec"]["maxReplicas"]), (1, 3))

    def test_metric_unavailable(self):
        def obj(status):
            return {"status": {"conditions": [{"type": "ScalingActive", "status": status}]}}
        self.assertTrue(vm_aware.metric_unavailable(obj("False")))
        self.assertFalse(vm_aware.metric_unavailable(obj("True")))
        self.assertFalse(vm_aware.metric_unavailable({}))


if __name__ == "__main__":
    unittest.main()
