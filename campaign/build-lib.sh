#!/usr/bin/env bash
# Shared build machinery for the campaign wheel build scripts
# (vllm-wheel-build.sh, vllm-source-build.sh, deepep-wheel-build.sh).
# Sourced, never executed: no top-level side effects. Also written for the
# macOS system Bash 3.2: indexed arrays only, and no empty-array expansion.

# --- wheel validation and atomic publication ---------------------------------

wheel_is_valid() {
  python3 -c 'import sys,zipfile; p=sys.argv[1]; sys.exit(0 if zipfile.is_zipfile(p) and zipfile.ZipFile(p).testzip() is None else 1)' "$1" 2>/dev/null
}

# Copies a built wheel into a shared cache directory so waiters can only ever
# observe a fully written file: copy to a dot-prefixed temporary, validate the
# copy, then rename into place. Prints the published wheel path.
publish_wheel_atomic() {
  local wheel=$1 wheel_dir=$2 name temp
  wheel_is_valid "$wheel" || return 1
  name=$(basename "$wheel")
  temp="${wheel_dir}/.${name}.$$.tmp"
  cp "$wheel" "$temp" || return 1
  wheel_is_valid "$temp" || return 1
  mv -f "$temp" "${wheel_dir}/${name}" || return 1
  printf '%s\n' "${wheel_dir}/${name}"
}

read_ready_field() {
  # $1: READY marker file, $2: key. Prints the value of its first key=value line.
  awk -F= -v key="$2" '$1 == key { print $2; exit }' "$1"
}

write_ready_file() {
  # $1: READY marker file; the remaining args are key=value lines. Published by
  # rename so a waiter never reads a partially written marker.
  local ready_file=$1 temp
  shift
  temp="${ready_file}.$$.tmp"
  printf '%s\n' "$@" > "$temp" && mv -f "$temp" "$ready_file"
}

# Validates that a READY marker names an existing wheel in $2 matching the
# shell glob $3 (for example 'vllm-*.whl'). Prints the wheel path.
ready_wheel_from_marker() {
  local ready_file=$1 wheel_dir=$2 glob=$3 wheel
  [ -f "$ready_file" ] || return 1
  wheel=$(read_ready_field "$ready_file" wheel)
  case "$wheel" in $glob) ;; *) return 1 ;; esac
  [ "$wheel" = "$(basename "$wheel")" ] || return 1
  [ -f "${wheel_dir}/${wheel}" ] || return 1
  printf '%s\n' "${wheel_dir}/${wheel}"
}

# Reads key=value identity lines from stdin and prints a 20-hex digest.
hash_cache_inputs() { sha256sum | cut -c1-20; }

# --- base runtime ABI and leader election -------------------------------------

# Detects the ABI of the base runtime image's prebuilt native stack. Built
# wheels reuse those extensions, so cache keys are partitioned by these values.
detect_base_runtime_abi() {
  BASE_RUNTIME_TORCH_VERSION=$(cd / && python3 -c 'import torch; print(torch.__version__)')
  BASE_RUNTIME_CUDA_VERSION=$(cd / && python3 -c 'import torch; print(torch.version.cuda or "none")')
  BASE_RUNTIME_PYTHON_ABI=$(python3 -c 'import sysconfig; print(sysconfig.get_config_var("SOABI") or "unknown")')
}

# Prefill-0 builds; every other pod waits for its READY wheel. A prebuild Job
# also elects itself so a source build can run without serving pods.
elect_build_leader() {
  BUILD_LEADER=0
  if [ "${VLLM_BUILD_PREBUILD:-0}" = 1 ] || { [ "${VLLM_BUILD_ROLE:-}" = prefill ] && [ "${LWS_WORKER_INDEX:-}" = 0 ]; }; then
    BUILD_LEADER=1
  fi
}

# --- build lock ----------------------------------------------------------------
# The lock is a directory on the shared cache carrying an ownership token and
# a heartbeat file. Callers set LOCK_DIR, LOCK_TOKEN, LOCK_HEARTBEAT_PID, and
# BUILD_LOCK_HELD first; every cache owns its own lock path.

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

# Kubernetes may restart a container in the same pod, reusing both its hostname
# and PID 1. The random token distinguishes that dead incarnation from this
# process. Returns 0 only when a lock was actually recovered.
recover_restarted_leader_lock() {
  local owner_host owner_pid previous_token stale_lock
  [ "$BUILD_LEADER" = 1 ] || return 1
  [ -d "$LOCK_DIR" ] || return 1
  owner_host=$(awk -F= '$1 == "host" { print $2; exit }' "${LOCK_DIR}/owner" 2>/dev/null || true)
  owner_pid=$(awk -F= '$1 == "pid" { print $2; exit }' "${LOCK_DIR}/owner" 2>/dev/null || true)
  previous_token=$(sed -n '1p' "${LOCK_DIR}/token" 2>/dev/null || true)
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

# A lock whose heartbeat stopped is abandoned: its owner died without cleanup.
# Renames it aside so a waiter can claim a fresh lock.
reclaim_stale_build_lock() {
  local lock_mtime lock_age stale_lock
  lock_mtime=$(stat -c %Y "${LOCK_DIR}/heartbeat" 2>/dev/null || stat -c %Y "$LOCK_DIR" 2>/dev/null || echo 0)
  [ "$lock_mtime" -gt 0 ] || return 1
  lock_age=$(($(date +%s) - lock_mtime))
  [ "$lock_age" -ge "${VLLM_BUILD_LOCK_STALE_SECONDS:-600}" ] || return 1
  stale_lock="${LOCK_DIR}.stale.${HOSTNAME:-unknown}.$$"
  mv "$LOCK_DIR" "$stale_lock" 2>/dev/null || return 1
  echo "Recovered stale build lock aged ${lock_age}s."
  rm -rf "$stale_lock"
}

write_lock_owner_file() {
  # $1 (optional): cache key recorded for diagnostics
  printf 'host=%s\npid=%s\ntoken=%s\nstarted=%s\nkey=%s\n' \
    "${HOSTNAME:-unknown}" "$$" "$LOCK_TOKEN" \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${1:-}" > "${LOCK_DIR}/owner"
}

# Installs the cached artifact through $1 when one exists; otherwise waits for
# the build leader to publish it. Non-leaders poll for the READY artifact. The
# leader claims the build lock (recovering stale and restarted locks) and gets
# return status 1: the caller must build while holding the lock. Exits 1 when
# the wait times out. $2 labels the artifact in log messages.
wait_for_ready_wheel() {
  local install_fn=$1 label=$2
  local lock_wait_seconds lock_poll_seconds waited=0
  if "$install_fn"; then
    return 0
  fi
  lock_wait_seconds=${VLLM_BUILD_LOCK_WAIT_SECONDS:-10800}
  lock_poll_seconds=${VLLM_BUILD_LOCK_POLL_SECONDS:-10}
  if [ "$BUILD_LEADER" != 1 ]; then
    echo "Non-leader pod waiting up to ${lock_wait_seconds}s for the ${label} READY wheel."
    while [ "$waited" -lt "$lock_wait_seconds" ]; do
      if "$install_fn"; then
        return 0
      fi
      sleep "$lock_poll_seconds"
      waited=$((waited + lock_poll_seconds))
    done
    echo "FATAL: timed out waiting for the ${label} READY wheel after ${waited}s."
    exit 1
  fi
  recover_restarted_leader_lock || true
  if ! claim_build_lock; then
    echo "Another pod owns ${LOCK_DIR}; waiting up to ${lock_wait_seconds}s for its READY wheel."
    while [ "$waited" -lt "$lock_wait_seconds" ]; do
      if "$install_fn"; then
        return 0
      fi
      if [ -d "$LOCK_DIR" ]; then
        reclaim_stale_build_lock && claim_build_lock && break
      elif claim_build_lock; then
        break
      fi
      sleep "$lock_poll_seconds"
      waited=$((waited + lock_poll_seconds))
    done
    if [ "$BUILD_LOCK_HELD" != 1 ]; then
      echo "FATAL: timed out acquiring the ${label} build lock after ${waited}s."
      exit 1
    fi
  fi
  return 1
}

# --- pinned multi-branch recipes ------------------------------------------------
# Callers populate these before using the recipe helpers:
#   RECIPE_BRANCHES      display names, also the refs/heads/... fetch sources
#   RECIPE_ACTIONS       checkout | merge | cherry-pick | cherry-pick-m<N> |
#                        cherry-pick-parent1
#   RECIPE_SHAS          pinned 40-hex SHAs; an empty array resolves inputs fresh
#   RECIPE_FETCH_SOURCES what to fetch per input (recipe_fetch_branch_refs
#                        builds the common case)

validate_recipe_inputs() {
  if [ "${#RECIPE_BRANCHES[@]}" -eq 0 ] || [ "${#RECIPE_ACTIONS[@]}" -ne "${#RECIPE_BRANCHES[@]}" ] || \
     [ "${RECIPE_ACTIONS[0]}" != "checkout" ]; then
    echo "FATAL: BUILD_BRANCHES and BUILD_BRANCH_ACTIONS must have matching lengths and begin with checkout."
    return 1
  fi
  if [ "${#RECIPE_SHAS[@]}" -ne 0 ] && [ "${#RECIPE_SHAS[@]}" -ne "${#RECIPE_BRANCHES[@]}" ]; then
    echo "FATAL: VLLM_BUILD_SHAS must match VLLM_BUILD_REFS length."
    return 1
  fi
  local i
  for i in "${!RECIPE_BRANCHES[@]}"; do
    if ! [[ "${RECIPE_BRANCHES[$i]}" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$ ]]; then
      echo "FATAL: build input $i has an invalid branch name."
      return 1
    fi
    if [ -n "${RECIPE_SHAS[$i]:-}" ] && ! [[ "${RECIPE_SHAS[$i]}" =~ ^[0-9a-f]{40}$ ]]; then
      echo "FATAL: build input $i has no full Git SHA."
      return 1
    fi
    if [ "$i" -gt 0 ] && ! [[ "${RECIPE_ACTIONS[$i]}" =~ ^(merge|cherry-pick|cherry-pick-parent1|cherry-pick-m[1-9][0-9]*)$ ]]; then
      echo "FATAL: unsupported build action ${RECIPE_ACTIONS[$i]}."
      return 1
    fi
  done
}

validate_fetch_depth() {
  [[ "$1" =~ ^[1-9][0-9]*$ ]] || {
    echo "FATAL: VLLM_BUILD_FETCH_DEPTH must be a positive integer."
    return 1
  }
}

recipe_fetch_branch_refs() {
  local i
  RECIPE_FETCH_SOURCES=()
  for i in "${!RECIPE_BRANCHES[@]}"; do
    RECIPE_FETCH_SOURCES+=("refs/heads/${RECIPE_BRANCHES[$i]}")
  done
}

# Materializes refs/build-inputs/<i> in an existing Git repository and verifies
# that pinned SHAs did not move after campaign resolution. $1 repository, $2
# fetch source (URL or remote name), $3 depth; remaining args are extra
# `git fetch` flags such as --filter=blob:none.
fetch_build_inputs() {
  local repo_dir=$1 source=$2 depth=$3
  shift 3
  local -a refspecs=()
  local i
  for i in "${!RECIPE_BRANCHES[@]}"; do
    refspecs+=("${RECIPE_FETCH_SOURCES[$i]}:refs/build-inputs/${i}")
  done
  echo "Fetching pinned build inputs (depth=${depth})..."
  git -C "$repo_dir" fetch -q --no-tags --depth="$depth" "$@" "$source" "${refspecs[@]}"
  for i in "${!RECIPE_BRANCHES[@]}"; do
    [ -n "${RECIPE_SHAS[$i]:-}" ] || continue
    if [ "$(git -C "$repo_dir" rev-parse "refs/build-inputs/${i}")" != "${RECIPE_SHAS[$i]}" ]; then
      echo "FATAL: ${RECIPE_BRANCHES[$i]} moved while preparing build; refusing stale cache key."
      return 1
    fi
  done
}

# Checks out the base from input 0 and replays RECIPE_ACTIONS[1..] in order.
# `cherry-pick-parent1` picks the first parent of the input tip: a feature
# branch refreshed by merging main keeps its feature tip as the first parent,
# and replaying the merge delta (-m 1) would drag in unrelated upstream
# changes. Cherry-picks are staged so patches already contained in the base
# are skipped while original authorship is preserved.
apply_build_recipe() {
  local repo_dir=$1 committer_name=$2 committer_email=$3
  local i action branch sha ref target
  local -a pick
  echo "Checking out base ${RECIPE_BRANCHES[0]}..."
  git -C "$repo_dir" checkout -q --detach refs/build-inputs/0
  if [ -n "${RECIPE_SHAS[0]:-}" ] && [ "$(git -C "$repo_dir" rev-parse HEAD)" != "${RECIPE_SHAS[0]}" ]; then
    echo "FATAL: ${RECIPE_BRANCHES[0]} moved while preparing build; refusing stale cache key."
    return 1
  fi
  for ((i = 1; i < ${#RECIPE_BRANCHES[@]}; i++)); do
    action=${RECIPE_ACTIONS[$i]}
    branch=${RECIPE_BRANCHES[$i]}
    sha=${RECIPE_SHAS[$i]:-}
    ref="refs/build-inputs/${i}"
    if [[ "$action" == cherry-pick* ]]; then
      pick=(cherry-pick)
      target=${sha:-$ref}
      if [[ "$action" =~ ^cherry-pick-m([1-9][0-9]*)$ ]]; then
        pick+=(-m "${BASH_REMATCH[1]}")
      elif [ "$action" = "cherry-pick-parent1" ]; then
        target=$(git -C "$repo_dir" rev-parse "${ref}^1") || {
          echo "FATAL: first parent of ${branch} is unavailable."
          return 1
        }
      elif [ "$action" != "cherry-pick" ]; then
        echo "FATAL: invalid cherry-pick action ${action} for ${branch}."
        return 1
      fi
      if git -C "$repo_dir" merge-base --is-ancestor "$target" HEAD; then
        echo "Patch ${branch} is already in the build."
        continue
      fi
      echo "Applying ${branch} tip (${target})..."
      pick+=(--no-commit "$target")
      git -C "$repo_dir" "${pick[@]}" || {
        echo "FATAL: cherry-pick ${branch} failed"
        git -C "$repo_dir" status || true
        return 1
      }
      if git -C "$repo_dir" diff --cached --quiet; then
        echo "Patch ${branch} is already present with equivalent changes."
      else
        git -C "$repo_dir" \
          -c "user.name=${committer_name}" \
          -c "user.email=${committer_email}" \
          commit --no-gpg-sign --no-verify -C "$target" || {
            echo "FATAL: commit ${branch} failed"
            git -C "$repo_dir" status || true
            return 1
          }
      fi
    elif [ "$action" = "merge" ]; then
      echo "Merging ${branch}..."
      git -C "$repo_dir" \
        -c "user.name=${committer_name}" \
        -c "user.email=${committer_email}" \
        merge --no-edit "$ref" || {
          echo "FATAL: merge ${branch} failed"
          git -C "$repo_dir" status || true
          return 1
        }
    else
      echo "FATAL: unsupported action ${action} for ${branch}."
      return 1
    fi
  done
}
