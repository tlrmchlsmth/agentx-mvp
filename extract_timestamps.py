#!/usr/bin/env python3
"""Extract start/end timestamps (epoch seconds) from profile_export_aiperf.json with 60s padding."""
import json, sys
from datetime import datetime, timezone

with open(sys.argv[1]) as f:
    d = json.load(f)

pad = 60


def _parse_ts(s):
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


print(_parse_ts(d["start_time"]) - pad)
print(_parse_ts(d["end_time"]) + pad)
