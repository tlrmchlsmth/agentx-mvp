"""Resumable, checksummed downloads from a campaign artifacts Pod."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time

BLOCK = 1024 * 1024
MIN_BLOCKS = 1
MAX_BLOCKS = 32
GROW_AFTER = 4
MAX_FAILURES = 8
CHUNK_HEADER = re.compile(rb"^([0-9a-f]{64})\n$")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(BLOCK), b""):
            digest.update(block)
    return digest.hexdigest()


def _kubectl(namespace: str, pod: str, *command: str, timeout: int = 120) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["kubectl", "-n", namespace, "exec", pod, "--", *command],
                          capture_output=True, check=False, timeout=timeout)


def _remote_metadata(namespace: str, pod: str, remote: str) -> tuple[int, str]:
    result = _kubectl(namespace, pod, "sh", "-c",
                      'test -f "$1" && stat -c %s "$1" && sha256sum "$1"', "sh", remote,
                      timeout=900)
    if result.returncode:
        raise RuntimeError(f"Cannot inspect {remote}: {result.stderr.decode(errors='replace').strip()}")
    lines = result.stdout.decode().splitlines()
    if len(lines) != 2 or not lines[0].isdigit() or not re.fullmatch(r"[0-9a-f]{64}  .+", lines[1]):
        raise RuntimeError(f"Invalid remote metadata for {remote}")
    return int(lines[0]), lines[1][:64]


def _chunk(namespace: str, pod: str, remote: str, offset_blocks: int,
           count_blocks: int, expected_size: int) -> bytes:
    # Read twice on the Pod: once for a digest and once for the payload. The
    # final full-file digest also detects a source that changed between reads.
    script = ('set -e; dd if="$1" bs=1048576 skip="$2" count="$3" 2>/dev/null | sha256sum | cut -d " " -f 1; '
              'dd if="$1" bs=1048576 skip="$2" count="$3" 2>/dev/null')
    result = _kubectl(namespace, pod, "sh", "-c", script, "sh", remote,
                      str(offset_blocks), str(count_blocks))
    if result.returncode:
        raise RuntimeError(result.stderr.decode(errors="replace").strip() or "kubectl exec failed")
    header, separator, payload = result.stdout.partition(b"\n")
    if not separator or not CHUNK_HEADER.fullmatch(header + separator):
        raise RuntimeError("Missing remote chunk checksum")
    if len(payload) != expected_size or hashlib.sha256(payload).hexdigest().encode() != header:
        raise RuntimeError("Chunk size or checksum mismatch")
    return payload


def _save_checkpoint(path: Path, state: dict) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(state, separators=(",", ":")))
    os.replace(temporary, path)


def download_file(namespace: str, pod: str, remote: str, destination: Path) -> Path:
    """Download a PVC file, resuming verified MiB-aligned chunks after failure."""
    size, digest = _remote_metadata(namespace, pod, remote)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file() and destination.stat().st_size == size:
        if _file_sha256(destination) == digest:
            return destination
    partial = destination.with_name(destination.name + ".part")
    checkpoint = destination.with_name(destination.name + ".part.json")
    state = {"remote": remote, "size": size, "sha256": digest, "chunks": []}
    if partial.exists() and checkpoint.exists():
        try:
            saved = json.loads(checkpoint.read_text())
            if (saved.get("remote"), saved.get("size"), saved.get("sha256")) == (remote, size, digest):
                with partial.open("rb") as source:
                    verified = 0
                    for entry in saved["chunks"]:
                        if (type(entry.get("size")) is not int or
                                not 0 < entry["size"] <= MAX_BLOCKS * BLOCK or
                                verified + entry["size"] > size):
                            break
                        chunk = source.read(entry["size"])
                        if (len(chunk) != entry["size"] or
                                hashlib.sha256(chunk).hexdigest() != entry["sha256"]):
                            break
                        state["chunks"].append(entry)
                        verified += entry["size"]
        except (OSError, ValueError, KeyError, TypeError):
            pass
    offset = sum(entry["size"] for entry in state["chunks"])
    if offset % BLOCK and offset != size:
        offset = 0
        state["chunks"] = []
    with partial.open("a+b") as output:
        output.truncate(offset)
        output.seek(offset)
        chunk_blocks = MIN_BLOCKS
        successes = failures = 0
        while offset < size:
            blocks = min(chunk_blocks, (size - offset + BLOCK - 1) // BLOCK)
            expected = min(size - offset, blocks * BLOCK)
            try:
                payload = _chunk(namespace, pod, remote, offset // BLOCK, blocks, expected)
            except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                failures += 1
                successes = 0
                chunk_blocks = max(MIN_BLOCKS, chunk_blocks // 2)
                if failures >= MAX_FAILURES:
                    raise RuntimeError(f"Download stalled at {offset}/{size} bytes for {remote}: {exc}") from exc
                time.sleep(min(failures, 3))
                continue
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
            state["chunks"].append({"size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()})
            _save_checkpoint(checkpoint, state)
            offset += len(payload)
            failures = 0
            successes += 1
            if successes >= GROW_AFTER:
                chunk_blocks = min(MAX_BLOCKS, chunk_blocks * 2)
                successes = 0
    if _file_sha256(partial) != digest or _remote_metadata(namespace, pod, remote) != (size, digest):
        partial.unlink(missing_ok=True)
        checkpoint.unlink(missing_ok=True)
        raise RuntimeError(f"Remote file changed during download or checksum mismatch: {remote}")
    os.replace(partial, destination)
    checkpoint.unlink(missing_ok=True)
    return destination


def download_tree(namespace: str, pod: str, remote_root: str, destination: Path) -> Path:
    """Download files independently so a failed file does not restart the tree."""
    result = _kubectl(namespace, pod, "find", remote_root, "-type", "f", "-print0")
    if result.returncode:
        raise RuntimeError(f"Cannot list {remote_root}: {result.stderr.decode(errors='replace').strip()}")
    root = Path(remote_root)
    for raw in result.stdout.split(b"\0"):
        if not raw:
            continue
        remote = os.fsdecode(raw)
        relative = Path(remote).relative_to(root)
        if not relative.parts or ".." in relative.parts:
            raise RuntimeError(f"Invalid artifact path: {remote}")
        download_file(namespace, pod, remote, destination / relative)
    return destination
