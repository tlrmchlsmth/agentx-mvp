from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

MODULE_PATH = Path(__file__).resolve().parents[1] / "campaign" / "run.py"
spec = importlib.util.spec_from_file_location("campaign_runner", MODULE_PATH)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class CampaignTests(unittest.TestCase):
    def config(self, root: Path):
        for name in ("baseline", "candidate"):
            (root / name).mkdir()
        return {
            "id": "test-campaign", "namespace": "vllm", "overlay_root": str(root),
            "results_pvc": "results", "benchmark_queue": "live-benchmark-client",
            "campaign_queue": "benchmark-campaign", "rollout_timeout_seconds": 60,
            "admission_timeout_seconds": 60, "cleanup_timeout_seconds": 60,
            "continue_on_failure": False,
            "overlays": [{"name": name, "path": name, "model_label": "kimi-k3",
                          "pod_selector": "app=kimi-k3", "expected_pods": 2}
                         for name in ("baseline", "candidate")],
            "benchmarks": [{"tool": "aiperf", "concurrencies": [1, 4], "duration_seconds": 60}],
        }

    def test_rejects_namespace_escape_and_existing_resources(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.config(root)
            path = root / "config.json"
            config["overlays"][0]["path"] = "../elsewhere"
            path.write_text(json.dumps(config))
            with self.assertRaisesRegex(ValueError, "relative"):
                runner.load_config(path)
            with self.assertRaisesRegex(ValueError, "cluster-scoped"):
                runner.validate_manifest("apiVersion: v1\nkind: Namespace\nmetadata:\n  name: vllm\n", "vllm")
            with self.assertRaisesRegex(ValueError, "stay in namespace"):
                runner.validate_manifest("apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: test\n  namespace: other\n", "vllm")

    def test_runs_overlays_sequentially_and_records_results(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.config(root)
            actions = []
            manifest = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: marker\n  namespace: vllm\n"

            def fake_call(args, **kwargs):
                self.assertEqual(args[:2], ["kubectl", "kustomize"])
                actions.append("render:" + Path(args[2]).name)
                return SimpleNamespace(stdout=manifest, returncode=0)

            def fake_kube(namespace, *args, **kwargs):
                self.assertEqual(namespace, "vllm")
                if args[0] == "get":
                    actions.append("check" if "-f" in args else "get")
                    return SimpleNamespace(stdout="", returncode=0)
                actions.append(args[0])
                return SimpleNamespace(stdout="", stderr="", returncode=0)

            def fake_submit(config, overlay, bench, folder, baseline, commit):
                actions.append("benchmark:" + overlay["name"])
                return {"tool": "aiperf", "status": "completed", "job": "job-" + overlay["name"],
                        "artifacts": "/workload/aiperf-agentx/example", "measurements": []}

            with patch.object(runner, "call", side_effect=fake_call), \
                 patch.object(runner, "kube", side_effect=fake_kube), \
                 patch.object(runner, "snapshot", return_value=[]), \
                 patch.object(runner, "wait_ready", return_value=["pod:uid"]), \
                 patch.object(runner, "build_commit", return_value="a" * 40), \
                 patch.object(runner, "submit_benchmark", side_effect=fake_submit), \
                 patch.object(runner, "wait_gone"):
                self.assertEqual(runner.run(config, root), 0)
            self.assertEqual(actions, ["render:baseline", "check", "apply", "get", "benchmark:baseline", "delete",
                                       "render:candidate", "check", "apply", "get", "benchmark:candidate", "delete"])
            summary = json.loads((root / "campaigns/test-campaign/summary.json").read_text())
            self.assertEqual(summary["status"], "completed")
            self.assertEqual(len(summary["overlays"]), 2)
            self.assertTrue((root / "campaigns/test-campaign/index.html").exists())

    def test_submit_uses_separate_campaign_queue_and_results_pvc(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self.config(Path(directory))
            created = []

            def fake_kube(namespace, *args, **kwargs):
                self.assertEqual(namespace, "vllm")
                if args[0] == "create":
                    created.append(json.loads(kwargs["input_text"]))
                return SimpleNamespace(stdout="", stderr="", returncode=0)

            with patch.object(runner, "kube", side_effect=fake_kube):
                runner.submit(config, "registry.example/campaign:sha", "benchmark-campaign")
            self.assertEqual([item["kind"] for item in created], ["ConfigMap", "Job"])
            job = created[1]
            self.assertEqual(job["metadata"]["labels"]["kueue.x-k8s.io/queue-name"], "benchmark-campaign")
            self.assertTrue(job["spec"]["suspend"])
            pod = job["spec"]["template"]["spec"]
            self.assertEqual(pod["serviceAccountName"], "benchmark-campaign")
            self.assertEqual(pod["volumes"][1]["persistentVolumeClaim"]["claimName"], "results")

    def test_cleanup_failure_stops_before_next_overlay(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.config(root)
            config["continue_on_failure"] = True
            actions = []
            manifest = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: marker\n  namespace: vllm\n"

            def fake_kube(namespace, *args, **kwargs):
                actions.append(args[0])
                if args[0] == "delete":
                    return SimpleNamespace(stdout="", stderr="teardown failed", returncode=1)
                return SimpleNamespace(stdout="", stderr="", returncode=0)

            with patch.object(runner, "call", return_value=SimpleNamespace(stdout=manifest)), \
                 patch.object(runner, "kube", side_effect=fake_kube), \
                 patch.object(runner, "snapshot", return_value=[]), \
                 patch.object(runner, "wait_ready", side_effect=RuntimeError("not ready")):
                self.assertEqual(runner.run(config, root), 1)
            self.assertEqual(actions, ["get", "apply", "delete"])
            summary = json.loads((root / "campaigns/test-campaign/summary.json").read_text())
            self.assertEqual(len(summary["overlays"]), 1)
            self.assertEqual(summary["overlays"][0]["cleanup_error"], "teardown failed")


if __name__ == "__main__":
    unittest.main()
