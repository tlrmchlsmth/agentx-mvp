#!/usr/bin/env bash
# Optional DeepEP wheel build. Sourced after vLLM is installed by
# vllm-wheel-build.sh, using the same shared build-cache PVC and lock helpers.

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
  } | sha256sum | cut -c1-20
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
  wheel=$(awk -F= '$1 == "wheel" { print $2; exit }' "$DEEPEP_READY")
  commit=$(awk -F= '$1 == "commit" { print $2; exit }' "$DEEPEP_READY")
  [ "$commit" = "$DEEPEP_SHA" ] || return 1
  case "$wheel" in deep_ep-*.whl) ;; *) return 1 ;; esac
  [ "$wheel" = "$(basename "$wheel")" ] || return 1
  [ -f "${DEEPEP_WHEEL_DIR}/${wheel}" ] || return 1
  printf '%s\n' "${DEEPEP_WHEEL_DIR}/${wheel}"
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
  local link_dir local_wheels build_log wheel published temp_wheel
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
  uv pip install --system -q setuptools wheel setuptools_scm ninja cmake || return 1

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
    /usr/lib/x86_64-linux-gnu /usr/lib -type f -name 'libnvshmem_host.so*' -print 2>/dev/null | sort | head -1 || true)
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
    wheel_is_valid "$wheel" || return 1
    temp_wheel="${DEEPEP_WHEEL_DIR}/.$(basename "$wheel").$$.tmp"
    cp "$wheel" "$temp_wheel" || return 1
    wheel_is_valid "$temp_wheel" || return 1
    published="${DEEPEP_WHEEL_DIR}/$(basename "$wheel")"
    mv -f "$temp_wheel" "$published" || return 1
  done
  [ -n "$published" ] || { echo "FATAL: DeepEP build produced no wheel."; return 1; }
  uv pip install --system --force-reinstall --no-deps "$published" || return 1
  (cd / && python3 -c 'import deep_ep') || return 1
  printf 'wheel=%s\ncommit=%s\n' "$(basename "$published")" "$DEEPEP_SHA" \
    > "${DEEPEP_READY}.$$.tmp" || return 1
  mv -f "${DEEPEP_READY}.$$.tmp" "$DEEPEP_READY" || return 1
  echo "Built and cached DeepEP ${DEEPEP_REF}@${DEEPEP_SHA}: $published"
  cd / || return 1
  rm -rf /tmp/deepep-src "$local_wheels" "$link_dir" || return 1
}

if ! install_ready_deepep; then
  if [ "$BUILD_LEADER" != 1 ]; then
    waited=0
    wait_seconds=${VLLM_BUILD_LOCK_WAIT_SECONDS:-10800}
    poll_seconds=${VLLM_BUILD_LOCK_POLL_SECONDS:-10}
    echo "Waiting for the DeepEP READY wheel from the build leader."
    while [ "$waited" -lt "$wait_seconds" ]; do
      if install_ready_deepep; then break; fi
      sleep "$poll_seconds"
      waited=$((waited + poll_seconds))
    done
    if [ "$waited" -ge "$wait_seconds" ]; then
      echo "FATAL: timed out waiting for the DeepEP READY wheel."
      exit 1
    fi
  else
    recover_restarted_leader_lock || true
    waited=0
    wait_seconds=${VLLM_BUILD_LOCK_WAIT_SECONDS:-10800}
    poll_seconds=${VLLM_BUILD_LOCK_POLL_SECONDS:-10}
    stale_seconds=${VLLM_BUILD_LOCK_STALE_SECONDS:-600}
    while ! claim_build_lock; do
      if install_ready_deepep; then break; fi
      if [ -d "$LOCK_DIR" ]; then
        lock_mtime=$(stat -c %Y "${LOCK_DIR}/heartbeat" 2>/dev/null || stat -c %Y "$LOCK_DIR" 2>/dev/null || echo 0)
        lock_age=$(($(date +%s) - lock_mtime))
        if [ "$lock_mtime" -gt 0 ] && [ "$lock_age" -ge "$stale_seconds" ]; then
          stale_lock="${LOCK_DIR}.stale.${HOSTNAME:-unknown}.$$"
          if mv "$LOCK_DIR" "$stale_lock" 2>/dev/null; then
            rm -rf "$stale_lock"
          fi
        fi
      fi
      if [ "$waited" -ge "$wait_seconds" ]; then
        echo "FATAL: timed out acquiring DeepEP build lock."
        exit 1
      fi
      sleep "$poll_seconds"
      waited=$((waited + poll_seconds))
    done
    if [ "$BUILD_LOCK_HELD" = 1 ]; then
      trap release_build_lock EXIT
      if ! install_ready_deepep; then
        start_build_lock_heartbeat
        rm -f "$DEEPEP_READY"
        printf 'host=%s\npid=%s\ntoken=%s\n' "${HOSTNAME:-unknown}" "$$" "$LOCK_TOKEN" \
          > "${LOCK_DIR}/owner"
        if ! build_deepep_wheel; then
          echo "FATAL: DeepEP build failed."
          exit 1
        fi
      fi
      release_build_lock
      trap - EXIT
    fi
  fi
fi
