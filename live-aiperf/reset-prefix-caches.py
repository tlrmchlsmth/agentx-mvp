#!/usr/bin/env python3
"""Reset every prefill and decode serving pod's prefix cache."""

import json
import os
import ssl
import time
import urllib.parse
import urllib.request


NAMESPACE = os.environ["AIPERF_NAMESPACE"]
MODEL_LABEL = os.environ["MODEL_LABEL"]
TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"
CA_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
API = f"https://{os.environ['KUBERNETES_SERVICE_HOST']}:{os.environ['KUBERNETES_SERVICE_PORT_HTTPS']}"

with open(TOKEN_PATH, encoding="utf-8") as token_file:
    TOKEN = token_file.read().strip()
SSL_CONTEXT = ssl.create_default_context(cafile=CA_PATH)


def kube_request(path):
    request = urllib.request.Request(
        API + path, headers={"Authorization": f"Bearer {TOKEN}"}
    )
    with urllib.request.urlopen(request, context=SSL_CONTEXT, timeout=30) as response:
        return json.load(response)


def serving_pods():
    selector = urllib.parse.quote(
        f"llm-d.ai/model={MODEL_LABEL},llm-d.ai/inference-serving=true", safe=""
    )
    result = kube_request(f"/api/v1/namespaces/{NAMESPACE}/pods?labelSelector={selector}")
    targets = []
    for pod in result["items"]:
        metadata = pod["metadata"]
        status = pod.get("status", {})
        role = metadata.get("labels", {}).get("llm-d.ai/role")
        if role not in {"prefill", "decode"} or metadata.get("deletionTimestamp"):
            continue
        ready = any(
            condition.get("type") == "Ready" and condition.get("status") == "True"
            for condition in status.get("conditions", [])
        )
        if not ready or not status.get("podIP"):
            raise RuntimeError(f"serving pod is not ready: {metadata['name']}")
        targets.append((metadata["name"], role, status["podIP"]))
    if {role for _, role, _ in targets} != {"prefill", "decode"}:
        raise RuntimeError(f"expected prefill and decode targets, found: {targets!r}")
    return sorted(targets)


def reset_target(name, role, address):
    port = 8000 if role == "prefill" else 8200
    host = f"[{address}]" if ":" in address else address
    url = f"http://{host}:{port}/reset_prefix_cache"
    last_error = None
    for attempt in range(1, 11):
        try:
            request = urllib.request.Request(url, method="POST")
            with urllib.request.urlopen(request, timeout=60) as response:
                if response.status == 200:
                    print(f"Reset prefix cache: {name} ({role}, {address})", flush=True)
                    return
                last_error = RuntimeError(f"HTTP {response.status}")
        except Exception as error:
            last_error = error
        if attempt != 10:
            time.sleep(3)
    raise RuntimeError(f"failed to reset {name} at {url}: {last_error}")


for target in serving_pods():
    reset_target(*target)
