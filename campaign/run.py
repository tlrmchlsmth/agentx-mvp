#!/usr/bin/env python3
"""Run a sequence of Kustomize overlays and benchmark each deployed model."""
from __future__ import annotations

import argparse
import base64
from collections import Counter
from contextlib import contextmanager
import copy
import csv
import hashlib
import html
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
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
NAME = re.compile(r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$")
DIMENSION_NAME = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
JOB_LINE = re.compile(r"^Job queued: ([a-z0-9-]+) ", re.MULTILINE)
GIT_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
BUILD_ACTION = re.compile(r"^(?:checkout|merge|cherry-pick|cherry-pick-parent1|cherry-pick-m[1-9][0-9]*)$")


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
    if "monitoring" in config and "aiperf" not in tools:
        raise ValueError("monitoring requires an aiperf benchmark")
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
    measurements = []
    for index, stage in enumerate(stages, 1):
        successes = stage["successful_requests"]
        errors = stage["error_requests"]
        duration = stage["duration_seconds"]
        if successes <= 0 or duration <= 0:
            raise RuntimeError(f"Nyann stage {index} has no successful requests or measured duration")
        measurements.append({"concurrency": stage["concurrency"], "sample": f"stage-{index}",
                             "successful_requests": successes, "error_requests": errors,
                             "metrics": {"request_throughput": {"avg": successes / duration, "unit": "req/s"},
                                         "output_token_throughput": {"avg": stage["output_tokens_per_second"], "unit": "tokens/s"},
                                         "time_to_first_token": {"p90": stage["ttft_ms"]["p90"], "unit": "ms"},
                                         "inter_token_latency": {"p90": stage["itl_ms"]["p90"], "unit": "ms"}}})
    return measurements


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
    result = {"tool": tool, "job": job_name, "run_id": run_id, "artifacts": str(artifact),
            "report": f"{artifact}/index.html" if tool == "aiperf" else None,
            "measurements": measurements, "status": "failed" if monitoring_error else "completed"}
    if monitoring_error:
        result["error"] = monitoring_error
    return result


def write_summary(destination: Path, summary: dict[str, Any], *, embedded_reports: bool = False) -> str:
    tmp = destination / "summary.json.tmp"
    tmp.write_text(json.dumps(summary, indent=2) + "\n")
    tmp.replace(destination / "summary.json")
    dimensions = sorted({key for record in summary["overlays"] for key in record.get("dimensions", {})})
    fields = ["build", "overlay", *dimensions, "tool", "sample", "concurrency", "status",
              "successful_requests", "error_requests",
              "requests_per_s", "requests_unit", "output_tokens_per_s", "output_tokens_unit",
              "ttft_p90", "ttft_unit", "itl_p90", "itl_unit", "report", "artifacts", "error",
              "llm_d_commit", "vllm_commits", "deepep_commit", "vllm_image"]
    rows = []
    identities = []
    for record in summary["overlays"]:
        build_inputs = record.get("vllm_build_inputs")
        steps = build_inputs.get("steps", []) if build_inputs else []
        vllm_commits = (
            ", ".join(f"{step['action']} {step['ref']}@{step['commit']}" for step in steps)
            if steps else "N/A (using configured image; no source commit)" if build_inputs else
            "unknown (build not resolved)"
        )
        identities.append({"build": record.get("build", ""), "overlay": record.get("overlay", record["name"]),
                           "status": record["status"], "llm_d_commit": summary.get("source_commit", ""),
                           "vllm_commits": vllm_commits,
                           "deepep_commit": build_inputs.get("deepep", {}).get("commit", "") if build_inputs else "",
                           "vllm_image": summary.get("vllm_image", "")})
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
                       "report": bench.get("report", ""),
                       "artifacts": bench.get("artifacts", ""), "error": bench.get("error", ""),
                       "llm_d_commit": summary.get("source_commit", ""),
                       "vllm_commits": vllm_commits,
                       "deepep_commit": identities[-1]["deepep_commit"],
                       "vllm_image": summary.get("vllm_image", "")}
                row.update(record.get("dimensions", {}))
                rows.append(row)
    with (destination / "comparison.csv").open("w", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    build_rows = []
    for build in summary.get("builds", []):
        inputs = ", ".join(f"{step['action']} {step['ref']}@{step['commit'][:12]}" for step in build.get("inputs", {}).get("steps", [])) or "nightly image"
        deepep = build.get("inputs", {}).get("deepep")
        if deepep:
            inputs += f"; DeepEP {deepep['ref']}@{deepep['commit'][:12]}"
        build_rows.append("<tr>" + "".join(f"<td>{html.escape(str(value))}</td>" for value in
                        (build["name"], build["status"], inputs, build.get("error", ""))) + "</tr>")
    display_fields = ["build", "overlay", *dimensions, "tool", "sample", "concurrency", "status",
                      "successful_requests", "error_requests", "requests_per_s", "output_tokens_per_s",
                      "ttft_p90", "itl_p90", "report", "error"]
    headings = {"llm_d_commit": "llm-d commit", "vllm_commits": "vLLM source commits",
                "deepep_commit": "DeepEP commit", "vllm_image": "vLLM image",
                "requests_per_s": "Requests/s", "output_tokens_per_s": "Output tokens/s",
                "ttft_p90": "TTFT p90", "itl_p90": "ITL p90"}
    def header(names: list[str]) -> str:
        return "".join(f"<th>{html.escape(headings.get(field, field.replace('_', ' ').title()))}</th>" for field in names)

    def html_cell(row: dict[str, Any], field: str) -> str:
        value = str(row.get(field, ""))
        unit_field = {"requests_per_s": "requests_unit", "output_tokens_per_s": "output_tokens_unit",
                      "ttft_p90": "ttft_unit", "itl_p90": "itl_unit"}.get(field)
        if unit_field and value:
            value += " " + str(row.get(unit_field, ""))
        if field == "report" and value:
            if embedded_reports:
                return "<td>AIPerf charts below</td>"
            try:
                relative = Path(value).relative_to("/workload")
            except ValueError:
                pass
            else:
                href = "../../" + relative.as_posix()
                return f'<td><a href="{html.escape(href, quote=True)}">AIPerf dashboard</a></td>'
        return f"<td>{html.escape(value)}</td>"

    body = "".join("<tr>" + "".join(html_cell(row, field) for field in display_fields) + "</tr>" for row in rows)
    identity_fields = ["build", "overlay", "status", "llm_d_commit", "vllm_commits", "deepep_commit", "vllm_image"]
    identity_body = "".join("<tr>" + "".join(html_cell(row, field) for field in identity_fields) + "</tr>"
                            for row in identities)
    mock_notice = ("<p><strong>MOCK DATA:</strong> generated locally to test report rendering; "
                   "no benchmark or Grafana query ran.</p>" if summary.get("mode") == "mock-test" else "")
    fragment = ("<style>.campaign-identity{width:100%;table-layout:fixed;border-collapse:collapse}"
                ".campaign-identity th,.campaign-identity td{overflow-wrap:anywhere;vertical-align:top}"
                ".campaign-measurements{overflow-x:auto}"
                ".campaign-identity th,.campaign-identity td,.campaign-measurements th,.campaign-measurements td"
                "{border:1px solid #444;padding:6px;text-align:left}</style>" +
                f"<h1>Campaign {html.escape(summary['id'])}</h1><p>Status: {html.escape(summary['status'])}</p>") + mock_notice + \
        "<h2>Builds</h2><table><tr><th>Build</th><th>Status</th><th>Resolved inputs</th><th>Error</th></tr>" + \
        "".join(build_rows) + "</table>" + \
        "<h2>Run identity</h2><table class='campaign-identity'><tr>" + header(identity_fields) + "</tr>" + identity_body + "</table>" + \
        "<h2>All configurations</h2><div class='campaign-measurements'><table><tr>" + header(display_fields) + "</tr>" + body + "</table></div>"
    return fragment


def write_final_report(destination: Path, summary: dict[str, Any]) -> None:
    """Embed every completed AIPerf sweep in the same HTML as the matrix and nyann rows."""
    runs = []
    for record in summary["overlays"]:
        for bench in record.get("benchmarks", []):
            if bench.get("tool") != "aiperf" or (bench.get("status") != "completed" and not bench.get("measurements")):
                continue
            artifact = Path(bench["artifacts"])
            sweep_runs = 0
            for directory in sorted(artifact.iterdir()):
                data = AIPERF_REPORT.run_data(directory)
                if data is None:
                    continue
                label = f"{record.get('build', 'default')} / {record.get('overlay', record['name'])}"
                dimensions = ", ".join(f"{key}={value}" for key, value in sorted(record.get("dimensions", {}).items()))
                build = record.get("vllm_build_inputs", {})
                steps = build.get("steps", [])
                identity = " + ".join(f"{step['ref']}@{step['commit'][:12]}" for step in steps)
                label += f" — vLLM {identity or summary.get('vllm_image', 'nightly image')}"
                data["metadata"].update({
                    "campaign_label": f"{label} ({dimensions})" if dimensions else label,
                    "vllm_image": summary.get("vllm_image", ""),
                    "vllm_build_steps": steps,
                    "vllm_build_commit": record.get("vllm_build_commit", steps[0]["commit"] if steps else ""),
                    "deepep_build": build.get("deepep"),
                })
                metadata_file = directory / "benchmark-metadata.json"
                metadata_file.write_text(json.dumps(data["metadata"], indent=2) + "\n")
                runs.append(data)
                sweep_runs += 1
            if sweep_runs:
                AIPERF_REPORT.write_index(artifact)
    fragment = write_summary(destination, summary, embedded_reports=bool(runs))
    if runs:
        AIPERF_REPORT.write_index_from_runs(destination, runs, extra_html=fragment,
                                            model_label=f"Campaign {summary['id']}", save_monitoring_overlay=False)


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
                               "vllm_image": config["vllm_image"], "overlays": []}
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
                kube(namespace, "cp", f"{pod_name}:{artifact}", str(destination))
            return destination

        return run(config, output, artifact_fetcher=fetch)
    finally:
        deleted = kube(namespace, "delete", "pod", pod_name, "--ignore-not-found", "--wait=true",
                       f"--timeout={config['cleanup_timeout_seconds']}s", check=False)
        if deleted.returncode:
            raise CleanupError(f"could not remove artifact Pod {pod_name}: {deleted.stderr.strip()}")


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
        for attempt in range(3):
            result = kube(namespace, "cp", f"{pod_name}:{profile.parent / filename}",
                          str(target), check=False)
            if result.returncode == 0:
                break
            target.unlink(missing_ok=True)
            if not required:
                break
            if attempt < 2:
                time.sleep(1)
        else:
            if required:
                raise RuntimeError(f"Could not copy {profile.parent / filename}: {result.stderr.strip()}")
    return sample


def preview_local(config: dict[str, Any], output: Path) -> int:
    """Render completed AIPerf samples from an active run-local campaign."""
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
    pod_name = "campaign-artifacts-" + hashlib.sha256(config["id"].encode()).hexdigest()[:16]
    namespace = config["namespace"]
    remote_root = "/workload/aiperf-agentx"
    listing = kube(namespace, "exec", pod_name, "--", "find", remote_root,
                   "-mindepth", "3", "-maxdepth", "3", "-type", "f",
                   "-name", "profile_export_aiperf.json").stdout
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
    if not paths:
        print("No completed AIPerf samples yet; preview HTML was not created")
        return 0
    preview = campaign_dir / "preview"
    preview.mkdir(exist_ok=True)
    runs = []
    groups: dict[str, list[Path]] = {}
    with tempfile.TemporaryDirectory(prefix="campaign-preview-") as temporary:
        for path in sorted(paths):
            sample = copy_aiperf_sample(namespace, pod_name, path,
                                        Path(temporary) / path.parent.parent.name)
            data = AIPERF_REPORT.run_data(sample)
            if data is None or not data["profile"]:
                continue  # The writer may still be finishing this sample.
            groups.setdefault(path.parent.parent.name, []).append(sample)
            data["metadata"].update({"campaign_label": labels[path.parent.parent.name],
                                     "vllm_image": config["vllm_image"]})
            runs.append(data)
        if not runs:
            print("No complete AIPerf profiles yet; preview HTML was not created")
            return 0
        monitoring = config.get("monitoring")
        missing_monitoring = []
        if monitoring:
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
        notice = (f"<p>In-progress preview: {len(runs)} completed AIPerf samples. "
                  "Unfinished samples and nyann results are not included.</p>")
        if missing_monitoring:
            samples = ", ".join(html.escape(name) for name in missing_monitoring)
            notice += (f"<p>No vLLM Grafana samples were recorded during {samples}; "
                       "those empty dashboards are omitted. Scraping may have begun after these runs.</p>")
        AIPERF_REPORT.write_index_from_runs(preview, runs, extra_html=notice,
                                            model_label=f"Campaign {config['id']} preview",
                                            save_monitoring_overlay=False)
    print(f"Preview: {preview / 'index.html'} ({len(runs)} completed AIPerf samples)")
    return 0


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
    if "monitoring" in config:
        for record in summary["overlays"]:
            for bench in record.get("benchmarks", []):
                if bench.get("tool") != "aiperf" or not bench.get("measurements"):
                    continue
                export_campaign_monitoring(config, Path(bench["artifacts"]), bench["job"],
                                           record["serving_pods"], bench["measurements"])
    write_final_report(campaign_dir, summary)
    print(f"Final report: {campaign_dir / 'index.html'}")
    return 0


def submit(config: dict[str, Any], image: str, service_account: str) -> None:
    namespace, campaign_id = config["namespace"], config["id"]
    if "monitoring" in config and "grafana_service" in config["monitoring"]:
        raise ValueError("monitoring.grafana_service is for run-local; use grafana_url and a namespace-local Secret for submit")
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
    if "monitoring" in config:
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
    print(f"Status: kubectl -n {namespace} get job {configmap_name}")
    print(f"Logs: kubectl -n {namespace} logs -f job/{configmap_name}")
    print(f"Results: {config['results_pvc']}:/workload/campaigns/{campaign_id}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["validate", "submit", "run", "run-local", "test-local", "preview-local", "report-local"])
    parser.add_argument("config", type=Path)
    parser.add_argument("--image", help="runner image for submit")
    parser.add_argument("--service-account", default="benchmark-campaign")
    parser.add_argument("--source-dir", type=Path, help="local llm-d checkout for test-local; otherwise fetch source.repo/ref")
    parser.add_argument("--output", type=Path, help="new artifact directory for test-local")
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
        if args.action == "preview-local":
            if args.output is None:
                raise ValueError("--output is required for preview-local")
            return preview_local(config, args.output)
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
