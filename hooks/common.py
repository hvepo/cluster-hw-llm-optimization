#!/usr/bin/env python3
"""Shared Antigravity hook I/O helpers (camelCase JSON stdin/stdout contract)."""
import json
import os
import platform
import re
import subprocess
import sys

_WIN_DRIVE_RE = re.compile(r"^([A-Za-z]):[\\/](.*)$")
_WSL_UNC_RE = re.compile(r"^(?:\\\\|//)(?:wsl\.localhost|wsl\$)[\\/][^\\/]+([\\/].*)?$", re.IGNORECASE)
_MNT_DRIVE_RE = re.compile(r"^/mnt/([a-zA-Z])(?:/(.*))?$")


def read_hook_input():
    raw = sys.stdin.read().strip()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {}


def normalize_local_path(raw_path):
    if not raw_path:
        return raw_path
    p = str(raw_path).strip()
    if not p:
        return p
    is_win = platform.system().lower().startswith("windows")
    if not is_win:
        m_unc = _WSL_UNC_RE.match(p)
        if m_unc:
            tail = (m_unc.group(1) or "/").replace("\\", "/")
            return tail if tail.startswith("/") else "/" + tail
        m_drv = _WIN_DRIVE_RE.match(p)
        if m_drv:
            drv = m_drv.group(1).lower()
            rest = m_drv.group(2).replace("\\", "/").lstrip("/")
            return f"/mnt/{drv}/{rest}" if rest else f"/mnt/{drv}"
        return p
    m_mnt = _MNT_DRIVE_RE.match(p)
    if m_mnt:
        drv = m_mnt.group(1).upper()
        rest = (m_mnt.group(2) or "").replace("/", "\\")
        return f"{drv}:\\{rest}" if rest else f"{drv}:\\"
    if p.startswith("/"):
        try:
            w = subprocess.check_output(
                ["wsl.exe", "-e", "wslpath", "-w", p],
                stdin=subprocess.DEVNULL,
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=5,
            ).strip()
            if w:
                return w
        except Exception:
            rel = p.lstrip("/").replace("/", "\\")
            return f"\\\\wsl.localhost\\Ubuntu\\{rel}"
    return p


def find_repo_root():
    env_r = normalize_local_path(os.environ.get("CLUSTER_REPO_ROOT"))
    if env_r and os.path.isdir(env_r):
        return env_r
    here = os.path.abspath(os.path.dirname(__file__))
    for cand in (here, os.path.dirname(here), os.path.dirname(os.path.dirname(here))):
        marker = os.path.join(cand, "repo_root.txt")
        if os.path.isfile(marker):
            with open(marker, "r", encoding="utf-8") as f:
                p = normalize_local_path(f.read().strip())
            if p and os.path.isdir(p):
                return p
        if os.path.isfile(os.path.join(cand, "cluster.example.json")):
            return cand
    return os.path.abspath(os.path.join(here, ".."))


def emit_json(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()
