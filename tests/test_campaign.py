from __future__ import annotations

import importlib.util
import json
import os
import subprocess
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
            "id": "test-campaign", "namespace": "vllm",
            "source": {"repo": "https://github.com/example/llm-d.git", "ref": "feature/bench"},
            "results_pvc": "results", "benchmark_queue": "live-benchmark-client",
            "campaign_queue": "benchmark-campaign", "rollout_timeout_seconds": 60,
            "admission_timeout_seconds": 60, "cleanup_timeout_seconds": 60,
            "continue_on_failure": False,
            "overlays": [{"name": name, "path": name, "model_label": "test-model",
                          "pod_selector": "app=test-model", "expected_pods": 2}
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

            def fake_submit(config, overlay, bench, folder, baseline, commit, source_commit):
                actions.append("benchmark:" + overlay["name"])
                return {"tool": "aiperf", "status": "completed", "job": "job-" + overlay["name"],
                        "artifacts": "/workload/aiperf-agentx/example", "measurements": []}

            with patch.object(runner, "fetch_source", return_value=(root, "a" * 40)), \
                 patch.object(runner.shutil, "rmtree"), \
                 patch.object(runner, "call", side_effect=fake_call), \
                 patch.object(runner, "kube", side_effect=fake_kube), \
                 patch.object(runner, "snapshot", return_value=[]), \
                 patch.object(runner, "wait_ready", return_value=["pod:uid"]), \
                 patch.object(runner, "build_commit", return_value="a" * 40), \
                 patch.object(runner, "submit_benchmark", side_effect=fake_submit), \
                 patch.object(runner, "wait_gone"):
                self.assertEqual(runner.run(config, root), 0, (root / "campaigns/test-campaign/summary.json").read_text())
            self.assertEqual(actions, ["render:baseline", "check", "apply", "get", "benchmark:baseline", "delete",
                                       "render:candidate", "check", "apply", "get", "benchmark:candidate", "delete"])
            summary = json.loads((root / "campaigns/test-campaign/summary.json").read_text())
            self.assertEqual(summary["status"], "completed")
            self.assertEqual(summary["source_commit"], "a" * 40)
            self.assertEqual(len(summary["overlays"]), 2)
            self.assertTrue((root / "campaigns/test-campaign/index.html").exists())

    def test_rejects_unsafe_source_and_records_checkout_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.config(root)
            path = root / "config.json"
            config["source"]["repo"] = "https://user:secret@github.com/example/llm-d.git"
            path.write_text(json.dumps(config))
            with self.assertRaisesRegex(ValueError, "without credentials"):
                runner.load_config(path)
            config["source"]["repo"] = "https://github.com/example/llm-d.git"
            config["source"]["ref"] = "-unsafe"
            path.write_text(json.dumps(config))
            with self.assertRaisesRegex(ValueError, "branch, tag, or commit"):
                runner.load_config(path)
            config["source"]["ref"] = "feature/bench"
            with patch.object(runner, "fetch_source", side_effect=RuntimeError("ref not found")):
                with self.assertRaisesRegex(RuntimeError, "ref not found"):
                    runner.run(config, root)
            summary = json.loads((root / "campaigns/test-campaign/summary.json").read_text())
            self.assertEqual(summary["status"], "failed")
            self.assertIn("ref not found", summary["error"])

    def test_fetches_selected_fork_ref_once(self):
        calls = []

        def fake_call(args, **kwargs):
            calls.append(args)
            return SimpleNamespace(stdout="b" * 40 + "\n", returncode=0)

        with patch.object(runner, "call", side_effect=fake_call):
            checkout, commit = runner.fetch_source({"repo": "https://github.com/example/llm-d.git", "ref": "feature/bench"})
        try:
            self.assertEqual(commit, "b" * 40)
            self.assertEqual(calls[1][-2:], ["origin", "https://github.com/example/llm-d.git"])
            self.assertEqual(calls[2][-2:], ["origin", "feature/bench"])
        finally:
            runner.shutil.rmtree(checkout)

    def test_missing_vllm_build_marker_is_allowed(self):
        with patch.object(runner, "kube", return_value=SimpleNamespace(stdout="")):
            self.assertIsNone(runner.build_commit("vllm"))

    def test_live_submitters_use_campaign_source_without_build_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            kubectl = root / "kubectl"
            kubectl.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$MOCK_KUBECTL_LOG\"\ncase \"$*\" in *wait*) exit 1;; esac\nexit 0\n")
            kubectl.chmod(0o755)
            log = root / "kubectl.log"
            env = os.environ.copy()
            env.update({"PATH": f"{root}:{env['PATH']}", "MOCK_KUBECTL_LOG": str(log),
                        "LIVE_AIPERF_NAMESPACE": "vllm", "LIVE_NYANN_NAMESPACE": "vllm",
                        "LIVE_BENCHMARK_SOURCE_REF": "feature/bench",
                        "LIVE_BENCHMARK_SOURCE_COMMIT": "a" * 40,
                        "LIVE_BENCHMARK_SOURCE_KIND": "llm-d", "MODEL_LABEL": "test-model"})
            for script, args in (("live-aiperf/submit.sh", ["1", "60"]),
                                 ("live-nyann/submit.sh", ["1", "1024", "512", "60", "0"])):
                result = subprocess.run(["bash", str(MODULE_PATH.parents[1] / script), *args],
                                        env=env, text=True, capture_output=True)
                self.assertNotEqual(result.returncode, 0)  # fake kubectl has no serving Pods
                self.assertNotIn("source ref/commit is missing", result.stderr)
            self.assertNotIn("get configmap vllm-build-ref", log.read_text())

    def test_report_source_identity_is_generic(self):
        report_path = MODULE_PATH.parents[1] / "live-aiperf/report.py"
        report_spec = importlib.util.spec_from_file_location("live_report", report_path)
        report = importlib.util.module_from_spec(report_spec)
        report_spec.loader.exec_module(report)
        metadata = {"source_kind": "llm-d", "source_ref": "feature/bench", "source_commit": "a" * 40}
        self.assertEqual(report.source_ref(metadata), "feature/bench")
        self.assertEqual(report.source_commit(metadata), "a" * 40)
        row = report.source_row({"metadata": metadata, "yaml": ""})
        self.assertIn("llm-d", row)
        self.assertNotIn("github.com/elvircrn/vllm", row)
        self.assertEqual(report.source_commit({"vllm_build_commit": "b" * 40}), "b" * 40)

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

            with patch.object(runner, "fetch_source", return_value=(root, "a" * 40)), \
                 patch.object(runner.shutil, "rmtree"), \
                 patch.object(runner, "call", return_value=SimpleNamespace(stdout=manifest)), \
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
