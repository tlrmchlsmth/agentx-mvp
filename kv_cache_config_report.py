#!/usr/bin/env python3
"""Extract KV cache token counts from vLLM startup logs.

Parses log files saved by `just dump-logs` for the line:
    GPU KV cache size: 2,934,834 tokens

Classifies pods as prefill/decode based on the log filename (pod name),
sums per-rank tokens for each role, and writes kv_cache_config.json.
"""
import argparse
import glob
import json
import os
import re
import sys

KV_RE = re.compile(r"GPU KV cache size:\s*([\d,]+)\s*tokens")


def classify_role(filename):
    name = os.path.basename(filename).lower()
    if "prefill" in name:
        return "prefill"
    if "decode" in name or "agg" in name:
        return "decode"
    return "decode"


def extract_kv_tokens(log_dir):
    totals = {"prefill": 0, "decode": 0}
    found = False
    for path in sorted(glob.glob(os.path.join(log_dir, "*.log"))):
        role = classify_role(path)
        seen = set()
        with open(path, errors="replace") as f:
            for line in f:
                m = KV_RE.search(line)
                if m:
                    tokens = int(m.group(1).replace(",", ""))
                    if tokens not in seen:
                        seen.add(tokens)
                        totals[role] += tokens
                        found = True
    return totals if found else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("log_dir", help="Directory containing vLLM log files (e.g. results_*/logs)")
    ap.add_argument("-o", "--output", required=True, help="Output JSON path")
    args = ap.parse_args()

    if not os.path.isdir(args.log_dir):
        print(f"WARNING: log directory not found: {args.log_dir}", file=sys.stderr)
        sys.exit(0)

    totals = extract_kv_tokens(args.log_dir)
    if totals is None:
        print("WARNING: no 'GPU KV cache size' found in logs", file=sys.stderr)
        sys.exit(0)

    result = {k: (v if v > 0 else None) for k, v in totals.items()}
    with open(args.output, "w") as f:
        json.dump(result, f, indent=2)
    print(f"KV cache config: prefill={result.get('prefill')}  decode={result.get('decode')}")


if __name__ == "__main__":
    main()
