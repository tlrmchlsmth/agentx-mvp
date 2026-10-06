#!/usr/bin/env python3
"""Run a sequence of Kustomize overlays and benchmark each deployed model."""
from __future__ import annotations

import argparse
import base64
import io
from collections import Counter
from contextlib import contextmanager
import copy
import csv
import hashlib
import html
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import time
import tempfile
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from datetime import datetime, timezone
from typing import Any, Callable

import yaml

ROOT = Path(__file__).resolve().parents[1]
REPORT_SPEC = importlib.util.spec_from_file_location("live_aiperf_report", ROOT / "live-aiperf" / "report.py")
if REPORT_SPEC is None or REPORT_SPEC.loader is None:
    raise RuntimeError("Could not load the live AIPerf report reader")
AIPERF_REPORT = importlib.util.module_from_spec(REPORT_SPEC)
REPORT_SPEC.loader.exec_module(AIPERF_REPORT)
TRANSFER_SPEC = importlib.util.spec_from_file_location("campaign_transfer", Path(__file__).with_name("transfer.py"))
if TRANSFER_SPEC is None or TRANSFER_SPEC.loader is None:
    raise RuntimeError("Could not load campaign artifact transfer")
TRANSFER = importlib.util.module_from_spec(TRANSFER_SPEC)
TRANSFER_SPEC.loader.exec_module(TRANSFER)
NAME = re.compile(r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$")
DIMENSION_NAME = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
JOB_LINE = re.compile(r"^Job queued: ([a-z0-9-]+) ", re.MULTILINE)
GIT_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
BUILD_ACTION = re.compile(r"^(?:checkout|merge|cherry-pick|cherry-pick-parent1|cherry-pick-m[1-9][0-9]*)$")
CAMPAIGN_SOURCE_FILES = (
    "campaign/run.py", "campaign/transfer.py", "campaign/vllm-wheel-build.sh",
    "campaign/vllm-source-build.sh", "campaign/deepep-wheel-build.sh",
    "live-aiperf/submit.sh", "live-aiperf/report.py",
    "live-aiperf/plotly-basic-2.35.2.min.js.gz", "live-aiperf/reset-prefix-caches.py",
    "live-aiperf/capture-llmd-resources.sh", "live-nyann/submit.sh",
    "export_dashboard.py", "gen_interactivity_chart.py", "overlay_dashboards.py",
)


class CleanupError(RuntimeError):
    """A running child workload or deployment might still exist."""


def call(args: list[str], *, input_text: str | None = None, env: dict[str, str] | None = None,
         check: bool = True, timeout: int | None = None,
         errors: str = "strict") -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, input=input_text, text=True, encoding="utf-8", errors=errors,
                            capture_output=True, env=env, timeout=timeout)
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


def git_repo(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an HTTPS Git URL")
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError(f"{field} must be an HTTPS Git URL without credentials")
    return value


def git_branch(value: Any, field: str) -> str:
    if not isinstance(value, str) or not GIT_REF.fullmatch(value) or ".." in value or value.endswith(".lock"):
        raise ValueError(f"{field} must be a Git branch name")
    return value


def validate_steps(steps: Any, field: str) -> None:
    if not isinstance(steps, list) or len(steps) > 32:
        raise ValueError(f"{field} needs 0-32 entries")
    for index, step in enumerate(steps):
        if not isinstance(step, dict) or set(step) != {"ref", "action"}:
            raise ValueError(f"{field}[{index}] needs ref and action")
        git_branch(step["ref"], f"{field}[{index}].ref")
        action = step["action"]
        if not isinstance(action, str) or not BUILD_ACTION.fullmatch(action) or (index == 0) != (action == "checkout"):
            raise ValueError(f"{field}[{index}].action is invalid")


def validate_deepep(value: Any, field: str) -> None:
    if not isinstance(value, dict) or set(value) != {"repo", "ref"}:
        raise ValueError(f"{field} needs repo and ref")
    git_repo(value["repo"], f"{field}.repo")
    git_branch(value["ref"], f"{field}.ref")


def load_config(path: Path) -> dict[str, Any]:
    config = json.loads(path.read_text())
    if not isinstance(config, dict):
        raise ValueError("campaign must be a JSON object")
    allowed = {"id", "namespace", "source", "vllm_image", "build_repo", "builds", "monitoring", "base_url", "results_pvc", "benchmark_queue",
               "campaign_queue", "overlays", "benchmarks", "rollout_timeout_seconds",
               "admission_timeout_seconds", "cleanup_timeout_seconds", "continue_on_failure"}
    if set(config) - allowed:
        raise ValueError(f"unknown campaign fields: {sorted(set(config) - allowed)}")
    for key in ("id", "namespace", "results_pvc"):
        required_name(config.get(key), key)
    for key in ("benchmark_queue", "campaign_queue"):
        required_name(config.get(key), key)
    image = config.get("vllm_image")
    if not isinstance(image, str) or not image or len(image) > 512 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@-]*", image):
        raise ValueError("vllm_image must be an explicit container image reference")
    if "base_url" in config:
        url = config["base_url"]
        if not isinstance(url, str) or len(url) > 512:
            raise ValueError("base_url must be an HTTP(S) model API URL")
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or not parsed.path.endswith("/v1"):
            raise ValueError("base_url must be an HTTP(S) model API URL ending in /v1 without credentials")
    if "monitoring" in config:
        monitoring = config["monitoring"]
        remote_keys = {"grafana_url", "auth_secret", "dashboard_uid"}
        local_keys = {"grafana_namespace", "grafana_service", "auth_secret", "dashboard_uid"}
        if not isinstance(monitoring, dict) or set(monitoring) not in (remote_keys, local_keys):
            raise ValueError("monitoring needs either grafana_url or grafana_namespace/grafana_service, plus auth_secret and dashboard_uid")
        if "grafana_url" in monitoring:
            url = monitoring["grafana_url"]
            if not isinstance(url, str) or len(url) > 512:
                raise ValueError("monitoring.grafana_url must be an HTTP(S) URL")
            parsed = urlsplit(url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError("monitoring.grafana_url must be an HTTP(S) URL without credentials")
        else:
            required_name(monitoring["grafana_namespace"], "monitoring.grafana_namespace")
            required_name(monitoring["grafana_service"], "monitoring.grafana_service")
        required_name(monitoring["auth_secret"], "monitoring.auth_secret")
        uid = monitoring["dashboard_uid"]
        if not isinstance(uid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", uid):
            raise ValueError("monitoring.dashboard_uid must be a Grafana dashboard UID")
    source = config.get("source")
    if not isinstance(source, dict) or set(source) != {"repo", "ref"}:
        raise ValueError("source needs repo and ref")
    repo, ref = git_repo(source["repo"], "source.repo"), source["ref"]
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
    builds = config.get("builds")
    if builds is not None:
        if not isinstance(builds, list) or not 1 <= len(builds) <= 16:
            raise ValueError("builds must contain 1-16 entries")
        has_vllm_steps = any(isinstance(build, dict) and bool(build.get("steps")) for build in builds)
        if has_vllm_steps or "build_repo" in config:
            git_repo(config.get("build_repo"), "build_repo")
        build_names = set()
        for build in builds:
            if not isinstance(build, dict) or "name" not in build or set(build) - {"name", "steps", "deepep"}:
                raise ValueError("each build needs a name; steps and deepep are optional")
            name = required_name(build["name"], "build.name")
            if name in build_names:
                raise ValueError(f"duplicate build: {name}")
            build_names.add(name)
            if "steps" in build:
                validate_steps(build["steps"], f"build {name}.steps")
            if "deepep" in build:
                validate_deepep(build["deepep"], f"build {name}.deepep")
        if len(builds) * len(overlays) > 128:
            raise ValueError("builds x overlays may not exceed 128 combinations")
    elif "build_repo" in config:
        raise ValueError("build_repo requires builds")
    seen = set()
    for overlay in overlays:
        required = {"name", "path", "model_label", "pod_selector", "expected_pods"}
        if not isinstance(overlay, dict) or not required.issubset(overlay) or set(overlay) - required - {"build", "dimensions", "vllm_cli_args", "vllm_env"}:
            raise ValueError("each overlay needs name, path, model_label, pod_selector, expected_pods; build/dimensions/vllm_cli_args/vllm_env are optional")
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
        if "vllm_cli_args" in overlay:
            role_args = overlay["vllm_cli_args"]
            if not isinstance(role_args, dict) or not role_args or len(role_args) > 8:
                raise ValueError(f"overlay {name}.vllm_cli_args must map serving roles to CLI arguments")
            for role, args in role_args.items():
                if not isinstance(role, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,62}", role):
                    raise ValueError(f"overlay {name}.vllm_cli_args has an invalid role")
                if not isinstance(args, list) or not args or len(args) > 16 or any(
                    not isinstance(arg, str) or not re.fullmatch(r"-{1,2}[A-Za-z0-9][A-Za-z0-9_.=-]{0,127}", arg)
                    for arg in args
                ):
                    raise ValueError(f"overlay {name}.vllm_cli_args.{role} must contain simple CLI flags")
        if "vllm_env" in overlay:
            role_env = overlay["vllm_env"]
            if not isinstance(role_env, dict) or not role_env or len(role_env) > 8:
                raise ValueError(f"overlay {name}.vllm_env must map serving roles to environment variables")
            for role, values in role_env.items():
                if not isinstance(role, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,62}", role):
                    raise ValueError(f"overlay {name}.vllm_env has an invalid role")
                if not isinstance(values, dict) or not values or len(values) > 32 or any(
                    not isinstance(key, str) or not re.fullmatch(r"[A-Z_][A-Z0-9_]{0,127}", key) or
                    not isinstance(value, str) or len(value) > 1024
                    for key, value in values.items()
                ):
                    raise ValueError(f"overlay {name}.vllm_env.{role} must map environment names to strings")
        dimensions = overlay.get("dimensions", {})
        reserved = {"build", "overlay", "tool", "sample", "concurrency", "status", "requests_per_s",
                    "output_tokens_per_s", "ttft_p90", "itl_p90", "artifacts", "error",
                    "successful_requests", "error_requests", "requests_unit", "output_tokens_unit",
                    "ttft_unit", "itl_unit", "report", "llm_d_commit", "vllm_commits",
                    "deepep_commit", "vllm_image"}
        if not isinstance(dimensions, dict) or len(dimensions) > 16 or any(
            not isinstance(key, str) or not DIMENSION_NAME.fullmatch(key) or key in reserved or
            not isinstance(value, (str, int, float, bool)) or len(str(value)) > 80
            for key, value in dimensions.items()
        ):
            raise ValueError(f"overlay {name}.dimensions must map short names to scalar values")
        if builds is not None and "build" in overlay:
            raise ValueError("top-level builds cannot be combined with per-overlay build")
        if "build" in overlay:
            build = overlay["build"]
            if not isinstance(build, dict) or not build or set(build) - {"repo", "steps", "deepep"}:
                raise ValueError(f"overlay {name} build may contain repo, steps, or deepep")
            if "steps" in build:
                validate_steps(build["steps"], f"overlay {name} build.steps")
                git_repo(build.get("repo"), f"overlay {name} build.repo")
            elif "repo" in build:
                git_repo(build["repo"], f"overlay {name} build.repo")
            if "deepep" in build:
                validate_deepep(build["deepep"], f"overlay {name} build.deepep")
    tools = set()
    for bench in benchmarks:
        if not isinstance(bench, dict):
            raise ValueError("benchmark entries must be objects")
        tool = bench.get("tool")
        fields = {"tool", "concurrencies", "duration_seconds"}
        if tool == "nyann":
            fields |= {"isl", "osl", "warmup_seconds"}
        elif tool == "aiperf":
            fields |= {"max_context_length"}
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
        bounded_int(bench["duration_seconds"], "duration_seconds", 900 if tool == "aiperf" else 1, 7200)
        if tool == "aiperf":
            bounded_int(bench["max_context_length"], "max_context_length", 1024, 1000000)
        if tool == "nyann":
            for key in ("isl", "osl"):
                bounded_int(bench[key], key, 1, 1000000)
            bounded_int(bench["warmup_seconds"], "warmup_seconds", 0, 3600)
    if len("campaign-" + config["id"]) > 63:
        raise ValueError("campaign ID is too long for Kubernetes Job/ConfigMap names")
    combined_names = set()
    for overlay in overlays:
        names = [f"{build['name']}-{overlay['name']}" for build in builds] if builds else [overlay["name"]]
        for name in names:
            if name in combined_names:
                raise ValueError(f"build/overlay combination name collides: {name}")
            combined_names.add(name)
        if any(len(f"{config['id']}-{name}-{tool}") > 120 for name in names for tool in tools):
            raise ValueError("campaign/build/overlay names produce a run ID longer than 120 characters")
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


def resolve_build(build: dict[str, Any], pins: dict[tuple[str, str], str] | None = None) -> dict[str, Any]:
    """Pin requested source branches; an empty list uses the runtime image."""
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    if pins is None:
        pins = {}
    steps = build.get("steps", [])
    repo = build.get("repo")
    refs = [f"refs/heads/{step['ref']}" for step in steps]
    unresolved = list(dict.fromkeys(ref for ref in refs if (repo, ref) not in pins))
    if unresolved:
        result = call(["git", "ls-remote", "--exit-code", "--heads", repo, *unresolved], env=env, timeout=120)
        resolved = {}
        for line in result.stdout.strip().splitlines():
            fields = line.split("\t")
            if len(fields) != 2 or not re.fullmatch(r"[0-9a-f]{40}", fields[0]) or fields[1] not in unresolved:
                raise RuntimeError("vLLM branch resolution returned an unexpected ref")
            resolved[fields[1]] = fields[0]
        if any(ref not in resolved for ref in unresolved):
            raise RuntimeError(f"could not resolve vLLM branches: {', '.join(ref for ref in unresolved if ref not in resolved)}")
        pins.update({(repo, ref): commit for ref, commit in resolved.items()})
    result = {"mode": "source" if steps else "nightly", "steps": [
        {"ref": step["ref"], "action": step["action"], "commit": pins[(repo, f"refs/heads/{step['ref']}")]}
        for step in steps]}
    if steps:
        result["repo"] = repo
    if "deepep" in build:
        recipe = build["deepep"]
        ref = f"refs/heads/{recipe['ref']}"
        if (recipe["repo"], ref) not in pins:
            remote = call(["git", "ls-remote", "--exit-code", "--heads", recipe["repo"], ref], env=env, timeout=120).stdout.strip()
            match = re.fullmatch(r"([0-9a-f]{40})\t" + re.escape(ref), remote)
            if not match:
                raise RuntimeError(f"could not resolve DeepEP branch {recipe['ref']}")
            pins[(recipe["repo"], ref)] = match.group(1)
        result["deepep"] = {"repo": recipe["repo"], "ref": recipe["ref"], "commit": pins[(recipe["repo"], ref)]}
    return result


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
    if args and args[0] == "logs":
        # Workload progress output can contain arbitrary bytes from a model
        # response; decoding must not abort campaign monitoring or previews.
        kwargs.setdefault("errors", "replace")
    return call(["kubectl", "-n", namespace, *args], **kwargs)


def snapshot(namespace: str, selector: str) -> list[str]:
    data = json.loads(kube(namespace, "get", "pods", "-l", selector, "-o", "json").stdout)
    return sorted(f"{pod['metadata']['name']}:{pod['metadata']['uid']}" for pod in data["items"])


def build_commit(namespace: str) -> str | None:
    output = kube(namespace, "get", "configmap", "vllm-build-ref", "--ignore-not-found", "-o", "json").stdout.strip()
    if not output:
        return None
    data = json.loads(output)
    commit = data.get("data", {}).get("VLLM_BUILD_COMMIT", "")
    if not commit:
        return None
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise RuntimeError("vllm-build-ref has no valid commit")
    return commit


def worker_pods(documents: list[Any]):
    """Yield the Pod templates before a DisaggregatedSet creates its child LWSs."""
    for item in documents:
        if not isinstance(item, dict):
            continue
        if item.get("kind") == "LeaderWorkerSet":
            roles = [(item.get("metadata", {}).get("name", ""), item.get("spec", {}))]
        elif item.get("kind") == "DisaggregatedSet":
            roles = [(role.get("name", ""), role.get("spec", {})) for role in item.get("spec", {}).get("roles", [])
                     if isinstance(role, dict)]
        else:
            continue
        for role, spec in roles:
            pod = spec.get("leaderWorkerTemplate", {}).get("workerTemplate", {}).get("spec")
            if isinstance(pod, dict):
                yield role, pod


def apply_vllm_image(rendered: str, image: str) -> str:
    """Use the campaign's explicit runtime image in every vLLM worker."""
    documents = list(yaml.safe_load_all(rendered))
    workers = 0
    for _, pod in worker_pods(documents):
        for container in pod.get("containers", []):
            if container.get("name") == "vllm":
                container["image"] = image
                workers += 1
    if not workers:
        raise ValueError("overlay has no LeaderWorkerSet vllm container or DisaggregatedSet vllm container for vllm_image")
    return yaml.safe_dump_all(documents, sort_keys=False)


def apply_vllm_cli_args(rendered: str, role_args: dict[str, list[str]] | None) -> str:
    """Append explicit vLLM flags to selected serving roles, including DisaggregatedSet roles."""
    if not role_args:
        return rendered
    documents = list(yaml.safe_load_all(rendered))
    matched = set()
    for role, pod in worker_pods(documents):
        if role not in role_args:
            continue
        containers = [container for container in pod.get("containers", []) if container.get("name") == "vllm"]
        if len(containers) != 1:
            raise ValueError(f"role {role} needs exactly one vllm container for vllm_cli_args")
        container = containers[0]
        args = container.get("args")
        if container.get("command", [])[-2:] == ["/bin/bash", "-c"] and isinstance(args, list) and len(args) == 1:
            script = args[0]
            if not isinstance(script, str) or "exec vllm serve" not in script:
                raise ValueError(f"role {role} has no supported vllm serve command")
            args[0] = script.rstrip() + " " + " ".join(role_args[role])
        elif isinstance(args, list) and all(isinstance(arg, str) for arg in args):
            args.extend(role_args[role])
        else:
            raise ValueError(f"role {role} has no supported vllm arguments")
        matched.add(role)
    if matched != set(role_args):
        raise ValueError(f"vllm_cli_args roles missing from overlay: {sorted(set(role_args) - matched)}")
    return yaml.safe_dump_all(documents, sort_keys=False)


def apply_vllm_env(rendered: str, role_env: dict[str, dict[str, str]] | None) -> str:
    """Set explicit environment variables on selected vLLM serving roles."""
    if not role_env:
        return rendered
    documents = list(yaml.safe_load_all(rendered))
    matched = set()
    for role, pod in worker_pods(documents):
        if role not in role_env:
            continue
        containers = [container for container in pod.get("containers", []) if container.get("name") == "vllm"]
        if len(containers) != 1:
            raise ValueError(f"role {role} needs exactly one vllm container for vllm_env")
        environment = containers[0].setdefault("env", [])
        for key, value in role_env[role].items():
            environment[:] = [entry for entry in environment if entry.get("name") != key]
            environment.append({"name": key, "value": value})
        matched.add(role)
    if matched != set(role_env):
        raise ValueError(f"vllm_env roles missing from overlay: {sorted(set(role_env) - matched)}")
    return yaml.safe_dump_all(documents, sort_keys=False)


def prepare_overlay_manifest(rendered: str, config: dict[str, Any], overlay: dict[str, Any]) -> str:
    rendered = apply_vllm_image(rendered, config["vllm_image"])
    rendered = apply_vllm_cli_args(rendered, overlay.get("vllm_cli_args"))
    return apply_vllm_env(rendered, overlay.get("vllm_env"))


def inject_vllm_build_script(rendered: str, build: dict[str, Any] | None = None,
                             cache_pvc: str | None = None) -> str:
    """Use the versioned campaign build recipe in both builder and serving Pods."""
    build = build or {"mode": "nightly", "steps": []}
    source_build = bool(build.get("steps"))
    deepep_build = "deepep" in build
    documents = list(yaml.safe_load_all(rendered))
    candidates = []
    for item in documents:
        if not isinstance(item, dict) or item.get("kind") != "ConfigMap":
            continue
        for key, value in item.get("data", {}).items():
            if key.endswith(".sh") and isinstance(value, str) and all(
                marker in value for marker in ("VLLM_BUILD_COMMIT", "BUILD_VARIANT=", "/shared/vllm-build")
            ):
                candidates.append((item, key))
    if not candidates:
        if source_build or deepep_build:
            return inject_generic_build(documents, build, cache_pvc)
        return rendered
    if len(candidates) != 1:
        raise ValueError("overlay contains multiple vLLM wheel build scripts")
    script_map, old_key = candidates[0]
    old_name = script_map["metadata"]["name"]
    if old_name != "vllm-build" and any(
        isinstance(item, dict) and item.get("kind") == "ConfigMap" and item.get("metadata", {}).get("name") == "vllm-build"
        for item in documents
    ):
        raise ValueError("overlay already contains a conflicting vllm-build ConfigMap")
    script_map["metadata"]["name"] = "vllm-build"
    script_map["data"] = {
        "vllm-wheel-build.sh": (ROOT / "campaign" / "vllm-wheel-build.sh").read_text(),
        "deepep-wheel-build.sh": (ROOT / "campaign" / "deepep-wheel-build.sh").read_text(),
    }
    ref_map = next((item for item in documents if isinstance(item, dict) and item.get("kind") == "ConfigMap" and
                    item.get("metadata", {}).get("name") == "vllm-build-ref"), None)
    if ref_map is None and source_build:
        raise ValueError("vLLM build script requires a vllm-build-ref ConfigMap")
    if ref_map is None and deepep_build:
        ref_map = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "vllm-build-ref"}, "data": {}}
        documents.append(ref_map)
    if ref_map is not None:
        data = ref_map.setdefault("data", {})
        for key in ("VLLM_BUILD_REF", "VLLM_BUILD_COMMIT", "VLLM_BUILD_REPO", "VLLM_BUILD_REFS",
                    "VLLM_BUILD_ACTIONS", "VLLM_BUILD_SHAS", "DEEPEP_BUILD_REPO", "DEEPEP_BUILD_REF",
                    "DEEPEP_BUILD_COMMIT"):
            data.pop(key, None)
        data["DEEPEP_BUILD_ENABLED"] = "1" if deepep_build else "0"
    if source_build:
        steps = build["steps"]
        ref_map.setdefault("data", {}).update({
            "VLLM_BUILD_REF": steps[0]["ref"], "VLLM_BUILD_COMMIT": steps[0]["commit"],
            "VLLM_BUILD_REPO": build["repo"],
            "VLLM_BUILD_REFS": " ".join(step["ref"] for step in steps),
            "VLLM_BUILD_ACTIONS": " ".join(step["action"] for step in steps),
            "VLLM_BUILD_SHAS": " ".join(step["commit"] for step in steps),
        })
    if deepep_build:
        deepep = build["deepep"]
        ref_map["data"].update({
            "DEEPEP_BUILD_REPO": deepep["repo"],
            "DEEPEP_BUILD_REF": deepep["ref"], "DEEPEP_BUILD_COMMIT": deepep["commit"],
        })
    references = 0
    for _, pod in worker_pods(documents):
        old_volumes = set()
        for volume in pod.get("volumes", []):
            if volume.get("configMap", {}).get("name") == old_name:
                old_volumes.add(volume["name"])
                volume["name"] = "vllm-build"
                volume["configMap"]["name"] = "vllm-build"
        for container in pod.get("containers", []):
            for mount in container.get("volumeMounts", []):
                if mount.get("name") in old_volumes:
                    mount["name"] = "vllm-build"
            for field in ("command", "args"):
                if field in container:
                    references += sum(old_key in part for part in container[field])
                    container[field] = [part.replace(old_key, "vllm-wheel-build.sh") for part in container[field]]
            if any("vllm-wheel-build.sh" in part for field in ("command", "args") for part in container.get(field, [])):
                env = container.setdefault("env", [])
                managed = {"VLLM_BUILD_REF", "VLLM_BUILD_COMMIT", "VLLM_BUILD_REPO", "VLLM_BUILD_REFS",
                           "VLLM_BUILD_ACTIONS", "VLLM_BUILD_SHAS", "VLLM_BUILD_MODE", "DEEPEP_BUILD_ENABLED",
                           "DEEPEP_BUILD_REPO", "DEEPEP_BUILD_REF", "DEEPEP_BUILD_COMMIT", "VLLM_BUILD_BASE_IMAGE_ID"}
                env[:] = [item for item in env if item.get("name") not in managed]
                existing = {item.get("name") for item in env}
                env.append({"name": "VLLM_BUILD_MODE", "value": "source" if source_build else "nightly"})
                if source_build:
                    for key in ("VLLM_BUILD_REF", "VLLM_BUILD_COMMIT", "VLLM_BUILD_REPO", "VLLM_BUILD_REFS",
                                "VLLM_BUILD_ACTIONS", "VLLM_BUILD_SHAS"):
                        env.append({"name": key, "valueFrom": {"configMapKeyRef": {"name": "vllm-build-ref", "key": key}}})
                image = container.get("image", "")
                image_id = image.split("@", 1)[1] if "@sha256:" in image else image
                env.append({"name": "VLLM_BUILD_BASE_IMAGE_ID", "value": image_id})
                if "VLLM_BUILD_ROLE" not in existing:
                    env.append({"name": "VLLM_BUILD_ROLE", "value": "prefill"})
                env.append({"name": "DEEPEP_BUILD_ENABLED", "value": "1" if deepep_build else "0"})
                if deepep_build:
                    for key in ("DEEPEP_BUILD_REPO", "DEEPEP_BUILD_REF", "DEEPEP_BUILD_COMMIT"):
                        env.append({"name": key, "valueFrom": {"configMapKeyRef": {"name": "vllm-build-ref", "key": key}}})
    if not references:
        raise ValueError("vLLM build script is not sourced by a serving container")
    return yaml.safe_dump_all(documents, sort_keys=False)


def inject_generic_build(documents: list[Any], build: dict[str, Any], cache_pvc: str | None) -> str:
    """Add a pinned wheel build to overlays without an embedded build recipe."""
    if not cache_pvc:
        raise ValueError("source builds without an overlay build script need results_pvc as a shared cache")
    if any(isinstance(item, dict) and item.get("kind") == "ConfigMap" and
           item.get("metadata", {}).get("name") in {"vllm-build", "vllm-build-ref"} for item in documents):
        raise ValueError("overlay has a conflicting vLLM build ConfigMap")
    steps = build.get("steps", [])
    deepep = build.get("deepep")
    data = {"DEEPEP_BUILD_ENABLED": "1" if deepep else "0"}
    if steps:
        data.update({"VLLM_BUILD_REF": steps[0]["ref"], "VLLM_BUILD_COMMIT": steps[0]["commit"],
                     "VLLM_BUILD_REPO": build["repo"],
                     "VLLM_BUILD_REFS": " ".join(step["ref"] for step in steps),
                     "VLLM_BUILD_ACTIONS": " ".join(step["action"] for step in steps),
                     "VLLM_BUILD_SHAS": " ".join(step["commit"] for step in steps)})
    if deepep:
        data.update({"DEEPEP_BUILD_REPO": deepep["repo"], "DEEPEP_BUILD_REF": deepep["ref"],
                     "DEEPEP_BUILD_COMMIT": deepep["commit"]})
    documents.extend([
        {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "vllm-build"}, "data": {
            "vllm-wheel-build.sh": (ROOT / "campaign" / "vllm-source-build.sh").read_text(),
            "deepep-wheel-build.sh": (ROOT / "campaign" / "deepep-wheel-build.sh").read_text()}},
        {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "vllm-build-ref"}, "data": data},
    ])
    workers = 0
    for role, pod in worker_pods(documents):
        for container in pod.get("containers", []):
            if container.get("name") != "vllm":
                continue
            workers += 1
            volumes = pod.setdefault("volumes", [])
            if any(volume.get("name") in {"vllm-build", "build-cache"} for volume in volumes):
                raise ValueError("overlay has a conflicting vLLM build volume")
            volumes.extend([{"name": "vllm-build", "configMap": {"name": "vllm-build"}},
                            {"name": "build-cache", "persistentVolumeClaim": {"claimName": cache_pvc}}])
            mounts = container.setdefault("volumeMounts", [])
            if any(mount.get("name") in {"vllm-build", "build-cache"} or
                   mount.get("mountPath") in {"/opt/build-scripts", "/shared/vllm-build"} for mount in mounts):
                raise ValueError("overlay has a conflicting vLLM build mount")
            mounts.extend([{"name": "vllm-build", "mountPath": "/opt/build-scripts", "readOnly": True},
                           {"name": "build-cache", "mountPath": "/shared/vllm-build"}])
            if container.get("command", [])[:2] != ["/bin/bash", "-c"] or len(container.get("args", [])) != 1:
                raise ValueError("generic vLLM build needs a bash -c serving command")
            container["args"][0] = "source /opt/build-scripts/vllm-wheel-build.sh\n" + container["args"][0]
            env = container.setdefault("env", [])
            env.extend([{"name": "VLLM_BUILD_MODE", "value": "source" if steps else "nightly"},
                        {"name": "VLLM_BUILD_BASE_IMAGE_ID", "value": container["image"]},
                        {"name": "VLLM_BUILD_ROLE", "value": role or "worker"},
                        {"name": "DEEPEP_BUILD_ENABLED", "value": "1" if deepep else "0"}])
            for key in (["VLLM_BUILD_REF", "VLLM_BUILD_COMMIT", "VLLM_BUILD_REPO", "VLLM_BUILD_REFS",
                         "VLLM_BUILD_ACTIONS", "VLLM_BUILD_SHAS"] if steps else []) + ([
                         "DEEPEP_BUILD_REPO", "DEEPEP_BUILD_REF", "DEEPEP_BUILD_COMMIT"] if deepep else []):
                env.append({"name": key, "valueFrom": {"configMapKeyRef": {"name": "vllm-build-ref", "key": key}}})
    if not workers:
        raise ValueError("overlay has no vLLM worker for source build")
    return yaml.safe_dump_all(documents, sort_keys=False)


def vllm_prebuild(rendered: str, config: dict[str, Any], overlay: dict[str, Any], folder: Path,
                  *, preview_only: bool = False) -> str | None:
    """Build or preview the exact cache-warming Job for a serving worker."""
    documents = [item for item in yaml.safe_load_all(rendered) if isinstance(item, dict)]
    maps = {item.get("metadata", {}).get("name"): item for item in documents if item.get("kind") == "ConfigMap"}
    script_map = maps.get("vllm-build")
    if not script_map or "vllm-wheel-build.sh" not in script_map.get("data", {}):
        return None
    ref_map = maps.get("vllm-build-ref")
    candidates = []
    for _, pod in worker_pods(documents):
        for container in pod.get("containers", []):
            if any("vllm-wheel-build.sh" in part for field in ("command", "args") for part in container.get(field, [])):
                role = next((env.get("value") for env in container.get("env", []) if env.get("name") == "VLLM_BUILD_ROLE"), "")
                candidates.append((role == "prefill", pod, container))
    if not candidates:
        raise ValueError("vLLM build needs a worker container that sources the script")
    _, pod, serving = next((candidate for candidate in candidates if candidate[0]), candidates[0])
    serving_env = {item.get("name"): item for item in serving.get("env", [])}
    mode = serving_env.get("VLLM_BUILD_MODE", {}).get("value", "source")
    deepep_enabled = serving_env.get("DEEPEP_BUILD_ENABLED", {}).get("value") == "1"
    if mode not in {"source", "nightly"}:
        raise ValueError(f"unknown vLLM build mode: {mode}")
    if mode == "nightly" and not deepep_enabled:
        return None
    if not ref_map or (mode == "source" and not re.fullmatch(
        r"[0-9a-f]{40}", ref_map.get("data", {}).get("VLLM_BUILD_COMMIT", ""))):
        raise ValueError("vLLM build requires a vllm-build-ref ConfigMap with an immutable commit")
    mounts = [copy.deepcopy(m) for m in serving.get("volumeMounts", []) if m.get("name") in {"build-cache", "vllm-build"}]
    volumes = [copy.deepcopy(v) for v in pod.get("volumes", []) if v.get("name") in {"build-cache", "vllm-build"}]
    if {m.get("name") for m in mounts} != {"build-cache", "vllm-build"} or {v.get("name") for v in volumes} != {"build-cache", "vllm-build"}:
        raise ValueError("vLLM prefill worker needs script and shared build-cache mounts")
    script_volume = next(v for v in volumes if v["name"] == "vllm-build")
    if script_volume.get("configMap", {}).get("name") != "vllm-build":
        raise ValueError("vLLM script volume must use the rendered ConfigMap")
    env = [copy.deepcopy(e) for e in serving.get("env", []) if e.get("name", "").startswith(("VLLM_BUILD_", "DEEPEP_BUILD_"))]
    required_env = {"VLLM_BUILD_MODE", "VLLM_BUILD_BASE_IMAGE_ID", "VLLM_BUILD_ROLE"}
    if mode == "source":
        required_env |= {"VLLM_BUILD_REF", "VLLM_BUILD_COMMIT"}
    if deepep_enabled:
        required_env |= {"DEEPEP_BUILD_REPO", "DEEPEP_BUILD_REF", "DEEPEP_BUILD_COMMIT"}
    if not required_env.issubset({e["name"] for e in env}):
        raise ValueError("vLLM prefill worker is missing build identity")
    env = [item for item in env if item["name"] not in {"VLLM_BUILD_ROLE", "LWS_WORKER_INDEX"}]
    env.extend(({"name": "VLLM_BUILD_ROLE", "value": "prefill"}, {"name": "LWS_WORKER_INDEX", "value": "0"},
                {"name": "VLLM_BUILD_PREBUILD", "value": "1"}))
    builder = {"name": "build", "image": serving["image"], "imagePullPolicy": serving.get("imagePullPolicy", "IfNotPresent"),
               "command": ["/bin/bash", "-c"], "args": ["source /opt/build-scripts/vllm-wheel-build.sh"],
               "env": env, "resources": copy.deepcopy(serving.get("resources", {})), "volumeMounts": mounts}
    if "securityContext" in serving:
        builder["securityContext"] = copy.deepcopy(serving["securityContext"])
    name = "campaign-build-" + hashlib.sha256(f"{config['id']}/{overlay['name']}".encode()).hexdigest()[:16]
    job = {"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": name, "namespace": config["namespace"]},
           "spec": {"backoffLimit": 0, "activeDeadlineSeconds": config["rollout_timeout_seconds"],
                    "template": {"spec": {"restartPolicy": "Never", "serviceAccountName": pod.get("serviceAccountName", "default"),
                                          "volumes": volumes, "containers": [builder]}}}}
    for field in ("nodeSelector", "tolerations", "imagePullSecrets", "runtimeClassName", "priorityClassName"):
        if field in pod:
            job["spec"]["template"]["spec"][field] = copy.deepcopy(pod[field])
    if preview_only:
        (folder / "prebuild-job.yaml").write_text(yaml.safe_dump(job, sort_keys=False))
        return ref_map["data"]["VLLM_BUILD_COMMIT"] if mode == "source" else "nightly"
    # These are owned by the saved overlay manifest and deleted with it on failure.
    service_account = next((item for item in documents if item.get("kind") == "ServiceAccount" and
                            item.get("metadata", {}).get("name") == pod.get("serviceAccountName")), None)
    created = []
    try:
        for item in (script_map, ref_map, service_account):
            if item is None:
                continue
            kube(config["namespace"], "create", "-f", "-", input_text=json.dumps(item))
            created.append(item)
        kube(config["namespace"], "create", "-f", "-", input_text=json.dumps(job))
        waited = kube(config["namespace"], "wait", "--for=condition=complete", f"job/{name}",
                      f"--timeout={config['rollout_timeout_seconds']}s", check=False)
        logs = kube(config["namespace"], "logs", f"job/{name}", check=False)
        (folder / "build.log").write_text(logs.stdout + logs.stderr)
        if waited.returncode:
            raise RuntimeError(f"vLLM prebuild failed: {waited.stderr.strip()}; see {folder / 'build.log'}")
    finally:
        errors = []
        for kind, resource_name in [("job", name)] + [
            (item["kind"].lower(), item["metadata"]["name"]) for item in reversed(created)
        ]:
            deleted = kube(config["namespace"], "delete", kind, resource_name, "--ignore-not-found", "--wait=true",
                           f"--timeout={config['cleanup_timeout_seconds']}s", check=False)
            if deleted.returncode:
                errors.append(f"{kind}/{resource_name}: {deleted.stderr.strip()}")
        if errors:
            raise CleanupError("prebuild resource cleanup failed: " + "; ".join(errors))
    return ref_map["data"]["VLLM_BUILD_COMMIT"] if mode == "source" else "nightly"


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


def aiperf_measurements(artifact: Path, run_id: str, concurrencies: list[int]) -> list[dict[str, Any]]:
    counts = Counter(concurrencies)
    seen: Counter[int] = Counter()
    expected_samples = {}
    for concurrency in concurrencies:
        seen[concurrency] += 1
        sample = f"c{concurrency}" if counts[concurrency] == 1 else f"c{concurrency}-r{seen[concurrency]}"
        expected_samples[sample] = concurrency
    measurements = []
    for directory in sorted(artifact.iterdir()):
        data = AIPERF_REPORT.run_data(directory)
        if data is None:
            continue
        profile, metadata = data["profile"], data["metadata"]
        if metadata.get("run_id") != f"{run_id}-{directory.name}" or metadata.get("concurrency") != expected_samples.get(directory.name):
            raise RuntimeError(f"AIPerf artifact {directory.name} has unexpected run identity or concurrency")
        metrics = {}
        for key in ("request_throughput", "output_token_throughput", "time_to_first_token", "inter_token_latency"):
            value = profile.get(key, {})
            if isinstance(value, dict):
                metrics[key] = {field: value[field] for field in ("avg", "p90", "unit") if field in value}
        measurements.append({"concurrency": metadata["concurrency"], "sample": directory.name, "metrics": metrics})
    if {item["sample"] for item in measurements} != set(expected_samples):
        raise RuntimeError(f"AIPerf artifacts have samples {[item['sample'] for item in measurements]}, "
                           f"expected {list(expected_samples)}")
    return measurements


def nyann_measurements(log: str, concurrencies: list[int]) -> list[dict[str, Any]]:
    decoder = json.JSONDecoder()
    summary = None
    for match in re.finditer(r"(?m)^\{", log):
        try:
            candidate, _ = decoder.raw_decode(log[match.start():])
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict) and isinstance(candidate.get("stages"), list):
            summary = candidate
    if summary is None:
        raise RuntimeError("Nyann Job completed without a machine-readable stage summary in its logs")
    stages = summary["stages"]
    if [stage.get("concurrency") for stage in stages] != concurrencies:
        raise RuntimeError("Nyann stage concurrencies differ from the requested sweep")

    def latency_stats(values: dict[str, Any]) -> dict[str, Any]:
        result = {key: value for key, value in values.items()
                  if key in {"avg", "min", "p10", "p50", "p90", "p95", "p99", "max"}}
        if "avg" not in result and "mean" in values:
            result["avg"] = values["mean"]
        result["unit"] = "ms"
        return result

    measurements = []
    for index, stage in enumerate(stages, 1):
        successes = stage["successful_requests"]
        errors = stage["error_requests"]
        duration = stage["duration_seconds"]
        if successes <= 0 or duration <= 0:
            raise RuntimeError(f"Nyann stage {index} has no successful requests or measured duration")
        metrics = {"request_throughput": {"avg": successes / duration, "unit": "req/s"},
                   "output_token_throughput": {"avg": stage["output_tokens_per_second"], "unit": "tokens/s"},
                   "time_to_first_token": latency_stats(stage["ttft_ms"]),
                   "inter_token_latency": latency_stats(stage["itl_ms"]),
                   "request_count": {"avg": successes, "unit": "requests"},
                   "request_error_rate": {"avg": 100 * errors / (successes + errors), "unit": "%"}}
        if isinstance(stage.get("e2e_latency_ms"), dict):
            metrics["request_latency"] = latency_stats(stage["e2e_latency_ms"])
        if "total_output_tokens" in stage:
            metrics["total_output_tokens"] = {"avg": stage["total_output_tokens"], "unit": "tokens"}
            metrics["output_tokens_per_request"] = {"avg": stage["total_output_tokens"] / successes,
                                                     "unit": "tokens/request"}
        measurements.append({"concurrency": stage["concurrency"], "sample": f"stage-{index}",
                             "successful_requests": successes, "error_requests": errors, "metrics": metrics})
    return measurements


def nyann_live_measurements(log: str, concurrencies: list[int], duration_seconds: int) -> list[dict[str, Any]]:
    """Read completed Nyann stage rows while its final JSON is still unavailable."""
    try:
        return nyann_measurements(log, concurrencies)
    except RuntimeError as exc:
        if "without a machine-readable stage summary" not in str(exc):
            raise
    stages: dict[int, dict[str, Any]] = {}
    current_stage = None

    def milliseconds(value: str) -> float:
        match = re.fullmatch(r"([\d.]+)(ms|s)", value)
        if match is None:
            raise ValueError(value)
        return float(match.group(1)) * (1000 if match.group(2) == "s" else 1)

    for line in log.splitlines():
        started = re.search(r'Stage started" stage=(\d+)/\d+ concurrency=(\d+)', line)
        if started:
            index, concurrency = map(int, started.groups())
            current_stage = index if 1 <= index <= len(concurrencies) and concurrencies[index - 1] == concurrency else None
            continue
        columns = line.split()
        if current_stage is None or len(columns) != 14 or not all(value.isdigit() for value in columns[:4]):
            continue
        try:
            concurrency, successes, errors, _ = map(int, columns[:4])
            if concurrency != concurrencies[current_stage - 1] or successes <= 0 or duration_seconds <= 0:
                continue
            throughput = float(columns[4])
            ttft = [milliseconds(value) for value in columns[5:10]]
            itl = [milliseconds(value) for value in columns[10:14]]
        except ValueError:
            continue
        stages[current_stage] = {
            "concurrency": concurrency, "sample": f"stage-{current_stage}",
            "successful_requests": successes, "error_requests": errors,
            "metrics": {
                "request_throughput": {"avg": successes / duration_seconds, "unit": "req/s"},
                "output_token_throughput": {"avg": throughput, "unit": "tokens/s"},
                "time_to_first_token": {**dict(zip(("avg", "p10", "p50", "p95", "p99"), ttft)), "unit": "ms"},
                "inter_token_latency": {**dict(zip(("p10", "p50", "p95", "p99"), itl)), "unit": "ms"},
                "request_count": {"avg": successes, "unit": "requests"},
                "request_error_rate": {"avg": 100 * errors / (successes + errors), "unit": "%"},
                "total_output_tokens": {"avg": int(columns[3]), "unit": "tokens"},
                "output_tokens_per_request": {"avg": int(columns[3]) / successes, "unit": "tokens/request"},
            },
        }
        current_stage = None
    return [stages[index] for index in sorted(stages)]


def nyann_distribution(values: list[float]) -> dict[str, Any]:
    ordered = sorted(values)
    result = {"avg": sum(ordered) / len(ordered), "min": ordered[0], "max": ordered[-1], "unit": "ms"}
    for percentile in (10, 50, 90, 95, 99):
        position = (len(ordered) - 1) * percentile / 100
        lower = math.floor(position)
        upper = math.ceil(position)
        result[f"p{percentile}"] = ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)
    return result


def enrich_nyann_request_metrics(measurements: list[dict[str, Any]], log: str,
                                 concurrencies: list[int], duration_seconds: int,
                                 request_files: list[Path]) -> None:
    """Calculate request-level TPOT from Nyann JSONL, excluding warmup and stage crossings."""
    if not request_files or not measurements:
        return
    windows = nyann_stage_windows(log, concurrencies, measurements, duration_seconds)
    samples = {item["sample"]: item for item in measurements if item["sample"] in windows}
    values = {sample: {"count": 0, "tpot": [], "e2e": []} for sample in samples}
    for path in request_files:
        with path.open(encoding="utf-8", errors="replace") as source:
            for line in source:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue  # A live snapshot can end in a partial JSONL line.
                if row.get("status") != "ok":
                    continue
                start, end = row.get("t0"), row.get("tend")
                if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
                    continue
                for sample in samples:
                    window_start, window_end = windows[sample]
                    if not window_start <= start < window_end or not window_start <= end < window_end:
                        continue
                    bucket = values[sample]
                    bucket["count"] += 1
                    latency = row.get("latency_ms")
                    ttft = row.get("ttft_ms")
                    tokens = row.get("output_tokens")
                    if isinstance(latency, (int, float)) and math.isfinite(latency) and latency >= 0:
                        bucket["e2e"].append(float(latency))
                        if (isinstance(ttft, (int, float)) and math.isfinite(ttft) and
                                isinstance(tokens, int) and tokens > 1 and latency >= ttft):
                            bucket["tpot"].append((latency - ttft) / (tokens - 1))
                    break
    for sample, bucket in values.items():
        item = samples[sample]
        if bucket["count"] != item["successful_requests"]:
            continue  # Wait for a complete snapshot rather than plot a biased distribution.
        if bucket["tpot"]:
            item["metrics"]["time_per_output_token"] = nyann_distribution(bucket["tpot"])
        if "request_latency" not in item["metrics"] and bucket["e2e"]:
            item["metrics"]["request_latency"] = nyann_distribution(bucket["e2e"])


def nyann_stage_windows(log: str, concurrencies: list[int],
                        measurements: list[dict[str, Any]], duration_seconds: int) -> dict[str, tuple[float, float]]:
    """Use Nyann's recorded stage times, or completed live rows and scheduled duration."""
    decoder = json.JSONDecoder()
    for match in reversed(list(re.finditer(r"(?m)^\{", log))):
        try:
            candidate, _ = decoder.raw_decode(log[match.start():])
        except json.JSONDecodeError:
            continue
        if not isinstance(candidate, dict) or not isinstance(candidate.get("stages"), list):
            continue
        timestamps = candidate.get("timestamps", {}).get("stages", [])
        if len(timestamps) != len(concurrencies) or [stage.get("concurrency") for stage in timestamps] != concurrencies:
            raise RuntimeError("Nyann stage timestamps differ from the requested sweep")
        windows = {}
        for index, stage in enumerate(timestamps, 1):
            start, end = stage["start_time"], stage["end_time"]
            if not isinstance(start, (int, float)) or not isinstance(end, (int, float)) or end <= start:
                raise RuntimeError(f"Nyann stage {index} has invalid timestamps")
            windows[f"stage-{index}"] = (start, end)
        return windows
    starts = {}
    for match in re.finditer(
        r'(?m)^time=(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z).*?'
        r'Stage started" stage=(\d+)/\d+ concurrency=(\d+)', log):
        timestamp, index, concurrency = match.groups()
        index = int(index)
        if 1 <= index <= len(concurrencies) and concurrencies[index - 1] == int(concurrency):
            starts[index] = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp()
    windows = {}
    for measurement in measurements:
        match = re.fullmatch(r"stage-(\d+)", measurement["sample"])
        if match is None:
            continue
        index = int(match.group(1))
        if index not in starts:
            continue
        start = starts[index]
        end = starts.get(index + 1, start + duration_seconds)
        if end > start:
            windows[measurement["sample"]] = (start, end)
    return windows


@contextmanager
def grafana_connection(monitoring: dict[str, str]):
    """Use configured in-cluster URL or a short-lived local service tunnel."""
    if "grafana_url" in monitoring:
        user = os.environ.get("CAMPAIGN_GRAFANA_USER")
        password = os.environ.get("CAMPAIGN_GRAFANA_PASSWORD")
        if not user or not password:
            raise RuntimeError("Grafana credentials from monitoring.auth_secret are unavailable")
        yield monitoring["grafana_url"], f"{user}:{password}"
        return
    namespace = monitoring["grafana_namespace"]
    secret = json.loads(kube(namespace, "get", "secret", monitoring["auth_secret"], "-o", "json").stdout)
    try:
        user = base64.b64decode(secret["data"]["admin-user"]).decode()
        password = base64.b64decode(secret["data"]["admin-password"]).decode()
    except (KeyError, ValueError, UnicodeDecodeError) as exc:
        raise RuntimeError("Grafana Secret needs admin-user and admin-password") from exc
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    forward = subprocess.Popen(
        ["kubectl", "-n", namespace, "port-forward", f"svc/{monitoring['grafana_service']}", f"{port}:80"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(60):
            if forward.poll() is not None:
                raise RuntimeError("Grafana port-forward exited before becoming ready")
            try:
                with urllib.request.urlopen(url + "/api/health", timeout=1):
                    break
            except (OSError, urllib.error.URLError):
                time.sleep(0.25)
        else:
            raise RuntimeError("Grafana port-forward did not become ready")
        yield url, f"{user}:{password}"
    finally:
        forward.terminate()
        try:
            forward.wait(timeout=5)
        except subprocess.TimeoutExpired:
            forward.kill()
            forward.wait()


def export_campaign_monitoring(config: dict[str, Any], artifact: Path, job_name: str,
                               baseline: list[str], measurements: list[dict[str, Any]]) -> None:
    """Use the existing Grafana exporter on the completed AIPerf sweep."""
    monitoring = config.get("monitoring")
    if not monitoring:
        return
    log = kube(config["namespace"], "logs", f"job/{job_name}", "--timestamps=true").stdout
    log_path = artifact / "aiperf-job.log"
    log_path.write_text(log)
    names = [entry.split(":", 1)[0] for entry in baseline]
    if not names:
        raise RuntimeError("Grafana export needs the saved serving Pod names")
    directories = [artifact / measurement["sample"] for measurement in measurements]
    with grafana_connection(monitoring) as (url, auth):
        env = os.environ.copy()
        env["CAMPAIGN_GRAFANA_AUTH"] = auth
        command = [sys.executable, str(ROOT / "export_dashboard.py"),
                   "--grafana-url", url, "--auth-env", "CAMPAIGN_GRAFANA_AUTH",
                   "--dashboard", monitoring["dashboard_uid"],
                   "--plotly-bundle", str(ROOT / "live-aiperf" / "plotly-basic-2.35.2.min.js.gz"),
                   "--aiperf-log", str(log_path), "--pod-regex", "|".join(re.escape(name) for name in names),
                   "--metrics-namespace", config["namespace"],
                   "results", *(str(directory) for directory in directories), "--pad", "0"]
        output = call(command, env=env).stdout
    (artifact / "grafana-export.log").write_text(output)
    if any(not (directory / "dashboard.html").is_file() or not (directory / "dashboard.html").stat().st_size
           for directory in directories):
        raise RuntimeError("Grafana exporter did not produce every AIPerf dashboard")


def export_nyann_monitoring(config: dict[str, Any], overlay_dir: Path, serving_pods: list[str],
                            measurements: list[dict[str, Any]], log: str, bench: dict[str, Any]) -> None:
    """Export one Grafana dashboard for each completed Nyann stage."""
    monitoring = config.get("monitoring")
    if not monitoring or not measurements:
        return
    names = [entry.split(":", 1)[0] for entry in serving_pods]
    if not names:
        raise RuntimeError("Nyann Grafana export needs the serving Pod names")
    windows = nyann_stage_windows(log, bench["concurrencies"], measurements, bench["duration_seconds"])
    if {item["sample"] for item in measurements} - set(windows):
        raise RuntimeError("Nyann log has no time window for a completed stage")
    tasks = []
    for measurement in measurements:
        sample = measurement["sample"]
        start, end = windows[sample]
        directory = overlay_dir / "nyann" / sample
        directory.mkdir(parents=True, exist_ok=True)
        dashboard = directory / "dashboard.html"
        marker = directory / "dashboard-window.json"
        identity = {"start": start, "end": end, "pods": names,
                    "dashboard_uid": monitoring["dashboard_uid"]}
        if dashboard.is_file() and dashboard.stat().st_size and AIPERF_REPORT.read_json(marker) == identity:
            continue
        tasks.append((sample, start, end, directory, dashboard, marker, identity))
    if not tasks:
        return
    with grafana_connection(monitoring) as (url, auth):
        env = os.environ.copy()
        env["CAMPAIGN_GRAFANA_AUTH"] = auth
        for sample, start, end, directory, dashboard, marker, identity in tasks:
            staged = directory / "dashboard.html.tmp"
            command = [sys.executable, str(ROOT / "export_dashboard.py"),
                       "--grafana-url", url, "--auth-env", "CAMPAIGN_GRAFANA_AUTH",
                       "--dashboard", monitoring["dashboard_uid"],
                       "--plotly-bundle", str(ROOT / "live-aiperf" / "plotly-basic-2.35.2.min.js.gz"),
                       "--pod-regex", "|".join(re.escape(name) for name in names),
                       "--metrics-namespace", config["namespace"],
                       "single", "--start", str(start), "--end", str(end), "--output", str(staged)]
            try:
                call(command, env=env)
                if not staged.is_file() or not staged.stat().st_size:
                    raise RuntimeError(f"Grafana exporter did not produce {sample} dashboard")
                staged.replace(dashboard)
                marker.write_text(json.dumps(identity, indent=2) + "\n")
            finally:
                staged.unlink(missing_ok=True)


def submit_benchmark(config: dict[str, Any], overlay: dict[str, Any], bench: dict[str, Any],
                     campaign_dir: Path, baseline: list[str], commit: str | None, source_commit: str,
                     artifact_fetcher: Callable[[Path], Path] | None = None) -> dict[str, Any]:
    tool = bench["tool"]
    run_id = f"{config['id']}-{overlay['name']}-{tool}"
    if len(run_id) > 120:
        raise ValueError("campaign/overlay names produce a run ID longer than 120 characters")
    env = os.environ.copy()
    env.update({"MODEL_LABEL": overlay["model_label"], "RESULTS_PVC": config["results_pvc"],
                "LIVE_BENCHMARK_QUEUE": config["benchmark_queue"],
                "LIVE_AIPERF_NAMESPACE": config["namespace"], "LIVE_NYANN_NAMESPACE": config["namespace"],
                "LIVE_AIPERF_RUN_ID": run_id, "LIVE_NYANN_RUN_ID": run_id,
                "LIVE_BENCHMARK_SOURCE_REF": config["source"]["ref"],
                "LIVE_BENCHMARK_SOURCE_COMMIT": source_commit,
                "LIVE_BENCHMARK_SOURCE_KIND": "llm-d"})
    if "base_url" in config:
        env["BASE_URL"] = config["base_url"]
    concurrencies = ",".join(str(value) for value in bench["concurrencies"])
    if tool == "aiperf":
        env["MAX_CONTEXT_LENGTH"] = str(bench["max_context_length"])
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
        diagnostics_path = campaign_dir / f"{tool}-job-failure.txt"
        diagnostics = []
        for label, args in (("Logs", ("logs", f"job/{job_name}", "--all-containers=true")),
                            ("Job", ("describe", "job", job_name)),
                            ("Pods", ("describe", "pods", "-l", f"job-name={job_name}"))):
            try:
                result = kube(config["namespace"], *args, check=False)
                diagnostics.append(f"## {label}\n{result.stdout}{result.stderr}")
            except Exception as exc:
                diagnostics.append(f"## {label}\nCould not collect diagnostics: {exc}\n")
        diagnostics_path.write_text("\n".join(diagnostics))
        deleted = kube(config["namespace"], "delete", "job", job_name, "--ignore-not-found",
                       "--cascade=foreground", "--wait=true",
                       f"--timeout={config['cleanup_timeout_seconds']}s", check=False)
        if deleted.returncode:
            raise CleanupError(f"could not remove child Job {job_name}: {deleted.stderr.strip()}") from failure
        raise RuntimeError(f"{failure}; diagnostics: {diagnostics_path}") from failure
    if snapshot(config["namespace"], overlay["pod_selector"]) != baseline or build_commit(config["namespace"]) != commit:
        raise RuntimeError(f"{overlay['name']}: serving deployment changed during {tool} benchmark")
    artifact = Path(f"/workload/{'aiperf-agentx' if tool == 'aiperf' else 'nyann-agentx'}/{run_id}")
    if artifact_fetcher is not None:
        artifact = artifact_fetcher(artifact)
    if not artifact.exists():
        raise RuntimeError(f"{job_name} completed without expected artifacts: {artifact}")
    measurements = []
    monitoring_error = None
    if tool == "aiperf":
        measurements = aiperf_measurements(artifact, run_id, bench["concurrencies"])
        try:
            export_campaign_monitoring(config, artifact, job_name, baseline, measurements)
        except Exception as exc:
            monitoring_error = f"Grafana export failed: {exc}"
    else:
        log = kube(config["namespace"], "logs", f"job/{job_name}").stdout
        (campaign_dir / "nyann-job.log").write_text(log)
        measurements = nyann_measurements(log, bench["concurrencies"])
        enrich_nyann_request_metrics(measurements, log, bench["concurrencies"], bench["duration_seconds"],
                                     sorted(artifact.glob("requests_*.jsonl")))
        try:
            export_nyann_monitoring(config, campaign_dir, baseline, measurements, log, bench)
        except Exception as exc:
            monitoring_error = f"Grafana export failed: {exc}"
    result = {"tool": tool, "job": job_name, "run_id": run_id, "artifacts": str(artifact),
            "report": f"{artifact}/index.html" if tool == "aiperf" else None,
            "measurements": measurements, "status": "failed" if monitoring_error else "completed"}
    if monitoring_error:
        result["error"] = monitoring_error
    return result


def write_summary(destination: Path, summary: dict[str, Any]) -> None:
    tmp = destination / "summary.json.tmp"
    tmp.write_text(json.dumps(summary, indent=2) + "\n")
    tmp.replace(destination / "summary.json")
    dimensions = sorted({key for record in summary["overlays"] for key in record.get("dimensions", {})})
    fields = ["build", "overlay", *dimensions, "tool", "sample", "concurrency", "status",
              "successful_requests", "error_requests",
              "requests_per_s", "requests_unit", "output_tokens_per_s", "output_tokens_unit",
              "ttft_p90", "ttft_unit", "itl_p90", "itl_unit", "tpot_p90", "tpot_unit",
              "e2e_p90", "e2e_unit", "report", "artifacts", "error",
              "llm_d_commit", "vllm_commits", "deepep_commit", "vllm_image"]
    rows = []
    for record in summary["overlays"]:
        build_inputs = record.get("vllm_build_inputs")
        steps = build_inputs.get("steps", []) if build_inputs else []
        vllm_commits = (
            ", ".join(f"{step['action']} {step['ref']}@{step['commit']}" for step in steps)
            if steps else "N/A (using configured image; no source commit)" if build_inputs else
            "unknown (build not resolved)"
        )
        deepep_commit = build_inputs.get("deepep", {}).get("commit", "") if build_inputs else ""
        for bench in record.get("benchmarks") or [{"tool": "", "status": record["status"], "error": record.get("error", "")}]:
            for measurement in bench.get("measurements") or [{}]:
                metrics = measurement.get("metrics", {})
                row = {"build": record.get("build", ""), "overlay": record.get("overlay", record["name"]),
                       "tool": bench["tool"], "sample": measurement.get("sample", ""),
                       "concurrency": measurement.get("concurrency", ""), "status": bench["status"],
                       "successful_requests": measurement.get("successful_requests", ""),
                       "error_requests": measurement.get("error_requests", ""),
                       "requests_per_s": metrics.get("request_throughput", {}).get("avg", ""),
                       "requests_unit": metrics.get("request_throughput", {}).get("unit", ""),
                       "output_tokens_per_s": metrics.get("output_token_throughput", {}).get("avg", ""),
                       "output_tokens_unit": metrics.get("output_token_throughput", {}).get("unit", ""),
                       "ttft_p90": metrics.get("time_to_first_token", {}).get("p90", ""),
                       "ttft_unit": metrics.get("time_to_first_token", {}).get("unit", ""),
                       "itl_p90": metrics.get("inter_token_latency", {}).get("p90", ""),
                       "itl_unit": metrics.get("inter_token_latency", {}).get("unit", ""),
                       "tpot_p90": metrics.get("time_per_output_token", {}).get("p90", ""),
                       "tpot_unit": metrics.get("time_per_output_token", {}).get("unit", ""),
                       "e2e_p90": metrics.get("request_latency", {}).get("p90", ""),
                       "e2e_unit": metrics.get("request_latency", {}).get("unit", ""),
                       "report": bench.get("report", ""),
                       "artifacts": bench.get("artifacts", ""), "error": bench.get("error", ""),
                       "llm_d_commit": summary.get("source_commit", ""),
                       "vllm_commits": vllm_commits,
                       "deepep_commit": deepep_commit,
                       "vllm_image": summary.get("vllm_image", "")}
                row.update(record.get("dimensions", {}))
                rows.append(row)
    with (destination / "comparison.csv").open("w", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def nyann_chart_runs(destination: Path, summary: dict[str, Any], record: dict[str, Any],
                     bench: dict[str, Any], gpu_metadata: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Shape Nyann measurements for the shared interactive chart renderer."""
    build = record.get("vllm_build_inputs", {})
    steps = build.get("steps", [])
    identity = " + ".join(f"{step['ref']}@{step['commit'][:12]}" for step in steps)
    dimensions = ", ".join(f"{key}={value}" for key, value in sorted(record.get("dimensions", {}).items()))
    label = f"{record.get('build', 'nightly')} / {record.get('overlay', record['name'])}"
    label += f" — vLLM {identity or summary.get('vllm_image', 'nightly image')}"
    if dimensions:
        label += f" ({dimensions})"
    repeats = Counter(item["concurrency"] for item in bench["measurements"])
    seen: Counter[int] = Counter()
    runs = []
    for measurement in bench["measurements"]:
        concurrency = measurement["concurrency"]
        seen[concurrency] += 1
        directory = destination / record["name"] / "nyann" / measurement["sample"]
        dashboard = directory / "dashboard.html"
        runs.append({
            "directory": directory,
            "profile": measurement["metrics"],
            "metadata": {
                "run_id": f"{bench.get('run_id', summary['id'] + '-' + record['name'] + '-nyann')}-c{concurrency}",
                "concurrency": concurrency,
                "repeat_count": repeats[concurrency], "repeat_index": seen[concurrency],
                "campaign_label": f"{label} / Nyann",
                "benchmark_tool": "nyann", "model_label": summary["id"],
                "source_kind": "llm-d", "source_ref": summary.get("source_ref", "unknown"),
                "source_commit": summary.get("source_commit", ""),
                "vllm_image": summary.get("vllm_image", ""),
                "vllm_build_steps": steps, "deepep_build": build.get("deepep"),
                **(gpu_metadata or {}),
            },
            "yaml": "", "aiperf_job_yaml": "", "llmd_yaml": "",
            "dashboard": dashboard.read_bytes() if dashboard.is_file() else None,
        })
    return runs


def write_final_report(destination: Path, summary: dict[str, Any]) -> None:
    """Pass campaign samples to the existing interactive report renderer."""
    runs = []
    for record in summary["overlays"]:
        build = record.get("vllm_build_inputs", {})
        steps = build.get("steps", [])
        identity = " + ".join(f"{step['ref']}@{step['commit'][:12]}" for step in steps)
        dimensions = ", ".join(f"{key}={value}" for key, value in sorted(record.get("dimensions", {}).items()))
        label = f"{record.get('build', 'nightly')} / {record.get('overlay', record['name'])}"
        label += f" — vLLM {identity or summary.get('vllm_image', 'nightly image')}"
        if dimensions:
            label += f" ({dimensions})"
        gpu_metadata = {}
        for bench in record.get("benchmarks", []):
            if bench.get("tool") != "aiperf" or (bench.get("status") != "completed" and not bench.get("measurements")):
                continue
            artifact = Path(bench["artifacts"])
            sweep_runs = 0
            for directory in sorted(artifact.iterdir()):
                data = AIPERF_REPORT.run_data(directory)
                if data is None:
                    continue
                data["metadata"].update({
                    "campaign_label": f"{label} / AIPerf",
                    "benchmark_tool": "aiperf",
                    "vllm_image": summary.get("vllm_image", ""),
                    "vllm_build_steps": steps,
                    "vllm_build_commit": record.get("vllm_build_commit", steps[0]["commit"] if steps else ""),
                    "deepep_build": build.get("deepep"),
                })
                gpu_metadata = {key: data["metadata"][key] for key in
                                ("prefill_gpu_count", "decode_gpu_count", "total_gpu_count")
                                if key in data["metadata"]}
                metadata_file = directory / "benchmark-metadata.json"
                metadata_file.write_text(json.dumps(data["metadata"], indent=2) + "\n")
                runs.append(data)
                sweep_runs += 1
            if sweep_runs:
                AIPERF_REPORT.write_index(artifact)
        for bench in record.get("benchmarks", []):
            if bench.get("tool") != "nyann" or not bench.get("measurements"):
                continue
            runs.extend(nyann_chart_runs(destination, summary, record, bench, gpu_metadata))
    write_summary(destination, summary)
    campaign_config = AIPERF_REPORT.read_json(destination / "campaign.json")
    AIPERF_REPORT.write_index_from_runs(destination, runs, model_label=f"Campaign {summary['id']}",
                                        save_monitoring_overlay=False, compact_header=True,
                                        campaign=summary,
                                        extra_html=AIPERF_REPORT.nyann_setup(campaign_config, destination))


def render_overlay(overlay_root: Path, overlay: dict[str, Any]) -> str:
    path = (overlay_root / overlay["path"]).resolve(strict=True)
    if not path.is_relative_to(overlay_root) or not path.is_dir():
        raise ValueError(f"overlay {overlay['name']} escapes overlay_root or is not a directory")
    rendered = call(["kubectl", "kustomize", str(path)]).stdout
    if not rendered.strip():
        raise RuntimeError(f"overlay {overlay['name']} rendered no resources")
    return rendered


def prepare_matrix_builds(config: dict[str, Any], overlay_root: Path, destination: Path,
                          summary: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, str], bool, bool]:
    """Build every variant before starting any serving overlay."""
    base_manifests = {overlay["name"]: prepare_overlay_manifest(render_overlay(overlay_root, overlay), config, overlay)
                      for overlay in config["overlays"]}
    resolved = {}
    pins: dict[tuple[str, str], str] = {}
    failed = False
    stopped = False
    summary["builds"] = []
    for variant in config["builds"]:
        name = variant["name"]
        record: dict[str, Any] = {"name": name, "status": "running", "prebuilds": []}
        summary["builds"].append(record)
        folder = destination / "builds" / name
        folder.mkdir(parents=True)
        write_summary(destination, summary)
        cleanup_error = False
        try:
            build = resolve_build({**({"repo": config["build_repo"]} if "build_repo" in config else {}),
                                   "steps": variant.get("steps", []),
                                   **({"deepep": variant["deepep"]} if "deepep" in variant else {})}, pins)
            record["inputs"] = build
            resolved[name] = build
            for overlay in config["overlays"]:
                overlay_folder = folder / overlay["name"]
                overlay_folder.mkdir()
                rendered = inject_vllm_build_script(base_manifests[overlay["name"]], build,
                                                    config["results_pvc"])
                validate_manifest(rendered, config["namespace"])
                manifest = overlay_folder / "manifest.yaml"
                manifest.write_text(rendered)
                existing = kube(config["namespace"], "get", "-f", str(manifest), "--ignore-not-found", "-o", "name").stdout.strip()
                if existing:
                    raise RuntimeError(f"prebuild would modify pre-existing resources: {existing}")
                if snapshot(config["namespace"], "llm-d.ai/inference-serving=true"):
                    raise RuntimeError("prebuild requires an empty serving namespace")
                commit = vllm_prebuild(rendered, config, overlay, overlay_folder) if build["mode"] == "source" or "deepep" in build else None
                if (build["mode"] == "source" or "deepep" in build) and not commit:
                    raise RuntimeError(f"overlay {overlay['name']} has no compatible vLLM build script")
                if commit:
                    prebuild = {"overlay": overlay["name"], "mode": build["mode"],
                                "log": str(overlay_folder / "build.log")}
                    if build["mode"] == "source":
                        prebuild["base_commit"] = commit
                    record["prebuilds"].append(prebuild)
                write_summary(destination, summary)
            record["status"] = "completed"
        except Exception as exc:
            record["status"] = "failed"
            record["error"] = str(exc)
            cleanup_error = isinstance(exc, CleanupError)
            resolved.pop(name, None)
            failed = True
        write_summary(destination, summary)
        if record["status"] == "failed" and (cleanup_error or not config["continue_on_failure"]):
            stopped = True
            break
    return resolved, base_manifests, failed, stopped


def write_mock_report(config: dict[str, Any], destination: Path, summary: dict[str, Any]) -> None:
    """Feed deterministic mock artifacts to the existing AIPerf report path."""
    summary["mode"] = "mock-test"
    for record in summary["overlays"]:
        if record["status"] != "validated":
            continue
        for bench in record["benchmarks"]:
            concurrencies = bench.pop("concurrencies")
            if bench["tool"] == "aiperf":
                run_id = f"{config['id']}-{record['name']}-aiperf"
                artifact = destination / "mock-artifacts" / run_id
                counts = Counter(concurrencies)
                seen: Counter[int] = Counter()
                for concurrency in concurrencies:
                    seen[concurrency] += 1
                    sample = (f"c{concurrency}" if counts[concurrency] == 1 else
                              f"c{concurrency}-r{seen[concurrency]}")
                    folder = artifact / sample
                    folder.mkdir(parents=True)
                    (folder / "benchmark-metadata.json").write_text(json.dumps({
                        "run_id": f"{run_id}-{sample}", "concurrency": concurrency,
                        "source_kind": "llm-d", "source_ref": config["source"]["ref"],
                        "source_commit": summary["source_commit"], "model_label": overlay_model_label(config, record),
                        "topology": record.get("dimensions", {}).get("topology", "unknown"),
                        "total_gpu_count": 2, "prefill_gpu_count": 1, "decode_gpu_count": 1,
                        "mock_data": True,
                    }, indent=2) + "\n")
                    (folder / "profile_export_aiperf.json").write_text(json.dumps({
                        "request_throughput": {"avg": concurrency, "unit": "req/s"},
                        "output_token_throughput": {"avg": concurrency * 100, "unit": "tokens/s"},
                        "input_token_throughput": {"avg": concurrency * 200, "unit": "tokens/s"},
                        "e2e_output_token_throughput": {"avg": 100, "unit": "tokens/s"},
                        "time_to_first_token": {"p90": 100, "unit": "ms"},
                        "inter_token_latency": {"p90": 10, "unit": "ms"},
                    }, indent=2) + "\n")
                bench.update({"status": "mocked", "artifacts": str(artifact),
                              "measurements": aiperf_measurements(artifact, run_id, concurrencies)})
            else:
                bench.update({"status": "mocked", "measurements": [
                    {"sample": f"stage-{index}", "concurrency": concurrency,
                     "metrics": {"request_throughput": {"avg": concurrency, "unit": "req/s"},
                                 "output_token_throughput": {"avg": concurrency * 100, "unit": "tokens/s"},
                                 "time_to_first_token": {"p90": 100, "unit": "ms"},
                                 "inter_token_latency": {"p90": 10, "unit": "ms"}}}
                    for index, concurrency in enumerate(concurrencies, 1)]})
    write_final_report(destination, summary)


def overlay_model_label(config: dict[str, Any], record: dict[str, Any]) -> str:
    return next(overlay["model_label"] for overlay in config["overlays"] if overlay["name"] == record["overlay"])


def test_local(config: dict[str, Any], destination: Path, source_dir: Path | None = None) -> int:
    """Render and validate every campaign case without contacting Kubernetes."""
    destination.mkdir(parents=True, exist_ok=False)
    (destination / "campaign.json").write_text(json.dumps(config, indent=2) + "\n")
    def save_plan() -> None:
        (destination / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")

    summary: dict[str, Any] = {
        "id": config["id"], "mode": "local-test", "status": "validating",
        "started_at": datetime.now(timezone.utc).isoformat(), "overlays": [], "builds": [],
        "vllm_image": config["vllm_image"],
    }
    temporary_source = source_dir is None
    try:
        if temporary_source:
            overlay_root, source_commit = fetch_source(config["source"])
        else:
            overlay_root = source_dir.resolve(strict=True)
            if not overlay_root.is_dir():
                raise ValueError("--source-dir must be an llm-d checkout directory")
            source_commit = call(["git", "-C", str(overlay_root), "rev-parse", "HEAD"], timeout=60).stdout.strip()
            if not re.fullmatch(r"[0-9a-f]{40}", source_commit):
                raise ValueError("--source-dir has no valid Git HEAD")
            summary["source_dir"] = str(overlay_root)
        summary["source_commit"] = source_commit
        (destination / "source-commit.txt").write_text(source_commit + "\n")
        pins: dict[tuple[str, str], str] = {}
        resolved: dict[str, dict[str, Any]] = {}
        if "builds" in config:
            for variant in config["builds"]:
                record: dict[str, Any] = {"name": variant["name"], "status": "validated"}
                summary["builds"].append(record)
                try:
                    recipe = {"steps": variant.get("steps", [])}
                    if "build_repo" in config:
                        recipe["repo"] = config["build_repo"]
                    if "deepep" in variant:
                        recipe["deepep"] = variant["deepep"]
                    build = resolve_build(recipe, pins)
                    record["inputs"] = build
                    resolved[variant["name"]] = build
                except Exception as exc:
                    record["status"] = "failed"
                    record["error"] = str(exc)
        cases = ([(variant, overlay) for variant in config["builds"] for overlay in config["overlays"]]
                 if "builds" in config else [(None, overlay) for overlay in config["overlays"]])
        for variant, overlay in cases:
            name = f"{variant['name']}-{overlay['name']}" if variant else overlay["name"]
            record = {"name": name, "overlay": overlay["name"], "dimensions": overlay.get("dimensions", {}),
                      "status": "validated", "benchmarks": []}
            if variant:
                record["build"] = variant["name"]
            summary["overlays"].append(record)
            if variant and variant["name"] not in resolved:
                record["status"] = "skipped"
                record["error"] = "build branch resolution failed"
                continue
            folder = destination / name
            folder.mkdir()
            try:
                build = (resolved[variant["name"]] if variant else
                         resolve_build(overlay["build"], pins) if "build" in overlay else
                         {"mode": "nightly", "steps": []})
                record["vllm_build_inputs"] = build
                rendered = render_overlay(overlay_root, overlay)
                rendered = prepare_overlay_manifest(rendered, config, overlay)
                rendered = inject_vllm_build_script(rendered, build, config["results_pvc"])
                validate_manifest(rendered, config["namespace"])
                (folder / "manifest.yaml").write_text(rendered)
                record["manifest_sha256"] = hashlib.sha256(rendered.encode()).hexdigest()
                if build["mode"] == "source" or "deepep" in build:
                    commit = vllm_prebuild(rendered, config, overlay, folder, preview_only=True)
                    if not commit:
                        raise ValueError("overlay has no compatible vLLM prebuild script")
                    record["prebuild_job"] = str(folder / "prebuild-job.yaml")
                for bench in config["benchmarks"]:
                    record["benchmarks"].append({"tool": bench["tool"], "status": "planned",
                                                 "concurrencies": bench["concurrencies"]})
            except Exception as exc:
                record["status"] = "failed"
                record["error"] = str(exc)
        summary["status"] = "failed" if any(item["status"] == "failed" for item in
                                             [*summary["builds"], *summary["overlays"]]) else "validated"
        summary["finished_at"] = datetime.now(timezone.utc).isoformat()
        if summary["status"] == "validated" and any(bench["tool"] == "aiperf" for bench in config["benchmarks"]):
            write_mock_report(config, destination, summary)
        else:
            save_plan()
        print(f"Local campaign test {summary['status']}; artifacts: {destination}", flush=True)
        return 0 if summary["status"] == "validated" else 1
    except Exception as exc:
        summary["status"] = "failed"
        summary["error"] = str(exc)
        summary["finished_at"] = datetime.now(timezone.utc).isoformat()
        save_plan()
        print(f"Local campaign test failed: {exc}; artifacts: {destination}", file=sys.stderr)
        return 1
    finally:
        if temporary_source and "overlay_root" in locals():
            shutil.rmtree(overlay_root)


def run(config: dict[str, Any], results_root: Path = Path("/workload"),
        artifact_fetcher: Callable[[Path], Path] | None = None) -> int:
    destination = results_root / "campaigns" / config["id"]
    destination.mkdir(parents=True, exist_ok=False)
    (destination / "campaign.json").write_text(json.dumps(config, indent=2) + "\n")
    summary: dict[str, Any] = {"id": config["id"], "status": "running", "started_at": datetime.now(timezone.utc).isoformat(),
                               "source_ref": config["source"]["ref"], "vllm_image": config["vllm_image"],
                               "planned_deployments": len(config["overlays"]) * len(config.get("builds", [None])),
                               "overlays": []}
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
    resolved_builds: dict[str, dict[str, Any]] = {}
    matrix_builds: dict[str, dict[str, Any]] = {}
    base_manifests: dict[str, str] = {}
    stopped = False
    if "builds" in config:
        try:
            matrix_builds, base_manifests, failed, stopped = prepare_matrix_builds(
                config, overlay_root, destination, summary)
        except Exception as exc:
            summary["status"] = "failed"
            summary["error"] = f"build preparation failed: {exc}"
            summary["finished_at"] = datetime.now(timezone.utc).isoformat()
            write_summary(destination, summary)
            shutil.rmtree(overlay_root)
            return 1
        build_status = {record["name"]: record for record in summary["builds"]}
        for variant in config["builds"]:
            name = variant["name"]
            if name not in build_status:
                build_status[name] = {"name": name, "status": "skipped", "error": "earlier build failed"}
                summary["builds"].append(build_status[name])
            if stopped or name not in matrix_builds:
                reason = ("build preparation stopped after a failure" if stopped and name in matrix_builds
                          else build_status[name].get("error", "build was not prepared"))
                for overlay in config["overlays"]:
                    summary["overlays"].append({"name": f"{name}-{overlay['name']}", "build": name,
                                                "overlay": overlay["name"], "dimensions": overlay.get("dimensions", {}),
                                                "status": "skipped", "error": reason, "benchmarks": []})
        write_summary(destination, summary)
    cases = ([(variant, overlay) for variant in config["builds"] if variant["name"] in matrix_builds
              for overlay in config["overlays"]] if not stopped and "builds" in config else
             [(None, overlay) for overlay in config["overlays"]] if "builds" not in config else [])
    for variant, base_overlay in cases:
        overlay = dict(base_overlay)
        if variant:
            overlay["name"] = f"{variant['name']}-{base_overlay['name']}"
        name = overlay["name"]
        print(f"Deploying overlay {name}", flush=True)
        record: dict[str, Any] = {"name": name, "status": "running", "benchmarks": [],
                                  "overlay": base_overlay["name"], "dimensions": base_overlay.get("dimensions", {})}
        if variant:
            record["build"] = variant["name"]
        summary["overlays"].append(record)
        write_summary(destination, summary)
        folder = destination / name
        folder.mkdir()
        manifest = folder / "manifest.yaml"
        applied = False
        try:
            rendered = (base_manifests[base_overlay["name"]] if variant else
                        prepare_overlay_manifest(render_overlay(overlay_root, overlay), config, overlay))
            build = matrix_builds[variant["name"]] if variant else None
            if build:
                record["vllm_build_inputs"] = build
            elif "build" in overlay:
                key = json.dumps(overlay["build"], sort_keys=True)
                if key not in resolved_builds:
                    resolved_builds[key] = resolve_build(overlay["build"])
                build = resolved_builds[key]
                record["vllm_build_inputs"] = build
            build = build or {"mode": "nightly", "steps": []}
            rendered = inject_vllm_build_script(rendered, build, config["results_pvc"])
            validate_manifest(rendered, config["namespace"])
            manifest.write_text(rendered)
            record["manifest_sha256"] = hashlib.sha256(rendered.encode()).hexdigest()
            existing = kube(config["namespace"], "get", "-f", str(manifest), "--ignore-not-found", "-o", "name").stdout.strip()
            if existing:
                raise RuntimeError(f"overlay {name} would modify pre-existing resources: {existing}")
            if snapshot(config["namespace"], "llm-d.ai/inference-serving=true"):
                raise RuntimeError(f"overlay {name} cannot start while serving Pods already exist in the namespace")
            applied = True  # prebuild or apply may partially succeed; clean up the saved manifest
            if not variant and (build["mode"] == "source" or "deepep" in build):
                prebuild_commit = vllm_prebuild(rendered, config, overlay, folder)
                if prebuild_commit:
                    if build["mode"] == "source":
                        record["prebuild_commit"] = prebuild_commit
                    else:
                        record["deepep_prebuilt"] = True
                    print(f"Overlay {name} build cache prepared", flush=True)
            kube(config["namespace"], "apply", "-f", str(manifest))
            baseline = wait_ready(config, overlay)
            print(f"Overlay {name} ready: {len(baseline)} serving Pods", flush=True)
            commit = build_commit(config["namespace"])
            record["serving_pods"] = baseline
            if commit and build["mode"] == "source":
                record["vllm_build_commit"] = commit
            (folder / "serving-pods.json").write_text(kube(config["namespace"], "get", "pods", "-l", overlay["pod_selector"], "-o", "json").stdout)
            for bench in config["benchmarks"]:
                try:
                    if artifact_fetcher is None:
                        result = submit_benchmark(config, overlay, bench, folder, baseline, commit, source_commit)
                    else:
                        result = submit_benchmark(config, overlay, bench, folder, baseline, commit, source_commit,
                                                  artifact_fetcher)
                except Exception as exc:
                    result = {"tool": bench["tool"], "status": "failed", "error": str(exc)}
                    if isinstance(exc, CleanupError):
                        record["cleanup_error"] = str(exc)
                    failed = True
                record["benchmarks"].append(result)
                write_summary(destination, summary)
                if result["status"] != "completed":
                    failed = True
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
    try:
        write_final_report(destination, summary)
    except Exception as exc:
        summary["status"] = "failed"
        summary["report_error"] = str(exc)
        write_summary(destination, summary)
        failed = True
    print(f"Campaign {config['id']} {summary['status']}; artifacts: {destination}", flush=True)
    return 1 if failed else 0


def run_local(config: dict[str, Any], output: Path) -> int:
    """Orchestrate with local kubectl; copy Job artifacts from the shared PVC."""
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    namespace = config["namespace"]
    pod_name = "campaign-artifacts-" + hashlib.sha256(config["id"].encode()).hexdigest()[:16]
    pod = {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": pod_name, "namespace": namespace,
           "labels": {"app.kubernetes.io/name": "benchmark-campaign-artifacts"}},
           "spec": {"restartPolicy": "Always", "containers": [{"name": "artifacts",
           "image": "docker.io/library/alpine:3.20", "command": ["sleep", "86400"],
           "resources": {"requests": {"cpu": "50m", "memory": "64Mi"},
                         "limits": {"cpu": "500m", "memory": "256Mi"}},
           "volumeMounts": [{"name": "results", "mountPath": "/workload"}]}],
           "volumes": [{"name": "results", "persistentVolumeClaim": {"claimName": config["results_pvc"]}}]}}
    kube(namespace, "get", "localqueue", config["benchmark_queue"])
    kube(namespace, "get", "pvc", config["results_pvc"])
    kube(namespace, "create", "-f", "-", input_text=json.dumps(pod))
    try:
        kube(namespace, "wait", "--for=condition=Ready", f"pod/{pod_name}", "--timeout=600s")

        def fetch(artifact: Path) -> Path:
            destination = output / artifact.relative_to("/workload")
            destination.parent.mkdir(parents=True, exist_ok=True)
            if artifact.parent.name == "aiperf-agentx":
                profiles = kube(namespace, "exec", pod_name, "--", "find", str(artifact),
                                "-mindepth", "2", "-maxdepth", "2", "-type", "f",
                                "-name", "profile_export_aiperf.json").stdout.splitlines()
                if not profiles:
                    raise RuntimeError(f"No completed AIPerf profiles in {artifact}")
                for profile in profiles:
                    copy_aiperf_sample(namespace, pod_name, Path(profile), destination)
            else:
                TRANSFER.download_tree(namespace, pod_name, str(artifact), destination)
            return destination

        return run(config, output, artifact_fetcher=fetch)
    except KeyboardInterrupt:
        campaign_dir = output / "campaigns" / config["id"]
        cleanup_error = None
        try:
            delete_campaign_benchmark_jobs(config)
        except Exception as exc:
            cleanup_error = str(exc)
        mark_campaign_cancelled(campaign_dir, cleanup_error)
        print(f"Campaign {config['id']} cancelled", flush=True)
        return 130
    finally:
        deleted = kube(namespace, "delete", "pod", pod_name, "--ignore-not-found", "--wait=true",
                       f"--timeout={config['cleanup_timeout_seconds']}s", check=False)
        if deleted.returncode:
            raise CleanupError(f"could not remove artifact Pod {pod_name}: {deleted.stderr.strip()}")


def start_local(config_path: Path, output: Path, campaign_id: str) -> Path:
    """Detach local orchestration while its workloads run in Kubernetes."""
    output = output.resolve()
    if output.exists():
        raise ValueError(f"Output directory already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    log_path = output.with_name(output.name + ".log")
    command = [sys.executable, str(Path(__file__).resolve()), "run-local",
               str(config_path.resolve()), "--output", str(output)]
    with log_path.open("x") as log:
        try:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log,
                                       stderr=subprocess.STDOUT, start_new_session=True)
        except Exception:
            log_path.unlink(missing_ok=True)
            raise
    summary_path = output / "campaigns" / campaign_id / "summary.json"
    for _ in range(30):
        returncode = process.poll()
        if returncode is not None:
            raise RuntimeError(f"Campaign runner exited ({returncode}); see {log_path}")
        if summary_path.is_file():
            break
        time.sleep(0.1)
    print(f"Campaign runner started: PID {process.pid}; log: {log_path}", flush=True)
    print(f"Campaign status: {summary_path}", flush=True)
    return log_path


def campaign_run_ids(config: dict[str, Any]) -> set[str]:
    ids = set()
    for variant in config.get("builds", [None]):
        for overlay in config["overlays"]:
            name = f"{variant['name']}-{overlay['name']}" if variant else overlay["name"]
            ids.update(f"{config['id']}-{name}-{bench['tool']}" for bench in config["benchmarks"])
    return ids


def delete_campaign_benchmark_jobs(config: dict[str, Any]) -> None:
    """Delete only benchmark Jobs carrying an exact run ID in this campaign."""
    namespace = config["namespace"]
    jobs = json.loads(kube(namespace, "get", "jobs", "-o", "json").stdout)["items"]
    run_ids = campaign_run_ids(config)
    for job in jobs:
        if job["metadata"].get("annotations", {}).get("benchmark.llm-d.ai/run-id") in run_ids:
            conditions = {item.get("type"): item.get("status") for item in job.get("status", {}).get("conditions", [])}
            if conditions.get("Complete") == "True" or conditions.get("Failed") == "True":
                continue
            name = job["metadata"]["name"]
            kube(namespace, "delete", "job", name, "--ignore-not-found", "--cascade=foreground",
                 "--wait=true", f"--timeout={config['cleanup_timeout_seconds']}s")


def mark_campaign_cancelled(campaign_dir: Path, cleanup_error: str | None = None) -> None:
    summary_path = campaign_dir / "summary.json"
    if not summary_path.is_file():
        return
    summary = json.loads(summary_path.read_text())
    if summary.get("status") != "running":
        return
    for record in summary.get("overlays", []):
        if record.get("status") == "running":
            record["status"] = "cancelled"
        for bench in record.get("benchmarks", []):
            if bench.get("status") == "running":
                bench["status"] = "cancelled"
    summary["status"] = "cancelled"
    summary["finished_at"] = datetime.now(timezone.utc).isoformat()
    if cleanup_error:
        summary["cleanup_error"] = cleanup_error
    write_summary(campaign_dir, summary)


def local_runner_pids(output: Path, config_path: Path | None = None) -> list[int]:
    """Find run-local Python processes for this exact absolute output path."""
    result = call(["ps", "-axo", "pid=,command="])
    pids = []
    for line in result.stdout.splitlines():
        fields = line.strip().split(maxsplit=1)
        if len(fields) != 2 or not fields[0].isdigit():
            continue
        try:
            args = shlex.split(fields[1])
        except ValueError:
            continue
        if len(args) < 5 or "run-local" not in args or "--output" not in args:
            continue
        action = args.index("run-local")
        if action == 0 or Path(args[action - 1]).resolve() != Path(__file__).resolve():
            continue
        if config_path is not None and (action + 1 >= len(args) or
                Path(args[action + 1]).resolve() != config_path.resolve()):
            continue
        index = args.index("--output")
        if index + 1 < len(args) and Path(args[index + 1]).is_absolute() and \
                Path(args[index + 1]).resolve() == output.resolve():
            pids.append(int(fields[0]))
    return pids


def stop_local(config: dict[str, Any], output: Path, config_path: Path) -> None:
    """Interrupt one local campaign, then verify and finish its cleanup."""
    output = output.resolve()
    campaign_dir = output / "campaigns" / config["id"]
    saved_config = campaign_dir / "campaign.json"
    if saved_config.is_file():
        saved = json.loads(saved_config.read_text())
        if {key: value for key, value in saved.items() if key != "monitoring"} != \
                {key: value for key, value in config.items() if key != "monitoring"}:
            raise ValueError(f"{campaign_dir} does not match this campaign configuration")
        status = json.loads((campaign_dir / "summary.json").read_text())["status"]
        if status != "running":
            print(f"Campaign {config['id']} is already {status}")
            return
    elif not output.is_dir():
        raise ValueError(f"No local campaign output at {output}")
    pids = local_runner_pids(output, config_path)
    if len(pids) > 1:
        raise RuntimeError(f"Found {len(pids)} matching run-local processes for {output}; "
                           "no signal was sent and no cluster resources were deleted")
    pid = pids[0] if pids else None
    if pid is not None:
        os.kill(pid, signal.SIGINT)
    else:
        print(f"No live runner for {config['id']}; reconciling saved campaign state", flush=True)
    job_error = None
    try:
        delete_campaign_benchmark_jobs(config)
    except Exception as exc:
        job_error = str(exc)
    if pid is not None:
        deadline = time.monotonic() + config["cleanup_timeout_seconds"] + 30
        while pid in local_runner_pids(output, config_path) and time.monotonic() < deadline:
            time.sleep(1)
        if pid in local_runner_pids(output, config_path):
            raise RuntimeError(f"Runner process {pid} is still active; cleanup is not complete")
    pod_name = "campaign-artifacts-" + hashlib.sha256(config["id"].encode()).hexdigest()[:16]
    kube(config["namespace"], "delete", "pod", pod_name, "--ignore-not-found", "--wait=true",
         f"--timeout={config['cleanup_timeout_seconds']}s")
    if not saved_config.is_file():
        campaign_dir.mkdir(parents=True, exist_ok=True)
        saved_config.write_text(json.dumps(config, indent=2) + "\n")
        write_summary(campaign_dir, {"id": config["id"], "status": "running", "overlays": [],
                                     "vllm_image": config["vllm_image"]})
    leftovers = []
    for overlay in config["overlays"]:
        leftovers.extend(snapshot(config["namespace"], overlay["pod_selector"]))
    cleanup_error = job_error or (f"Serving Pods remain: {', '.join(sorted(set(leftovers)))}" if leftovers else None)
    mark_campaign_cancelled(campaign_dir, cleanup_error)
    if cleanup_error:
        raise CleanupError(f"Campaign {config['id']} was interrupted but cleanup needs attention: {cleanup_error}")
    print(f"Campaign {config['id']} cancelled; artifacts retained at {output}", flush=True)


def copy_aiperf_sample(namespace: str, pod_name: str, profile: Path, destination: Path) -> Path:
    """Copy report inputs without transferring potentially huge raw traces."""
    sample_name = profile.parent.name
    if profile.name != "profile_export_aiperf.json" or not re.fullmatch(r"c[1-9][0-9]*(?:-r[1-9][0-9]*)?", sample_name):
        raise ValueError(f"Invalid AIPerf profile path: {profile}")
    sample = destination / sample_name
    sample.mkdir(parents=True, exist_ok=True)
    for filename in ("profile_export_aiperf.json", "benchmark-metadata.json",
                     "serving-pods.yaml", "aiperf-job.yaml", "llm-d-deployment.yaml", "dashboard.html"):
        required = filename in {"profile_export_aiperf.json", "benchmark-metadata.json"}
        target = sample / filename
        try:
            TRANSFER.download_file(namespace, pod_name, str(profile.parent / filename), target)
        except RuntimeError:
            if required:
                raise
    return sample


def copy_nyann_request_files(config: dict[str, Any], pod_name: str, record: dict[str, Any],
                             campaign_dir: Path, *, stable: bool) -> list[Path]:
    """Bring Nyann request metrics to the local report, snapshotting active JSONL first."""
    namespace = config["namespace"]
    run_id = f"{config['id']}-{record['name']}-nyann"
    remote_root = Path("/workload/nyann-agentx") / run_id
    listing = kube(namespace, "exec", pod_name, "--", "find", str(remote_root), "-maxdepth", "1",
                   "-type", "f", "-name", "requests_*.jsonl", check=False)
    if listing.returncode:
        return []
    files = []
    for line in sorted(listing.stdout.splitlines()):
        remote = Path(line)
        if remote.parent != remote_root or not re.fullmatch(r"requests_[0-9]+\.jsonl", remote.name):
            raise RuntimeError(f"Unexpected Nyann request artifact: {remote}")
        local = campaign_dir / record["name"] / "nyann-requests" / remote.name
        if stable:
            TRANSFER.download_file(namespace, pod_name, str(remote), local)
        else:
            snapshot = kube(namespace, "exec", pod_name, "--", "sh", "-c",
                            'snapshot="$(mktemp /tmp/nyann-requests.XXXXXX)"; '
                            'cp "$1" "$snapshot"; printf "%s" "$snapshot"',
                            "sh", str(remote)).stdout.strip()
            try:
                TRANSFER.download_file(namespace, pod_name, snapshot, local)
            finally:
                kube(namespace, "exec", pod_name, "--", "rm", "-f", snapshot, check=False)
        files.append(local)
    return files


def download_pvc_artifacts(config: dict[str, Any], remote: str | None, output: Path) -> Path:
    """Resume this campaign's PVC artifacts after a run-local process has exited."""
    root = Path("/workload")
    if remote is not None:
        path = Path(remote)
        if not path.is_absolute() or ".." in path.parts or path == root or root not in path.parents:
            raise ValueError("--remote must be an artifact directory below /workload")
    pod_name = "campaign-download-" + secrets.token_hex(8)
    namespace = config["namespace"]
    pod = {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": pod_name, "namespace": namespace},
           "spec": {"restartPolicy": "Never", "containers": [{"name": "artifacts",
           "image": "docker.io/library/alpine:3.20", "command": ["sleep", "86400"],
           "resources": {"requests": {"cpu": "50m", "memory": "64Mi"},
                         "limits": {"cpu": "500m", "memory": "256Mi"}},
           "volumeMounts": [{"name": "results", "mountPath": "/workload"}]}],
           "volumes": [{"name": "results", "persistentVolumeClaim": {"claimName": config["results_pvc"]}}]}}
    kube(namespace, "create", "-f", "-", input_text=json.dumps(pod))
    try:
        kube(namespace, "wait", "--for=condition=Ready", f"pod/{pod_name}", "--timeout=600s")
        listing = kube(namespace, "exec", pod_name, "--", "sh", "-c",
                       'for root in /workload/aiperf-agentx /workload/nyann-agentx; do '
                       'if test -d "$root"; then find "$root" -mindepth 1 -maxdepth 1 -type d; fi; done').stdout
        expected = campaign_run_ids(config)
        available = sorted(Path(line) for line in listing.splitlines()
                           if Path(line).name in expected and Path(line).parent.name in
                           {"aiperf-agentx", "nyann-agentx"})
        if remote is None:
            paths = available
            if not paths:
                raise RuntimeError(f"No PVC artifact directories exist for campaign {config['id']}")
        else:
            paths = [path]
            if path not in available:
                choices = ", ".join(str(item) for item in available) or "none"
                raise RuntimeError(f"Artifact directory {path} does not exist for this campaign. Available: {choices}. "
                                   "Omit --remote to download all available artifacts.")
        for item in paths:
            destination = output.resolve() / item.relative_to(root)
            TRANSFER.download_tree(namespace, pod_name, str(item), destination)
            print(f"Downloaded artifacts: {destination}", flush=True)
    finally:
        deleted = kube(namespace, "delete", "pod", pod_name, "--ignore-not-found", "--wait=true",
                       f"--timeout={config['cleanup_timeout_seconds']}s", check=False)
        if deleted.returncode:
            raise CleanupError(f"could not remove artifact Pod {pod_name}: {deleted.stderr.strip()}")
    return output.resolve()


def download_cluster_report(config: dict[str, Any], output: Path,
                            destination: Path | None = None) -> Path:
    """Fetch a finished cluster Job's report inputs and render one local HTML file."""
    namespace = config["namespace"]
    remote_campaign = Path("/workload/campaigns") / config["id"]
    campaign_dir = output.resolve() / "campaigns" / config["id"]
    pod_name = "campaign-download-" + secrets.token_hex(8)
    pod = {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": pod_name, "namespace": namespace},
           "spec": {"restartPolicy": "Never", "containers": [{"name": "artifacts",
           "image": "docker.io/library/alpine:3.20", "command": ["sleep", "86400"],
           "resources": {"requests": {"cpu": "50m", "memory": "64Mi"},
                         "limits": {"cpu": "500m", "memory": "256Mi"}},
           "volumeMounts": [{"name": "results", "mountPath": "/workload"}]}],
           "volumes": [{"name": "results", "persistentVolumeClaim": {"claimName": config["results_pvc"]}}]}}
    kube(namespace, "create", "-f", "-", input_text=json.dumps(pod))
    try:
        kube(namespace, "wait", "--for=condition=Ready", f"pod/{pod_name}", "--timeout=600s")
        # Copy only stable files while a runner is writing its campaign tree.
        remote_summary = json.loads(kube(namespace, "exec", pod_name, "--", "cat",
                                         str(remote_campaign / "summary.json")).stdout)
        if remote_summary["status"] == "running":
            campaign_dir.mkdir(parents=True, exist_ok=True)
            TRANSFER.download_file(namespace, pod_name, str(remote_campaign / "campaign.json"),
                                   campaign_dir / "campaign.json")
            write_summary(campaign_dir, remote_summary)
            for record in remote_summary.get("overlays", []):
                folder = campaign_dir / record["name"]
                for filename in ("nyann-submit.log", "aiperf-submit.log", "serving-pods.json"):
                    remote_file = remote_campaign / record["name"] / filename
                    exists = kube(namespace, "exec", pod_name, "--", "test", "-f", str(remote_file), check=False)
                    if exists.returncode == 0:
                        TRANSFER.download_file(namespace, pod_name, str(remote_file), folder / filename)
            preview_local(config, output, pod_name=pod_name)
        else:
            TRANSFER.download_tree(namespace, pod_name, str(remote_campaign), campaign_dir)
            summary = json.loads((campaign_dir / "summary.json").read_text())
            if summary["status"] == "running":
                raise RuntimeError(f"Campaign {config['id']} changed during download; retry after it finishes")
            for record in summary.get("overlays", []):
                for bench in record.get("benchmarks", []):
                    if bench.get("tool") != "aiperf" or not bench.get("measurements"):
                        continue
                    remote_artifact = Path(bench["artifacts"])
                    if remote_artifact.parent != Path("/workload/aiperf-agentx") or not remote_artifact.name.startswith(config["id"] + "-"):
                        raise RuntimeError(f"Unexpected AIPerf artifact path: {remote_artifact}")
                    local_artifact = output.resolve() / remote_artifact.relative_to("/workload")
                    for measurement in bench["measurements"]:
                        sample = measurement["sample"]
                        if not re.fullmatch(r"c[1-9][0-9]*(?:-r[1-9][0-9]*)?", sample):
                            raise RuntimeError(f"Unexpected AIPerf sample: {sample}")
                        profile = remote_artifact / sample / "profile_export_aiperf.json"
                        copy_aiperf_sample(namespace, pod_name, profile, local_artifact)
                    bench["artifacts"] = str(local_artifact)
                    bench["report"] = str(local_artifact / "index.html")
                if any(bench.get("tool") == "nyann" and bench.get("measurements")
                       for bench in record.get("benchmarks", [])):
                    copy_nyann_request_files(config, pod_name, record, campaign_dir, stable=True)
            write_summary(campaign_dir, summary)
    finally:
        deleted = kube(namespace, "delete", "pod", pod_name, "--ignore-not-found", "--wait=true",
                       f"--timeout={config['cleanup_timeout_seconds']}s", check=False)
        if deleted.returncode:
            raise CleanupError(f"could not remove artifact Pod {pod_name}: {deleted.stderr.strip()}")
    if remote_summary["status"] != "running":
        report_local(config, output)
    return download_latest_local(config, output, destination, refresh_running=False)


def preview_progress(config: dict[str, Any], campaign_dir: Path, summary: dict[str, Any],
                     completed: set[tuple[str, str]], started: set[tuple[str, str]]) -> str:
    """Show every planned benchmark in the same HTML as completed results."""
    records = {record["name"]: record for record in summary.get("overlays", [])}
    rows = []
    status_counts: Counter[str] = Counter()
    running = []

    def add_row(name: str, tool: str, sample: str, status: str) -> None:
        status_counts[status] += 1
        if status == "running":
            running.append(f"{name} · {tool} {sample}")
        rows.append("<tr>" + "".join(f"<td>{html.escape(value)}</td>" for value in
                                      (name, tool, sample, status)) + "</tr>")

    for variant in config.get("builds", [None]):
        for overlay in config["overlays"]:
            name = f"{variant['name']}-{overlay['name']}" if variant else overlay["name"]
            record = records.get(name, {})
            benchmarks = {bench["tool"]: bench for bench in record.get("benchmarks", [])}
            for bench in config["benchmarks"]:
                tool = bench["tool"]
                result = benchmarks.get(tool, {})
                run_id = f"{config['id']}-{name}-{tool}"
                if tool == "nyann":
                    live_log = campaign_dir / name / "nyann-live.log"
                    if live_log.is_file() and result.get("status") not in {"completed", "skipped"}:
                        log = live_log.read_text(errors="replace")
                        started_stages = max((int(value) for value in
                                              re.findall(r'Stage started" stage=(\d+)/\d+ concurrency=\d+', log)),
                                             default=0)
                        finished_stages = min(len(bench["concurrencies"]), len(re.findall(
                            r"(?m)^\s*\d+\s+\d+\s+\d+\s+\d+\s+[\d.]+\s+", log)))
                        if started_stages:
                            for index, concurrency in enumerate(bench["concurrencies"], 1):
                                if index <= finished_stages:
                                    status = "completed"
                                elif index == started_stages:
                                    status = "running"
                                elif result.get("status") == "failed":
                                    status = "failed"
                                else:
                                    status = "pending"
                                add_row(name, tool, f"stage-{index} (c{concurrency})", status)
                            continue
                    if result.get("status") in {"completed", "failed", "skipped"}:
                        status = result["status"]
                    elif record.get("status") in {"failed", "skipped"}:
                        status = record["status"]
                    elif (campaign_dir / name / "nyann-submit.log").is_file():
                        status = "running"
                    else:
                        status = "pending"
                    samples = ", ".join(f"c{value}" for value in bench["concurrencies"])
                    add_row(name, tool, samples, status)
                    continue
                repeat_counts = Counter(bench["concurrencies"])
                seen: Counter[int] = Counter()
                for concurrency in bench["concurrencies"]:
                    seen[concurrency] += 1
                    sample = (f"c{concurrency}" if repeat_counts[concurrency] == 1 else
                              f"c{concurrency}-r{seen[concurrency]}")
                    if tool == "aiperf" and (run_id, sample) in completed:
                        status = "completed"
                    elif result.get("status") in {"failed", "skipped"}:
                        status = result["status"]
                    elif record.get("status") in {"failed", "skipped"}:
                        status = record["status"]
                    elif tool == "aiperf" and (run_id, sample) in started:
                        status = "running"
                    else:
                        status = "pending"
                    add_row(name, tool, sample, status)
    updated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    summary_parts = [f"<strong>{status_counts[status]} {status}</strong>" for status in
                     ("completed", "running", "pending", "failed", "skipped") if status_counts[status]]
    status_line = '<span class="separator">·</span>'.join(summary_parts)
    running_line = (f'<p class="campaign-running">Now running: {html.escape("; ".join(running))}</p>'
                    if running else "")
    return (f'<div class="campaign-status">{status_line}'
            f'<time class="campaign-updated">Updated {updated}</time></div>' + running_line +
            '<details id="campaign-progress" class="campaign-detail"><summary>Campaign progress</summary>'
            "<table><thead><tr><th>Overlay</th><th>Tool</th><th>Sample</th><th>Status</th></tr></thead><tbody>"
            + "".join(rows) + "</tbody></table></details>")


def preview_local(config: dict[str, Any], output: Path, *, auto_refresh: bool = False,
                  pod_name: str | None = None) -> int:
    """Render active and completed work into one portable campaign HTML file."""
    campaign_dir = output.resolve() / "campaigns" / config["id"]
    saved_config = campaign_dir / "campaign.json"
    if not saved_config.is_file():
        raise ValueError(f"{campaign_dir} does not match this campaign configuration")
    saved = json.loads(saved_config.read_text())
    if {key: value for key, value in saved.items() if key != "monitoring"} != \
            {key: value for key, value in config.items() if key != "monitoring"}:
        raise ValueError(f"{campaign_dir} does not match this campaign configuration")
    summary = json.loads((campaign_dir / "summary.json").read_text())
    if summary["status"] != "running":
        print(f"Campaign is {summary['status']}; use its final report if available")
        return 0
    pod_name = pod_name or "campaign-artifacts-" + hashlib.sha256(config["id"].encode()).hexdigest()[:16]
    namespace = config["namespace"]
    remote_root = "/workload/aiperf-agentx"
    if kube(namespace, "exec", pod_name, "--", "test", "-d", remote_root, check=False).returncode == 0:
        listing = kube(namespace, "exec", pod_name, "--", "find", remote_root,
                       "-mindepth", "3", "-maxdepth", "3", "-type", "f",
                       "-name", "profile_export_aiperf.json").stdout
        directories = kube(namespace, "exec", pod_name, "--", "find", remote_root,
                           "-mindepth", "2", "-maxdepth", "2", "-type", "d").stdout
    else:
        listing = directories = ""
    labels = {}
    for variant in config.get("builds", [None]):
        for overlay in config["overlays"]:
            name = f"{variant['name']}-{overlay['name']}" if variant else overlay["name"]
            run_id = f"{config['id']}-{name}-aiperf"
            labels[run_id] = name
    paths = []
    for line in listing.splitlines():
        path = Path(line)
        if path.parent.parent.name in labels and path.name == "profile_export_aiperf.json":
            paths.append(path)
    for record in summary.get("overlays", []):
        folder = campaign_dir / record["name"]
        submission = folder / "nyann-submit.log"
        if not submission.is_file():
            continue
        match = JOB_LINE.search(submission.read_text())
        if match is None:
            continue
        logs = kube(namespace, "logs", f"job/{match.group(1)}", check=False)
        if logs.returncode == 0:
            (folder / "nyann-live.log").write_text(logs.stdout)
    preview = campaign_dir / "preview"
    preview.mkdir(exist_ok=True)
    runs = []
    groups: dict[str, list[Path]] = {}
    with tempfile.TemporaryDirectory(prefix="campaign-preview-", dir=preview) as temporary:
        for path in sorted(paths):
            try:
                sample = copy_aiperf_sample(namespace, pod_name, path,
                                            Path(temporary) / path.parent.parent.name)
            except RuntimeError as exc:
                print(f"Skipping unfinished or unavailable preview sample {path.parent}: {exc}", flush=True)
                continue
            data = AIPERF_REPORT.run_data(sample)
            if data is None or not data["profile"]:
                continue  # The writer may still be finishing this sample.
            groups.setdefault(path.parent.parent.name, []).append(sample)
            data["metadata"].update({"campaign_label": labels[path.parent.parent.name],
                                     "vllm_image": config["vllm_image"]})
            runs.append(data)
        monitoring = config.get("monitoring")
        missing_monitoring = []
        if monitoring and runs:
            with grafana_connection(monitoring) as (url, auth):
                for run_id, samples in groups.items():
                    folder = campaign_dir / labels[run_id]
                    submission = (folder / "aiperf-submit.log").read_text()
                    match = JOB_LINE.search(submission)
                    if match is None:
                        raise RuntimeError(f"Missing AIPerf Job name for {run_id}")
                    pods = json.loads((folder / "serving-pods.json").read_text())["items"]
                    names = [item["metadata"]["name"] for item in pods]
                    log_path = Path(temporary) / (run_id + ".log")
                    log_path.write_text(kube(namespace, "logs", f"job/{match.group(1)}",
                                             "--timestamps=true").stdout)
                    env = os.environ.copy()
                    env["CAMPAIGN_GRAFANA_AUTH"] = auth
                    call([sys.executable, str(ROOT / "export_dashboard.py"),
                          "--grafana-url", url, "--auth-env", "CAMPAIGN_GRAFANA_AUTH",
                          "--dashboard", monitoring["dashboard_uid"],
                          "--plotly-bundle", str(ROOT / "live-aiperf" / "plotly-basic-2.35.2.min.js.gz"),
                          "--aiperf-log", str(log_path),
                          "--pod-regex", "|".join(re.escape(name) for name in names),
                          "--metrics-namespace", namespace,
                          "results", *(str(sample) for sample in samples), "--pad", "0"], env=env)
                for data in runs:
                    dashboard = data["directory"] / "dashboard.html"
                    if not dashboard.is_file():
                        raise RuntimeError(f"Grafana export did not create {dashboard}")
                    page = dashboard.read_text()
                    panels_match = re.search(r"const panels = ({.*?});\s*\n\s*const rows", page, re.DOTALL)
                    if panels_match is None:
                        raise RuntimeError(f"Grafana export has no panel data: {dashboard}")
                    panels = json.loads(panels_match.group(1))
                    has_vllm_data = any(
                        "vllm:" in query.get("expr", "") and
                        any(series.get("values") for series in query.get("series", []))
                        for panel in panels.values() for query in panel.get("queries", [])
                    )
                    if has_vllm_data:
                        data["dashboard"] = page.encode()
                    else:
                        data["dashboard"] = None
                        missing_monitoring.append(data["directory"].name)
        nyann_specs = [bench for bench in config["benchmarks"] if bench["tool"] == "nyann"]
        missing_nyann_metrics = []
        if nyann_specs:
            spec = nyann_specs[0]
            for record in summary.get("overlays", []):
                nyann_result = next((bench for bench in record.get("benchmarks", [])
                                     if bench.get("tool") == "nyann" and bench.get("measurements")), None)
                if nyann_result is None:
                    live_log = campaign_dir / record["name"] / "nyann-live.log"
                    if not live_log.is_file():
                        continue
                    measurements = nyann_live_measurements(live_log.read_text(errors="replace"),
                                                            spec["concurrencies"], spec["duration_seconds"])
                    if not measurements:
                        continue
                    nyann_result = {"tool": "nyann", "measurements": measurements}
                folder = campaign_dir / record["name"]
                log_path = folder / "nyann-live.log"
                if not log_path.is_file():
                    log_path = folder / "nyann-job.log"
                if log_path.is_file():
                    log = log_path.read_text(errors="replace")
                    try:
                        files = copy_nyann_request_files(config, pod_name, record, campaign_dir,
                                                         stable=nyann_result.get("status") == "completed")
                        enrich_nyann_request_metrics(nyann_result["measurements"], log,
                                                     spec["concurrencies"], spec["duration_seconds"], files)
                    except Exception as exc:
                        missing_nyann_metrics.append(f"{record['name']}: {exc}")
                if not all("time_per_output_token" in item["metrics"]
                           for item in nyann_result["measurements"]):
                    missing_nyann_metrics.append(f"{record['name']}: TPOT waits for complete request records")
                if monitoring:
                    try:
                        if not log_path.is_file():
                            raise RuntimeError("Nyann Job log is unavailable")
                        pods_file = folder / "serving-pods.json"
                        if pods_file.is_file():
                            pods = [item["metadata"]["name"] for item in json.loads(pods_file.read_text())["items"]]
                        else:
                            pods = record.get("serving_pods", [])
                        export_nyann_monitoring(config, folder, pods, nyann_result["measurements"],
                                                log_path.read_text(errors="replace"), spec)
                    except Exception as exc:
                        missing_monitoring.append(f"{record['name']} Nyann: {exc}")
                runs.extend(nyann_chart_runs(campaign_dir, summary, record, nyann_result))
        completed = {(path.parent.parent.name, path.parent.name) for path in paths}
        started = {(path.parent.name, path.name) for line in directories.splitlines()
                   if (path := Path(line)).parent.name in labels}
        progress = preview_progress(config, campaign_dir, summary, completed, started)
        notice = progress + AIPERF_REPORT.nyann_setup(config, campaign_dir)
        if missing_nyann_metrics:
            notice += ('<p class="campaign-note">' + html.escape("; ".join(missing_nyann_metrics)) + '</p>')
        if missing_monitoring:
            samples = ", ".join(html.escape(name) for name in missing_monitoring)
            notice += (f'<p class="campaign-note">{samples}: no vLLM monitoring was recorded. '
                       'Available GPU series still appear in the overlay.</p>')
        render = Path(temporary) / "render"
        render.mkdir()
        if runs:
            AIPERF_REPORT.write_index_from_runs(render, runs, extra_html=notice,
                                                model_label=f"Campaign {config['id']}",
                                                save_monitoring_overlay=False,
                                                compact_header=True)
            page = (render / "index.html").read_text()
        else:
            page = AIPERF_REPORT.document(f"Campaign {config['id']}",
                                          '<section class="campaign-overview">' + notice + '</section>')
            page = page.replace('</head>', AIPERF_REPORT.preview_header_css() + '</head>', 1)
        if auto_refresh:
            page = page.replace("</body>", "<script>setTimeout(() => location.reload(), 90000);</script></body>", 1)
        staged = render / "index.html"
        staged.write_text(page)
        staged.replace(preview / "index.html")
    print(f"Preview: {preview / 'index.html'} ({len(runs)} completed samples)")
    return 0


def watch_preview_local(config: dict[str, Any], output: Path) -> int:
    """Keep one preview file current until the final campaign report exists."""
    campaign_dir = output.resolve() / "campaigns" / config["id"]
    while True:
        summary = json.loads((campaign_dir / "summary.json").read_text())
        if summary["status"] != "running":
            if summary["status"] in {"failed", "cancelled"}:
                print(f"Campaign {summary['status']}; retained the last partial preview in "
                      f"{campaign_dir / 'preview/index.html'}",
                      file=sys.stderr)
                return 1
            final = campaign_dir / "index.html"
            if final.is_file():
                preview = campaign_dir / "preview"
                preview.mkdir(exist_ok=True)
                staged = preview / "index.html.tmp"
                shutil.copyfile(final, staged)
                staged.replace(preview / "index.html")
                print(f"Final report: {preview / 'index.html'}")
                return 0
        else:
            try:
                preview_local(config, output, auto_refresh=True)
            except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
                print(f"Preview refresh failed; retrying: {exc}", file=sys.stderr, flush=True)
        time.sleep(60)


def download_latest_local(config: dict[str, Any], output: Path, destination: Path | None = None,
                          *, refresh_running: bool = True) -> Path:
    """Refresh and save the latest self-contained campaign HTML in one command."""
    campaign_dir = output.resolve() / "campaigns" / config["id"]
    saved_config = campaign_dir / "campaign.json"
    if not saved_config.is_file():
        raise ValueError(f"No campaign at {campaign_dir}")
    saved = json.loads(saved_config.read_text())
    if {key: value for key, value in saved.items() if key != "monitoring"} != \
            {key: value for key, value in config.items() if key != "monitoring"}:
        raise ValueError(f"{campaign_dir} does not match this campaign configuration")
    status = json.loads((campaign_dir / "summary.json").read_text())["status"]
    if status == "running" and refresh_running:
        preview_local(config, output)
    preview = campaign_dir / "preview" / "index.html"
    final = campaign_dir / "index.html"
    source = final if status != "running" and final.is_file() else preview
    if not source.is_file():
        raise RuntimeError(f"No report is available for campaign {config['id']} ({status})")
    destination = (destination or Path.home() / "Downloads" / f"{config['id']}-latest.html").expanduser()
    if destination.is_dir():
        destination /= f"{config['id']}-latest.html"
    destination = destination.resolve()
    if destination != source.resolve():
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix=f".{destination.name}.", dir=destination.parent,
                                         delete=False) as temporary:
            staged = Path(temporary.name)
        try:
            shutil.copyfile(source, staged)
            staged.replace(destination)
        finally:
            staged.unlink(missing_ok=True)
    print(f"Downloaded latest report: {destination} (campaign {status})")
    return destination


def report_local(config: dict[str, Any], output: Path) -> int:
    """Backfill Grafana dashboards and regenerate a finished local campaign report."""
    campaign_dir = output.resolve() / "campaigns" / config["id"]
    saved_config = campaign_dir / "campaign.json"
    if not saved_config.is_file():
        raise ValueError(f"{campaign_dir} does not match this campaign configuration")
    saved = json.loads(saved_config.read_text())
    if {key: value for key, value in saved.items() if key != "monitoring"} != \
            {key: value for key, value in config.items() if key != "monitoring"}:
        raise ValueError(f"{campaign_dir} does not match this campaign configuration")
    summary = json.loads((campaign_dir / "summary.json").read_text())
    if summary["status"] == "running":
        raise ValueError("campaign is still running; use preview-local until it finishes")
    summary.setdefault("source_ref", config["source"]["ref"])
    summary.setdefault("planned_deployments", len(config["overlays"]) * len(config.get("builds", [None])))
    nyann_spec = next((bench for bench in config["benchmarks"] if bench["tool"] == "nyann"), None)
    if nyann_spec:
        for record in summary["overlays"]:
            folder = campaign_dir / record["name"]
            log_path = folder / "nyann-job.log"
            if not log_path.is_file():
                continue
            log = log_path.read_text(errors="replace")
            files = sorted((folder / "nyann-requests").glob("requests_*.jsonl"))
            for bench in record.get("benchmarks", []):
                if bench.get("tool") == "nyann" and bench.get("measurements"):
                    if bench.get("status") == "completed":
                        bench["measurements"] = nyann_measurements(log, nyann_spec["concurrencies"])
                    enrich_nyann_request_metrics(bench["measurements"], log,
                                                 nyann_spec["concurrencies"], nyann_spec["duration_seconds"], files)
    if "monitoring" in config:
        for record in summary["overlays"]:
            for bench in record.get("benchmarks", []):
                if not bench.get("measurements"):
                    continue
                if bench.get("tool") == "aiperf":
                    export_campaign_monitoring(config, Path(bench["artifacts"]), bench["job"],
                                               record["serving_pods"], bench["measurements"])
                elif bench.get("tool") == "nyann" and nyann_spec:
                    folder = campaign_dir / record["name"]
                    log_path = folder / "nyann-job.log"
                    export_nyann_monitoring(config, folder, record["serving_pods"],
                                            bench["measurements"], log_path.read_text(errors="replace"),
                                            nyann_spec)
    write_summary(campaign_dir, summary)
    write_final_report(campaign_dir, summary)
    print(f"Final report: {campaign_dir / 'index.html'}")
    return 0


def campaign_source_bundle() -> bytes:
    """Bundle the checked-out campaign implementation for an in-cluster Job."""
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w:gz") as bundle:
        for name in CAMPAIGN_SOURCE_FILES:
            source = ROOT / name
            if not source.is_file():
                raise RuntimeError(f"Campaign source file is missing: {source}")
            bundle.add(source, arcname=name)
    return archive.getvalue()


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
    bundle = campaign_source_bundle()
    job_config = copy.deepcopy(config)
    local_monitoring = "monitoring" in config and "grafana_service" in config["monitoring"]
    if local_monitoring:
        job_config.pop("monitoring")  # Backfill through the existing local exporter after download.
    configmap = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": configmap_name, "namespace": namespace},
                 "data": {"campaign.json": json.dumps(job_config)},
                 "binaryData": {"source.tar.gz": base64.b64encode(bundle).decode()}}
    configmap["metadata"]["annotations"] = {"benchmark.llm-d.ai/source-sha256": hashlib.sha256(bundle).hexdigest()}
    if len(json.dumps(configmap).encode()) >= 900_000:
        raise ValueError("Campaign source bundle exceeds the safe ConfigMap size")
    kube(namespace, "create", "-f", "-", input_text=json.dumps(configmap))
    job = {"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": configmap_name, "namespace": namespace,
           "labels": {"kueue.x-k8s.io/queue-name": config["campaign_queue"], "app.kubernetes.io/name": "benchmark-campaign"}},
           "spec": {"suspend": True, "backoffLimit": 0, "template": {"spec": {"restartPolicy": "Never",
           "serviceAccountName": service_account, "containers": [{"name": "runner", "image": image,
           "command": ["python3", "/workspace/agentx-mvp/campaign/run.py", "run", "/campaign/campaign.json"],
           "resources": {"requests": {"cpu": "1", "memory": "1Gi", "ephemeral-storage": "1Gi"},
                         "limits": {"cpu": "2", "memory": "2Gi", "ephemeral-storage": "4Gi"}},
           "volumeMounts": [{"name": "config", "mountPath": "/campaign", "readOnly": True},
                            {"name": "results", "mountPath": "/workload"},
                            {"name": "source", "mountPath": "/workspace/agentx-mvp"}]}],
           "initContainers": [{"name": "unpack-source", "image": image,
                               "command": ["sh", "-c", "tar -xzf /campaign/source.tar.gz -C /workspace/agentx-mvp"],
                               "volumeMounts": [{"name": "config", "mountPath": "/campaign", "readOnly": True},
                                                {"name": "source", "mountPath": "/workspace/agentx-mvp"}]}],
           "volumes": [{"name": "config", "configMap": {"name": configmap_name}},
                       {"name": "results", "persistentVolumeClaim": {"claimName": config["results_pvc"]}},
                       {"name": "source", "emptyDir": {}}]}}}}
    if "monitoring" in job_config:
        secret = config["monitoring"]["auth_secret"]
        job["spec"]["template"]["spec"]["containers"][0]["env"] = [
            {"name": "CAMPAIGN_GRAFANA_USER", "valueFrom": {"secretKeyRef": {"name": secret, "key": "admin-user"}}},
            {"name": "CAMPAIGN_GRAFANA_PASSWORD", "valueFrom": {"secretKeyRef": {"name": secret, "key": "admin-password"}}},
        ]
    try:
        kube(namespace, "create", "-f", "-", input_text=json.dumps(job))
    except Exception:
        kube(namespace, "delete", "configmap", configmap_name, "--ignore-not-found", check=False)
        raise
    print(f"Campaign queued: {namespace}/{configmap_name}")
    if local_monitoring:
        print("Grafana dashboards will be backfilled after downloading the PVC report")
    print(f"Status: kubectl -n {namespace} get job {configmap_name}")
    print(f"Logs: kubectl -n {namespace} logs -f job/{configmap_name}")
    print(f"Results: {config['results_pvc']}:/workload/campaigns/{campaign_id}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["validate", "submit", "run", "run-local", "start-local", "test-local",
                                           "preview-local", "download-latest", "download-cluster", "download-artifacts", "stop-local", "report-local"])
    parser.add_argument("config", type=Path)
    parser.add_argument("--image", help="runner image for submit")
    parser.add_argument("--service-account", default="benchmark-campaign")
    parser.add_argument("--source-dir", type=Path, help="local llm-d checkout for test-local; otherwise fetch source.repo/ref")
    parser.add_argument("--output", type=Path, help="new artifact directory for test-local")
    parser.add_argument("--watch", action="store_true", help="refresh preview-local until the campaign finishes")
    parser.add_argument("--dest", type=Path, help="HTML destination for download-latest/download-cluster")
    parser.add_argument("--remote", help="PVC artifact directory below /workload for download-artifacts")
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
        if args.action == "test-local":
            if args.output is None:
                raise ValueError("--output is required for test-local")
            return test_local(config, args.output, args.source_dir)
        if args.action == "run-local":
            if args.output is None:
                raise ValueError("--output is required for run-local")
            return run_local(config, args.output)
        if args.action == "start-local":
            if args.output is None:
                raise ValueError("--output is required for start-local")
            start_local(args.config, args.output, config["id"])
            return 0
        if args.action == "stop-local":
            if args.output is None:
                raise ValueError("--output is required for stop-local")
            stop_local(config, args.output, args.config)
            return 0
        if args.action == "preview-local":
            if args.output is None:
                raise ValueError("--output is required for preview-local")
            return watch_preview_local(config, args.output) if args.watch else preview_local(config, args.output)
        if args.action == "download-latest":
            if args.output is None:
                raise ValueError("--output is required for download-latest")
            if args.watch:
                raise ValueError("--watch is only supported with preview-local")
            download_latest_local(config, args.output, args.dest)
            return 0
        if args.action == "download-cluster":
            if args.output is None:
                raise ValueError("--output is required for download-cluster")
            download_cluster_report(config, args.output, args.dest)
            return 0
        if args.action == "download-artifacts":
            if args.output is None:
                raise ValueError("--output is required for download-artifacts")
            download_pvc_artifacts(config, args.remote, args.output)
            return 0
        if args.watch:
            raise ValueError("--watch is only supported with preview-local")
        if args.dest is not None:
            raise ValueError("--dest is only supported with download-latest")
        if args.action == "report-local":
            if args.output is None:
                raise ValueError("--output is required for report-local")
            return report_local(config, args.output)
        return run(config)
    except (ValueError, RuntimeError, TimeoutError, OSError, json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
        print(f"campaign: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
