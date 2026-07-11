#!/usr/bin/env python3
"""Extract start/end timestamps (epoch seconds) from a result directory with 60s padding.

Accepts a directory path. Tries profile_export_aiperf.json first, falls back to profile_export.jsonl.
"""
import json, os, sys

d_dir = sys.argv[1]
pad = 60
ns_to_s = 1e9

aiperf = os.path.join(d_dir, "profile_export_aiperf.json")
jsonl = os.path.join(d_dir, "profile_export.jsonl")

if os.path.exists(aiperf):
    with open(aiperf) as f:
        d = json.load(f)
    print(d["min_request_timestamp"]["avg"] / ns_to_s - pad)
    print(d["max_response_timestamp"]["avg"] / ns_to_s + pad)
else:
    ts = []
    with open(jsonl) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            m = r.get("metadata", {})
            if m.get("request_start_ns"):
                ts.append(m["request_start_ns"])
            if m.get("request_end_ns"):
                ts.append(m["request_end_ns"])
    print(min(ts) / ns_to_s - pad)
    print(max(ts) / ns_to_s + pad)
