#!/usr/bin/env bash
# Generic campaign build for overlays without their own wheel build script.
# The prebuild Job publishes one pinned wheel; serving Pods only install it.
set -Eeuo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/build-lib.sh"

BASE_RUNTIME_IMAGE_ID=${VLLM_BUILD_BASE_IMAGE_ID:?missing base image}
detect_base_runtime_abi
elect_build_leader

if [ "${VLLM_BUILD_MODE:-}" = source ]; then
  : "${VLLM_BUILD_REPO:?missing vLLM repository}"
  : "${VLLM_BUILD_COMMIT:?missing vLLM commit}"
  read -r -a RECIPE_BRANCHES <<< "${VLLM_BUILD_REFS:?missing vLLM refs}"
  read -r -a RECIPE_ACTIONS <<< "${VLLM_BUILD_ACTIONS:?missing vLLM actions}"
  read -r -a RECIPE_SHAS <<< "${VLLM_BUILD_SHAS:?missing vLLM SHAs}"
  validate_recipe_inputs
  if [ "${RECIPE_SHAS[0]}" != "$VLLM_BUILD_COMMIT" ]; then
    echo 'Invalid vLLM build inputs' >&2
    exit 1
  fi

  # Cache identity includes the mounted build scripts themselves: editing the
  # recipe machinery re-keys the cache so stale wheels are never reused.
  KEY=$(printf '%s\n' "$(sha256sum /opt/build-scripts/vllm-wheel-build.sh)" \
    "$(sha256sum /opt/build-scripts/build-lib.sh)" "$VLLM_BUILD_REPO" \
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
    recipe_fetch_branch_refs
    fetch_build_inputs "$SRC" "$VLLM_BUILD_REPO" 256
    apply_build_recipe "$SRC" campaign campaign@localhost
    git -C "$SRC" submodule update --init --recursive
    # Match the base image's CUDA/PyTorch stack. Native libraries come from
    # the exact first upstream commit; custom native changes need an overlay recipe.
    uv pip install --system -q setuptools setuptools_scm setuptools_rust ninja cmake pybind11 build
    (cd "$SRC" && VLLM_USE_PRECOMPILED=1 \
      VLLM_PRECOMPILED_WHEEL_COMMIT="${RECIPE_SHAS[0]}" \
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
  source "$(dirname "${BASH_SOURCE[0]}")/deepep-wheel-build.sh"
fi
