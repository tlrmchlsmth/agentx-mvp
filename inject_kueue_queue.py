#!/usr/bin/env python3
"""Add a Kueue queue label to rendered manifesto LeaderWorkerSets."""

from __future__ import annotations

import argparse
import sys
from typing import Any

import yaml


QUEUE_LABEL = "kueue.x-k8s.io/queue-name"

# Go yaml.v2 (used by Kubernetes) interprets these bare tokens as booleans
# per YAML 1.1. PyYAML 6.x stopped treating single-letter variants (n, y,
# N, Y) as booleans, so safe_dump emits them unquoted — which Go then
# unmarshals as bool, breaking e.g. EnvVar.value.  Force quoting for all of
# them so the roundtrip through this script is Kubernetes-safe.
_GO_YAML_BOOLS = frozenset({
    "y", "Y", "yes", "Yes", "YES",
    "n", "N", "no", "No", "NO",
    "true", "True", "TRUE",
    "false", "False", "FALSE",
    "on", "On", "ON",
    "off", "Off", "OFF",
})


class _KubeSafeDumper(yaml.SafeDumper):
    """SafeDumper that quotes strings Go would interpret as booleans."""


def _kube_str_representer(dumper: _KubeSafeDumper, data: str) -> yaml.ScalarNode:
    if data in _GO_YAML_BOOLS:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="'")
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


_KubeSafeDumper.add_representer(str, _kube_str_representer)


def add_queue_label(obj: dict[str, Any], queue: str) -> None:
    if obj.get("kind") != "LeaderWorkerSet":
        return
    metadata = obj.setdefault("metadata", {})
    labels = metadata.setdefault("labels", {})
    labels[QUEUE_LABEL] = queue


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--queue", required=True, help="Kueue LocalQueue name")
    args = parser.parse_args()

    docs = list(yaml.safe_load_all(sys.stdin))
    for doc in docs:
        if isinstance(doc, dict):
            add_queue_label(doc, args.queue)

    yaml.dump_all(
        docs,
        sys.stdout,
        Dumper=_KubeSafeDumper,
        explicit_start=True,
        sort_keys=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
