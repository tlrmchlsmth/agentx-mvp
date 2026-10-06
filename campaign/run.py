#!/usr/bin/env python3
"""Run a sequence of Kustomize overlays and benchmark each deployed model."""
from __future__ import annotations

import argparse
from collections import Counter
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
import subprocess
import sys
import time
import tempfile
from urllib.parse import urlsplit
from datetime import datetime, timezone
from typing import Any

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
    allowed = {"id", "namespace", "source", "vllm_image", "build_repo", "builds", "results_pvc", "benchmark_queue",
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
        if not isinstance(overlay, dict) or not required.issubset(overlay) or set(overlay) - required - {"build", "dimensions"}:
            raise ValueError("each overlay needs name, path, model_label, pod_selector, expected_pods; build/dimensions are optional")
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
        dimensions = overlay.get("dimensions", {})
        reserved = {"build", "overlay", "tool", "sample", "concurrency", "status", "requests_per_s",
                    "output_tokens_per_s", "ttft_p90", "itl_p90", "artifacts", "error",
                    "successful_requests", "error_requests", "requests_unit", "output_tokens_unit",
                    "ttft_unit", "itl_unit", "report"}
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


def apply_vllm_image(rendered: str, image: str) -> str:
    """Use the campaign's explicit runtime image in every vLLM worker."""
    documents = list(yaml.safe_load_all(rendered))
    workers = 0
    for item in documents:
        if not isinstance(item, dict) or item.get("kind") != "LeaderWorkerSet":
            continue
        pod = item.get("spec", {}).get("leaderWorkerTemplate", {}).get("workerTemplate", {}).get("spec", {})
        for container in pod.get("containers", []):
            if container.get("name") == "vllm":
                container["image"] = image
                workers += 1
    if not workers:
        raise ValueError("overlay has no LeaderWorkerSet vllm container for vllm_image")
    return yaml.safe_dump_all(documents, sort_keys=False)


def inject_vllm_build_script(rendered: str, build: dict[str, Any] | None = None) -> str:
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
            raise ValueError("overlay build requires a compatible vLLM wheel build script")
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
    for item in documents:
        if not isinstance(item, dict) or item.get("kind") != "LeaderWorkerSet":
            continue
        pod = item.get("spec", {}).get("leaderWorkerTemplate", {}).get("workerTemplate", {}).get("spec", {})
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
        raise ValueError("vLLM build script is not sourced by a LeaderWorkerSet container")
    return yaml.safe_dump_all(documents, sort_keys=False)


def vllm_prebuild(rendered: str, config: dict[str, Any], overlay: dict[str, Any], folder: Path,
                  *, preview_only: bool = False) -> str | None:
    """Build or preview the exact cache-warming Job for a serving LWS."""
    documents = [item for item in yaml.safe_load_all(rendered) if isinstance(item, dict)]
    maps = {item.get("metadata", {}).get("name"): item for item in documents if item.get("kind") == "ConfigMap"}
    script_map = maps.get("vllm-build")
    if not script_map or "vllm-wheel-build.sh" not in script_map.get("data", {}):
        return None
    ref_map = maps.get("vllm-build-ref")
    candidates = []
    for item in documents:
        if item.get("kind") != "LeaderWorkerSet":
            continue
        pod = item.get("spec", {}).get("leaderWorkerTemplate", {}).get("workerTemplate", {}).get("spec", {})
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
    env.extend(({"name": "VLLM_BUILD_ROLE", "value": "prefill"}, {"name": "LWS_WORKER_INDEX", "value": "0"}))
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


def submit_benchmark(config: dict[str, Any], overlay: dict[str, Any], bench: dict[str, Any],
                     campaign_dir: Path, baseline: list[str], commit: str | None, source_commit: str) -> dict[str, Any]:
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
        measurements = aiperf_measurements(Path(artifact), run_id, bench["concurrencies"])
    else:
        log = kube(config["namespace"], "logs", f"job/{job_name}").stdout
        (campaign_dir / "nyann-job.log").write_text(log)
        measurements = nyann_measurements(log, bench["concurrencies"])
    return {"tool": tool, "job": job_name, "run_id": run_id, "artifacts": artifact,
            "report": f"{artifact}/index.html" if tool == "aiperf" else None,
            "measurements": measurements, "status": "completed"}


def write_summary(destination: Path, summary: dict[str, Any], *, embedded_reports: bool = False) -> str:
    tmp = destination / "summary.json.tmp"
    tmp.write_text(json.dumps(summary, indent=2) + "\n")
    tmp.replace(destination / "summary.json")
    dimensions = sorted({key for record in summary["overlays"] for key in record.get("dimensions", {})})
    fields = ["build", "overlay", *dimensions, "tool", "sample", "concurrency", "status",
              "successful_requests", "error_requests",
              "requests_per_s", "requests_unit", "output_tokens_per_s", "output_tokens_unit",
              "ttft_p90", "ttft_unit", "itl_p90", "itl_unit", "report", "artifacts", "error"]
    rows = []
    for record in summary["overlays"]:
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
                       "artifacts": bench.get("artifacts", ""), "error": bench.get("error", "")}
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
    header = "".join(f"<th>{html.escape(field.replace('_', ' ').title())}</th>" for field in fields)
    def html_cell(row: dict[str, Any], field: str) -> str:
        value = str(row.get(field, ""))
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

    body = "".join("<tr>" + "".join(html_cell(row, field) for field in fields) + "</tr>" for row in rows)
    local_note = "<p>Local validation only: no deployment or benchmark was run.</p>" if summary.get("mode") == "local-test" else ""
    fragment = f"<h1>Campaign {html.escape(summary['id'])}</h1><p>Status: {html.escape(summary['status'])}</p>" + local_note + \
        "<h2>Builds</h2><table><tr><th>Build</th><th>Status</th><th>Resolved inputs</th><th>Error</th></tr>" + \
        "".join(build_rows) + "</table>" + \
        "<h2>All configurations</h2><table><tr>" + header + "</tr>" + body + "</table>"
    page = "<!doctype html><html><head><meta charset='utf-8'><title>Benchmark campaign</title>" + \
        "<style>body{font:14px system-ui;margin:2rem}table{border-collapse:collapse}th,td{border:1px solid #aaa;padding:.5rem;text-align:left}</style>" + \
        "</head><body>" + fragment + "</body></html>"
    (destination / "index.html").write_text(page)
    return fragment


def write_final_report(destination: Path, summary: dict[str, Any]) -> None:
    """Embed every completed AIPerf sweep in the same HTML as the matrix and nyann rows."""
    runs = []
    for record in summary["overlays"]:
        for bench in record.get("benchmarks", []):
            if bench.get("tool") != "aiperf" or bench.get("status") != "completed":
                continue
            artifact = Path(bench["artifacts"])
            for directory in sorted(artifact.iterdir()):
                data = AIPERF_REPORT.run_data(directory)
                if data is None:
                    continue
                label = f"{record.get('build', 'default')} / {record.get('overlay', record['name'])}"
                dimensions = ", ".join(f"{key}={value}" for key, value in sorted(record.get("dimensions", {}).items()))
                data["metadata"]["campaign_label"] = f"{label} ({dimensions})" if dimensions else label
                runs.append(data)
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
    base_manifests = {overlay["name"]: apply_vllm_image(render_overlay(overlay_root, overlay), config["vllm_image"])
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
                rendered = inject_vllm_build_script(base_manifests[overlay["name"]], build)
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


def test_local(config: dict[str, Any], destination: Path, source_dir: Path | None = None) -> int:
    """Render and validate every campaign case without contacting Kubernetes."""
    destination.mkdir(parents=True, exist_ok=False)
    (destination / "campaign.json").write_text(json.dumps(config, indent=2) + "\n")
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
                rendered = apply_vllm_image(rendered, config["vllm_image"])
                rendered = inject_vllm_build_script(rendered, build)
                validate_manifest(rendered, config["namespace"])
                (folder / "manifest.yaml").write_text(rendered)
                record["manifest_sha256"] = hashlib.sha256(rendered.encode()).hexdigest()
                if build["mode"] == "source" or "deepep" in build:
                    commit = vllm_prebuild(rendered, config, overlay, folder, preview_only=True)
                    if not commit:
                        raise ValueError("overlay has no compatible vLLM prebuild script")
                    record["prebuild_job"] = str(folder / "prebuild-job.yaml")
                for bench in config["benchmarks"]:
                    record["benchmarks"].append({"tool": bench["tool"], "status": "planned", "measurements": [
                        {"sample": f"c{concurrency}", "concurrency": concurrency}
                        for concurrency in bench["concurrencies"]]})
            except Exception as exc:
                record["status"] = "failed"
                record["error"] = str(exc)
        summary["status"] = "failed" if any(item["status"] == "failed" for item in
                                             [*summary["builds"], *summary["overlays"]]) else "validated"
        summary["finished_at"] = datetime.now(timezone.utc).isoformat()
        write_summary(destination, summary)
        print(f"Local campaign test {summary['status']}; artifacts: {destination}", flush=True)
        return 0 if summary["status"] == "validated" else 1
    except Exception as exc:
        summary["status"] = "failed"
        summary["error"] = str(exc)
        summary["finished_at"] = datetime.now(timezone.utc).isoformat()
        write_summary(destination, summary)
        print(f"Local campaign test failed: {exc}; artifacts: {destination}", file=sys.stderr)
        return 1
    finally:
        if temporary_source and "overlay_root" in locals():
            shutil.rmtree(overlay_root)


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
                        apply_vllm_image(render_overlay(overlay_root, overlay), config["vllm_image"]))
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
            rendered = inject_vllm_build_script(rendered, build)
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
                    result = submit_benchmark(config, overlay, bench, folder, baseline, commit, source_commit)
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
    try:
        write_final_report(destination, summary)
    except Exception as exc:
        summary["status"] = "failed"
        summary["report_error"] = str(exc)
        write_summary(destination, summary)
        failed = True
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
    parser.add_argument("action", choices=["validate", "submit", "run", "test-local"])
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
        return run(config)
    except (ValueError, RuntimeError, TimeoutError, OSError, json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
        print(f"campaign: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
