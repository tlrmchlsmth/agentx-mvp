#!/usr/bin/env bash
# Optional DeepEP wheel build. Sourced by the campaign build script with either
# the image's vLLM or a campaign-built vLLM. Uses the shared build-cache PVC.
source "$(dirname "${BASH_SOURCE[0]}")/build-lib.sh"

DEEPEP_REPO=${DEEPEP_BUILD_REPO:?FATAL: DEEPEP_BUILD_REPO is required}
DEEPEP_REF=${DEEPEP_BUILD_REF:?FATAL: DEEPEP_BUILD_REF is required}
DEEPEP_SHA=${DEEPEP_BUILD_COMMIT:?FATAL: DEEPEP_BUILD_COMMIT is required}
if ! [[ "$DEEPEP_SHA" =~ ^[0-9a-f]{40}$ ]] ||
   ! [[ "$DEEPEP_REF" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$ ]]; then
  echo "FATAL: DeepEP needs a branch and full pinned commit."
  exit 1
fi

# Keep the recipe and ABI inputs in the cache identity. This key is independent
# of the vLLM branch composition, so multiple variants can reuse one wheel.
DEEPEP_VARIANT=v10-pinned-abi-locked
DEEPEP_HASH=$(
  {
    printf 'variant=%s\n' "$DEEPEP_VARIANT"
    printf 'repo=%s\nref=%s\nsha=%s\n' "$DEEPEP_REPO" "$DEEPEP_REF" "$DEEPEP_SHA"
    printf 'base_image=%s\ntorch=%s\ncuda=%s\npython_abi=%s\n' \
      "$BASE_RUNTIME_IMAGE_ID" "$BASE_RUNTIME_TORCH_VERSION" \
      "$BASE_RUNTIME_CUDA_VERSION" "$BASE_RUNTIME_PYTHON_ABI"
  } | hash_cache_inputs
)
DEEPEP_CACHE_ROOT=${DEEPEP_BUILD_CACHE_ROOT:-/shared/vllm-build/deepep_build}
DEEPEP_CACHE="${DEEPEP_CACHE_ROOT}/${DEEPEP_HASH}-${DEEPEP_VARIANT}"
DEEPEP_WHEEL_DIR="${DEEPEP_CACHE}/wheel"
DEEPEP_READY="${DEEPEP_CACHE}/READY"
LOCK_DIR="${DEEPEP_CACHE}/build.lock"
LOCK_TOKEN="${HOSTNAME:-unknown}.$$.$RANDOM"
LOCK_HEARTBEAT_PID=""
BUILD_LOCK_HELD=0
mkdir -p "$DEEPEP_CACHE"

deepep_ready_wheel() {
  local wheel commit
  [ -f "$DEEPEP_READY" ] || return 1
  wheel=$(read_ready_field "$DEEPEP_READY" wheel)
  commit=$(read_ready_field "$DEEPEP_READY" commit)
  [ "$commit" = "$DEEPEP_SHA" ] || return 1
  ready_wheel_from_marker "$DEEPEP_READY" "$DEEPEP_WHEEL_DIR" 'deep_ep-*.whl'
}

install_ready_deepep() {
  local wheel
  wheel=$(deepep_ready_wheel) || return 1
  wheel_is_valid "$wheel" || return 1
  echo "Installing cached DeepEP ${DEEPEP_REF}@${DEEPEP_SHA}: $(basename "$wheel")"
  uv pip install --system --force-reinstall --no-deps "$wheel" || return 1
  (cd / && python3 -c 'import deep_ep') || return 1
}

build_deepep_wheel() {
  local fetched nvrtc_header nvrtc_library nccl_library nvshmem_library
  local link_dir local_wheels build_log wheel published
  echo "Building DeepEP ${DEEPEP_REF}@${DEEPEP_SHA}; cache ${DEEPEP_CACHE}"
  rm -rf /tmp/deepep-src /tmp/deepep-wheel-out || return 1
  mkdir -p /tmp/deepep-src /tmp/deepep-wheel-out "$DEEPEP_WHEEL_DIR" "${DEEPEP_CACHE}/logs" || return 1
  cd /tmp/deepep-src || return 1
  git init -q || return 1
  git fetch -q --no-tags --depth=1 "$DEEPEP_REPO" "refs/heads/${DEEPEP_REF}" || return 1
  fetched=$(git rev-parse FETCH_HEAD)
  if [ "$fetched" != "$DEEPEP_SHA" ]; then
    echo "FATAL: DeepEP branch ${DEEPEP_REF} moved after campaign resolution."
    return 1
  fi
  git checkout -q --detach FETCH_HEAD || return 1
  uv pip install --system -q setuptools setuptools_scm ninja cmake || return 1

  # The base image can place CUDA and communication libraries under pip's
  # nvidia package rather than under /usr/local/cuda.
  nvrtc_header=$(find /usr/local/cuda* /usr/local/lib/python*/dist-packages/nvidia \
    -name nvrtc.h -print 2>/dev/null | head -1 || true)
  nvrtc_library=$(find /usr/local/cuda* /usr/local/lib/python*/dist-packages/nvidia \
    -name 'libnvrtc.so*' -print 2>/dev/null | sort | head -1 || true)
  if [ -z "$nvrtc_header" ] || [ -z "$nvrtc_library" ]; then
    echo "FATAL: DeepEP build needs the NVRTC header and library."
    return 1
  fi
  export CPATH="$(dirname "$nvrtc_header"):${CPATH:-}"
  export LIBRARY_PATH="$(dirname "$nvrtc_library"):${LIBRARY_PATH:-}"
  export LD_LIBRARY_PATH="$(dirname "$nvrtc_library"):${LD_LIBRARY_PATH:-}"

  link_dir=/tmp/deepep-link
  rm -rf "$link_dir" || return 1
  mkdir -p "$link_dir" || return 1
  nccl_library=$(find /usr/local/lib/python*/dist-packages/nvidia /usr/local/cuda* \
    /usr/lib/x86_64-linux-gnu /usr/lib -type f -name 'libnccl.so*' -print 2>/dev/null | sort | head -1 || true)
  nvshmem_library=$(find /usr/local/lib/python*/dist-packages/nvidia /usr/local/cuda* \
    /usr/lib -type f -name 'libnvshmem_host.so*' -print 2>/dev/null | sort | head -1 || true)
  if [ -n "$nccl_library" ]; then
    ln -sf "$nccl_library" "$link_dir/libnccl.so" || return 1
    ln -sf "$nccl_library" "$link_dir/libnccl.so.2" || return 1
    export LD_LIBRARY_PATH="$(dirname "$nccl_library"):${LD_LIBRARY_PATH:-}"
  else
    echo "WARN: libnccl.so was not found; DeepEP linking may fail."
  fi
  if [ -n "$nvshmem_library" ]; then
    ln -sf "$nvshmem_library" "$link_dir/libnvshmem_host.so" || return 1
    export LD_LIBRARY_PATH="$(dirname "$nvshmem_library"):${LD_LIBRARY_PATH:-}"
  else
    echo "WARN: libnvshmem_host.so was not found; DeepEP linking may fail."
  fi
  export LIBRARY_PATH="${link_dir}:${LIBRARY_PATH:-}"

  local_wheels=/tmp/deepep-wheel-out
  build_log="${DEEPEP_CACHE}/logs/build-${HOSTNAME:-$(hostname)}.log"
  TORCH_CUDA_ARCH_LIST=9.0 MAX_JOBS=${MAX_JOBS:-$(nproc)} NVCC_THREADS=${NVCC_THREADS:-8} \
    uv build --wheel --no-build-isolation -o "$local_wheels" . 2>&1 | tee "$build_log" || return 1
  published=""
  for wheel in "$local_wheels"/deep_ep-*.whl; do
    [ -f "$wheel" ] || continue
    published=$(publish_wheel_atomic "$wheel" "$DEEPEP_WHEEL_DIR") || return 1
  done
  [ -n "$published" ] || { echo "FATAL: DeepEP build produced no wheel."; return 1; }
  uv pip install --system --force-reinstall --no-deps "$published" || return 1
  (cd / && python3 -c 'import deep_ep') || return 1
  write_ready_file "$DEEPEP_READY" "wheel=$(basename "$published")" "commit=${DEEPEP_SHA}"
  echo "Built and cached DeepEP ${DEEPEP_REF}@${DEEPEP_SHA}: $published"
  cd / || return 1
  rm -rf /tmp/deepep-src "$local_wheels" "$link_dir" || return 1
}

if ! install_ready_deepep; then
  # The nightly path sources this script without a leader election.
  BUILD_LEADER=${BUILD_LEADER:-0}
  if ! wait_for_ready_wheel install_ready_deepep "DeepEP"; then
    trap release_build_lock EXIT

    # A publisher may have completed between our initial lookup and lock
    # acquisition. Recheck while holding the lock before paying for a build.
    if ! install_ready_deepep; then
      start_build_lock_heartbeat
      rm -f "$DEEPEP_READY"
      write_lock_owner_file "${DEEPEP_HASH}-${DEEPEP_VARIANT}"
      if ! build_deepep_wheel; then
        echo "FATAL: DeepEP build failed."
        exit 1
      fi
    fi
    release_build_lock
    trap - EXIT
  fi
fi
