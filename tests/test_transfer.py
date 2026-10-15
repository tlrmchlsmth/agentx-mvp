"""Exercise transfer integrity, adaptive sizing, and restart checkpoints."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from campaign import transfer


class TransferTests(unittest.TestCase):
    def test_binary_chunk_requires_remote_digest(self):
        payload = b"\x00\xff" * 23
        header = hashlib.sha256(payload).hexdigest().encode() + b"\n"
        with patch.object(transfer, "_kubectl", return_value=SimpleNamespace(
                returncode=0, stdout=header + payload, stderr=b"")):
            self.assertEqual(transfer._chunk("ns", "pod", "/workload/file", 0, 1, len(payload)), payload)
        with patch.object(transfer, "_kubectl", return_value=SimpleNamespace(
                returncode=0, stdout=header + payload[:-1] + b"x", stderr=b"")):
            with self.assertRaisesRegex(RuntimeError, "checksum mismatch"):
                transfer._chunk("ns", "pod", "/workload/file", 0, 1, len(payload))

    def test_adapts_chunk_size_and_retries_corruption(self):
        data = bytes(range(256)) * (transfer.BLOCK * 12 // 256 + 1)
        data = data[:transfer.BLOCK * 12 + 19]
        calls = []
        failed = False

        def metadata(*args):
            return len(data), hashlib.sha256(data).hexdigest()

        def chunk(namespace, pod, remote, offset, blocks, expected):
            nonlocal failed
            calls.append((offset, blocks))
            if blocks == 2 and not failed:
                failed = True
                raise RuntimeError("checksum mismatch")
            payload = data[offset * transfer.BLOCK:offset * transfer.BLOCK + expected]
            self.assertEqual(len(payload), expected)
            return payload

        with tempfile.TemporaryDirectory() as directory, \
             patch.object(transfer, "_remote_metadata", side_effect=metadata), \
             patch.object(transfer, "_chunk", side_effect=chunk), \
             patch.object(transfer.time, "sleep"):
            target = Path(directory) / "artifact.bin"
            transfer.download_file("ns", "pod", "/workload/artifact.bin", target)
            self.assertEqual(target.read_bytes(), data)
            self.assertIn((4, 2), calls)  # four successes grow 1 MiB to 2 MiB
            self.assertIn((4, 1), calls)  # failure halves it again
            self.assertFalse(target.with_name("artifact.bin.part.json").exists())

    def test_restart_verifies_checkpoint_and_resumes(self):
        data = b"a" * transfer.BLOCK + b"b" * transfer.BLOCK + b"c" * 17
        digest = hashlib.sha256(data).hexdigest()
        seen = []

        def chunk(namespace, pod, remote, offset, blocks, expected):
            seen.append(offset)
            return data[offset * transfer.BLOCK:offset * transfer.BLOCK + expected]

        with tempfile.TemporaryDirectory() as directory, \
             patch.object(transfer, "_remote_metadata", return_value=(len(data), digest)), \
             patch.object(transfer, "_chunk", side_effect=chunk):
            target = Path(directory) / "artifact.bin"
            partial = Path(directory) / "artifact.bin.part"
            partial.write_bytes(b"a" * transfer.BLOCK + b"corrupted padding")
            checkpoint = Path(directory) / "artifact.bin.part.json"
            checkpoint.write_text(json.dumps({"remote": "/workload/artifact.bin", "size": len(data),
                "sha256": digest, "chunks": [{"size": transfer.BLOCK,
                "sha256": hashlib.sha256(b"a" * transfer.BLOCK).hexdigest()}]}))
            transfer.download_file("ns", "pod", "/workload/artifact.bin", target)
            self.assertEqual(seen[0], 1)
            self.assertEqual(target.read_bytes(), data)

    def test_bad_chunk_is_never_checkpointed(self):
        data = b"z" * transfer.BLOCK
        digest = hashlib.sha256(data).hexdigest()
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(transfer, "_remote_metadata", return_value=(len(data), digest)), \
             patch.object(transfer, "_chunk", side_effect=RuntimeError("checksum mismatch")), \
             patch.object(transfer.time, "sleep"):
            target = Path(directory) / "artifact.bin"
            with self.assertRaisesRegex(RuntimeError, "Download stalled"):
                transfer.download_file("ns", "pod", "/workload/artifact.bin", target)
            self.assertEqual(target.with_name("artifact.bin.part").stat().st_size, 0)
            self.assertFalse(target.exists())
