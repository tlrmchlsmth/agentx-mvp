from __future__ import annotations

import importlib.util
import base64
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
import tempfile
import unittest
import yaml
from unittest.mock import patch
from types import SimpleNamespace

MODULE_PATH = Path(__file__).resolve().parents[1] / "campaign" / "run.py"
spec = importlib.util.spec_from_file_location("campaign_runner", MODULE_PATH)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class CampaignTests(unittest.TestCase):
    def test_kubectl_logs_replace_invalid_utf8(self):
        with patch.object(runner, "call", return_value=SimpleNamespace(stdout="")) as command:
            runner.kube("vllm", "logs", "job/example")
        self.assertEqual(command.call_args.kwargs["errors"], "replace")

    def config(self, root: Path):
        for name in ("baseline", "candidate"):
            (root / name).mkdir()
        return {
            "id": "test-campaign", "namespace": "vllm",
            "source": {"repo": "https://github.com/example/llm-d.git", "ref": "feature/bench"},
            "vllm_image": "vllm/example@sha256:abc",
            "results_pvc": "results", "benchmark_queue": "live-benchmark-client",
            "campaign_queue": "benchmark-campaign", "rollout_timeout_seconds": 60,
            "admission_timeout_seconds": 60, "cleanup_timeout_seconds": 60,
            "continue_on_failure": False,
            "overlays": [{"name": name, "path": name, "model_label": "test-model",
                          "pod_selector": "app=test-model", "expected_pods": 2}
                         for name in ("baseline", "candidate")],
            "benchmarks": [{"tool": "aiperf", "concurrencies": [1, 4], "duration_seconds": 900,
                            "max_context_length": 131072}],
        }

    def test_local_grafana_service_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.config(root)
            config["monitoring"] = {"grafana_namespace": "llm-d-monitoring",
                                    "grafana_service": "llmd-grafana",
                                    "auth_secret": "llmd-grafana",
                                    "dashboard_uid": "wideep-overview"}
            path = root / "campaign.json"
            path.write_text(json.dumps(config))
            loaded = runner.load_config(path)
            self.assertEqual(loaded["monitoring"], config["monitoring"])
            with self.assertRaisesRegex(ValueError, "for run-local"):
                runner.submit(loaded, "runner:test", "benchmark-campaign")

    def test_rejects_namespace_escape_and_existing_resources(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.config(root)
            path = root / "config.json"
            config["overlays"][0]["path"] = "../elsewhere"
            path.write_text(json.dumps(config))
            with self.assertRaisesRegex(ValueError, "relative"):
                runner.load_config(path)
            config["overlays"][0]["path"] = "baseline"
            config["benchmarks"][0]["duration_seconds"] = 600
            path.write_text(json.dumps(config))
            with self.assertRaisesRegex(ValueError, "duration_seconds"):
                runner.load_config(path)
            with self.assertRaisesRegex(ValueError, "cluster-scoped"):
                runner.validate_manifest("apiVersion: v1\nkind: Namespace\nmetadata:\n  name: vllm\n", "vllm")
            with self.assertRaisesRegex(ValueError, "stay in namespace"):
                runner.validate_manifest("apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: test\n  namespace: other\n", "vllm")
            with self.assertRaisesRegex(ValueError, "no LeaderWorkerSet vllm container"):
                runner.apply_vllm_image("apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: test\n", "vllm/image:nightly")

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
                 patch.object(runner, "apply_vllm_image", side_effect=lambda rendered, image: rendered), \
                 patch.object(runner, "kube", side_effect=fake_kube), \
                 patch.object(runner, "snapshot", return_value=[]), \
                 patch.object(runner, "wait_ready", return_value=["pod:uid"]), \
                 patch.object(runner, "build_commit", return_value="a" * 40), \
                 patch.object(runner, "submit_benchmark", side_effect=fake_submit), \
                 patch.object(runner, "write_final_report", side_effect=lambda destination, summary: runner.write_summary(destination, summary)), \
                 patch.object(runner, "wait_gone"):
                self.assertEqual(runner.run(config, root), 0, (root / "campaigns/test-campaign/summary.json").read_text())
            self.assertEqual(actions, ["render:baseline", "check", "apply", "get", "benchmark:baseline", "delete",
                                       "render:candidate", "check", "apply", "get", "benchmark:candidate", "delete"])
            summary = json.loads((root / "campaigns/test-campaign/summary.json").read_text())
            self.assertEqual(summary["status"], "completed")
            self.assertEqual(summary["source_commit"], "a" * 40)
            self.assertEqual(len(summary["overlays"]), 2)
            self.assertFalse((root / "campaigns/test-campaign/index.html").exists())

    def test_matrix_prepares_all_builds_before_deploying_every_combination(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.config(root)
            config["build_repo"] = "https://github.com/example/vllm.git"
            config["builds"] = [
                {"name": "branch2", "steps": [{"ref": "branch0", "action": "checkout"},
                                               {"ref": "branch1", "action": "merge"},
                                               {"ref": "branch2", "action": "cherry-pick"}]},
                {"name": "branch3", "steps": [{"ref": "branch0", "action": "checkout"},
                                               {"ref": "branch1", "action": "merge"},
                                               {"ref": "branch3", "action": "cherry-pick"}]},
            ]
            config["overlays"][0]["dimensions"] = {"mtp": "off", "topology": "pd"}
            config["overlays"][1]["dimensions"] = {"mtp": "on", "topology": "aggregate"}
            path = root / "config.json"
            path.write_text(json.dumps(config))
            runner.load_config(path)
            events = []
            manifest = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: marker\n  namespace: vllm\n"

            def fake_resolve(build, pins=None):
                name = build["steps"][-1]["ref"]
                events.append("resolve:" + name)
                return {"mode": "source", "repo": build["repo"], "steps": [
                    {**step, "commit": ("b" if name == "branch2" else "c") * 40} for step in build["steps"]]}

            def fake_prebuild(rendered, config, overlay, folder):
                events.append("prebuild:" + folder.parent.name + "/" + overlay["name"])
                (folder / "build.log").write_text("cache ready\n")
                return "a" * 40

            def fake_kube(namespace, *args, **kwargs):
                if args[0] == "apply":
                    events.append("deploy:" + Path(args[-1]).parent.name)
                return SimpleNamespace(stdout="", stderr="", returncode=0)

            def fake_submit(config, overlay, bench, folder, baseline, commit, source_commit):
                events.append("benchmark:" + overlay["name"])
                return {"tool": "aiperf", "status": "completed", "artifacts": "/workload/example",
                        "measurements": [{"sample": "c1", "concurrency": 1,
                                          "metrics": {"request_throughput": {"avg": 12}}}]}

            with patch.object(runner, "fetch_source", return_value=(root, "a" * 40)), \
                 patch.object(runner.shutil, "rmtree"), \
                 patch.object(runner, "render_overlay", return_value=manifest), \
                 patch.object(runner, "apply_vllm_image", side_effect=lambda rendered, image: rendered), \
                 patch.object(runner, "resolve_build", side_effect=fake_resolve), \
                 patch.object(runner, "inject_vllm_build_script", side_effect=lambda rendered, build, cache_pvc: rendered), \
                 patch.object(runner, "vllm_prebuild", side_effect=fake_prebuild), \
                 patch.object(runner, "kube", side_effect=fake_kube), \
                 patch.object(runner, "snapshot", return_value=[]), \
                 patch.object(runner, "wait_ready", return_value=["pod:uid"]), \
                 patch.object(runner, "build_commit", return_value="a" * 40), \
                 patch.object(runner, "submit_benchmark", side_effect=fake_submit), \
                 patch.object(runner, "write_final_report", side_effect=lambda destination, summary: runner.write_summary(destination, summary)), \
                 patch.object(runner, "wait_gone"):
                self.assertEqual(runner.run(config, root), 0)
            self.assertEqual(len([event for event in events if event.startswith("prebuild:")]), 4)
            self.assertLess(max(i for i, event in enumerate(events) if event.startswith("prebuild:")),
                            min(i for i, event in enumerate(events) if event.startswith("deploy:")))
            summary = json.loads((root / "campaigns/test-campaign/summary.json").read_text())
            self.assertEqual([record["name"] for record in summary["overlays"]],
                             ["branch2-baseline", "branch2-candidate", "branch3-baseline", "branch3-candidate"])
            comparison = (root / "campaigns/test-campaign/comparison.csv").read_text()
            self.assertIn("branch2,baseline,off,pd", comparison)
            self.assertIn("branch3,candidate,on,aggregate", comparison)

    def test_nightly_matrix_skips_vllm_prebuild(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.config(root)
            config["builds"] = [{"name": "nightly"}]
            manifest = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: marker\n  namespace: vllm\n"
            destination = root / "results"
            destination.mkdir()
            summary = {"id": config["id"], "status": "running", "overlays": []}
            with patch.object(runner, "render_overlay", return_value=manifest), \
                 patch.object(runner, "apply_vllm_image", side_effect=lambda rendered, image: rendered), \
                 patch.object(runner, "kube", return_value=SimpleNamespace(stdout="")), \
                 patch.object(runner, "snapshot", return_value=[]), \
                 patch.object(runner, "vllm_prebuild", side_effect=AssertionError("unexpected prebuild")):
                resolved, _, failed, stopped = runner.prepare_matrix_builds(config, root, destination, summary)
            self.assertEqual(resolved["nightly"], {"mode": "nightly", "steps": []})
            self.assertEqual(summary["builds"][0]["prebuilds"], [])
            self.assertFalse(failed or stopped)

    def test_local_test_renders_plan_without_kubernetes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.config(root)
            config["builds"] = [{"name": "nightly", "steps": []}]
            manifest = yaml.safe_dump({
                "apiVersion": "leaderworkerset.x-k8s.io/v1", "kind": "LeaderWorkerSet",
                "metadata": {"name": "serving", "namespace": "vllm"},
                "spec": {"leaderWorkerTemplate": {"workerTemplate": {"spec": {"containers": [
                    {"name": "vllm", "image": "old/image:tag"}]}}}},
            })
            output = root / "local-test"
            with patch.object(runner, "call", return_value=SimpleNamespace(stdout="a" * 40 + "\n")), \
                 patch.object(runner, "render_overlay", return_value=manifest), \
                 patch.object(runner, "kube", side_effect=AssertionError("Kubernetes must not be contacted")):
                self.assertEqual(runner.test_local(config, output, root), 0)
            summary = json.loads((output / "summary.json").read_text())
            self.assertEqual(summary["status"], "validated")
            self.assertEqual(len(summary["overlays"]), 2)
            self.assertEqual(summary["overlays"][0]["benchmarks"][0]["status"], "mocked")
            self.assertEqual([item["concurrency"] for item in summary["overlays"][0]["benchmarks"][0]["measurements"]], [1, 4])
            page = (output / "index.html").read_text()
            self.assertIn("MOCK DATA", page)
            self.assertIn("nightly / baseline", page)
            self.assertIn("N/A (using configured image; no source commit)", page)
            identity_table = page.split("<table class='campaign-identity'>", 1)[1].split("</table>", 1)[0]
            self.assertEqual(identity_table.count("<tr>"), 3)  # header and one row per overlay
            measurements_table = page.split("<div class='campaign-measurements'>", 1)[1].split("</table>", 1)[0]
            self.assertNotIn("<th>Artifacts</th>", measurements_table)
            self.assertNotIn("<th>vLLM source commits</th>", measurements_table)
            self.assertIn(config["vllm_image"], (output / "nightly-baseline/manifest.yaml").read_text())
            self.assertFalse((output / "nightly-baseline/prebuild-job.yaml").exists())
            with patch.object(runner, "call", return_value=SimpleNamespace(stdout="a" * 40 + "\n")), \
                 patch.object(runner, "render_overlay", return_value="kind: ConfigMap\nmetadata:\n  name: marker\n"), \
                 patch.object(runner, "kube", side_effect=AssertionError("Kubernetes must not be contacted")):
                self.assertEqual(runner.test_local(config, root / "invalid-test", root), 1)
            failed = json.loads((root / "invalid-test/summary.json").read_text())
            self.assertEqual(failed["status"], "failed")
            self.assertIn("no LeaderWorkerSet vllm container", failed["overlays"][0]["error"])

    def test_live_local_runner_copies_pvc_artifacts_and_removes_helper(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.config(root)
            output = root / "live-output"
            actions = []

            def fake_kube(namespace, *args, **kwargs):
                self.assertEqual(namespace, "vllm")
                actions.append(args[0])
                if args[0] == "create":
                    pod = json.loads(kwargs["input_text"])
                    self.assertEqual(pod["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"], "results")
                if args[0] == "exec":
                    return SimpleNamespace(stdout="/workload/aiperf-agentx/sample/c1/profile_export_aiperf.json\n",
                                           stderr="", returncode=0)
                if args[0] == "cp":
                    Path(args[2]).write_text("{}")
                return SimpleNamespace(stdout="", stderr="", returncode=0)

            def fake_run(value, results_root, artifact_fetcher=None):
                self.assertEqual(results_root, output.resolve())
                self.assertIsNotNone(artifact_fetcher)
                copied = artifact_fetcher(Path("/workload/aiperf-agentx/sample"))
                self.assertEqual(copied, output.resolve() / "aiperf-agentx/sample")
                self.assertTrue(copied.is_dir())
                self.assertTrue((copied / "c1/profile_export_aiperf.json").is_file())
                self.assertFalse((copied / "c1/profile_export_raw.jsonl").exists())
                return 0

            with patch.object(runner, "kube", side_effect=fake_kube), \
                 patch.object(runner, "run", side_effect=fake_run):
                self.assertEqual(runner.run_local(config, output), 0)
            self.assertEqual(actions, ["get", "get", "create", "wait", "exec", *(["cp"] * 6), "delete"])

    def test_aiperf_report_matches_each_requested_sample(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_id = "test-campaign-branch2-baseline-aiperf"
            for sample, concurrency in (("c1-r1", 1), ("c4", 4), ("c1-r2", 1)):
                folder = root / sample
                folder.mkdir()
                (folder / "benchmark-metadata.json").write_text(json.dumps(
                    {"run_id": f"{run_id}-{sample}", "concurrency": concurrency}))
                (folder / "profile_export_aiperf.json").write_text(json.dumps(
                    {"request_throughput": {"avg": concurrency * 2, "unit": "req/s"},
                     "time_to_first_token": {"p90": 30, "unit": "ms"}}))
            measurements = runner.aiperf_measurements(root, run_id, [1, 4, 1])
            self.assertEqual(len(measurements), 3)
            self.assertEqual(sum(item["metrics"]["request_throughput"]["avg"] for item in measurements), 12)
            fragment = runner.write_summary(root, {"id": "test", "status": "completed", "overlays": [
                {"name": "build-baseline", "build": "build", "overlay": "baseline", "status": "completed",
                 "benchmarks": [{"tool": "aiperf", "status": "completed", "measurements": measurements,
                                 "report": f"/workload/aiperf-agentx/{run_id}/index.html"}]}]})
            self.assertIn(f"../../aiperf-agentx/{run_id}/index.html", fragment)
            self.assertFalse((root / "index.html").exists())
            with self.assertRaisesRegex(RuntimeError, "unexpected|expected"):
                runner.aiperf_measurements(root, run_id, [1, 4, 4])

    def test_nyann_stage_summary_enters_comparison_report(self):
        stages = [{"concurrency": concurrency, "successful_requests": 10,
                   "error_requests": 1, "duration_seconds": 5,
                   "output_tokens_per_second": concurrency * 20,
                   "ttft_ms": {"p90": 30}, "itl_ms": {"p90": 4}}
                  for concurrency in (1, 4)]
        log = "stage output\n" + json.dumps({"total_requests": 22, "stages": stages}, indent=2) + "\n"
        measurements = runner.nyann_measurements(log, [1, 4])
        self.assertEqual([item["metrics"]["request_throughput"]["avg"] for item in measurements], [2, 2])
        with tempfile.TemporaryDirectory() as directory:
            runner.write_summary(Path(directory), {"id": "example", "status": "completed", "overlays": [
                {"name": "build-pd", "build": "build", "overlay": "pd", "status": "completed",
                 "benchmarks": [{"tool": "nyann", "status": "completed", "measurements": measurements}]}]})
            comparison = (Path(directory) / "comparison.csv").read_text()
            self.assertIn("build,pd,nyann,stage-1,1,completed,10,1,2.0,req/s,20,tokens/s,30,ms,4,ms", comparison)

    def test_final_html_embeds_all_aiperf_variants_and_nyann_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            destination = root / "campaigns" / "campaign"
            destination.mkdir(parents=True)
            records = []
            for build, throughput in (("branch2", 10), ("branch3", 20)):
                build_commit = ("b" if build == "branch2" else "c") * 40
                run_id = f"campaign-{build}-pd-aiperf"
                sample = root / "aiperf-agentx" / run_id / "c1"
                sample.mkdir(parents=True)
                (sample / "benchmark-metadata.json").write_text(json.dumps({
                    "run_id": f"{run_id}-c1", "concurrency": 1, "source_kind": "llm-d",
                    "source_ref": "feature", "source_commit": "a" * 40,
                    "model_label": "test-model", "topology": "pd", "total_gpu_count": 4,
                    "prefill_gpu_count": 2, "decode_gpu_count": 2}))
                (sample / "profile_export_aiperf.json").write_text(json.dumps({
                    "request_throughput": {"avg": throughput, "unit": "req/s"},
                    "output_token_throughput": {"avg": throughput * 100, "unit": "tokens/s"},
                    "time_to_first_token": {"p90": 50, "unit": "ms"},
                    "inter_token_latency": {"p90": 5, "unit": "ms"}}))
                dashboard = ('<script>const panels = {"gpu":{"title":"GPU","unit":"percent",'
                             '"queries":[{"expr":"DCGM_FI_DEV_GPU_UTIL","series":'
                             '[{"labels":{},"values":[[0,"50"]]}]}]}};\n'
                             'const rows = [];\n</script>')
                (sample / "dashboard.html").write_text(dashboard)
                records.append({"name": f"{build}-pd", "build": build, "overlay": "pd",
                                "dimensions": {"mtp": "off"}, "status": "completed",
                                "vllm_build_inputs": {"mode": "source", "steps": [
                                    {"action": "checkout", "ref": "branch0", "commit": "d" * 40},
                                    {"action": "merge", "ref": build, "commit": build_commit}]},
                                "benchmarks": [{"tool": "aiperf", "status": "completed",
                                                "artifacts": str(sample.parent),
                                                "report": f"/workload/aiperf-agentx/{run_id}/index.html",
                                                "measurements": []}]})
            records[0]["benchmarks"].append({"tool": "nyann", "status": "completed",
                                              "measurements": [{"sample": "stage-1", "concurrency": 4,
                                                                "metrics": {"request_throughput": {"avg": 7}}}]})
            summary = {"id": "campaign", "status": "completed", "source_commit": "a" * 40,
                       "vllm_image": "vllm/example@sha256:abc", "overlays": records}
            runner.write_final_report(destination, summary)
            page = (destination / "index.html").read_text()
            self.assertIn("branch2 / pd", page)
            self.assertIn("branch3 / pd", page)
            self.assertIn("b" * 40, page)
            self.assertIn("c" * 40, page)
            self.assertIn("branch2@bbbbbbbbbbbb", page)
            self.assertIn("branch3@cccccccccccc", page)
            self.assertIn("nyann", page)
            self.assertIn(base64.b64encode(dashboard.encode()).decode(), page)
            self.assertIn('id="monitoring-overlay"', page)
            self.assertFalse((destination / "monitoring-overlay.html").exists())
            self.assertNotIn('src="https://cdn.plot.ly', page)
            self.assertNotIn("../../aiperf-agentx", page)
            comparison = (destination / "comparison.csv").read_text()
            self.assertIn("merge branch2@" + "b" * 40, comparison)
            self.assertIn("merge branch3@" + "c" * 40, comparison)
            for build, commit in (("branch2", "b" * 40), ("branch3", "c" * 40)):
                metadata = json.loads((root / "aiperf-agentx" / f"campaign-{build}-pd-aiperf" /
                                       "c1/benchmark-metadata.json").read_text())
                self.assertEqual(metadata["vllm_build_steps"][-1]["commit"], commit)
                self.assertEqual(metadata["source_commit"], "a" * 40)
                self.assertIn(commit, (root / "aiperf-agentx" / f"campaign-{build}-pd-aiperf" /
                                       "index.html").read_text())

    def test_preview_local_renders_only_completed_samples(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.config(root)
            campaign = root / "campaigns" / config["id"]
            campaign.mkdir(parents=True)
            (campaign / "campaign.json").write_text(json.dumps(config))
            (campaign / "summary.json").write_text(json.dumps({"status": "running"}))
            run_id = f"{config['id']}-baseline-aiperf"
            remote = Path(f"/workload/aiperf-agentx/{run_id}/c1")
            source = root / "completed-sample"
            source.mkdir()
            (source / "profile_export_aiperf.json").write_text(json.dumps({
                "request_throughput": {"avg": 2, "unit": "req/s"},
                "output_token_throughput": {"avg": 200, "unit": "tokens/s"}}))
            (source / "benchmark-metadata.json").write_text(json.dumps({
                "run_id": f"{run_id}-c1", "concurrency": 1,
                "source_kind": "llm-d", "source_ref": "main", "source_commit": "a" * 40}))
            copy_attempts = []

            def fake_kube(namespace, *args, **kwargs):
                if args[0] == "exec":
                    listing = (str(remote / "profile_export_aiperf.json") if "-type" in args and
                               args[args.index("-type") + 1] == "f" else str(remote))
                    return SimpleNamespace(stdout=listing + "\n")
                if args[0] == "cp":
                    filename = Path(args[2]).name
                    if filename == "profile_export_aiperf.json":
                        copy_attempts.append(args)
                        if len(copy_attempts) == 1:
                            return SimpleNamespace(returncode=1, stderr="temporary WebSocket disconnect")
                    if not (source / filename).is_file():
                        return SimpleNamespace(returncode=1, stderr="file not found")
                    runner.shutil.copyfile(source / filename, Path(args[2]))
                    return SimpleNamespace(returncode=0, stderr="")
                raise AssertionError(args)

            with patch.object(runner, "kube", side_effect=fake_kube), patch.object(runner.time, "sleep"):
                self.assertEqual(runner.preview_local(config, root), 0)
            self.assertEqual(len(copy_attempts), 2)
            page = (campaign / "preview/index.html").read_text()
            self.assertIn("<h1>Campaign test-campaign</h1>", page)
            self.assertNotIn("Disaggregated Serving — Interactivity vs Throughput</h1>", page)
            self.assertIn('<section class="campaign-overview">', page)
            self.assertIn("<strong>1 completed</strong>", page)
            self.assertIn("<summary>All configurations</summary>", page)
            self.assertIn("<summary>Run identity</summary>", page)
            self.assertIn("baseline", page)
            self.assertIn(config["vllm_image"], page)
            self.assertIn('id="root"', page)
            self.assertIn('id="campaign-progress"', page)
            self.assertIn("<td>baseline</td><td>aiperf</td><td>c1</td><td>completed</td>", page)
            self.assertIn("<td>baseline</td><td>aiperf</td><td>c4</td><td>pending</td>", page)

    def test_preview_local_shows_running_sample_before_first_result(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.config(root)
            config["benchmarks"].append({"tool": "nyann", "concurrencies": [1, 4],
                                          "duration_seconds": 600, "isl": 1024, "osl": 512,
                                          "warmup_seconds": 60})
            campaign = root / "campaigns" / config["id"]
            campaign.mkdir(parents=True)
            (campaign / "campaign.json").write_text(json.dumps(config))
            (campaign / "summary.json").write_text(json.dumps({"status": "running"}))
            remote = f"/workload/aiperf-agentx/{config['id']}-baseline-aiperf/c1"

            def fake_kube(namespace, *args, **kwargs):
                return SimpleNamespace(stdout="" if "-name" in args else remote + "\n")

            with patch.object(runner, "kube", side_effect=fake_kube):
                self.assertEqual(runner.preview_local(config, root, auto_refresh=True), 0)
            page = (campaign / "preview/index.html").read_text()
            self.assertIn("<td>baseline</td><td>aiperf</td><td>c1</td><td>running</td>", page)
            self.assertIn("<td>candidate</td><td>aiperf</td><td>c1</td><td>pending</td>", page)
            self.assertIn("<td>baseline</td><td>nyann</td><td>c1, c4</td><td>pending</td>", page)
            self.assertIn("location.reload()", page)

    def test_failed_campaign_keeps_last_partial_preview(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.config(root)
            campaign = root / "campaigns" / config["id"]
            preview = campaign / "preview"
            preview.mkdir(parents=True)
            (campaign / "summary.json").write_text(json.dumps({"status": "failed"}))
            (campaign / "index.html").write_text("minimal final report")
            (preview / "index.html").write_text("completed sample charts")
            self.assertEqual(runner.watch_preview_local(config, root), 1)
            self.assertEqual((preview / "index.html").read_text(), "completed sample charts")

    def test_monitoring_overlay_includes_partial_gpu_data_and_excludes_empty_samples(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runs = []
            for concurrency in (1, 8, 32, 64):
                sample = root / f"c{concurrency}"
                sample.mkdir()
                expr = "DCGM_FI_DEV_GPU_UTIL" if concurrency == 1 else "vllm:num_requests_running"
                values = [] if concurrency == 64 else [[0, "1"]]
                dashboard = (f'const panels = {{"1":{{"title":"Requests","unit":"req/s",'
                             f'"queries":[{{"expr":"{expr}","series":'
                             f'[{{"labels":{{}},"values":{json.dumps(values)}}}]}}]}}}};\n'
                             'const rows = [{"type":"panel","id":"1"}];\n')
                (sample / "dashboard.html").write_text(dashboard)
                runs.append({"directory": sample,
                             "metadata": {"run_id": f"sweep-c{concurrency}", "concurrency": concurrency},
                             "dashboard": None if concurrency == 1 else dashboard.encode()})
            self.assertIsNone(runner.AIPERF_REPORT.monitoring_overlay(root, runs[1::2], save_file=False))
            page = runner.AIPERF_REPORT.monitoring_overlay(root, runs, save_file=False).decode()
            labels = json.loads(re.search(r"const labels = (\[.*?\]);", page).group(1))
            self.assertEqual(labels, ["sweep / c1 (no vLLM metrics)", "sweep / c8", "sweep / c32"])
            self.assertIn("sweep / c1: no vLLM metrics were scraped", page)

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

    def test_build_steps_pin_ordered_actions_and_reject_invalid_recipes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.config(root)
            recipe = {"repo": "https://github.com/example/vllm.git", "steps": [
                {"ref": "base", "action": "checkout"},
                {"ref": "feature", "action": "merge"},
                {"ref": "patch", "action": "cherry-pick-m2"},
                {"ref": "parent", "action": "cherry-pick-parent1"},
            ]}
            config["overlays"][0]["build"] = recipe
            path = root / "config.json"
            path.write_text(json.dumps(config))
            runner.load_config(path)
            shas = [f"{n:x}" * 40 for n in range(1, 5)]
            calls = []

            def fake_call(args, **kwargs):
                calls.append(args)
                return SimpleNamespace(stdout="".join(f"{sha}\t{ref}\n" for sha, ref in zip(shas, args[-4:])))

            with patch.object(runner, "call", side_effect=fake_call):
                resolved = runner.resolve_build(recipe)
            self.assertEqual([step["action"] for step in resolved["steps"]],
                             ["checkout", "merge", "cherry-pick-m2", "cherry-pick-parent1"])
            self.assertEqual([step["commit"] for step in resolved["steps"]], shas)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][-3], "refs/heads/feature")
            recipe["steps"][0]["action"] = "merge"
            path.write_text(json.dumps(config))
            with self.assertRaisesRegex(ValueError, "action is invalid"):
                runner.load_config(path)

    def test_optional_deepep_branch_is_validated_and_pinned(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.config(root)
            config["build_repo"] = "https://github.com/example/vllm.git"
            config["builds"] = [{"name": "candidate", "steps": [{"ref": "base", "action": "checkout"}],
                                 "deepep": {"repo": "https://github.com/example/DeepEP.git", "ref": "fast-dispatch"}}]
            path = root / "config.json"
            path.write_text(json.dumps(config))
            runner.load_config(path)
            calls = []

            def fake_call(args, **kwargs):
                calls.append(args)
                self.assertEqual(args[:2], ["git", "ls-remote"])
                if any("DeepEP.git" in arg for arg in args):
                    return SimpleNamespace(stdout="d" * 40 + "\trefs/heads/fast-dispatch\n")
                return SimpleNamespace(stdout="b" * 40 + "\trefs/heads/base\n")

            with patch.object(runner, "call", side_effect=fake_call):
                pins = {}
                resolved = runner.resolve_build({"repo": config["build_repo"],
                                                 "steps": config["builds"][0]["steps"],
                                                 "deepep": config["builds"][0]["deepep"]}, pins)
                again = runner.resolve_build({"repo": config["build_repo"],
                                              "steps": config["builds"][0]["steps"],
                                              "deepep": config["builds"][0]["deepep"]}, pins)
            self.assertEqual(resolved["deepep"]["commit"], "d" * 40)
            self.assertEqual(again, resolved)
            self.assertEqual(len(calls), 2)
            config["builds"][0]["deepep"]["ref"] = "-invalid"
            path.write_text(json.dumps(config))
            with self.assertRaisesRegex(ValueError, "branch name"):
                runner.load_config(path)

    def test_nightly_build_needs_no_vllm_repo_or_git_resolution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.config(root)
            config["builds"] = [{"name": "nightly"}]
            path = root / "config.json"
            path.write_text(json.dumps(config))
            runner.load_config(path)
            del config["vllm_image"]
            path.write_text(json.dumps(config))
            with self.assertRaisesRegex(ValueError, "vllm_image"):
                runner.load_config(path)
            config["vllm_image"] = "vllm/example:nightly"
            with patch.object(runner, "call", side_effect=AssertionError("unexpected Git call")):
                self.assertEqual(runner.resolve_build({}), {"mode": "nightly", "steps": []})
            config["builds"] = [{"name": "nightly", "steps": []}]
            path.write_text(json.dumps(config))
            runner.load_config(path)
            config["builds"] = [{"name": "nightly-deepep", "deepep": {
                "repo": "https://github.com/example/DeepEP.git", "ref": "feature"}}]
            path.write_text(json.dumps(config))
            runner.load_config(path)

    def test_monitoring_requires_explicit_endpoint_secret_and_dashboard(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.config(root)
            config["monitoring"] = {"grafana_url": "http://llmd-grafana.vllm.svc.cluster.local",
                                    "auth_secret": "llmd-grafana", "dashboard_uid": "wideep-overview"}
            path = root / "config.json"
            path.write_text(json.dumps(config))
            runner.load_config(path)
            del config["monitoring"]["auth_secret"]
            path.write_text(json.dumps(config))
            with self.assertRaisesRegex(ValueError, "auth_secret"):
                runner.load_config(path)
            config["monitoring"]["auth_secret"] = "llmd-grafana"
            config["monitoring"]["grafana_url"] = "https://user:password@example.com"
            path.write_text(json.dumps(config))
            with self.assertRaisesRegex(ValueError, "without credentials"):
                runner.load_config(path)

    def test_campaign_passes_explicit_model_api_url_to_benchmarks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.config(root)
            config["base_url"] = "http://wide-ep-epp.vllm.svc.cluster.local/v1"
            path = root / "config.json"
            path.write_text(json.dumps(config))
            runner.load_config(path)
            config["base_url"] = "http://user:password@wide-ep-epp.vllm/v1"
            path.write_text(json.dumps(config))
            with self.assertRaisesRegex(ValueError, "base_url"):
                runner.load_config(path)
            config["base_url"] = "http://wide-ep-epp.vllm.svc.cluster.local/v1"

            def capture_call(args, **kwargs):
                self.assertEqual(kwargs["env"]["BASE_URL"], config["base_url"])
                raise RuntimeError("URL passed to submitter")

            with patch.object(runner, "call", side_effect=capture_call):
                with self.assertRaisesRegex(RuntimeError, "URL passed to submitter"):
                    runner.submit_benchmark(config, config["overlays"][0], config["benchmarks"][0],
                                            root, [], None, "a" * 40)

    def test_campaign_monitoring_uses_existing_exporter_and_secret_env(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.config(root)
            config["monitoring"] = {"grafana_url": "http://llmd-grafana.vllm.svc.cluster.local",
                                    "auth_secret": "llmd-grafana", "dashboard_uid": "wideep-overview"}
            artifact = root / "aiperf"
            for sample in ("c1", "c4"):
                (artifact / sample).mkdir(parents=True)
            commands = []

            def fake_call(args, **kwargs):
                commands.append((args, kwargs))
                for sample in ("c1", "c4"):
                    (artifact / sample / "dashboard.html").write_text("dashboard")
                return SimpleNamespace(stdout="exported\n")

            with patch.dict(os.environ, {"CAMPAIGN_GRAFANA_USER": "admin",
                                      "CAMPAIGN_GRAFANA_PASSWORD": "private-password"}), \
                 patch.object(runner, "kube", return_value=SimpleNamespace(stdout="2026-10-06T00:00:00Z benchmark\n")), \
                 patch.object(runner, "call", side_effect=fake_call):
                runner.export_campaign_monitoring(config, artifact, "aiperf-job", ["pod-one:uid", "pod-two:uid"],
                                                  [{"sample": "c1"}, {"sample": "c4"}])
            command, kwargs = commands[0]
            self.assertIn("--auth-env", command)
            self.assertNotIn("private-password", " ".join(command))
            self.assertEqual(kwargs["env"]["CAMPAIGN_GRAFANA_AUTH"], "admin:private-password")
            self.assertIn("pod\\-one|pod\\-two", command)
            self.assertEqual((artifact / "grafana-export.log").read_text(), "exported\n")

    def test_nightly_script_uses_image_without_build_inputs(self):
        script = MODULE_PATH.parents[1] / "campaign/vllm-wheel-build.sh"
        result = subprocess.run(["bash", "-c", 'source "$1"', "bash", str(script)],
                                capture_output=True, text=True,
                                env={**os.environ, "VLLM_BUILD_MODE": "nightly", "DEEPEP_BUILD_ENABLED": "0"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Using vLLM from the runtime image", result.stdout)
        self.assertNotIn("VLLM_BUILD_REF is not set", result.stderr)

    def test_full_build_script_keeps_publisher_commands(self):
        script = MODULE_PATH.parents[1] / "campaign/vllm-wheel-build.sh"
        deepep_script = MODULE_PATH.parents[1] / "campaign/deepep-wheel-build.sh"
        syntax = subprocess.run(["bash", "-n", str(script), str(deepep_script)], capture_output=True, text=True)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        invalid_deepep = subprocess.run(["bash", str(deepep_script)], capture_output=True, text=True,
                                        env={**os.environ, "DEEPEP_BUILD_REPO": "https://github.com/example/DeepEP.git",
                                             "DEEPEP_BUILD_REF": "fast-dispatch", "DEEPEP_BUILD_COMMIT": "bad"})
        self.assertNotEqual(invalid_deepep.returncode, 0)
        self.assertIn("full pinned commit", invalid_deepep.stderr + invalid_deepep.stdout)
        bad_recipe = subprocess.run(["bash", str(script), "publish"], capture_output=True, text=True,
                                    env={**os.environ, "VLLM_BUILD_REFS": "base patch", "VLLM_BUILD_ACTIONS": "checkout"})
        self.assertNotEqual(bad_recipe.returncode, 0)
        self.assertIn("matching lengths", bad_recipe.stderr + bad_recipe.stdout)
        deploy = subprocess.run(["bash", str(script), "publish-and-deploy"], capture_output=True, text=True,
                                env={**os.environ, "VLLM_BUILD_OVERLAY": "/tmp/example"})
        self.assertNotEqual(deploy.returncode, 0)
        self.assertIn("VLLM_BUILD_REF_FILE", deploy.stderr + deploy.stdout)

    def test_optional_deepep_reuses_ready_wheel(self):
        script = MODULE_PATH.parents[1] / "campaign/deepep-wheel-build.sh"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, ref, commit = "https://github.com/example/DeepEP.git", "fast-dispatch", "d" * 40
            key = ("variant=v10-pinned-abi-locked\n" + f"repo={repo}\nref={ref}\nsha={commit}\n" +
                   "base_image=sha256:test\ntorch=2.8\ncuda=12.8\npython_abi=cp312\n")
            digest = hashlib.sha256(key.encode()).hexdigest()[:20]
            cache = root / f"{digest}-v10-pinned-abi-locked"
            wheel = cache / "wheel" / "deep_ep-1.0-py3-none-any.whl"
            wheel.parent.mkdir(parents=True)
            wheel.write_bytes(b"test wheel")
            (cache / "READY").write_text(f"wheel={wheel.name}\ncommit={commit}\n")
            env = {**os.environ, "DEEPEP_BUILD_REPO": repo, "DEEPEP_BUILD_REF": ref,
                   "DEEPEP_BUILD_COMMIT": commit, "DEEPEP_BUILD_CACHE_ROOT": str(root),
                   "BASE_RUNTIME_IMAGE_ID": "sha256:test", "BASE_RUNTIME_TORCH_VERSION": "2.8",
                   "BASE_RUNTIME_CUDA_VERSION": "12.8", "BASE_RUNTIME_PYTHON_ABI": "cp312"}
            command = ('set -euo pipefail; BUILD_LEADER=0; '
                       'wheel_is_valid(){ test -f "$1"; }; uv(){ return 0; }; python3(){ return 0; }; '
                       'source "$1"')
            result = subprocess.run(["bash", "-c", command, "bash", str(script)],
                                    capture_output=True, text=True, env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Installing cached DeepEP", result.stdout)
            self.assertNotIn("Building DeepEP", result.stdout)

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

    def test_vllm_build_script_is_bundled_and_prewarms_shared_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.config(root)
            pod = {"serviceAccountName": "kimi-k3", "nodeSelector": {"gpu": "h200"},
                   "volumes": [{"name": "legacy-build", "configMap": {"name": "legacy-build"}},
                               {"name": "build-cache", "persistentVolumeClaim": {"claimName": "kimi-cache"}}],
                   "containers": [{"name": "vllm", "image": "vllm/example@sha256:abc", "resources": {"requests": {"nvidia.com/gpu": "8"}},
                                   "args": ["source /opt/build-scripts/legacy-build.sh"],
                                   "env": [{"name": "VLLM_BUILD_ROLE", "value": "prefill"},
                                           {"name": "VLLM_BUILD_REF", "valueFrom": {"configMapKeyRef": {"name": "vllm-build-ref", "key": "VLLM_BUILD_REF"}}},
                                           {"name": "VLLM_BUILD_COMMIT", "valueFrom": {"configMapKeyRef": {"name": "vllm-build-ref", "key": "VLLM_BUILD_COMMIT"}}},
                                           {"name": "VLLM_BUILD_BASE_IMAGE_ID", "value": "sha256:abc"}],
                                   "volumeMounts": [{"name": "legacy-build", "mountPath": "/opt/build-scripts"},
                                                    {"name": "build-cache", "mountPath": "/shared/vllm-build"}]}]}
            manifest = "\n---\n".join([
                yaml.safe_dump({"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "legacy-build"},
                                "data": {"legacy-build.sh": "# VLLM_BUILD_COMMIT; BUILD_VARIANT=x; /shared/vllm-build"}}),
                yaml.safe_dump({"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "vllm-build-ref"},
                                "data": {"VLLM_BUILD_REF": "benchmark/ref", "VLLM_BUILD_COMMIT": "a" * 40}}),
                yaml.safe_dump({"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": "kimi-k3"}}),
                yaml.safe_dump({"apiVersion": "leaderworkerset.x-k8s.io/v1", "kind": "LeaderWorkerSet",
                                "metadata": {"name": "prefill"}, "spec": {"leaderWorkerTemplate": {"workerTemplate": {"spec": pod}}}})])
            created = []
            actions = []

            def fake_kube(namespace, *args, **kwargs):
                actions.append(args[0])
                if args[0] == "create":
                    created.append(json.loads(kwargs["input_text"]))
                return SimpleNamespace(stdout="cache hit\n" if args[0] == "logs" else "", stderr="", returncode=0)

            build = {"repo": "https://github.com/example/vllm.git", "steps": [
                {"ref": "base", "action": "checkout", "commit": "b" * 40},
                {"ref": "feature", "action": "cherry-pick", "commit": "c" * 40},
            ], "deepep": {"repo": "https://github.com/example/DeepEP.git",
                            "ref": "fast-dispatch", "commit": "d" * 40}}
            rendered = runner.inject_vllm_build_script(manifest, build)
            rendered_docs = list(yaml.safe_load_all(rendered))
            self.assertEqual(rendered_docs[0]["metadata"]["name"], "vllm-build")
            self.assertEqual(rendered_docs[0]["data"]["vllm-wheel-build.sh"],
                             (MODULE_PATH.parents[1] / "campaign/vllm-wheel-build.sh").read_text())
            self.assertEqual(rendered_docs[0]["data"]["deepep-wheel-build.sh"],
                             (MODULE_PATH.parents[1] / "campaign/deepep-wheel-build.sh").read_text())
            self.assertEqual(rendered_docs[-1]["spec"]["leaderWorkerTemplate"]["workerTemplate"]["spec"]["containers"][0]["args"],
                             ["source /opt/build-scripts/vllm-wheel-build.sh"])
            self.assertEqual(rendered_docs[1]["data"]["VLLM_BUILD_ACTIONS"], "checkout cherry-pick")
            self.assertEqual(rendered_docs[1]["data"]["VLLM_BUILD_SHAS"], " ".join(["b" * 40, "c" * 40]))
            self.assertEqual(rendered_docs[1]["data"]["VLLM_BUILD_COMMIT"], "b" * 40)
            self.assertEqual(rendered_docs[1]["data"]["DEEPEP_BUILD_COMMIT"], "d" * 40)
            self.assertEqual(rendered_docs[1]["data"]["DEEPEP_BUILD_ENABLED"], "1")
            with patch.object(runner, "kube", side_effect=AssertionError("Kubernetes must not be contacted")):
                self.assertEqual(runner.vllm_prebuild(rendered, config, config["overlays"][0], root,
                                                       preview_only=True), "b" * 40)
            self.assertEqual(yaml.safe_load((root / "prebuild-job.yaml").read_text())["kind"], "Job")
            with patch.object(runner, "kube", side_effect=fake_kube):
                commit = runner.vllm_prebuild(rendered, config, config["overlays"][0], root)
            self.assertEqual(commit, "b" * 40)
            self.assertEqual(actions, ["create", "create", "create", "create", "wait", "logs",
                                       "delete", "delete", "delete", "delete"])
            self.assertEqual([item["kind"] for item in created], ["ConfigMap", "ConfigMap", "ServiceAccount", "Job"])
            job_pod = created[-1]["spec"]["template"]["spec"]
            self.assertEqual(job_pod["serviceAccountName"], "kimi-k3")
            self.assertEqual(job_pod["nodeSelector"], {"gpu": "h200"})
            self.assertEqual(job_pod["containers"][0]["image"], "vllm/example@sha256:abc")
            self.assertEqual(job_pod["containers"][0]["resources"]["requests"]["nvidia.com/gpu"], "8")
            self.assertIn("VLLM_BUILD_ACTIONS", {item["name"] for item in job_pod["containers"][0]["env"]})
            self.assertIn("DEEPEP_BUILD_COMMIT", {item["name"] for item in job_pod["containers"][0]["env"]})
            self.assertEqual((root / "build.log").read_text(), "cache hit\n")

            selected = runner.apply_vllm_image(manifest, "quay.io/example/vllm:nightly")
            selected_docs = list(yaml.safe_load_all(runner.inject_vllm_build_script(
                selected, {"mode": "nightly", "steps": []})))
            selected_container = selected_docs[-1]["spec"]["leaderWorkerTemplate"]["workerTemplate"]["spec"]["containers"][0]
            self.assertEqual(selected_container["image"], "quay.io/example/vllm:nightly")
            self.assertEqual(next(e["value"] for e in selected_container["env"]
                                  if e["name"] == "VLLM_BUILD_BASE_IMAGE_ID"), "quay.io/example/vllm:nightly")

            nightly = runner.inject_vllm_build_script(manifest, {"mode": "nightly", "steps": []})
            nightly_docs = list(yaml.safe_load_all(nightly))
            self.assertNotIn("VLLM_BUILD_COMMIT", nightly_docs[1]["data"])
            nightly_env = {item["name"]: item for item in nightly_docs[-1]["spec"]["leaderWorkerTemplate"]
                           ["workerTemplate"]["spec"]["containers"][0]["env"]}
            self.assertEqual(nightly_env["VLLM_BUILD_MODE"]["value"], "nightly")
            self.assertNotIn("VLLM_BUILD_REF", nightly_env)
            self.assertNotIn("VLLM_BUILD_COMMIT", nightly_env)
            self.assertIsNone(runner.vllm_prebuild(nightly, config, config["overlays"][0], root))

            nightly_deepep = runner.inject_vllm_build_script(manifest, {
                "mode": "nightly", "steps": [], "deepep": build["deepep"]})
            with patch.object(runner, "kube", side_effect=fake_kube):
                self.assertEqual(runner.vllm_prebuild(nightly_deepep, config, config["overlays"][0], root),
                                 "nightly")

    def test_disaggregatedset_image_and_generic_source_build(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.config(root)
            roles = []
            for role in ("prefill", "decode"):
                roles.append({"name": role, "spec": {"leaderWorkerTemplate": {"workerTemplate": {"spec": {
                    "serviceAccountName": "glm", "containers": [
                        {"name": "vllm", "image": "old:tag", "command": ["/bin/bash", "-c"],
                         "args": ["exec vllm serve model"], "resources": {"requests": {"nvidia.com/gpu": "8"}}},
                        {"name": "sidecar", "image": "router:tag"}],
                }}}}})
            manifest = yaml.safe_dump({"apiVersion": "disaggregatedset.x-k8s.io/v1",
                                       "kind": "DisaggregatedSet", "metadata": {"name": "glm", "namespace": "vllm"},
                                       "spec": {"roles": roles}})
            selected = runner.apply_vllm_image(manifest, "vllm/nightly:latest")
            selected_docs = list(yaml.safe_load_all(selected))
            selected_pods = list(runner.worker_pods(selected_docs))
            self.assertEqual(len(selected_pods), 2)
            for _, pod in selected_pods:
                self.assertEqual(pod["containers"][0]["image"], "vllm/nightly:latest")
                self.assertEqual(pod["containers"][1]["image"], "router:tag")
            tuned = runner.apply_vllm_cli_args(selected, {"prefill": ["-cc.cudagraph_mode=NONE"]})
            tuned_pods = dict(runner.worker_pods(list(yaml.safe_load_all(tuned))))
            self.assertEqual(tuned_pods["prefill"]["containers"][0]["args"],
                             ["exec vllm serve model -cc.cudagraph_mode=NONE"])
            self.assertEqual(tuned_pods["decode"]["containers"][0]["args"], ["exec vllm serve model"])
            with self.assertRaisesRegex(ValueError, "roles missing"):
                runner.apply_vllm_cli_args(selected, {"unknown": ["--enforce-eager"]})
            env_tuned = runner.apply_vllm_env(selected, {
                "prefill": {"VLLM_SERVER_DEV_MODE": "1"},
                "decode": {"VLLM_SERVER_DEV_MODE": "1"},
            })
            for _, pod in runner.worker_pods(list(yaml.safe_load_all(env_tuned))):
                self.assertIn({"name": "VLLM_SERVER_DEV_MODE", "value": "1"}, pod["containers"][0]["env"])
            with self.assertRaisesRegex(ValueError, "roles missing"):
                runner.apply_vllm_env(selected, {"unknown": {"VLLM_SERVER_DEV_MODE": "1"}})
            nightly = runner.inject_vllm_build_script(selected, {"mode": "nightly", "steps": []})
            self.assertEqual(len(list(yaml.safe_load_all(nightly))), 1)

            build = {"mode": "source", "repo": "https://github.com/vllm-project/vllm.git",
                     "steps": [{"ref": "main", "action": "checkout", "commit": "a" * 40}]}
            rendered = runner.inject_vllm_build_script(selected, build, "results")
            documents = list(yaml.safe_load_all(rendered))
            runner.validate_manifest(rendered, "vllm")
            self.assertEqual({item["metadata"]["name"] for item in documents[1:]},
                             {"vllm-build", "vllm-build-ref"})
            for role, pod in runner.worker_pods(documents):
                self.assertIn(role, {"prefill", "decode"})
                self.assertEqual(next(volume for volume in pod["volumes"] if volume["name"] == "build-cache")
                                 ["persistentVolumeClaim"]["claimName"], "results")
                serving = pod["containers"][0]
                self.assertTrue(serving["args"][0].startswith("source /opt/build-scripts/vllm-wheel-build.sh\n"))
                self.assertEqual(serving["image"], "vllm/nightly:latest")
            with patch.object(runner, "kube", side_effect=AssertionError("Kubernetes must not be contacted")):
                self.assertEqual(runner.vllm_prebuild(rendered, config, config["overlays"][0], root,
                                                       preview_only=True), "a" * 40)
            job = yaml.safe_load((root / "prebuild-job.yaml").read_text())
            self.assertEqual(job["spec"]["template"]["spec"]["containers"][0]["image"], "vllm/nightly:latest")
            self.assertIn({"name": "VLLM_BUILD_PREBUILD", "value": "1"},
                          job["spec"]["template"]["spec"]["containers"][0]["env"])
            deepep_only = {"mode": "nightly", "steps": [],
                           "deepep": {"repo": "https://github.com/deepseek-ai/DeepEP.git",
                                      "ref": "main", "commit": "b" * 40}}
            deepep_rendered = runner.inject_vllm_build_script(selected, deepep_only, "results")
            self.assertEqual(runner.vllm_prebuild(deepep_rendered, config, config["overlays"][0], root,
                                                   preview_only=True), "nightly")
            self.assertEqual(next(item for item in yaml.safe_load_all(deepep_rendered)
                                  if item["kind"] == "ConfigMap" and item["metadata"]["name"] == "vllm-build-ref")
                             ["data"]["DEEPEP_BUILD_COMMIT"], "b" * 40)

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
            for script, args in (("live-aiperf/submit.sh", ["1", "900"]),
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

            config["monitoring"] = {"grafana_url": "http://llmd-grafana.vllm.svc.cluster.local",
                                    "auth_secret": "llmd-grafana", "dashboard_uid": "wideep-overview"}
            created.clear()
            with patch.object(runner, "kube", side_effect=fake_kube):
                runner.submit(config, "registry.example/campaign:sha", "benchmark-campaign")
            environment = created[1]["spec"]["template"]["spec"]["containers"][0]["env"]
            self.assertEqual(environment[0]["valueFrom"]["secretKeyRef"],
                             {"name": "llmd-grafana", "key": "admin-user"})

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
                 patch.object(runner, "apply_vllm_image", side_effect=lambda rendered, image: rendered), \
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
