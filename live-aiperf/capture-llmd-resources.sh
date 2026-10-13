#!/usr/bin/env bash
set -euo pipefail

NAMESPACE="${1:?namespace is required}"
MODEL_LABEL="${2:?model label is required}"

resources=()
add_if_present() {
  local resource="$1"
  if kubectl get -n "$NAMESPACE" "$resource" -o name >/dev/null 2>&1; then
    resources+=("$resource")
  fi
}

# Core router, Gateway API, and build identity resources.
for resource in \
  configmap/wide-ep-lws-epp \
  configmap/llm-d-inference-gateway \
  configmap/vllm-build-ref \
  configmap/kimi-k3-rendered-configs \
  deployment/wide-ep-lws-epp \
  deployment/llm-d-inference-gateway-istio \
  serviceaccount/wide-ep-lws-epp \
  serviceaccount/llm-d-inference-gateway-istio \
  role/wide-ep-lws-epp-sa \
  role/wide-ep-lws-epp-non-sa \
  rolebinding/wide-ep-lws-epp-sa \
  rolebinding/wide-ep-lws-epp-non-sa \
  gateway.gateway.networking.k8s.io/llm-d-inference-gateway \
  httproute.gateway.networking.k8s.io/wide-ep-lws \
  inferencepool.inference.networking.k8s.io/wide-ep-lws
do
  add_if_present "$resource"
done

# Model-specific workload objects and all services forming the wide-EP path.
while IFS= read -r resource; do
  [[ -z "$resource" ]] || resources+=("$resource")
done < <(kubectl get -n "$NAMESPACE" serviceaccount,leaderworkerset.leaderworkerset.x-k8s.io \
  -l "llm-d.ai/model=${MODEL_LABEL}" -o name)
while IFS= read -r resource; do
  [[ -z "$resource" ]] || resources+=("$resource")
done < <(kubectl get -n "$NAMESPACE" service -o name | awk '/service\/(wide-ep-lws|llm-d-inference-gateway)/')

unique_resources=()
while IFS= read -r resource; do
  [[ -z "$resource" ]] || unique_resources+=("$resource")
done < <(printf '%s\n' "${resources[@]}" | awk 'NF && !seen[$0]++')
(( ${#unique_resources[@]} > 0 )) || { echo "No llm-d resources found" >&2; exit 1; }

echo "# Live llm-d resource snapshot"
echo "# Namespace: ${NAMESPACE}"
echo "# Model: ${MODEL_LABEL}"
echo "# Captured: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "# Secrets are intentionally excluded."
kubectl get -n "$NAMESPACE" "${unique_resources[@]}" -o yaml
