#!/usr/bin/env bash
# Generic campaign build for overlays without their own wheel build script.
# The prebuild Job publishes one pinned wheel; serving Pods only install it.
set -Eeuo pipefail

BASE_RUNTIME_IMAGE_ID=${VLLM_BUILD_BASE_IMAGE_ID:?missing base image}
BASE_RUNTIME_TORCH_VERSION=$(python3 -c 'import torch; print(torch.__version__)')
BASE_RUNTIME_CUDA_VERSION=$(python3 -c 'import torch; print(torch.version.cuda or "none")')
BASE_RUNTIME_PYTHON_ABI=$(python3 -c 'import sysconfig; print(sysconfig.get_config_var("SOABI") or "unknown")')
BUILD_LEADER=0
if [ "${VLLM_BUILD_PREBUILD:-0}" = 1 ] || { [ "${VLLM_BUILD_ROLE:-}" = prefill ] && [ "${LWS_WORKER_INDEX:-}" = 0 ]; }; then
  BUILD_LEADER=1
fi

if [ "${VLLM_BUILD_MODE:-}" = source ]; then
  : "${VLLM_BUILD_REPO:?missing vLLM repository}"
  : "${VLLM_BUILD_COMMIT:?missing vLLM commit}"
  read -r -a REFS <<< "${VLLM_BUILD_REFS:?missing vLLM refs}"
  read -r -a ACTIONS <<< "${VLLM_BUILD_ACTIONS:?missing vLLM actions}"
  read -r -a SHAS <<< "${VLLM_BUILD_SHAS:?missing vLLM SHAs}"
  if [ "${#REFS[@]}" -eq 0 ] || [ "${#REFS[@]}" -ne "${#ACTIONS[@]}" ] ||
     [ "${#REFS[@]}" -ne "${#SHAS[@]}" ] || [ "${ACTIONS[0]}" != checkout ] ||
     [ "${SHAS[0]}" != "$VLLM_BUILD_COMMIT" ]; then
    echo 'Invalid vLLM build inputs' >&2
    exit 1
  fi
  for sha in "${SHAS[@]}"; do
    [[ "$sha" =~ ^[0-9a-f]{40}$ ]] || { echo "Invalid vLLM SHA: $sha" >&2; exit 1; }
  done

  KEY=$(printf '%s\n' "$(sha256sum /opt/build-scripts/vllm-wheel-build.sh)" "$VLLM_BUILD_REPO" \
    "$VLLM_BUILD_REFS" "$VLLM_BUILD_ACTIONS" "$VLLM_BUILD_SHAS" \
    "$BASE_RUNTIME_IMAGE_ID" "$BASE_RUNTIME_TORCH_VERSION" "$BASE_RUNTIME_CUDA_VERSION" \
    "$BASE_RUNTIME_PYTHON_ABI" | sha256sum | cut -c1-24)
  CACHE="/shared/vllm-build/source/${KEY}"
  READY="$CACHE/READY"
  wheel_path() {
    local filename recorded_sha
    [ -f "$READY" ] || return 1
    filename=$(sed -n '1p' "$READY")
    recorded_sha=$(sed -n '2p' "$READY")
    case "$filename" in vllm-*.whl) ;; *) return 1 ;; esac
    [ "$filename" = "$(basename "$filename")" ] || return 1
    [ -f "$CACHE/$filename" ] || return 1
    [ "$(sha256sum "$CACHE/$filename" | cut -d' ' -f1)" = "$recorded_sha" ] || return 1
    printf '%s\n' "$CACHE/$filename"
  }

  if [ "${VLLM_BUILD_PREBUILD:-0}" = 1 ] && ! wheel_path >/dev/null; then
    command -v git >/dev/null || { apt-get update -qq && apt-get install -y -qq git; }
    command -v uv >/dev/null || python3 -m pip install -q uv
    SRC=$(mktemp -d /tmp/vllm-source.XXXXXX)
    OUT=$(mktemp -d /tmp/vllm-wheel.XXXXXX)
    git -C "$SRC" init -q
    for i in "${!REFS[@]}"; do
      git -C "$SRC" fetch -q --no-tags --depth=256 "$VLLM_BUILD_REPO" "refs/heads/${REFS[$i]}"
      [ "$(git -C "$SRC" rev-parse FETCH_HEAD)" = "${SHAS[$i]}" ] || {
        echo "vLLM branch ${REFS[$i]} moved after campaign resolution" >&2
        exit 1
      }
      git -C "$SRC" update-ref "refs/build-input/$i" "${SHAS[$i]}"
    done
    git -C "$SRC" checkout -q --detach "${SHAS[0]}"
    for ((i=1; i<${#REFS[@]}; i++)); do
      case "${ACTIONS[$i]}" in
        merge) git -C "$SRC" -c user.name=campaign -c user.email=campaign@localhost \
          merge --no-edit "refs/build-input/$i" ;;
        cherry-pick) git -C "$SRC" -c user.name=campaign -c user.email=campaign@localhost \
          cherry-pick "refs/build-input/$i" ;;
        cherry-pick-parent1) git -C "$SRC" -c user.name=campaign -c user.email=campaign@localhost \
          cherry-pick -m 1 "refs/build-input/$i" ;;
        cherry-pick-m*) git -C "$SRC" -c user.name=campaign -c user.email=campaign@localhost \
          cherry-pick -m "${ACTIONS[$i]#cherry-pick-m}" "refs/build-input/$i" ;;
        *) echo "Invalid vLLM action: ${ACTIONS[$i]}" >&2; exit 1 ;;
      esac
    done
    git -C "$SRC" submodule update --init --recursive
    # Match the base image's CUDA/PyTorch stack. Native libraries come from
    # the exact first upstream commit; custom native changes need an overlay recipe.
    uv pip install --system -q setuptools wheel setuptools_scm setuptools_rust ninja cmake pybind11 build
    (cd "$SRC" && VLLM_USE_PRECOMPILED=1 \
      VLLM_PRECOMPILED_WHEEL_COMMIT="${SHAS[0]}" \
      uv build --wheel --no-build-isolation -o "$OUT" .)
    WHEEL=$(find "$OUT" -maxdepth 1 -type f -name 'vllm-*.whl' -print -quit)
    [ -n "$WHEEL" ] || { echo 'vLLM wheel build produced no wheel' >&2; exit 1; }
    mkdir -p "$CACHE"
    cp "$WHEEL" "$CACHE/.$(basename "$WHEEL").$$.tmp"
    mv "$CACHE/.$(basename "$WHEEL").$$.tmp" "$CACHE/$(basename "$WHEEL")"
    printf '%s\n%s\n' "$(basename "$WHEEL")" \
      "$(sha256sum "$CACHE/$(basename "$WHEEL")" | cut -d' ' -f1)" > "$READY.$$.tmp"
    mv "$READY.$$.tmp" "$READY"
    rm -rf "$SRC" "$OUT"
  fi
  WHEEL=$(wheel_path) || { echo "Missing cached vLLM wheel for $KEY" >&2; exit 1; }
  python3 -m pip install --no-deps --force-reinstall "$WHEEL"
  python3 -c 'import vllm'
fi

if [ "${DEEPEP_BUILD_ENABLED:-0}" = 1 ]; then
  command -v uv >/dev/null || python3 -m pip install -q uv
  source /opt/build-scripts/deepep-wheel-build.sh
fi
