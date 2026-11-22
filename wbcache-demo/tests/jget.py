#!/usr/bin/env python3
"""Tiny jq-like helper for demo.sh: read JSON on stdin, print an expression.

Usage: echo '<json>' | jget.py "d['result']['source']"
The parsed JSON is available as variable `d`.
"""
import json
import sys

if len(sys.argv) != 2:
    sys.exit("usage: jget.py '<python expression on d>'")
d = json.load(sys.stdin)
ns = {"d": d, "f": d.get("flush") if isinstance(d, dict) else None}
print(eval(sys.argv[1], ns))  # noqa: S307 - local demo helper only
