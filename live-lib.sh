#!/usr/bin/env bash
# Shared discovery helpers for the live benchmark launchers
# (live-aiperf/submit.sh, live-nyann/submit.sh, live-aiperf/download-report.sh).
# Sourced, never executed: no top-level side effects, and written for the macOS
# system Bash 3.2 (indexed arrays only, no empty-array expansion).
#
# The functions share one contract: they print their result (namespace, queue)
# or export it as a variable (SOURCE_*, MODEL_LABEL), report errors on stderr,
# and return nonzero instead of exiting so callers control the exit code.

# Resolves the namespace carrying the deployed vLLM build. An explicit
# namespace in $1 (an environment variable name such as LIVE_AIPERF_NAMESPACE)
# always wins; otherwise the cluster must carry exactly one namespace with a
# valid vllm-build-ref marker. Prints the namespace.
discover_live_namespace() {
  local env_var=$1
  local namespace commit
  local -a deployment_namespaces=()
  namespace=${!env_var:-}
  if [[ -n "$namespace" ]]; then
    printf '%s\n' "$namespace"
    return 0
  fi
  echo "Discovering the deployed vLLM namespace..." >&2
  while IFS=$'\t' read -r namespace commit; do
    [[ "$commit" =~ ^[0-9a-f]{40}$ ]] && deployment_namespaces+=("$namespace")
  done < <(kubectl get configmaps --all-namespaces -o go-template='{{range .items}}{{if eq .metadata.name "vllm-build-ref"}}{{.metadata.namespace}}{{"\t"}}{{index .data "VLLM_BUILD_COMMIT"}}{{"\n"}}{{end}}{{end}}')
  if (( ${#deployment_namespaces[@]} != 1 )); then
    echo "Could not identify exactly one deployed vLLM namespace (found: ${deployment_namespaces[*]:-none})" >&2
    echo "Set ${env_var} when choosing intentionally among multiple deployments." >&2
    return 1
  fi
  printf '%s\n' "${deployment_namespaces[0]}"
}

# Validates and verifies the Kueue LocalQueue every live benchmark submits
# into. Prints the queue name.
require_live_benchmark_queue() {
  local namespace=$1 queue
  queue="${LIVE_BENCHMARK_QUEUE:-live-benchmark-client}"
  if [[ ! "$queue" =~ ^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$ ]] || (( ${#queue} > 63 )); then
    echo "invalid LIVE_BENCHMARK_QUEUE: ${queue}" >&2
    return 1
  fi
  kubectl get localqueue "$queue" -n "$namespace" -o name >/dev/null || {
    echo "Missing Kueue LocalQueue ${namespace}/${queue}; run just live-benchmark-kueue-setup ${namespace}" >&2
    return 1
  }
  printf '%s\n' "$queue"
}

# Resolves what the benchmark measures. Campaigns pin the llm-d deployment
# source through LIVE_BENCHMARK_SOURCE_*; standalone live runs fall back to
# the published vLLM build marker in the namespace. Exports SOURCE_REF,
# SOURCE_COMMIT, SOURCE_KIND, and SOURCE_SHORT.
resolve_live_benchmark_source() {
  local namespace=$1
  if [[ -n "${LIVE_BENCHMARK_SOURCE_COMMIT:-}" ]]; then
    SOURCE_REF="${LIVE_BENCHMARK_SOURCE_REF:-}"
    SOURCE_COMMIT="$LIVE_BENCHMARK_SOURCE_COMMIT"
    SOURCE_KIND="${LIVE_BENCHMARK_SOURCE_KIND:-llm-d}"
  else
    echo "Reading deployed vLLM build metadata..." >&2
    SOURCE_REF="$(kubectl get configmap vllm-build-ref -n "$namespace" -o jsonpath='{.data.VLLM_BUILD_REF}')"
    SOURCE_COMMIT="$(kubectl get configmap vllm-build-ref -n "$namespace" -o jsonpath='{.data.VLLM_BUILD_COMMIT}')"
    SOURCE_KIND=vllm
  fi
  if [[ ! "$SOURCE_REF" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]*$ ]] ||
     ! [[ "$SOURCE_COMMIT" =~ ^[0-9a-f]{40}$ ]] ||
     ! [[ "$SOURCE_KIND" == vllm || "$SOURCE_KIND" == llm-d ]]; then
    echo "Benchmark source ref/commit is missing or malformed" >&2
    return 1
  fi
  SOURCE_SHORT="${SOURCE_COMMIT:0:12}"
}

# Discovers the serving model from the cluster rather than baking a model name
# into the launchers. More than one model is an ambiguous benchmark target, so
# MODEL_LABEL is only required in that exceptional case. Exports MODEL_LABEL.
discover_live_serving_model() {
  local namespace=$1 discovered_model
  echo "Discovering the serving model..." >&2
  DISCOVERED_MODELS=()
  while IFS= read -r discovered_model; do
    [[ -n "$discovered_model" ]] && DISCOVERED_MODELS+=("$discovered_model")
  done < <(
    kubectl get pods -n "$namespace" -l llm-d.ai/inference-serving=true \
      -o go-template='{{range .items}}{{index .metadata.labels "llm-d.ai/model"}}{{"\n"}}{{end}}' \
      | awk 'NF' | sort -u
  )
  if [[ -z "${MODEL_LABEL:-}" ]]; then
    if (( ${#DISCOVERED_MODELS[@]} == 0 )); then
      echo "No inference-serving pods with llm-d.ai/model found in ${namespace}" >&2
      return 1
    fi
    if (( ${#DISCOVERED_MODELS[@]} != 1 )); then
      echo "More than one serving model was found: ${DISCOVERED_MODELS[*]}" >&2
      echo "Set MODEL_LABEL only when selecting one intentionally." >&2
      return 1
    fi
    MODEL_LABEL="${DISCOVERED_MODELS[0]}"
  fi
}
