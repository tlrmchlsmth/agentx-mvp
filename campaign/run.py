#!/usr/bin/env python3
"""Run a sequence of Kustomize overlays and benchmark each deployed model."""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import tempfile
from urllib.parse import urlsplit
from datetime import datetime, timezone
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
NAME = re.compile(r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$")
JOB_LINE = re.compile(r"^Job queued: ([a-z0-9-]+) ", re.MULTILINE)
GIT_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")


class CleanupError(RuntimeError):
    """A running child workload or deployment might still exist."""


def call(args: list[str], *, input_text: str | None = None, env: dict[str, str] | None = None,
         check: bool = True, timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, input=input_text, text=True, capture_output=True, env=env, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(f"{' '.join(args)} failed ({result.returncode}): {result.stderr.strip() or result.stdout.strip()}")
    return result


def required_name(value: Any, field: str) -> str:
    if not isinstance(value, str) or len(value) > 63 or not NAME.fullmatch(value):
        raise ValueError(f"{field} must be a Kubernetes name (at most 63 characters)")
    return value


def bounded_int(value: Any, field: str, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{field} must be an integer from {low} to {high}")
    return value


def load_config(path: Path) -> dict[str, Any]:
    config = json.loads(path.read_text())
    if not isinstance(config, dict):
        raise ValueError("campaign must be a JSON object")
    allowed = {"id", "namespace", "source", "results_pvc", "benchmark_queue",
               "campaign_queue", "overlays", "benchmarks", "rollout_timeout_seconds",
               "admission_timeout_seconds", "cleanup_timeout_seconds", "continue_on_failure"}
    if set(config) - allowed:
        raise ValueError(f"unknown campaign fields: {sorted(set(config) - allowed)}")
    for key in ("id", "namespace", "results_pvc"):
        required_name(config.get(key), key)
    for key in ("benchmark_queue", "campaign_queue"):
        required_name(config.get(key), key)
    source = config.get("source")
    if not isinstance(source, dict) or set(source) != {"repo", "ref"}:
        raise ValueError("source needs repo and ref")
    repo, ref = source["repo"], source["ref"]
    if not isinstance(repo, str):
        raise ValueError("source.repo must be an HTTPS Git URL")
    parsed = urlsplit(repo)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("source.repo must be an HTTPS Git URL without credentials")
    if not isinstance(ref, str) or not GIT_REF.fullmatch(ref) or ".." in ref or ref.endswith(".lock"):
        raise ValueError("source.ref must be a branch, tag, or commit")
    for key, default, low, high in (("rollout_timeout_seconds", 3600, 60, 21600),
                                    ("admission_timeout_seconds", 3600, 60, 21600),
                                    ("cleanup_timeout_seconds", 600, 60, 3600)):
        config[key] = bounded_int(config.get(key, default), key, low, high)
    if type(config.get("continue_on_failure", False)) is not bool:
        raise ValueError("continue_on_failure must be a boolean")
    config.setdefault("continue_on_failure", False)
    overlays = config.get("overlays")
    benchmarks = config.get("benchmarks")
    if not isinstance(overlays, list) or not overlays or len(overlays) > 32:
        raise ValueError("overlays must contain 1-32 entries")
    if not isinstance(benchmarks, list) or not benchmarks or len(benchmarks) > 4:
        raise ValueError("benchmarks must contain 1-4 entries")
    seen = set()
    for overlay in overlays:
        if not isinstance(overlay, dict) or set(overlay) != {"name", "path", "model_label", "pod_selector", "expected_pods"}:
            raise ValueError("each overlay needs name, path, model_label, pod_selector, expected_pods")
        name = required_name(overlay["name"], "overlay.name")
        if name in seen:
            raise ValueError(f"duplicate overlay: {name}")
        seen.add(name)
        path = overlay["path"]
        if not isinstance(path, str) or not path or Path(path).is_absolute() or ".." in Path(path).parts:
            raise ValueError(f"overlay {name} path must be relative to the llm-d repository")
        for key in ("model_label", "pod_selector"):
            if not isinstance(overlay[key], str) or not overlay[key].strip() or "\n" in overlay[key]:
                raise ValueError(f"overlay {name} requires {key}")
        bounded_int(overlay["expected_pods"], "expected_pods", 1, 1000)
    tools = set()
    for bench in benchmarks:
        if not isinstance(bench, dict):
            raise ValueError("benchmark entries must be objects")
        tool = bench.get("tool")
        fields = {"tool", "concurrencies", "duration_seconds"}
        if tool == "nyann":
            fields |= {"isl", "osl", "warmup_seconds"}
        elif tool != "aiperf":
            raise ValueError("tool must be aiperf or nyann")
        if set(bench) != fields or tool in tools:
            raise ValueError(f"{tool}: missing/extra fields or duplicate tool")
        tools.add(tool)
        values = bench["concurrencies"]
        if not isinstance(values, list) or not values or len(values) > 32:
            raise ValueError("concurrencies must contain 1-32 values")
        for value in values:
            bounded_int(value, "concurrency", 1, 2048 if tool == "aiperf" else 16384)
        bounded_int(bench["duration_seconds"], "duration_seconds", 60 if tool == "aiperf" else 1, 7200)
        if tool == "nyann":
            for key in ("isl", "osl"):
                bounded_int(bench[key], key, 1, 1000000)
            bounded_int(bench["warmup_seconds"], "warmup_seconds", 0, 3600)
    if len("campaign-" + config["id"]) > 63:
        raise ValueError("campaign ID is too long for Kubernetes Job/ConfigMap names")
    for overlay in overlays:
        if any(len(f"{config['id']}-{overlay['name']}-{tool}") > 120 for tool in tools):
            raise ValueError("campaign/overlay names produce a run ID longer than 120 characters")
    return config


def fetch_source(source: dict[str, str]) -> tuple[Path, str]:
    """Resolve one fork/ref once, so every overlay uses the same exact commit."""
    checkout = Path(tempfile.mkdtemp(prefix="llmd-campaign-"))
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        call(["git", "init", "-q", str(checkout)], env=env, timeout=60)
        call(["git", "-C", str(checkout), "remote", "add", "origin", source["repo"]], env=env, timeout=60)
        call(["git", "-C", str(checkout), "fetch", "--depth", "1", "origin", source["ref"]], env=env, timeout=600)
        call(["git", "-C", str(checkout), "checkout", "-q", "--detach", "FETCH_HEAD"], env=env, timeout=120)
        commit = call(["git", "-C", str(checkout), "rev-parse", "HEAD"], env=env, timeout=60).stdout.strip()
        if not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise RuntimeError("could not resolve source commit")
        return checkout, commit
    except Exception:
        shutil.rmtree(checkout)
        raise


def validate_manifest(rendered: str, namespace: str) -> None:
    cluster_scoped = {"Namespace", "CustomResourceDefinition", "ClusterRole", "ClusterRoleBinding",
                      "StorageClass", "PersistentVolume", "Node", "ResourceFlavor", "ClusterQueue"}
    documents = list(yaml.safe_load_all(rendered))
    if not documents:
        raise ValueError("overlay rendered no resources")
    for item in documents:
        if not isinstance(item, dict) or not item.get("kind") or not isinstance(item.get("metadata"), dict):
            raise ValueError("overlay contains a malformed resource")
        kind = item["kind"]
        if kind in cluster_scoped:
            raise ValueError(f"cluster-scoped {kind} is not allowed in a campaign overlay")
        metadata = item["metadata"]
        if not metadata.get("name") or metadata.get("namespace", namespace) != namespace:
            raise ValueError(f"{kind} must have a name and stay in namespace {namespace}")


def kube(namespace: str, *args: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
    return call(["kubectl", "-n", namespace, *args], **kwargs)


def snapshot(namespace: str, selector: str) -> list[str]:
    data = json.loads(kube(namespace, "get", "pods", "-l", selector, "-o", "json").stdout)
    return sorted(f"{pod['metadata']['name']}:{pod['metadata']['uid']}" for pod in data["items"])


def build_commit(namespace: str) -> str:
    data = json.loads(kube(namespace, "get", "configmap", "vllm-build-ref", "-o", "json").stdout)
    commit = data.get("data", {}).get("VLLM_BUILD_COMMIT", "")
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise RuntimeError("vllm-build-ref has no valid commit")
    return commit


def wait_ready(config: dict[str, Any], overlay: dict[str, Any]) -> list[str]:
    deadline = time.monotonic() + config["rollout_timeout_seconds"]
    namespace, selector = config["namespace"], overlay["pod_selector"]
    while time.monotonic() < deadline:
        pods = json.loads(kube(namespace, "get", "pods", "-l", selector, "-o", "json").stdout)["items"]
        if len(pods) == overlay["expected_pods"] and all(
            pod.get("metadata", {}).get("labels", {}).get("llm-d.ai/model") == overlay["model_label"] and
            pod.get("status", {}).get("phase") == "Running" and any(
                condition.get("type") == "Ready" and condition.get("status") == "True"
                for condition in pod.get("status", {}).get("conditions", [])) for pod in pods
        ):
            build_commit(namespace)
            return snapshot(namespace, selector)
        time.sleep(10)
    raise TimeoutError(f"{overlay['name']}: expected {overlay['expected_pods']} ready Pods for {selector}")


def wait_gone(config: dict[str, Any], selector: str) -> None:
    deadline = time.monotonic() + config["cleanup_timeout_seconds"]
    while time.monotonic() < deadline:
        if not snapshot(config["namespace"], selector):
            return
        time.sleep(5)
    raise TimeoutError(f"serving Pods still exist after cleanup: {selector}")


def wait_job(config: dict[str, Any], name: str, timeout: int) -> None:
    deadline = time.monotonic() + timeout
    namespace = config["namespace"]
    print(f"Waiting for benchmark Job {namespace}/{name}", flush=True)
    while time.monotonic() < deadline:
        job = json.loads(kube(namespace, "get", "job", name, "-o", "json").stdout)
        conditions = {item["type"]: item["status"] for item in job.get("status", {}).get("conditions", [])}
        if conditions.get("Complete") == "True":
            return
        if conditions.get("Failed") == "True":
            raise RuntimeError(f"benchmark Job {name} failed; inspect kubectl logs -n {namespace} job/{name}")
        time.sleep(10)
    raise TimeoutError(f"benchmark Job {name} exceeded admission/runtime timeout")


def submit_benchmark(config: dict[str, Any], overlay: dict[str, Any], bench: dict[str, Any],
                     campaign_dir: Path, baseline: list[str], commit: str) -> dict[str, Any]:
    tool = bench["tool"]
    run_id = f"{config['id']}-{overlay['name']}-{tool}"
    if len(run_id) > 120:
        raise ValueError("campaign/overlay names produce a run ID longer than 120 characters")
    env = os.environ.copy()
    env.update({"MODEL_LABEL": overlay["model_label"], "RESULTS_PVC": config["results_pvc"],
                "LIVE_BENCHMARK_QUEUE": config["benchmark_queue"],
                "LIVE_AIPERF_NAMESPACE": config["namespace"], "LIVE_NYANN_NAMESPACE": config["namespace"],
                "LIVE_AIPERF_RUN_ID": run_id, "LIVE_NYANN_RUN_ID": run_id})
    concurrencies = ",".join(str(value) for value in bench["concurrencies"])
    if tool == "aiperf":
        cmd = ["bash", str(ROOT / "live-aiperf/submit.sh"), concurrencies, str(bench["duration_seconds"])]
    else:
        cmd = ["bash", str(ROOT / "live-nyann/submit.sh"), concurrencies,
               str(bench["isl"]), str(bench["osl"]), str(bench["duration_seconds"]),
               str(bench["warmup_seconds"])]
    print(f"Submitting {tool} sweep for {overlay['name']}: {concurrencies}", flush=True)
    output = call(cmd, env=env).stdout
    (campaign_dir / f"{tool}-submit.log").write_text(output)
    match = JOB_LINE.search(output)
    if not match:
        raise RuntimeError(f"{tool} submit did not return a Job name")
    job_name = match.group(1)
    job = json.loads(kube(config["namespace"], "get", "job", job_name, "-o", "json").stdout)
    actual_id = job["metadata"].get("annotations", {}).get("benchmark.llm-d.ai/run-id")
    if actual_id != run_id:
        raise RuntimeError(f"{job_name} has unexpected run ID {actual_id!r}")
    runtime = len(bench["concurrencies"]) * bench["duration_seconds"] + 7200
    if tool == "nyann":
        runtime += bench["warmup_seconds"]
    try:
        wait_job(config, job_name, config["admission_timeout_seconds"] + runtime)
    except Exception as failure:
        deleted = kube(config["namespace"], "delete", "job", job_name, "--ignore-not-found",
                       "--cascade=foreground", "--wait=true",
                       f"--timeout={config['cleanup_timeout_seconds']}s", check=False)
        if deleted.returncode:
            raise CleanupError(f"could not remove child Job {job_name}: {deleted.stderr.strip()}") from failure
        raise
    if snapshot(config["namespace"], overlay["pod_selector"]) != baseline or build_commit(config["namespace"]) != commit:
        raise RuntimeError(f"{overlay['name']}: serving deployment changed during {tool} benchmark")
    artifact = f"/workload/{'aiperf-agentx' if tool == 'aiperf' else 'nyann-agentx'}/{run_id}"
    if not Path(artifact).exists():
        raise RuntimeError(f"{job_name} completed without expected artifacts: {artifact}")
    measurements = []
    if tool == "aiperf":
        for directory in sorted(Path(artifact).iterdir()):
            profile_path = directory / "profile_export_aiperf.json"
            if not profile_path.is_file():
                continue
            profile = json.loads(profile_path.read_text())
            metadata = json.loads((directory / "benchmark-metadata.json").read_text())
            metrics = {}
            for key in ("request_throughput", "output_token_throughput", "time_to_first_token", "inter_token_latency"):
                value = profile.get(key, {})
                if isinstance(value, dict):
                    metrics[key] = {field: value[field] for field in ("avg", "p90", "unit") if field in value}
            measurements.append({"concurrency": metadata["concurrency"], "sample": directory.name, "metrics": metrics})
        if len(measurements) != len(bench["concurrencies"]):
            raise RuntimeError(f"{job_name} completed with {len(measurements)} profiles, expected {len(bench['concurrencies'])}")
    return {"tool": tool, "job": job_name, "run_id": run_id, "artifacts": artifact,
            "report": f"{artifact}/index.html" if tool == "aiperf" else None,
            "measurements": measurements, "status": "completed"}


def write_summary(destination: Path, summary: dict[str, Any]) -> None:
    tmp = destination / "summary.json.tmp"
    tmp.write_text(json.dumps(summary, indent=2) + "\n")
    tmp.replace(destination / "summary.json")
    rows = []
    for overlay in summary["overlays"]:
        for bench in overlay.get("benchmarks", []):
            rows.append("<tr>" + "".join(f"<td>{html.escape(str(value or ''))}</td>" for value in (
                overlay["name"], bench["tool"], bench["status"], bench.get("job"),
                bench.get("artifacts"), bench.get("error"))) + "</tr>")
        if not overlay.get("benchmarks"):
            rows.append(f"<tr><td>{html.escape(overlay['name'])}</td><td></td><td>{html.escape(overlay['status'])}</td><td colspan=3>{html.escape(overlay.get('error', ''))}</td></tr>")
    comparisons = []
    for overlay in summary["overlays"]:
        for bench in overlay.get("benchmarks", []):
            for measurement in bench.get("measurements", []):
                metrics = measurement["metrics"]
                values = [overlay["name"], measurement["sample"],
                          metrics.get("request_throughput", {}).get("avg", ""),
                          metrics.get("output_token_throughput", {}).get("avg", ""),
                          metrics.get("time_to_first_token", {}).get("p90", ""),
                          metrics.get("inter_token_latency", {}).get("p90", "")]
                comparisons.append("<tr>" + "".join(f"<td>{html.escape(str(value))}</td>" for value in values) + "</tr>")
    page = "<!doctype html><meta charset='utf-8'><title>Benchmark campaign</title>" + \
        "<style>body{font:14px system-ui;margin:2rem}table{border-collapse:collapse}th,td{border:1px solid #aaa;padding:.5rem;text-align:left}</style>" + \
        f"<h1>Campaign {html.escape(summary['id'])}</h1><p>Status: {html.escape(summary['status'])}</p>" + \
        "<table><tr><th>Overlay</th><th>Tool</th><th>Status</th><th>Job</th><th>Artifacts on PVC</th><th>Error</th></tr>" + \
        "".join(rows) + "</table>" + \
        "<h2>AIPerf comparison</h2><table><tr><th>Overlay</th><th>Sample</th><th>Requests/s avg</th><th>Output tokens/s avg</th><th>TTFT p90</th><th>ITL p90</th></tr>" + \
        "".join(comparisons) + "</table>"
    (destination / "index.html").write_text(page)


def run(config: dict[str, Any], results_root: Path = Path("/workload")) -> int:
    destination = results_root / "campaigns" / config["id"]
    destination.mkdir(parents=True, exist_ok=False)
    (destination / "campaign.json").write_text(json.dumps(config, indent=2) + "\n")
    summary: dict[str, Any] = {"id": config["id"], "status": "running", "started_at": datetime.now(timezone.utc).isoformat(), "overlays": []}
    write_summary(destination, summary)
    try:
        overlay_root, source_commit = fetch_source(config["source"])
    except Exception as exc:
        summary["status"] = "failed"
        summary["error"] = f"llm-d source checkout failed: {exc}"
        summary["finished_at"] = datetime.now(timezone.utc).isoformat()
        write_summary(destination, summary)
        raise
    overlay_root = overlay_root.resolve(strict=True)
    summary["source_commit"] = source_commit
    (destination / "source-commit.txt").write_text(source_commit + "\n")
    write_summary(destination, summary)
    failed = False
    for overlay in config["overlays"]:
        name = overlay["name"]
        print(f"Deploying overlay {name}", flush=True)
        record: dict[str, Any] = {"name": name, "status": "running", "benchmarks": []}
        summary["overlays"].append(record)
        folder = destination / name
        folder.mkdir()
        manifest = folder / "manifest.yaml"
        applied = False
        try:
            overlay_path = (overlay_root / overlay["path"]).resolve(strict=True)
            if not overlay_path.is_relative_to(overlay_root) or not overlay_path.is_dir():
                raise ValueError(f"overlay {name} escapes overlay_root or is not a directory")
            rendered = call(["kubectl", "kustomize", str(overlay_path)]).stdout
            if not rendered.strip():
                raise RuntimeError(f"overlay {name} rendered no resources")
            validate_manifest(rendered, config["namespace"])
            manifest.write_text(rendered)
            record["manifest_sha256"] = hashlib.sha256(rendered.encode()).hexdigest()
            existing = kube(config["namespace"], "get", "-f", str(manifest), "--ignore-not-found", "-o", "name").stdout.strip()
            if existing:
                raise RuntimeError(f"overlay {name} would modify pre-existing resources: {existing}")
            if snapshot(config["namespace"], "llm-d.ai/inference-serving=true"):
                raise RuntimeError(f"overlay {name} cannot start while serving Pods already exist in the namespace")
            applied = True  # apply may partially succeed; always clean up its manifest
            kube(config["namespace"], "apply", "-f", str(manifest))
            baseline = wait_ready(config, overlay)
            print(f"Overlay {name} ready: {len(baseline)} serving Pods", flush=True)
            commit = build_commit(config["namespace"])
            record["serving_pods"] = baseline
            record["vllm_build_commit"] = commit
            (folder / "serving-pods.json").write_text(kube(config["namespace"], "get", "pods", "-l", overlay["pod_selector"], "-o", "json").stdout)
            for bench in config["benchmarks"]:
                try:
                    result = submit_benchmark(config, overlay, bench, folder, baseline, commit)
                except Exception as exc:
                    result = {"tool": bench["tool"], "status": "failed", "error": str(exc)}
                    if isinstance(exc, CleanupError):
                        record["cleanup_error"] = str(exc)
                    failed = True
                record["benchmarks"].append(result)
                write_summary(destination, summary)
                if result["status"] != "completed":
                    break
            record["status"] = "completed" if all(item["status"] == "completed" for item in record["benchmarks"]) else "failed"
        except Exception as exc:
            record["status"] = "failed"
            record["error"] = str(exc)
            failed = True
        finally:
            if applied:
                print(f"Removing overlay {name}", flush=True)
                deletion = kube(config["namespace"], "delete", "-f", str(manifest), "--ignore-not-found",
                                "--wait=true", f"--timeout={config['cleanup_timeout_seconds']}s", check=False)
                if deletion.returncode:
                    record["status"] = "failed"
                    record["cleanup_error"] = deletion.stderr.strip()
                    failed = True
                else:
                    try:
                        wait_gone(config, overlay["pod_selector"])
                    except Exception as exc:
                        record["status"] = "failed"
                        record["cleanup_error"] = str(exc)
                        failed = True
            write_summary(destination, summary)
        if record.get("cleanup_error") or (record["status"] == "failed" and not config["continue_on_failure"]):
            break
    shutil.rmtree(overlay_root)
    summary["status"] = "failed" if failed else "completed"
    summary["finished_at"] = datetime.now(timezone.utc).isoformat()
    write_summary(destination, summary)
    print(f"Campaign {config['id']} {summary['status']}; artifacts: {destination}", flush=True)
    return 1 if failed else 0


def submit(config: dict[str, Any], image: str, service_account: str) -> None:
    namespace, campaign_id = config["namespace"], config["id"]
    required_name(service_account, "service_account")
    if not image or any(char.isspace() for char in image):
        raise ValueError("image must be a container image reference")
    kube(namespace, "get", "localqueue", config["campaign_queue"])
    kube(namespace, "get", "localqueue", config["benchmark_queue"])
    kube(namespace, "get", "serviceaccount", service_account)
    configmap_name = f"campaign-{campaign_id}"
    if len(configmap_name) > 63:
        raise ValueError("campaign ID is too long for Kubernetes resource names")
    configmap = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": configmap_name, "namespace": namespace},
                 "data": {"campaign.json": json.dumps(config)}}
    kube(namespace, "create", "-f", "-", input_text=json.dumps(configmap))
    job = {"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": configmap_name, "namespace": namespace,
           "labels": {"kueue.x-k8s.io/queue-name": config["campaign_queue"], "app.kubernetes.io/name": "benchmark-campaign"}},
           "spec": {"suspend": True, "backoffLimit": 0, "template": {"spec": {"restartPolicy": "Never",
           "serviceAccountName": service_account, "containers": [{"name": "runner", "image": image,
           "command": ["python3", "/workspace/agentx-mvp/campaign/run.py", "run", "/campaign/campaign.json"],
           "resources": {"requests": {"cpu": "1", "memory": "1Gi", "ephemeral-storage": "1Gi"},
                         "limits": {"cpu": "2", "memory": "2Gi", "ephemeral-storage": "4Gi"}},
           "volumeMounts": [{"name": "config", "mountPath": "/campaign", "readOnly": True},
                            {"name": "results", "mountPath": "/workload"}]}],
           "volumes": [{"name": "config", "configMap": {"name": configmap_name}},
                       {"name": "results", "persistentVolumeClaim": {"claimName": config["results_pvc"]}}]}}}}
    try:
        kube(namespace, "create", "-f", "-", input_text=json.dumps(job))
    except Exception:
        kube(namespace, "delete", "configmap", configmap_name, "--ignore-not-found", check=False)
        raise
    print(f"Campaign queued: {namespace}/{configmap_name}")
    print(f"Status: kubectl -n {namespace} get job {configmap_name}")
    print(f"Logs: kubectl -n {namespace} logs -f job/{configmap_name}")
    print(f"Results: {config['results_pvc']}:/workload/campaigns/{campaign_id}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["validate", "submit", "run"])
    parser.add_argument("config", type=Path)
    parser.add_argument("--image", help="runner image for submit")
    parser.add_argument("--service-account", default="benchmark-campaign")
    args = parser.parse_args()
    try:
        config = load_config(args.config)
        if args.action == "validate":
            print("Campaign configuration is valid")
            return 0
        if args.action == "submit":
            if not args.image:
                raise ValueError("--image is required for submit")
            submit(config, args.image, args.service_account)
            return 0
        return run(config)
    except (ValueError, RuntimeError, TimeoutError, OSError, json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
        print(f"campaign: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
