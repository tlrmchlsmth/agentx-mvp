#!/usr/bin/env bash
# vLLM wheel build for benchmark campaign serving Pods and cache warmup Jobs.
# Source in the pinned vLLM runtime image; both paths use the same script and
# shared RWX cache. The build ref and full commit come from the overlay.
set -Eeuo pipefail

BUILD_REPO=${VLLM_BUILD_REPO:-https://github.com/elvircrn/vllm.git}
source "$(dirname "${BASH_SOURCE[0]}")/build-lib.sh"

# Run this file directly on the laptop to turn the ordered, moving branch set
# into one durable benchmark integration branch. The `checkout` action below
# means only "start the integration commit here"; it is never the branch used
# by Kubernetes. The generated branch name is intentionally unique, so a later
# experiment cannot overwrite this benchmark's source history.
publish_build_ref() {
  local ref_file target_branch work_dir merged_head remote_head fetch_depth

  if [ "$#" -ne 0 ]; then
    echo "Usage: $0"
    return 1
  fi

  # These are the original Kimi build inputs. A caller may override both
  # ordered lists; `checkout` establishes the local merge base and the pushed
  # benchmark branch is always the final consolidated HEAD.
  # PR #58372: populate completion_tokens_details.reasoning_tokens for Kimi K3.
  read -r -a RECIPE_ACTIONS <<< "${VLLM_BUILD_ACTIONS:-checkout cherry-pick-m2 cherry-pick cherry-pick-m2 cherry-pick-m2 cherry-pick cherry-pick cherry-pick cherry-pick}"
  read -r -a RECIPE_BRANCHES <<< "${VLLM_BUILD_REFS:-deepep-triton-epilogue fused-globalize-align-cuda kimi-k3-shard-sp-deepep-v2 humming-0.1.13-no-tuning-hacks fix/flashinfer-h200-backend vllm-profiling-qol dcp_fusion_triton nccl_cg codex/kimi-k3-reasoning-token-count}"
  RECIPE_SHAS=()
  validate_recipe_inputs || return 1
  recipe_fetch_branch_refs
  fetch_depth=${VLLM_BUILD_FETCH_DEPTH:-256}
  validate_fetch_depth "$fetch_depth" || return 1

  target_branch="benchmark/kimi-k3-$(date -u +%Y%m%dT%H%M%SZ)-$$"
  if git ls-remote --exit-code --heads "$BUILD_REPO" "refs/heads/${target_branch}" >/dev/null 2>&1; then
    echo "FATAL: generated benchmark branch unexpectedly already exists: ${target_branch}"
    return 1
  fi

  work_dir=$(mktemp -d "${TMPDIR:-/tmp}/kimi-k3-consolidate.XXXXXX") || return 1
  # A RETURN trap set inside a function also fires on the caller's return
  # unless cleared, and `work_dir` is gone by then; clear it as it fires.
  trap 'rm -rf -- "$work_dir"; trap - RETURN' RETURN
  # This temporary repository only creates and pushes an integration commit.
  # Keep it sparse (root files only), shallow, and blob-filtered; it never
  # compiles vLLM. The pod build intentionally uses a full checkout instead.
  git clone -q --no-checkout --sparse --filter=blob:none --depth="$fetch_depth" \
    "$BUILD_REPO" "$work_dir/vllm" || return 1

  # Resolve and fetch all inputs before merging. The refs/build-inputs
  # namespace preserves exactly the branch/action order requested by the
  # experimenter.
  fetch_build_inputs "$work_dir/vllm" origin "$fetch_depth" --filter=blob:none || return 1
  apply_build_recipe "$work_dir/vllm" benchmark-publisher benchmark-publisher@localhost || return 1

  merged_head=$(git -C "$work_dir/vllm" rev-parse HEAD) || return 1
  git -C "$work_dir/vllm" push origin "${merged_head}:refs/heads/${target_branch}" || return 1
  remote_head=$(git -C "$work_dir/vllm" ls-remote origin "refs/heads/${target_branch}" | awk '{print $1}')
  if [ "$remote_head" != "$merged_head" ]; then
    echo "FATAL: remote verification failed for ${target_branch}."
    return 1
  fi

  ref_file=${VLLM_BUILD_REF_FILE:-build-ref.env}
  printf 'VLLM_BUILD_REF=%s\nVLLM_BUILD_COMMIT=%s\n' "$target_branch" "$merged_head" \
    > "${ref_file}.tmp" && mv "${ref_file}.tmp" "$ref_file" || return 1

  echo "Published ${target_branch} at ${merged_head}"
}

publish_and_deploy() {
  local overlay namespace selector
  overlay=${VLLM_BUILD_OVERLAY:?FATAL: set VLLM_BUILD_OVERLAY to a Kustomize overlay path.}
  : "${VLLM_BUILD_REF_FILE:?FATAL: set VLLM_BUILD_REF_FILE to the overlay build-ref.env path.}"
  namespace=${NAMESPACE:-default}
  publish_build_ref "$@" || return 1
  kubectl --request-timeout=10m apply -n "$namespace" -k "$overlay" || return 1
  selector=${VLLM_BUILD_POD_SELECTOR:-}
  if [ -n "$selector" ]; then
    kubectl delete pods -n "$namespace" -l "$selector"
  fi
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  action=${1:-}
  if [ "$#" -gt 0 ]; then shift; fi
  case "$action" in
    publish) publish_build_ref "$@" ;;
    publish-and-deploy) publish_and_deploy "$@" ;;
    *) echo "Usage: $0 publish|publish-and-deploy" >&2; exit 2 ;;
  esac
  exit $?
fi

VLLM_BUILD_MODE=${VLLM_BUILD_MODE:-source}
case "$VLLM_BUILD_MODE" in
  nightly)
    # No branch recipe: leave the image's vLLM and its dependencies untouched.
    case "${DEEPEP_BUILD_ENABLED:-0}" in
      0) echo "Using vLLM from the runtime image."; return 0 ;;
      1)
        BASE_RUNTIME_IMAGE_ID=${VLLM_BUILD_BASE_IMAGE_ID:-unspecified}
        detect_base_runtime_abi
        elect_build_leader
        source "$(dirname "${BASH_SOURCE[0]}")/deepep-wheel-build.sh"
        return 0 ;;
      *) echo "FATAL: DEEPEP_BUILD_ENABLED must be 0 or 1."; exit 1 ;;
    esac ;;
  source) ;;
  *) echo "FATAL: VLLM_BUILD_MODE must be nightly or source."; exit 1 ;;
esac

BUILD_BRANCH=${VLLM_BUILD_REF:?FATAL: VLLM_BUILD_REF is not set; set a pinned build ref in the overlay first.}
BUILD_SHA=${VLLM_BUILD_COMMIT:?FATAL: VLLM_BUILD_COMMIT is not set; set a pinned build ref in the overlay first.}
if ! [[ "$BUILD_SHA" =~ ^[0-9a-f]{40}$ ]]; then
  echo "FATAL: VLLM_BUILD_COMMIT must be a full 40-character Git SHA."
  exit 1
fi
read -r -a RECIPE_BRANCHES <<< "${VLLM_BUILD_REFS:-$BUILD_BRANCH}"
read -r -a RECIPE_ACTIONS <<< "${VLLM_BUILD_ACTIONS:-checkout}"
read -r -a RECIPE_SHAS <<< "${VLLM_BUILD_SHAS:-$BUILD_SHA}"
validate_recipe_inputs
if [ "${RECIPE_SHAS[0]}" != "$BUILD_SHA" ] || [ "${RECIPE_BRANCHES[0]}" != "$BUILD_BRANCH" ]; then
  echo "FATAL: first build input differs from VLLM_BUILD_REF/COMMIT."
  exit 1
fi
BUILD_FETCH_DEPTH=${VLLM_BUILD_FETCH_DEPTH:-256}
validate_fetch_depth "$BUILD_FETCH_DEPTH"
recipe_fetch_branch_refs
BUILD_DESC=""
for i in "${!RECIPE_BRANCHES[@]}"; do
  BUILD_DESC+=" ${RECIPE_ACTIONS[$i]}:${RECIPE_BRANCHES[$i]}@${RECIPE_SHAS[$i]:0:12}"
done
echo "Using pinned build inputs:${BUILD_DESC}"

# The base vLLM image already provides a mutually compatible torch, torchvision,
# Triton, and CUDA stack. Re-resolving Humming's dependencies here can replace
# torch while leaving torchvision at the image version, which later fails with
# "RuntimeError: operator torchvision::nms does not exist". Install only the
# Humming package and preserve the image's pinned runtime stack.
uv pip install --system --force-reinstall --no-deps 'humming-kernels==0.1.13'
python3 -c 'import humming, torch, torchvision' || {
  echo "FATAL: Humming/PyTorch/torchvision import validation failed before build."
  exit 1
}

# Bump when the build RECIPE changes (flags, prunes) -- part of the
# cache key, so a bump forces a rebuild for the same branch SHAs.
BUILD_VARIANT=vllm-release4-prefill-leader-restart-safe-no-qutlass

# DeepEP v2's hybrid-mode SM estimator calls `ibstat` to discover RDMA
# bandwidth. If infiniband-diags is absent, DeepEP returns 0 GB/s and then
# divides by that value while comparing RDMA and NVLink time.
if ! command -v git >/dev/null 2>&1 || ! command -v ibstat >/dev/null 2>&1; then
  apt-get update -qq && \
    apt-get install -y -qq git infiniband-diags > /dev/null 2>&1 || {
      echo "FATAL: failed to install git/infiniband-diags."
      exit 1
    }
fi
command -v ibstat >/dev/null 2>&1 || {
  echo "FATAL: ibstat is unavailable after installing infiniband-diags."
  exit 1
}

# The wheel reuses native extensions from the base image, so the cache must be
# partitioned by that runtime too. This also prevents a floating/misconfigured
# image from consuming a wheel built against another torch/CUDA ABI.
BASE_RUNTIME_VLLM_VERSION=$(cd / && python3 -c 'import vllm; print(vllm.__version__)')
detect_base_runtime_abi
BASE_RUNTIME_IMAGE_ID=${VLLM_BUILD_BASE_IMAGE_ID:-unspecified}

# Cache identity includes branch names, actions, ordered SHAs, recipe, and the
# runtime ABI supplying the reused native extensions.
CACHE_HASH=$(
  {
    printf 'variant=%s\n' "$BUILD_VARIANT"
    printf 'actions=%s\n' "${VLLM_BUILD_ACTIONS:-checkout}"
    printf 'base_image=%s\n' "$BASE_RUNTIME_IMAGE_ID"
    printf 'base_vllm=%s\n' "$BASE_RUNTIME_VLLM_VERSION"
    printf 'torch=%s\n' "$BASE_RUNTIME_TORCH_VERSION"
    printf 'cuda=%s\n' "$BASE_RUNTIME_CUDA_VERSION"
    printf 'python_abi=%s\n' "$BASE_RUNTIME_PYTHON_ABI"
    for i in "${!RECIPE_BRANCHES[@]}"; do
      printf '%s=%s\n' "${RECIPE_BRANCHES[$i]}" "${RECIPE_SHAS[$i]}"
    done
  } | hash_cache_inputs
)

KEY="${CACHE_HASH}-${BUILD_VARIANT}"
CACHE=/shared/vllm-build/pr_build/${KEY}
WHEEL_DIR="${CACHE}/wheel"
READY_FILE="${CACHE}/READY"
LOCK_DIR="${CACHE}/build.lock"

# setuptools_scm would otherwise include the synthetic merge commit produced
# independently by each pod. Give every build for this cache key one stable
# version, which is also one of NIXL's compatibility-hash inputs.
VLLM_BUILD_VERSION="${BASE_RUNTIME_VLLM_VERSION%%+*}+llmd.${CACHE_HASH}"

echo "Building branch set: ${BUILD_DESC}"
echo "Build cache key: ${KEY}"
echo "Base runtime: image=${BASE_RUNTIME_IMAGE_ID} vllm=${BASE_RUNTIME_VLLM_VERSION} torch=${BASE_RUNTIME_TORCH_VERSION} cuda=${BASE_RUNTIME_CUDA_VERSION} python=${BASE_RUNTIME_PYTHON_ABI}"
echo "Deterministic wheel version: ${VLLM_BUILD_VERSION}"

command -v uv >/dev/null 2>&1 || python3 -m pip install -q uv

SITE=$(cd / && python3 -c "import sysconfig; print(sysconfig.get_paths()['purelib'])")
REUSE_DIRS="vllm_flash_attn third_party/deep_gemm third_party/flashmla"
REUSE_FILES="_flashmla_C.abi3.so _flashmla_extension_C.abi3.so"
rm -rf /tmp/base_reuse
mkdir -p /tmp/base_reuse
for d in $REUSE_DIRS; do
  if [ -d "${SITE}/vllm/${d}" ]; then
    mkdir -p "/tmp/base_reuse/$(dirname "$d")"
    cp -r "${SITE}/vllm/${d}" "/tmp/base_reuse/${d}"
  fi
done
for f in $REUSE_FILES; do
  if [ -f "${SITE}/vllm/${f}" ]; then
    cp "${SITE}/vllm/${f}" "/tmp/base_reuse/${f}"
  fi
done

ready_wheel_path() {
  [ -f "$READY_FILE" ] || return 1
  [ "$(read_ready_field "$READY_FILE" version)" = "$VLLM_BUILD_VERSION" ] || return 1
  ready_wheel_from_marker "$READY_FILE" "$WHEEL_DIR" 'vllm-*.whl'
}

validate_wheel_native_extensions() {
  local candidate=$1
  python3 - "$candidate" <<'PYEOF'
import sys
import zipfile

wheel = sys.argv[1]
required = {
    "vllm/_C_stable_libtorch.abi3.so",
    "vllm/_moe_C_stable_libtorch.abi3.so",
}
with zipfile.ZipFile(wheel) as archive:
    members = set(archive.namelist())

missing = sorted(required - members)
if missing:
    raise SystemExit(
        "FATAL: wheel is missing required native extension(s): "
        + ", ".join(missing)
    )
print("Validated wheel native extensions: " + ", ".join(sorted(required)))
PYEOF
}

validate_required_native_ops() {
  (
  cd / && python3 - <<'PYEOF'
import torch

try:
    import vllm._C_stable_libtorch  # noqa: F401
except Exception as exc:
    raise SystemExit(f"FATAL: failed to import vllm._C_stable_libtorch: {exc!r}")

try:
    import vllm._moe_C_stable_libtorch  # noqa: F401
except Exception as exc:
    raise SystemExit(f"FATAL: failed to import vllm._moe_C_stable_libtorch: {exc!r}")

moe_namespace = getattr(torch.ops, "_moe_C", None)
if moe_namespace is None or not hasattr(moe_namespace, "grouped_topk"):
    raise SystemExit(
        "FATAL: installed native vLLM extension does not register "
        "torch.ops._moe_C.grouped_topk"
    )

print("Validated native MoE op: torch.ops._moe_C.grouped_topk")
PYEOF
  )
}

install_ready_wheel() {
  local candidate
  candidate=$(ready_wheel_path) || return 1
  wheel_is_valid "$candidate" || return 1
  validate_wheel_native_extensions "$candidate" || return 1
  echo "Installing shared READY vLLM wheel for ${BUILD_DESC}: $(basename "$candidate")"
  uv pip install --system --force-reinstall --no-deps "$candidate" || return 1
  validate_required_native_ops || return 1
  INSTALLED_VLLM_VERSION=$(cd / && python3 -c 'import vllm; print(vllm.__version__)')
  if [ "$INSTALLED_VLLM_VERSION" != "$VLLM_BUILD_VERSION" ]; then
    echo "WARN: installed vLLM version ${INSTALLED_VLLM_VERSION} does not match READY version ${VLLM_BUILD_VERSION}."
    return 1
  fi
  WHEEL=$candidate
}

mkdir -p "$CACHE"
NEED_BUILD=1
if install_ready_wheel; then
  NEED_BUILD=0
fi

elect_build_leader
echo "vLLM build role=${VLLM_BUILD_ROLE:-unset} worker=${LWS_WORKER_INDEX:-unset} leader=${BUILD_LEADER}"

LOCK_TOKEN="${HOSTNAME:-unknown}.$$.$RANDOM"
LOCK_HEARTBEAT_PID=""
BUILD_LOCK_HELD=0

if [ "$NEED_BUILD" = 1 ]; then
  if wait_for_ready_wheel install_ready_wheel "vLLM"; then
    NEED_BUILD=0
  fi
fi

if [ "$NEED_BUILD" = 1 ]; then
  trap release_build_lock EXIT

  # A publisher may have completed between our initial lookup and lock
  # acquisition. Recheck while holding the lock before paying for a build.
  if install_ready_wheel; then
    NEED_BUILD=0
    release_build_lock
    trap - EXIT
  fi
fi

if [ "$NEED_BUILD" = 1 ]; then
  # We own the lock and are committed to rebuilding; hide any unusable stale
  # marker so no waiter repeatedly attempts it while this build runs.
  start_build_lock_heartbeat
  rm -f "$READY_FILE"
  write_lock_owner_file "$KEY"
  set -x
  apt-get update -qq && apt-get install -y -qq ccache mold > /dev/null 2>&1
  rm -rf /tmp/vllm-pr
  mkdir -p /tmp/vllm-pr && cd /tmp/vllm-pr || exit 1
  git init -q

  fetch_build_inputs /tmp/vllm-pr "$BUILD_REPO" "$BUILD_FETCH_DEPTH" || {
    echo "FATAL: pinned build fetch failed. Verify inputs remain available on ${BUILD_REPO}."
    exit 1
  }
  apply_build_recipe /tmp/vllm-pr vllm-build vllm-build@localhost

  MERGED_HEAD=$(git rev-parse HEAD)
  echo "Building ${BUILD_DESC} (merged HEAD ${MERGED_HEAD}); SLOW (~30 min)..."

  uv pip install --system -q setuptools setuptools_scm setuptools_rust ninja cmake || { echo "FATAL: build-deps install failed"; exit 1; }
  BASE_VLLM=$(cd / && python3 -c "import vllm, pathlib; print(pathlib.Path(vllm.__file__).parent)")
  cp -r "${BASE_VLLM}/vllm-rs" vllm/ 2>/dev/null || true
  cp "${BASE_VLLM}"/_rust_*.so vllm/ 2>/dev/null || true

  python3 - <<'PYEOF' || { echo "FATAL: setup.py prune failed (recipe drift?)"; exit 1; }
p = 'setup.py'
s = open(p).read()
s = s.replace(
    '    ext_modules.append(CMakeExtension(name="vllm.vllm_flash_attn._vllm_fa2_C"))\n',
    '')
s = s.replace(
    '        ext_modules.append(CMakeExtension(name="vllm.vllm_flash_attn._vllm_fa3_C"))\n',
    '        pass\n')
s = s.replace(
    '        ext_modules.append(CMakeExtension(name="vllm._deep_gemm_C", optional=True))\n',
    '')
s = s.replace(
    '        ext_modules.append(CMakeExtension(name="vllm._qutlass_C", optional=True))\n',
    '        pass\n')
s = s.replace(
    '        ext_modules.append(CMakeExtension(name="vllm._flashmla_C", optional=True))\n'
    '        ext_modules.append(\n'
    '            CMakeExtension(name="vllm._flashmla_extension_C", optional=True)\n'
    '        )\n',
    '        pass\n')
open(p, 'w').write(s)
assert '_vllm_fa2_C")' not in s and '_vllm_fa3_C")' not in s, 'FA prune failed'
assert '_deep_gemm_C"' not in s, 'DeepGEMM prune failed'
assert 'ext_modules.append(CMakeExtension(name="vllm._qutlass_C"' not in s, 'Qutlass prune failed'
assert 'name="vllm._flashmla_C"' not in s, 'FlashMLA prune failed'
print('Pruned FA2/FA3 + DeepGEMM + Qutlass + FlashMLA from setup.py ext_modules')
PYEOF

  # This deployment always selects --moe-backend humming. Marlin's generated
  # kernels are therefore dead weight, but dominate both compilation and the
  # final stable-libtorch links. Disable standard + MoE Marlin generation.
  # Keep moe_wna16.cu because torch_bindings.cpp always registers its
  # moe_wna16_gemm symbol, even when this deployment does not select that
  # backend. Remove only the generated marlin_moe_wna16 sources. Keep shared
  # MoE utilities such as grouped-topk and permutation kernels.
  python3 - <<'PYEOF' || { echo "FATAL: CMake source prune failed (recipe drift?)"; exit 1; }
import re

p = 'CMakeLists.txt'
s = open(p).read()
s, standard_gates = re.subn(
    r'(?m)^(\s*)cuda_archs_loose_intersection\(MARLIN_OTHER_ARCHS[^\n]*\)$',
    r'\1set(MARLIN_OTHER_ARCHS "") # llm-d Humming-only build',
    s,
)
s, moe_gates = re.subn(
    r'(?m)^(\s*)cuda_archs_loose_intersection\(MARLIN_MOE_OTHER_ARCHS[^\n]*\)$',
    r'\1set(MARLIN_MOE_OTHER_ARCHS "") # llm-d Humming-only build',
    s,
)
marker = 'message(STATUS "Enabling MoE C_stable extension.")'
assert standard_gates == 1, f'expected one standard Marlin gate, found {standard_gates}'
assert moe_gates == 1, f'expected one MoE Marlin gate, found {moe_gates}'
assert s.count(marker) == 1, 'MoE extension marker changed'
s = s.replace(
    marker,
    'list(FILTER VLLM_MOE_EXT_SRC EXCLUDE REGEX "marlin_moe_wna16")\n'
    + marker,
)
s = s.replace(
    '    include(cmake/external_projects/qutlass.cmake)\n',
    '')
open(p, 'w').write(s)
assert 'include(cmake/external_projects/qutlass.cmake)' not in s, 'Qutlass CMake prune failed'
print('Disabled standard/MoE Marlin generation, retained required moe_wna16 binding source, and pruned Qutlass')
PYEOF

  NVRTC_LIB=$(find /usr/local/cuda* -maxdepth 2 -path '*/lib64/libnvrtc.so' -print 2>/dev/null | sort | head -1)
  [ -n "$NVRTC_LIB" ] || NVRTC_LIB=$(find /usr/local/cuda* \
    /usr/local/lib/python3.12/dist-packages/nvidia \
    -name 'libnvrtc.so*' 2>/dev/null | sort | head -1)
  if [ -z "$NVRTC_LIB" ]; then
    echo "FATAL: could not locate libnvrtc for the CUDA build."
    exit 1
  fi
  echo "Using libnvrtc: $NVRTC_LIB"
  export CMAKE_ARGS="${CMAKE_ARGS:-} -DCUDA_nvrtc_LIBRARY=${NVRTC_LIB}"
  if command -v mold >/dev/null 2>&1; then
    export CMAKE_ARGS="${CMAKE_ARGS} -DCMAKE_LINKER_TYPE=MOLD"
  fi
  export CMAKE_BUILD_TYPE=Release
  export NVCC_THREADS=${NVCC_THREADS:-8}
  # No --use_fast_math: it approximates div/sqrt/rsqrt and flushes denormals,
  # unsafe to stack under the already-lossy MXFP4/block-FP8 quant path.
  export CUDAFLAGS="${CUDAFLAGS:-} -Xptxas -O3 --extra-device-vectorization"
  mkdir -p "$WHEEL_DIR" "${CACHE}/logs"
  LOCAL_WHEEL=/tmp/vllm-wheel-out
  rm -rf "$LOCAL_WHEEL" && mkdir -p "$LOCAL_WHEEL"
  BUILD_LOG="${CACHE}/logs/build-${HOSTNAME:-$(hostname)}.log"
  # Default to the CPU count visible inside the selected builder pod so this
  # remains correct if the common prefill/decode allocation changes later.
  if VLLM_TARGET_DEVICE=cuda \
       VLLM_VERSION_OVERRIDE="$VLLM_BUILD_VERSION" \
       TORCH_CUDA_ARCH_LIST=9.0 MAX_JOBS=${MAX_JOBS:-$(nproc)} \
       uv build --wheel --no-build-isolation -o "$LOCAL_WHEEL" . 2>&1 | tee "$BUILD_LOG"; then
    PUBLISHED_WHEEL=""
    for w in "$LOCAL_WHEEL"/vllm-*.whl; do
      [ -f "$w" ] || continue
      validate_wheel_native_extensions "$w" || exit 1
      PUBLISHED_WHEEL=$(publish_wheel_atomic "$w" "$WHEEL_DIR") || {
        echo "FATAL: built wheel is corrupt or could not be published: $w"
        exit 1
      }
    done
    [ -n "$PUBLISHED_WHEEL" ] || { echo "FATAL: build produced no vLLM wheel"; exit 1; }
    WHEEL=$PUBLISHED_WHEEL
    validate_wheel_native_extensions "$WHEEL" || { echo "FATAL: published wheel is missing native extensions"; exit 1; }
    uv pip install --system --force-reinstall --no-deps "$WHEEL" || { echo "FATAL: built wheel install failed"; exit 1; }
    validate_required_native_ops || { echo "FATAL: built wheel failed native MoE op validation"; exit 1; }
    INSTALLED_VLLM_VERSION=$(cd / && python3 -c 'import vllm; print(vllm.__version__)')
    if [ "$INSTALLED_VLLM_VERSION" != "$VLLM_BUILD_VERSION" ]; then
      echo "FATAL: installed vLLM version ${INSTALLED_VLLM_VERSION} does not match build version ${VLLM_BUILD_VERSION}."
      exit 1
    fi
    write_ready_file "$READY_FILE" \
      "wheel=$(basename "$WHEEL")" \
      "version=${VLLM_BUILD_VERSION}" \
      "merged_head=${MERGED_HEAD}" \
      "base_image=${BASE_RUNTIME_IMAGE_ID}" \
      "base_vllm=${BASE_RUNTIME_VLLM_VERSION}" \
      "torch=${BASE_RUNTIME_TORCH_VERSION}" \
      "cuda=${BASE_RUNTIME_CUDA_VERSION}" \
      "python_abi=${BASE_RUNTIME_PYTHON_ABI}"
    echo "Built + published vLLM ${BUILD_DESC} (${MERGED_HEAD}) -> ${WHEEL_DIR}"
  else
    echo "FATAL: vLLM ${BUILD_DESC} build failed; log at ${BUILD_LOG}; not publishing."
    exit 1
  fi
  set +x
  cd / || exit 1
  rm -rf /tmp/vllm-pr "$LOCAL_WHEEL"
  release_build_lock
  trap - EXIT
fi

for d in $REUSE_DIRS; do
  if [ -d "/tmp/base_reuse/${d}" ]; then
    rm -rf "${SITE}/vllm/${d}"
    mkdir -p "$(dirname "${SITE}/vllm/${d}")"
    cp -r "/tmp/base_reuse/${d}" "${SITE}/vllm/${d}"
    echo "Restored base-image ${d} into ${SITE}/vllm/${d}"
  else
    echo "WARN: no stashed base ${d} to restore (/tmp/base_reuse/${d} missing)"
  fi
done

for f in $REUSE_FILES; do
  if [ -f "/tmp/base_reuse/${f}" ]; then
    cp "/tmp/base_reuse/${f}" "${SITE}/vllm/${f}"
    echo "Restored base-image ${f} into ${SITE}/vllm/${f}"
  else
    echo "WARN: no stashed base ${f} to restore (/tmp/base_reuse/${f} missing)"
  fi
done


DEEPEP_BUILD_ENABLED=${DEEPEP_BUILD_ENABLED:-0}
case "$DEEPEP_BUILD_ENABLED" in
  0) ;;
  1) source "$(dirname "${BASH_SOURCE[0]}")/deepep-wheel-build.sh" ;;
  *) echo "FATAL: DEEPEP_BUILD_ENABLED must be 0 or 1."; exit 1 ;;
esac
# Bump the engine<->frontend startup handshake timeout (hardcoded
# 5 min in vllm/v1/engine/core.py -- no env/flag). Heavy multinode
# boot + slow W4A8 weight load can blow past 5 min before the
# front-end answers. Patch the installed file each boot.
python3 - <<'PYEOF' || { echo "FATAL: HANDSHAKE_TIMEOUT_MINS patch failed"; exit 1; }
import re
import pathlib
import vllm

p = pathlib.Path(vllm.__file__).parent / "v1/engine/core.py"
s = p.read_text()
pattern = r'HANDSHAKE_TIMEOUT_MINS = \d+'
assert re.search(pattern, s), "HANDSHAKE_TIMEOUT_MINS patch did not match"
s2 = re.sub(pattern, 'HANDSHAKE_TIMEOUT_MINS = 60', s)
if s2 != s:
    p.write_text(s2)
    print("Patched HANDSHAKE_TIMEOUT_MINS -> 60")
else:
    print("HANDSHAKE_TIMEOUT_MINS already 60")
PYEOF
