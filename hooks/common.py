#!/usr/bin/env python3
"""Shared Antigravity hook I/O helpers (camelCase JSON stdin/stdout contract)."""
import json
import os
import sys


def read_hook_input():
    raw = sys.stdin.read().strip()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {}


def find_repo_root():
    env_r = os.environ.get("CLUSTER_REPO_ROOT")
    if env_r and os.path.isdir(env_r):
        return env_r
    here = os.path.abspath(os.path.dirname(__file__))
    for cand in (here, os.path.dirname(here), os.path.dirname(os.path.dirname(here))):
        marker = os.path.join(cand, "repo_root.txt")
        if os.path.isfile(marker):
            with open(marker, "r", encoding="utf-8") as f:
                p = f.read().strip()
            if p and os.path.isdir(p):
                return p
        if os.path.isfile(os.path.join(cand, "cluster.example.json")):
            return cand
    return os.path.abspath(os.path.join(here, ".."))


def emit_json(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()
