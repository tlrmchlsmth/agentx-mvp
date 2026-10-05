#!/usr/bin/env bash
# vLLM wheel build for benchmark campaign serving Pods and cache warmup Jobs.
# Source in the pinned vLLM runtime image; both paths use the same script and
# shared RWX cache. The build ref and full commit come from the overlay.
set -Eeuo pipefail
set -x

BUILD_REPO=${VLLM_BUILD_REPO:-https://github.com/elvircrn/vllm.git}

# Run this file directly on the laptop to turn the ordered, moving branch set
# into one durable benchmark integration branch. The `checkout` action below
# means only "start the integration commit here"; it is never the branch used
# by Kubernetes. The generated branch name is intentionally unique, so a later
# experiment cannot overwrite this benchmark's source history.
publish_build_ref() {
  local ref_file target_branch work_dir merged_head remote_head fetch_depth
  local i action branch
  local -a source_branches source_actions

  if [ "$#" -ne 0 ]; then
    echo "Usage: $0"
    return 1
  fi

  # These are the original Kimi build inputs. A caller may override both
  # ordered lists; `checkout` establishes the local merge base and the pushed
  # benchmark branch is always the final consolidated HEAD.
  # PR #58372: populate completion_tokens_details.reasoning_tokens for Kimi K3.
  read -r -a source_actions <<< "${VLLM_BUILD_ACTIONS:-checkout cherry-pick-m2 cherry-pick cherry-pick-m2 cherry-pick-m2 cherry-pick cherry-pick cherry-pick cherry-pick}"
  read -r -a source_branches <<< "${VLLM_BUILD_REFS:-deepep-triton-epilogue fused-globalize-align-cuda kimi-k3-shard-sp-deepep-v2 humming-0.1.13-no-tuning-hacks fix/flashinfer-h200-backend vllm-profiling-qol dcp_fusion_triton nccl_cg codex/kimi-k3-reasoning-token-count}"
  if [ "${#source_branches[@]}" -eq 0 ] || [ "${#source_actions[@]}" -ne "${#source_branches[@]}" ] || \
     [ "${source_actions[0]}" != "checkout" ]; then
    echo "FATAL: BUILD_BRANCHES and BUILD_BRANCH_ACTIONS must have matching lengths and begin with checkout."
    return 1
  fi
  fetch_depth=${VLLM_BUILD_FETCH_DEPTH:-256}
  if ! [[ "$fetch_depth" =~ ^[1-9][0-9]*$ ]]; then
    echo "FATAL: VLLM_BUILD_FETCH_DEPTH must be a positive integer."
    return 1
  fi

  target_branch="benchmark/kimi-k3-$(date -u +%Y%m%dT%H%M%SZ)-$$"
  if git ls-remote --exit-code --heads "$BUILD_REPO" "refs/heads/${target_branch}" >/dev/null 2>&1; then
    echo "FATAL: generated benchmark branch unexpectedly already exists: ${target_branch}"
    return 1
  fi

  work_dir=$(mktemp -d "${TMPDIR:-/tmp}/kimi-k3-consolidate.XXXXXX") || return 1
  trap 'rm -rf -- "$work_dir"; trap - RETURN' RETURN
  # This temporary repository only creates and pushes an integration commit.
  # Keep it sparse (root files only), shallow, and blob-filtered; it never
  # compiles vLLM. The pod build intentionally uses a full checkout instead.
  git clone -q --no-checkout --sparse --filter=blob:none --depth="$fetch_depth" \
    "$BUILD_REPO" "$work_dir/vllm" || return 1
  git -C "$work_dir/vllm" remote add elvircrn "$BUILD_REPO" || return 1

  # Resolve and fetch all inputs before merging. Remote-tracking refs preserve
  # exactly the branch/action order requested by the experimenter.
  for i in "${!source_branches[@]}"; do
    branch=${source_branches[$i]}
    git -C "$work_dir/vllm" fetch -q --no-tags --filter=blob:none --depth="$fetch_depth" elvircrn \
      "refs/heads/${branch}:refs/remotes/benchmark-input/${i}" || {
      echo "FATAL: could not fetch elvircrn/${branch}."
      return 1
    }
  done
  git -C "$work_dir/vllm" checkout -q --detach refs/remotes/benchmark-input/0 || return 1

  for ((i = 1; i < ${#source_branches[@]}; i++)); do
    action=${source_actions[$i]}
    branch=${source_branches[$i]}
    case "$action" in
      merge)
        git -C "$work_dir/vllm" -c user.name=benchmark-publisher \
          -c user.email=benchmark-publisher@localhost merge --no-edit "refs/remotes/benchmark-input/${i}" || return 1
        ;;
      cherry-pick|cherry-pick-m*)
        if [ "$action" = "cherry-pick" ]; then
          git -C "$work_dir/vllm" cherry-pick "refs/remotes/benchmark-input/${i}" || return 1
        else
          if ! [[ "${action#cherry-pick-m}" =~ ^[1-9][0-9]*$ ]]; then
            echo "FATAL: invalid mainline action ${action}."
            return 1
          fi
          git -C "$work_dir/vllm" cherry-pick -m "${action#cherry-pick-m}" "refs/remotes/benchmark-input/${i}" || return 1
        fi
        ;;
      cherry-pick-parent1)
        # A feature branch may be refreshed by merging main into it. Its first
        # parent remains the feature tip; cherry-pick that commit, not the
        # merge delta, which would replay unrelated upstream changes.
        git -C "$work_dir/vllm" cherry-pick "refs/remotes/benchmark-input/${i}^1" || return 1
        ;;
      *)
        echo "FATAL: unsupported action ${action} for ${branch}."
        return 1
        ;;
    esac
  done

  merged_head=$(git -C "$work_dir/vllm" rev-parse HEAD) || return 1
  git -C "$work_dir/vllm" push elvircrn "${merged_head}:refs/heads/${target_branch}" || return 1
  remote_head=$(git -C "$work_dir/vllm" ls-remote elvircrn "refs/heads/${target_branch}" | awk '{print $1}')
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

BUILD_BRANCH=${VLLM_BUILD_REF:?FATAL: VLLM_BUILD_REF is not set; set a pinned build ref in the overlay first.}
BUILD_SHA=${VLLM_BUILD_COMMIT:?FATAL: VLLM_BUILD_COMMIT is not set; set a pinned build ref in the overlay first.}
if ! [[ "$BUILD_SHA" =~ ^[0-9a-f]{40}$ ]]; then
  echo "FATAL: VLLM_BUILD_COMMIT must be a full 40-character Git SHA."
  exit 1
fi
BUILD_BRANCHES=${VLLM_BUILD_REFS:-$BUILD_BRANCH}
BUILD_BRANCH_ACTIONS=${VLLM_BUILD_ACTIONS:-checkout}
BUILD_SHA_VALUES=${VLLM_BUILD_SHAS:-$BUILD_SHA}


BUILD_FETCH_DEPTH=${VLLM_BUILD_FETCH_DEPTH:-256}

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

read -r -a BUILD_BRANCH_ARRAY <<< "$BUILD_BRANCHES"
if [ "${#BUILD_BRANCH_ARRAY[@]}" -eq 0 ]; then
  echo "FATAL: BUILD_BRANCHES is empty."
  exit 1
fi
read -r -a BUILD_BRANCH_ACTION_ARRAY <<< "$BUILD_BRANCH_ACTIONS"
if [ "${#BUILD_BRANCH_ACTION_ARRAY[@]}" -ne "${#BUILD_BRANCH_ARRAY[@]}" ]; then
  echo "FATAL: BUILD_BRANCH_ACTIONS must match BUILD_BRANCHES length."
  exit 1
fi
if [ "${BUILD_BRANCH_ACTION_ARRAY[0]}" != "checkout" ]; then
  echo "FATAL: the first BUILD_BRANCH_ACTIONS entry must be checkout."
  exit 1
fi
read -r -a BUILD_SHA_ARRAY <<< "$BUILD_SHA_VALUES"
if [ "${#BUILD_SHA_ARRAY[@]}" -ne "${#BUILD_BRANCH_ARRAY[@]}" ]; then
  echo "FATAL: VLLM_BUILD_SHAS must match VLLM_BUILD_REFS length."
  exit 1
fi
for i in "${!BUILD_BRANCH_ARRAY[@]}"; do
  if ! [[ "${BUILD_SHA_ARRAY[$i]}" =~ ^[0-9a-f]{40}$ ]]; then
    echo "FATAL: build input $i has no full Git SHA."
    exit 1
  fi
  if ! [[ "${BUILD_BRANCH_ARRAY[$i]}" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$ ]]; then
    echo "FATAL: build input $i has an invalid branch name."
    exit 1
  fi
  if [ "$i" -gt 0 ] && ! [[ "${BUILD_BRANCH_ACTION_ARRAY[$i]}" =~ ^(merge|cherry-pick|cherry-pick-parent1|cherry-pick-m[1-9][0-9]*)$ ]]; then
    echo "FATAL: unsupported build action ${BUILD_BRANCH_ACTION_ARRAY[$i]}."
    exit 1
  fi
done
if [ "${BUILD_SHA_ARRAY[0]}" != "$BUILD_SHA" ] || [ "${BUILD_BRANCH_ARRAY[0]}" != "$BUILD_BRANCH" ]; then
  echo "FATAL: first build input differs from VLLM_BUILD_REF/COMMIT."
  exit 1
fi
if ! [[ "$BUILD_FETCH_DEPTH" =~ ^[1-9][0-9]*$ ]]; then
  echo "FATAL: VLLM_BUILD_FETCH_DEPTH must be a positive integer."
  exit 1
fi

# A campaign recipe fetches each branch and verifies it still points to the
# resolved SHA. A single pinned ref can still fetch its immutable object ID.
BUILD_REMOTE_REF_ARRAY=()
BUILD_DESC=""
for i in "${!BUILD_BRANCH_ARRAY[@]}"; do
  if [ -n "${VLLM_BUILD_REFS:-}" ]; then
    BUILD_REMOTE_REF_ARRAY+=("refs/heads/${BUILD_BRANCH_ARRAY[$i]}")
  else
    BUILD_REMOTE_REF_ARRAY+=("${BUILD_SHA_ARRAY[$i]}")
  fi
  BUILD_DESC+=" ${BUILD_BRANCH_ACTION_ARRAY[$i]}:${BUILD_BRANCH_ARRAY[$i]}@${BUILD_SHA_ARRAY[$i]:0:12}"
done
echo "Using pinned build inputs:${BUILD_DESC}"

# The wheel reuses native extensions from the base image, so the cache must be
# partitioned by that runtime too. This also prevents a floating/misconfigured
# image from consuming a wheel built against another torch/CUDA ABI.
BASE_RUNTIME_VLLM_VERSION=$(cd / && python3 -c 'import vllm; print(vllm.__version__)')
BASE_RUNTIME_TORCH_VERSION=$(cd / && python3 -c 'import torch; print(torch.__version__)')
BASE_RUNTIME_CUDA_VERSION=$(cd / && python3 -c 'import torch; print(torch.version.cuda or "none")')
BASE_RUNTIME_PYTHON_ABI=$(python3 -c 'import sysconfig; print(sysconfig.get_config_var("SOABI") or "unknown")')
BASE_RUNTIME_IMAGE_ID=${VLLM_BUILD_BASE_IMAGE_ID:-unspecified}

# Cache identity includes branch names, actions, ordered SHAs, recipe, and the
# runtime ABI supplying the reused native extensions.
CACHE_HASH=$(
  {
    printf 'variant=%s\n' "$BUILD_VARIANT"
    printf 'actions=%s\n' "$BUILD_BRANCH_ACTIONS"
    printf 'base_image=%s\n' "$BASE_RUNTIME_IMAGE_ID"
    printf 'base_vllm=%s\n' "$BASE_RUNTIME_VLLM_VERSION"
    printf 'torch=%s\n' "$BASE_RUNTIME_TORCH_VERSION"
    printf 'cuda=%s\n' "$BASE_RUNTIME_CUDA_VERSION"
    printf 'python_abi=%s\n' "$BASE_RUNTIME_PYTHON_ABI"
    for i in "${!BUILD_BRANCH_ARRAY[@]}"; do
      printf '%s=%s\n' "${BUILD_BRANCH_ARRAY[$i]}" "${BUILD_SHA_ARRAY[$i]}"
    done
  } | sha256sum | cut -c1-20
)

KEY="${CACHE_HASH}-${BUILD_VARIANT}"
CACHE=/shared/vllm-build/pr_build/${KEY}
WHEEL_DIR="${CACHE}/wheel"
READY_FILE="${CACHE}/READY"
LOCK_DIR="${CACHE}/build.lock"

# setuptools_scm would otherwise include the synthetic merge commit produced
# independently by each pod. Give every build for this cache key one stable
# version, which is also one of NIXL's compatibility-hash inputs.
BASE_VLLM_PUBLIC_VERSION=${BASE_RUNTIME_VLLM_VERSION%%+*}
VLLM_BUILD_VERSION="${BASE_VLLM_PUBLIC_VERSION}+llmd.${CACHE_HASH}"

echo "Building branch set: ${BUILD_DESC}"
echo "Build cache key: ${KEY}"
echo "Base runtime: image=${BASE_RUNTIME_IMAGE_ID} vllm=${BASE_RUNTIME_VLLM_VERSION} torch=${BASE_RUNTIME_TORCH_VERSION} cuda=${BASE_RUNTIME_CUDA_VERSION} python=${BASE_RUNTIME_PYTHON_ABI}"
echo "Deterministic wheel version: ${VLLM_BUILD_VERSION}"

command -v uv >/dev/null 2>&1 || python3 -m pip install -q uv

SITE=$(cd / && python3 -c "import sysconfig; print(sysconfig.get_paths()['purelib'])")
REUSE_DIRS="vllm_flash_attn third_party/deep_gemm third_party/flashmla"
REUSE_FILES="_flashmla_C.abi3.so _flashmla_extension_C.abi3.so"
rm -rf /tmp/base_reuse
for d in $REUSE_DIRS; do
  if [ -d "${SITE}/vllm/${d}" ]; then
    mkdir -p "/tmp/base_reuse/$(dirname "$d")"
    cp -r "${SITE}/vllm/${d}" "/tmp/base_reuse/${d}"
  fi
done
mkdir -p /tmp/base_reuse
for f in $REUSE_FILES; do
  [ -f "${SITE}/vllm/${f}" ] && cp "${SITE}/vllm/${f}" "/tmp/base_reuse/${f}"
done

ready_wheel_path() {
  local ready_version ready_wheel
  [ -f "$READY_FILE" ] || return 1
  ready_wheel=$(awk -F= '$1 == "wheel" { print $2; exit }' "$READY_FILE")
  ready_version=$(awk -F= '$1 == "version" { print $2; exit }' "$READY_FILE")
  [ "$ready_version" = "$VLLM_BUILD_VERSION" ] || return 1
  case "$ready_wheel" in
    vllm-*.whl) ;;
    *) return 1 ;;
  esac
  [ "$ready_wheel" = "$(basename "$ready_wheel")" ] || return 1
  [ -f "${WHEEL_DIR}/${ready_wheel}" ] || return 1
  printf '%s\n' "${WHEEL_DIR}/${ready_wheel}"
}

wheel_is_valid() {
  local candidate=$1
  python3 -c "import zipfile,sys; p=sys.argv[1]; sys.exit(0 if zipfile.is_zipfile(p) and zipfile.ZipFile(p).testzip() is None else 1)" "$candidate" 2>/dev/null
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

BUILD_LEADER=0
if [ "${VLLM_BUILD_ROLE:-}" = "prefill" ] && [ "${LWS_WORKER_INDEX:-}" = "0" ]; then
  BUILD_LEADER=1
fi
echo "vLLM build role=${VLLM_BUILD_ROLE:-unset} worker=${LWS_WORKER_INDEX:-unset} leader=${BUILD_LEADER}"

BUILD_LOCK_HELD=0
LOCK_TOKEN="${HOSTNAME:-unknown}.$$.$RANDOM"
LOCK_HEARTBEAT_PID=""

claim_build_lock() {
  mkdir "$LOCK_DIR" 2>/dev/null || return 1
  printf '%s\n' "$LOCK_TOKEN" > "${LOCK_DIR}/token"
  touch "${LOCK_DIR}/heartbeat"
  BUILD_LOCK_HELD=1
}

start_build_lock_heartbeat() {
  (
    while [ "$(sed -n '1p' "${LOCK_DIR}/token" 2>/dev/null || true)" = "$LOCK_TOKEN" ]; do
      touch "${LOCK_DIR}/heartbeat" || exit 0
      sleep 30
    done
  ) &
  LOCK_HEARTBEAT_PID=$!
}

release_build_lock() {
  if [ "$BUILD_LOCK_HELD" = 1 ]; then
    if [ -n "$LOCK_HEARTBEAT_PID" ]; then
      kill "$LOCK_HEARTBEAT_PID" 2>/dev/null || true
      wait "$LOCK_HEARTBEAT_PID" 2>/dev/null || true
      LOCK_HEARTBEAT_PID=""
    fi
    # A stale-lock recovery may have replaced our lock. Never remove a lock
    # unless it still carries this process's ownership token.
    CURRENT_LOCK_TOKEN=$(sed -n '1p' "${LOCK_DIR}/token" 2>/dev/null || true)
    if [ "$CURRENT_LOCK_TOKEN" = "$LOCK_TOKEN" ]; then
      rm -rf "$LOCK_DIR"
    fi
    BUILD_LOCK_HELD=0
  fi
}

recover_restarted_leader_lock() {
  local owner_host owner_pid previous_token stale_lock
  [ "$BUILD_LEADER" = 1 ] || return 1
  [ -d "$LOCK_DIR" ] || return 1

  owner_host=$(awk -F= '$1 == "host" { print $2; exit }' "${LOCK_DIR}/owner" 2>/dev/null || true)
  owner_pid=$(awk -F= '$1 == "pid" { print $2; exit }' "${LOCK_DIR}/owner" 2>/dev/null || true)
  previous_token=$(sed -n '1p' "${LOCK_DIR}/token" 2>/dev/null || true)

  # Kubernetes may restart the container in the same pod, reusing both its
  # hostname and PID 1. The random token distinguishes that dead incarnation
  # from this process. Two live PID-1 processes cannot coexist in one pod.
  if [ "$owner_host" = "${HOSTNAME:-unknown}" ] && \
     [ "$owner_pid" = "$$" ] && \
     [ -n "$previous_token" ] && \
     [ "$previous_token" != "$LOCK_TOKEN" ]; then
    stale_lock="${LOCK_DIR}.restarted.${HOSTNAME:-unknown}.$$.$RANDOM"
    if mv "$LOCK_DIR" "$stale_lock" 2>/dev/null; then
      echo "Recovered build lock left by a previous container incarnation of ${HOSTNAME:-unknown}."
      rm -rf "$stale_lock"
      return 0
    fi
  fi
  return 1
}

if [ "$NEED_BUILD" = 1 ] && [ "$BUILD_LEADER" != 1 ]; then
  LOCK_WAIT_SECONDS=${VLLM_BUILD_LOCK_WAIT_SECONDS:-10800}
  LOCK_POLL_SECONDS=${VLLM_BUILD_LOCK_POLL_SECONDS:-10}
  WAITED_SECONDS=0
  echo "Non-leader pod waiting up to ${LOCK_WAIT_SECONDS}s for the prefill-0 READY wheel."

  while [ "$WAITED_SECONDS" -lt "$LOCK_WAIT_SECONDS" ]; do
    if install_ready_wheel; then
      NEED_BUILD=0
      break
    fi
    sleep "$LOCK_POLL_SECONDS"
    WAITED_SECONDS=$((WAITED_SECONDS + LOCK_POLL_SECONDS))
  done

  if [ "$NEED_BUILD" = 1 ]; then
    echo "FATAL: timed out waiting for prefill-0 vLLM build after ${WAITED_SECONDS}s."
    exit 1
  fi
fi

if [ "$NEED_BUILD" = 1 ] && [ "$BUILD_LEADER" = 1 ]; then
  recover_restarted_leader_lock || true
  if claim_build_lock; then
    :
  else
    LOCK_WAIT_SECONDS=${VLLM_BUILD_LOCK_WAIT_SECONDS:-10800}
    LOCK_STALE_SECONDS=${VLLM_BUILD_LOCK_STALE_SECONDS:-600}
    LOCK_POLL_SECONDS=${VLLM_BUILD_LOCK_POLL_SECONDS:-10}
    WAITED_SECONDS=0
    echo "Another pod owns ${LOCK_DIR}; waiting up to ${LOCK_WAIT_SECONDS}s for its READY wheel."

    while [ "$WAITED_SECONDS" -lt "$LOCK_WAIT_SECONDS" ]; do
      if install_ready_wheel; then
        NEED_BUILD=0
        break
      fi

      if [ -d "$LOCK_DIR" ]; then
        LOCK_MTIME=$(stat -c %Y "${LOCK_DIR}/heartbeat" 2>/dev/null || stat -c %Y "$LOCK_DIR" 2>/dev/null || echo 0)
        NOW_EPOCH=$(date +%s)
        LOCK_AGE=$((NOW_EPOCH - LOCK_MTIME))
        if [ "$LOCK_MTIME" -gt 0 ] && [ "$LOCK_AGE" -ge "$LOCK_STALE_SECONDS" ]; then
          STALE_LOCK="${LOCK_DIR}.stale.${HOSTNAME:-unknown}.$$"
          if mv "$LOCK_DIR" "$STALE_LOCK" 2>/dev/null; then
            echo "Recovered stale build lock aged ${LOCK_AGE}s."
            rm -rf "$STALE_LOCK"
            if claim_build_lock; then
              break
            fi
          fi
        fi
      elif claim_build_lock; then
        break
      fi

      sleep "$LOCK_POLL_SECONDS"
      WAITED_SECONDS=$((WAITED_SECONDS + LOCK_POLL_SECONDS))
    done

    if [ "$NEED_BUILD" = 1 ] && [ "$BUILD_LOCK_HELD" != 1 ]; then
      echo "FATAL: timed out waiting for shared vLLM build after ${WAITED_SECONDS}s."
      exit 1
    fi
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
  printf 'host=%s\npid=%s\ntoken=%s\nstarted=%s\nkey=%s\n' \
    "${HOSTNAME:-unknown}" "$$" "$LOCK_TOKEN" \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$KEY" \
    > "${LOCK_DIR}/owner"
  apt-get update -qq && apt-get install -y -qq ccache mold > /dev/null 2>&1
  rm -rf /tmp/vllm-pr
  mkdir -p /tmp/vllm-pr && cd /tmp/vllm-pr || exit 1
  git init -q

  # Fetch all resolved refs and verify that none moved after resolution.
  BUILD_FETCH_REFS=()
  for i in "${!BUILD_BRANCH_ARRAY[@]}"; do
    BUILD_FETCH_REFS+=("${BUILD_REMOTE_REF_ARRAY[$i]}:refs/remotes/llmd-build/${i}")
  done
  echo "Fetching pinned build inputs (depth=${BUILD_FETCH_DEPTH})..."
  git fetch -q --no-tags --depth="$BUILD_FETCH_DEPTH" \
    "$BUILD_REPO" "${BUILD_FETCH_REFS[@]}" || {
    echo "FATAL: pinned build fetch failed. Verify inputs remain available on ${BUILD_REPO}."
    exit 1
  }

  for i in "${!BUILD_BRANCH_ARRAY[@]}"; do
    FETCHED_SHA=$(git rev-parse "refs/remotes/llmd-build/${i}")
    if [ "$FETCHED_SHA" != "${BUILD_SHA_ARRAY[$i]}" ]; then
      echo "FATAL: ${BUILD_BRANCH_ARRAY[$i]} moved while preparing build; refusing stale cache key."
      exit 1
    fi
  done

  BASE_BRANCH="${BUILD_BRANCH_ARRAY[0]}"
  BASE_SHA="${BUILD_SHA_ARRAY[0]}"

  echo "Checking out base ${BASE_BRANCH} (${BASE_SHA})..."
  git checkout -q --detach refs/remotes/llmd-build/0 || {
    echo "FATAL: checkout ${BASE_BRANCH} failed"
    exit 1
  }

  # Make sure the fetched tip is exactly the SHA used in the cache key.
  if [ "$(git rev-parse HEAD)" != "$BASE_SHA" ]; then
    echo "FATAL: ${BASE_BRANCH} moved while preparing build; refusing stale cache key."
    exit 1
  fi

  for ((i = 1; i < ${#BUILD_BRANCH_ARRAY[@]}; i++)); do
    MERGE_BRANCH="${BUILD_BRANCH_ARRAY[$i]}"
    MERGE_SHA="${BUILD_SHA_ARRAY[$i]}"
    BRANCH_ACTION="${BUILD_BRANCH_ACTION_ARRAY[$i]}"
    MERGE_REF="refs/remotes/llmd-build/${i}"

    if [[ "$BRANCH_ACTION" == cherry-pick* ]]; then
      CHERRY_PICK_MAINLINE=()
      CHERRY_PICK_TARGET=$MERGE_SHA
      if [[ "$BRANCH_ACTION" =~ ^cherry-pick-m([1-9][0-9]*)$ ]]; then
        CHERRY_PICK_MAINLINE=(-m "${BASH_REMATCH[1]}")
      elif [ "$BRANCH_ACTION" = "cherry-pick-parent1" ]; then
        CHERRY_PICK_TARGET=$(git rev-parse "${MERGE_SHA}^1") || {
          echo "FATAL: first parent of ${MERGE_SHA} is unavailable."
          exit 1
        }
      elif [ "$BRANCH_ACTION" != "cherry-pick" ]; then
        echo "FATAL: invalid cherry-pick action ${BRANCH_ACTION} for ${MERGE_BRANCH}."
        exit 1
      fi
      if git merge-base --is-ancestor "${CHERRY_PICK_TARGET}" HEAD; then
        echo "Patch ${MERGE_BRANCH} (${MERGE_SHA}) is already in the build."
        continue
      fi

      echo "Applying ${MERGE_BRANCH} tip (${MERGE_SHA})..."
      git cherry-pick "${CHERRY_PICK_MAINLINE[@]}" --no-commit "${CHERRY_PICK_TARGET}" || {
        echo "FATAL: cherry-pick ${MERGE_BRANCH} failed"
        git status || true
        exit 1
      }

      if git diff --cached --quiet; then
        echo "Patch ${MERGE_BRANCH} is already present with equivalent changes."
      else
        git \
          -c user.name=vllm-build \
          -c user.email=vllm-build@localhost \
          commit --no-gpg-sign --no-verify -C "${CHERRY_PICK_TARGET}" || {
            echo "FATAL: commit ${MERGE_BRANCH} failed"
            git status || true
            exit 1
          }
      fi
    elif [ "$BRANCH_ACTION" = "merge" ]; then
      echo "Merging ${MERGE_BRANCH} (${MERGE_SHA})..."
      git \
        -c user.name=vllm-build \
        -c user.email=vllm-build@localhost \
        merge --no-edit "$MERGE_REF" || {
          echo "FATAL: merge ${MERGE_BRANCH} failed"
          git status || true
          exit 1
        }
    else
      echo "FATAL: unsupported action ${BRANCH_ACTION} for ${MERGE_BRANCH}."
      exit 1
    fi
  done

  MERGED_HEAD=$(git rev-parse HEAD)
  echo "Building ${BUILD_DESC} (merged HEAD ${MERGED_HEAD}); SLOW (~30 min)..."

  uv pip install --system -q setuptools wheel setuptools_scm setuptools_rust ninja cmake || { echo "FATAL: build-deps install failed"; exit 1; }
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
  set -o pipefail
  # Default to the CPU count visible inside the selected builder pod so this
  # remains correct if the common prefill/decode allocation changes later.
  if VLLM_TARGET_DEVICE=cuda \
       VLLM_VERSION_OVERRIDE="$VLLM_BUILD_VERSION" \
       TORCH_CUDA_ARCH_LIST=9.0 MAX_JOBS=${MAX_JOBS:-$(nproc)} \
       uv build --wheel --no-build-isolation -o "$LOCAL_WHEEL" . 2>&1 | tee "$BUILD_LOG"; then
    PUBLISHED_WHEEL=""
    rm -f "$READY_FILE"
    find "$WHEEL_DIR" -maxdepth 1 -type f -name 'vllm-*.whl' -delete
    for w in "$LOCAL_WHEEL"/vllm-*.whl; do
      [ -f "$w" ] || continue
      wheel_is_valid "$w" || { echo "FATAL: built wheel is corrupt: $w"; exit 1; }
      validate_wheel_native_extensions "$w" || exit 1
      bn=$(basename "$w")
      PUBLISH_TMP="${WHEEL_DIR}/.${bn}.$$.tmp"
      cp "$w" "$PUBLISH_TMP"
      wheel_is_valid "$PUBLISH_TMP" || { echo "FATAL: copied wheel is corrupt: $PUBLISH_TMP"; exit 1; }
      mv -f "$PUBLISH_TMP" "${WHEEL_DIR}/${bn}"
      PUBLISHED_WHEEL="${WHEEL_DIR}/${bn}"
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
    READY_TMP="${READY_FILE}.$$.tmp"
    printf 'wheel=%s\nversion=%s\nmerged_head=%s\nbase_image=%s\nbase_vllm=%s\ntorch=%s\ncuda=%s\npython_abi=%s\n' \
      "$(basename "$WHEEL")" "$VLLM_BUILD_VERSION" "$MERGED_HEAD" \
      "$BASE_RUNTIME_IMAGE_ID" "$BASE_RUNTIME_VLLM_VERSION" \
      "$BASE_RUNTIME_TORCH_VERSION" "$BASE_RUNTIME_CUDA_VERSION" \
      "$BASE_RUNTIME_PYTHON_ABI" > "$READY_TMP"
    mv -f "$READY_TMP" "$READY_FILE"
    echo "Built + published vLLM ${BUILD_DESC} (${MERGED_HEAD}) -> ${WHEEL_DIR}"
  else
    echo "FATAL: vLLM ${BUILD_DESC} build failed; log at ${BUILD_LOG}; not publishing."
    exit 1
  fi
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
