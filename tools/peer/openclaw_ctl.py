#!/usr/bin/env python3
"""WSL-only OpenClaw runtime installer, config modernizer, HA lease sentinel, and Telegram helper.

Prevents Telegram `409 Conflict` split-brain between `torre` (primary) and `portatil` (standby)
using an atomic shared lease (`<sync_dir>/openclaw-ha/leader.json`) + peer fencing over `:1236`.
"""
import argparse
import hashlib
import json
import os
import platform
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

PEER_DIR = os.path.dirname(os.path.abspath(__file__))
TOOLS_DIR = os.path.abspath(os.path.join(PEER_DIR, ".."))
REPO_ROOT = os.path.abspath(os.path.join(TOOLS_DIR, ".."))
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

import cluster_lib  # noqa: E402


def state_dir():
    plat = cluster_lib.detect_platform()
    if plat == "windows":
        base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
    else:
        base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    d = os.path.join(base, "cluster-llm")
    os.makedirs(d, exist_ok=True)
    return d


def resolve_sync_dir(cfg=None):
    env_dir = (os.environ.get("CLUSTER_PEER_SYNC_DIR") or "").strip()
    if env_dir:
        return env_dir
    cfg = cfg or cluster_lib.load_cluster_config(REPO_ROOT)
    raw = ((cfg.get("peer") or {}).get("sync_dir") or "").strip()
    if raw and not raw.startswith("<"):
        return cluster_lib.normalize_local_path(raw)
    plat = cluster_lib.detect_platform()
    if plat in ("wsl", "linux"):
        if os.path.isdir("/mnt/g/My Drive"):
            return "/mnt/g/My Drive/Antigravity-Sync"
    elif plat == "windows":
        if os.path.isdir(r"G:\My Drive"):
            return r"G:\My Drive\Antigravity-Sync"
    return ""


def resolve_peer_token(cfg=None, sync_dir=None):
    env_tok = (os.environ.get("CLUSTER_PEER_TOKEN") or "").strip()
    if env_tok:
        return env_tok
    cfg = cfg or cluster_lib.load_cluster_config(REPO_ROOT)
    tok = ((cfg.get("peer") or {}).get("token") or "").strip()
    if tok and not tok.startswith("<"):
        return tok
    sdir = sync_dir if sync_dir is not None else resolve_sync_dir(cfg)
    if sdir:
        tok_path = os.path.join(sdir, "peer.token")
        if os.path.isfile(tok_path):
            try:
                val = open(tok_path, "r", encoding="utf-8").read().strip()
                if val:
                    return val
            except OSError:
                pass
    return ""


def openclaw_home():
    return os.environ.get("OPENCLAW_HOME") or os.path.join(os.path.expanduser("~"), ".openclaw")


def openclaw_config_path():
    return os.path.join(openclaw_home(), "openclaw.json")


def _node_base_dir():
    return os.path.join(os.path.expanduser("~"), ".local", "share", "nodejs")


def _node_current_bin():
    return os.path.join(_node_base_dir(), "current", "bin")


def _openclaw_npm_fallback_prefix():
    return os.path.join(os.path.expanduser("~"), ".local", "share", "openclaw-npm")


def _ensure_secure_openclaw_dir(path, private=False):
    os.makedirs(path, mode=0o700 if private else 0o755, exist_ok=True)
    if os.name == "nt":
        return
    try:
        if private:
            os.chmod(path, 0o700)
        else:
            mode = os.stat(path).st_mode & 0o777
            if mode & 0o022:
                os.chmod(path, mode & ~0o022)
    except OSError:
        pass


def find_linux_node_bin():
    cands = [os.path.join(_node_current_bin(), "node")]
    found = shutil.which("node")
    if found and not found.startswith("/mnt/") and found not in cands:
        cands.append(found)
    for p in ("/usr/local/bin/node", "/usr/bin/node"):
        if p not in cands:
            cands.append(p)
    for c in cands:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


def find_openclaw_bin():
    cands = [
        os.path.join(os.path.expanduser("~"), ".local", "bin", "openclaw"),
        os.path.join(_node_current_bin(), "openclaw"),
        os.path.join(_openclaw_npm_fallback_prefix(), "bin", "openclaw"),
    ]
    found = shutil.which("openclaw")
    if found and not found.startswith("/mnt/") and found not in cands:
        cands.append(found)
    for p in ("/usr/local/bin/openclaw", "/usr/bin/openclaw"):
        if p not in cands:
            cands.append(p)
    for c in cands:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


def _node_env(node_bin=None):
    env = os.environ.copy()
    parts = [
        os.path.join(os.path.expanduser("~"), ".local", "bin"),
        os.path.dirname(node_bin) if node_bin else "",
        _node_current_bin(),
        os.path.join(_openclaw_npm_fallback_prefix(), "bin"),
    ]
    dedup = []
    for p in parts:
        if p and p not in dedup:
            dedup.append(p)
    cur_path = env.get("PATH", "")
    # Place Linux Node ahead of Windows /mnt/c paths so npm/node never invoke Windows binaries
    env["PATH"] = ":".join(dedup + ([cur_path] if cur_path else []))
    return env


def _is_writable_prefix(prefix):
    if not prefix or not os.path.isdir(prefix) or not os.access(prefix, os.W_OK):
        return False
    for sub in (
        os.path.join(prefix, "lib"),
        os.path.join(prefix, "lib", "node_modules"),
        os.path.join(prefix, "bin"),
    ):
        if os.path.exists(sub) and not os.access(sub, os.W_OK):
            return False
    return True


def _resolve_npm_prefix(node_bin):
    candidate = os.path.dirname(os.path.dirname(os.path.abspath(node_bin)))
    if _is_writable_prefix(candidate):
        return candidate
    fallback = _openclaw_npm_fallback_prefix()
    os.makedirs(fallback, exist_ok=True)
    return fallback


def _node_major_version(node_bin):
    try:
        out = subprocess.check_output([node_bin, "-v"], stdin=subprocess.DEVNULL, text=True, stderr=subprocess.DEVNULL, timeout=5).strip()
        return int(out.lstrip("v").split(".", 1)[0])
    except Exception:
        return 0


def install_runtime(force=False):
    """Install user-space Linux Node.js v24 LTS + openclaw npm package in WSL/Linux (no sudo)."""
    if cluster_lib.detect_platform() == "windows":
        raise RuntimeError("OpenClaw runtime is WSL/Linux-only; run this inside Ubuntu WSL.")

    node_bin = find_linux_node_bin()
    node_ok = bool(node_bin and _node_major_version(node_bin) >= 24)
    oc_bin = find_openclaw_bin()
    if node_ok and oc_bin and not force:
        return {"ok": True, "already_installed": True, "node": node_bin, "openclaw": oc_bin}

    arch = platform.machine().lower()
    node_arch = "arm64" if arch in ("aarch64", "arm64") else "x64"

    if not node_ok or force:
        shasum_url = "https://nodejs.org/dist/latest-v24.x/SHASUMS256.txt"
        with urllib.request.urlopen(shasum_url, timeout=20) as r:
            shasum_txt = r.read().decode("utf-8", "replace")
        target_name = None
        expected_sha = None
        suffix = f"-linux-{node_arch}.tar.xz"
        for line in shasum_txt.splitlines():
            parts = line.strip().split()
            if len(parts) == 2 and parts[1].endswith(suffix) and parts[1].startswith("node-v24."):
                expected_sha, target_name = parts[0], parts[1]
                break
        if not target_name:
            raise RuntimeError(f"Could not find Node.js v24 tarball for {node_arch}")

        tar_url = f"https://nodejs.org/dist/latest-v24.x/{target_name}"
        base_dir = _node_base_dir()
        os.makedirs(base_dir, exist_ok=True)
        with tempfile.NamedTemporaryFile(suffix=".tar.xz", delete=False) as tf:
            tmp_tar = tf.name
        try:
            print(f"[openclaw] downloading {tar_url}")
            with urllib.request.urlopen(tar_url, timeout=120) as r, open(tmp_tar, "wb") as out_f:
                h = hashlib.sha256()
                while True:
                    chunk = r.read(65536)
                    if not chunk:
                        break
                    h.update(chunk)
                    out_f.write(chunk)
            got_sha = h.hexdigest()
            if got_sha != expected_sha:
                raise RuntimeError(f"Node.js SHA256 mismatch: expected {expected_sha}, got {got_sha}")
            with tarfile.open(tmp_tar, "r:xz") as tar:
                tar.extractall(base_dir)
            extracted_dir = os.path.join(base_dir, target_name[: -len(".tar.xz")])
            cur_link = os.path.join(base_dir, "current")
            if os.path.islink(cur_link) or os.path.exists(cur_link):
                if os.path.islink(cur_link):
                    os.unlink(cur_link)
                else:
                    shutil.rmtree(cur_link)
            os.symlink(extracted_dir, cur_link)
        finally:
            if os.path.exists(tmp_tar):
                os.remove(tmp_tar)
        node_bin = os.path.join(_node_current_bin(), "node")

    env = _node_env(node_bin)
    npm_bin = os.path.join(os.path.dirname(node_bin), "npm")
    if not (os.path.isfile(npm_bin) and os.access(npm_bin, os.X_OK)):
        npm_bin = shutil.which("npm", path=env["PATH"])
    if not npm_bin:
        raise RuntimeError(f"npm binary not found alongside Node.js ({node_bin})")
    prefix = _resolve_npm_prefix(node_bin)
    print(f"[openclaw] installing openclaw via {npm_bin} (prefix={prefix})")
    r = subprocess.run(
        [npm_bin, "install", "-g", "openclaw@latest", "--prefix", prefix],
        stdin=subprocess.DEVNULL,
        env=env,
        text=True,
        capture_output=True,
        timeout=300,
    )
    if r.returncode != 0:
        raise RuntimeError(f"npm install -g openclaw failed:\n{r.stderr or r.stdout}")

    installed_oc = os.path.join(prefix, "bin", "openclaw")
    local_bin_dir = os.path.join(os.path.expanduser("~"), ".local", "bin")
    os.makedirs(local_bin_dir, exist_ok=True)
    link_path = os.path.join(local_bin_dir, "openclaw")
    if os.path.islink(link_path) or os.path.exists(link_path):
        os.unlink(link_path)
    os.symlink(installed_oc, link_path)

    return {"ok": True, "already_installed": False, "node": node_bin, "openclaw": link_path}


def configure_openclaw(cfg=None, config_path=None):
    """Update openclaw.json to use Cluster Router Gateway (:1235) while preserving Telegram/auth secrets."""
    cfg = cfg or cluster_lib.load_cluster_config(REPO_ROOT)
    cpath = config_path or openclaw_config_path()
    _ensure_secure_openclaw_dir(os.path.dirname(cpath), private=True)

    data = {}
    if os.path.isfile(cpath):
        data = json.load(open(cpath, encoding="utf-8"))
        bak = f"{cpath}.{time.strftime('%Y%m%d-%H%M%S')}.bak"
        shutil.copy2(cpath, bak)

    gw = cfg.get("gateway") or {}
    gw_url = f"http://{gw.get('host', '127.0.0.1')}:{int(gw.get('port', 1235))}/v1"

    models_sec = data.setdefault("models", {})
    models_sec.setdefault("mode", "merge")
    providers = models_sec.setdefault("providers", {})
    existing_cl = providers.get("cluster-local") or {}
    api_key = existing_cl.get("apiKey") or "local-cluster"

    providers["cluster-local"] = {
        "baseUrl": gw_url,
        "apiKey": api_key,
        "api": "openai-completions",
        "models": [
            {
                "id": "cluster/code",
                "name": "Cluster Code (30B MoE -> 14B)",
                "api": "openai-completions",
                "contextWindow": 16384,
                "maxTokens": 4096,
            },
            {
                "id": "cluster/fast-agent",
                "name": "Cluster Fast Agent (8B 130 tok/s)",
                "api": "openai-completions",
                "contextWindow": 16384,
                "maxTokens": 4096,
            },
            {
                "id": "cluster/code-fast",
                "name": "Cluster Code Fast (14B 72 tok/s)",
                "api": "openai-completions",
                "contextWindow": 12288,
                "maxTokens": 4096,
            },
            {
                "id": "cluster/reasoning",
                "name": "Cluster Reasoning (R1-14B Think)",
                "api": "openai-completions",
                "contextWindow": 8192,
                "maxTokens": 4096,
            },
            {
                "id": "cluster/vision-heavy",
                "name": "Cluster Vision Heavy (26B 32K)",
                "api": "openai-completions",
                "contextWindow": 32768,
                "maxTokens": 4096,
            },
        ],
    }

    agents_sec = data.setdefault("agents", {})
    defaults = agents_sec.setdefault("defaults", {})
    ws_dir = defaults.get("workspace") or os.path.join(os.path.dirname(cpath), "workspace")
    defaults["workspace"] = ws_dir
    prev_model = defaults.get("model") or {}
    prev_fbs = [f for f in (prev_model.get("fallbacks") or []) if not str(f).startswith("cluster-local/")]
    fallbacks = ["cluster-local/cluster/fast-agent"] + prev_fbs
    defaults["model"] = {
        "primary": "cluster-local/cluster/code",
        "fallbacks": fallbacks,
    }

    gw_sec = data.setdefault("gateway", {})
    gw_sec.setdefault("mode", "local")
    gw_sec.setdefault("port", int(((cfg.get("peer") or {}).get("openclaw") or {}).get("port") or 18789))

    with open(cpath, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")

    _ensure_secure_openclaw_dir(ws_dir, private=False)
    ws_agents_md = os.path.join(ws_dir, "AGENTS.md")
    peer_ctl = os.path.join(REPO_ROOT, "tools", "peer", "peer_ctl.py")
    oc_ctl = os.path.join(REPO_ROOT, "tools", "peer", "openclaw_ctl.py")
    ws_instructions = (
        "# OpenClaw Cluster Agent Workspace Instructions\n\n"
        "You run on the active cluster leader (`torre` or `portatil`) and receive tasks from Telegram.\n\n"
        "## Dispatching Tasks to PC (`torre`) or Laptop (`portatil`)\n"
        "- Inspect cluster & peer node status:\n"
        f"  `python3 {peer_ctl} status`\n"
        "- Send an autonomous Antigravity coding/ops order (WSL, Windows, or auto; falls back to Google Drive queue if offline):\n"
        f"  `python3 {peer_ctl} send --to torre --os auto --wait 120 \"<task>\"`\n"
        f"  `python3 {peer_ctl} send --to portatil --os auto --wait 120 \"<task>\"`\n"
        "- Target Windows explicitly on either machine (`--os windows`) or WSL (`--os wsl`), or run a direct shell/PowerShell command:\n"
        f"  `python3 {peer_ctl} exec --to torre --os windows \"<powershell command>\"`\n"
        "- Check an async job result:\n"
        f"  `python3 {peer_ctl} job <job_id> --node <torre|portatil>`\n"
        "- Transfer OpenClaw leadership between nodes:\n"
        f"  `python3 {oc_ctl} takeover`\n"
    )
    with open(ws_agents_md, "w", encoding="utf-8") as f:
        f.write(ws_instructions)

    return {"ok": True, "config_path": cpath, "workspace_agents_md": ws_agents_md, "gateway_v1": gw_url}


class LeaseStore:
    """Atomic JSON HA lease in `<sync_dir>/openclaw-ha/leader.json` (DrvFS / Google Drive safe)."""

    def __init__(self, sync_dir):
        self.sync_dir = sync_dir
        self.ha_dir = os.path.join(sync_dir, "openclaw-ha") if sync_dir else os.path.join(state_dir(), "openclaw-ha")
        self.lease_path = os.path.join(self.ha_dir, "leader.json")

    def read_lease(self):
        if not os.path.isfile(self.lease_path):
            return None
        try:
            with open(self.lease_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def write_lease(self, rec):
        os.makedirs(self.ha_dir, exist_ok=True)
        tmp = os.path.join(self.ha_dir, f".leader.{os.getpid()}.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rec, f, indent=2)
            f.write("\n")
        os.replace(tmp, self.lease_path)
        return rec

    def acquire_or_renew(self, node_name, pid=None, ttl_s=30, force=False, manual_leader=None, now=None):
        ts = time.time() if now is None else now
        cur = self.read_lease()
        if not force and cur:
            holder = cur.get("holder_node")
            exp = float(cur.get("expires_at") or 0.0)
            if holder and holder != node_name and ts < exp and cur.get("state") == "LEADER":
                return False, cur
        if cur and cur.get("holder_node") == node_name and cur.get("state") == "LEADER":
            epoch = int(cur.get("epoch") or 1)
            acquired = float(cur.get("acquired_at") or ts)
            if manual_leader is None:
                manual_leader = cur.get("manual_leader")
        else:
            epoch = (int(cur.get("epoch") or 0) + 1) if cur else 1
            acquired = ts
        rec = {
            "service": "openclaw-gateway",
            "holder_node": node_name,
            "epoch": epoch,
            "acquired_at": acquired,
            "renewed_at": ts,
            "expires_at": ts + float(ttl_s),
            "pid": pid,
            "state": "LEADER",
            "manual_leader": manual_leader,
        }
        self.write_lease(rec)
        return True, rec

    def release(self, node_name, now=None):
        ts = time.time() if now is None else now
        cur = self.read_lease()
        if not cur or cur.get("holder_node") != node_name:
            return False
        cur["renewed_at"] = ts
        cur["expires_at"] = ts - 1.0
        cur["pid"] = None
        cur["state"] = "STANDBY"
        if cur.get("manual_leader") == node_name:
            cur["manual_leader"] = None
        self.write_lease(cur)
        return True


def _oc_pid_file():
    return os.path.join(state_dir(), "openclaw.pid")


def _oc_log_file():
    return os.path.join(state_dir(), "openclaw.log")


def _sentinel_pid_file():
    return os.path.join(state_dir(), "openclaw-sentinel.pid")


def _sentinel_log_file():
    return os.path.join(state_dir(), "openclaw-sentinel.log")


def _reap_children():
    if os.name == "nt" or not hasattr(os, "waitpid"):
        return
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
            if pid <= 0:
                break
        except ChildProcessError:
            break
        except OSError:
            break


def _is_proc_zombie(pid):
    try:
        with open(f"/proc/{int(pid)}/status", "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if line.startswith("State:"):
                    val = line.split(":", 1)[1].strip()
                    return val.startswith("Z")
    except OSError:
        pass
    return False


def _pid_alive(pid):
    _reap_children()
    try:
        os.kill(int(pid), 0)
    except Exception:
        return False
    return not _is_proc_zombie(pid)


def _read_pid(path):
    if not os.path.isfile(path):
        return None
    try:
        pid = int(open(path, "r", encoding="utf-8").read().strip())
        return pid if _pid_alive(pid) else None
    except Exception:
        return None


def _port_open(host, port, timeout=1.0):
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def sweep_orphan_openclaw(keep_pid=None):
    """Terminate stray `openclaw gateway` processes on Linux/WSL so Telegram never hits 409 Conflict."""
    proc_root = Path("/proc")
    if not proc_root.exists():
        return []
    my_uid = os.getuid()
    killed = []
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid in (os.getpid(), keep_pid):
            continue
        try:
            if entry.stat().st_uid != my_uid:
                continue
            raw = (entry / "cmdline").read_bytes()
            if not raw:
                continue
            cmd = " ".join(p.decode("utf-8", "ignore") for p in raw.split(b"\x00") if p).lower()
            if "openclaw_ctl.py" in cmd or "openclaw-sentinel" in cmd:
                continue
            if "openclaw-gateway" in cmd or ("openclaw" in cmd and "gateway" in cmd):
                os.kill(pid, signal.SIGTERM)
                killed.append(pid)
        except Exception:
            continue
    if killed:
        time.sleep(0.4)
        for pid in killed:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
    return killed


def _call_sibling_openclaw_fence(cfg, timeout=3.0):
    local_node = cfg.get("local_node", "torre")
    token = resolve_peer_token(cfg)
    if not token:
        return {"ok": False, "reason": "no peer_token configured"}
    results = {}
    for name, nd in (cfg.get("nodes") or {}).items():
        if name == local_node:
            continue
        purl = (nd.get("peer_url") or "").strip().rstrip("/")
        if not purl or "<" in purl:
            continue
        req = urllib.request.Request(
            purl + "/openclaw/control",
            data=json.dumps({"action": "fence"}).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                results[name] = json.loads(r.read().decode("utf-8", "replace"))
        except Exception as exc:
            results[name] = {"ok": False, "error": str(exc)}
    return {"ok": True, "fenced": results}


def cmd_start(cfg, force=False, manual_leader=None):
    oc_bin = find_openclaw_bin()
    if not oc_bin:
        return {"ok": False, "error": "openclaw binary not found in WSL. Run: python3 tools/peer/openclaw_ctl.py install-runtime"}

    local_node = cfg.get("local_node", "torre")
    oc_cfg = ((cfg.get("peer") or {}).get("openclaw") or {})
    ttl_s = int(oc_cfg.get("lease_ttl_s") or 30)
    port = int(oc_cfg.get("port") or 18789)
    store = LeaseStore(resolve_sync_dir(cfg))

    cur_pid = _read_pid(_oc_pid_file())
    if cur_pid:
        store.acquire_or_renew(local_node, pid=cur_pid, ttl_s=ttl_s, force=force, manual_leader=manual_leader)
        return {"ok": True, "already_running": True, "pid": cur_pid, "node": local_node, "port": port}

    ok, lease = store.acquire_or_renew(local_node, pid=None, ttl_s=ttl_s, force=force, manual_leader=manual_leader)
    if not ok:
        return {"ok": False, "error": f"Lease held by {lease.get('holder_node')} until {lease.get('expires_at')}", "lease": lease}

    _ensure_secure_openclaw_dir(openclaw_home(), private=True)
    sweep_orphan_openclaw()
    log_path = _oc_log_file()
    with open(log_path, "a", encoding="utf-8") as log_f:
        proc = subprocess.Popen(
            [oc_bin, "gateway", "run"],
            stdin=subprocess.DEVNULL,
            stdout=log_f,
            stderr=log_f,
            env=_node_env(),
            start_new_session=True,
        )
    with open(_oc_pid_file(), "w", encoding="utf-8") as f:
        f.write(str(proc.pid))

    store.acquire_or_renew(local_node, pid=proc.pid, ttl_s=ttl_s, force=True, manual_leader=manual_leader)
    deadline = time.time() + 20.0
    while time.time() < deadline:
        if proc.poll() is not None:
            return {"ok": False, "error": f"openclaw exited early ({proc.returncode}); check {log_path}"}
        if _port_open("127.0.0.1", port, timeout=0.5):
            break
        time.sleep(0.3)

    return {"ok": True, "pid": proc.pid, "node": local_node, "port": port, "port_open": _port_open("127.0.0.1", port)}


def cmd_stop(cfg, release_lease=True):
    local_node = cfg.get("local_node", "torre")
    pid = _read_pid(_oc_pid_file())
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
            for _ in range(15):
                if not _pid_alive(pid):
                    break
                time.sleep(0.2)
            if _pid_alive(pid):
                os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    if os.path.isfile(_oc_pid_file()):
        try:
            os.remove(_oc_pid_file())
        except OSError:
            pass
    swept = sweep_orphan_openclaw()
    if release_lease:
        LeaseStore(resolve_sync_dir(cfg)).release(local_node)
    return {"ok": True, "node": local_node, "stopped_pid": pid, "swept_orphans": swept}


def cmd_takeover(cfg):
    local_node = cfg.get("local_node", "torre")
    fence_res = _call_sibling_openclaw_fence(cfg, timeout=3.0)
    time.sleep(0.8)
    res = cmd_start(cfg, force=True, manual_leader=local_node)
    res["fence_result"] = fence_res
    return res


def cmd_status(cfg):
    local_node = cfg.get("local_node", "torre")
    oc_cfg = ((cfg.get("peer") or {}).get("openclaw") or {})
    port = int(oc_cfg.get("port") or 18789)
    pid = _read_pid(_oc_pid_file())
    sentinel_pid = _read_pid(_sentinel_pid_file())
    lease = LeaseStore(resolve_sync_dir(cfg)).read_lease()
    cpath = openclaw_config_path()
    tg_ok = False
    gw_url = None
    if os.path.isfile(cpath):
        try:
            oc_data = json.load(open(cpath, encoding="utf-8"))
            tg = (oc_data.get("channels") or {}).get("telegram") or {}
            tg_ok = bool(tg.get("enabled") and tg.get("botToken"))
            gw_url = (((oc_data.get("models") or {}).get("providers") or {}).get("cluster-local") or {}).get("baseUrl")
        except Exception:
            pass
    return {
        "local_node": local_node,
        "platform": cluster_lib.detect_platform(),
        "node_bin": find_linux_node_bin(),
        "openclaw_bin": find_openclaw_bin(),
        "config_path": cpath,
        "cluster_local_base_url": gw_url,
        "telegram_configured": tg_ok,
        "gateway_pid": pid,
        "gateway_port_open": _port_open("127.0.0.1", port, timeout=0.5),
        "sentinel_pid": sentinel_pid,
        "sync_dir": resolve_sync_dir(cfg),
        "lease": lease,
    }


def send_telegram_reply(message, chat_id=None):
    cpath = openclaw_config_path()
    if not os.path.isfile(cpath):
        return {"ok": False, "error": f"Missing {cpath}"}
    data = json.load(open(cpath, encoding="utf-8"))
    tg = (data.get("channels") or {}).get("telegram") or {}
    bot_token = (tg.get("botToken") or "").strip()
    target_chat = str(chat_id or ((tg.get("allowFrom") or [""])[0])).strip()
    if not bot_token or not target_chat:
        return {"ok": False, "error": "Telegram botToken or allowFrom chat_id missing in openclaw.json"}
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = json.dumps({"chat_id": target_chat, "text": str(message)[:4000]}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            resp = json.loads(r.read().decode("utf-8", "replace"))
        return {"ok": bool(resp.get("ok")), "message_id": (resp.get("result") or {}).get("message_id")}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _probe_node_peer(cfg, node_name, timeout=1.5):
    purl = (((cfg.get("nodes") or {}).get(node_name) or {}).get("peer_url") or "").strip().rstrip("/")
    if not purl or "<" in purl:
        return False
    try:
        with urllib.request.urlopen(purl + "/health", timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
        return bool(d.get("ok"))
    except Exception:
        return False


def run_sentinel_loop(cfg, once=False):
    local_node = cfg.get("local_node", "torre")
    oc_cfg = ((cfg.get("peer") or {}).get("openclaw") or {})
    primary = oc_cfg.get("primary_node") or "torre"
    ttl_s = int(oc_cfg.get("lease_ttl_s") or 30)
    interval_s = max(3, int(oc_cfg.get("probe_interval_s") or 10))
    need_fails = max(1, int(oc_cfg.get("failures_before_failover") or 3))
    store = LeaseStore(resolve_sync_dir(cfg))
    failures = 0

    with open(_sentinel_pid_file(), "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))

    while True:
        _reap_children()
        now = time.time()
        lease = store.read_lease()
        manual = (lease or {}).get("manual_leader")
        expired = (lease is None) or (now >= float(lease.get("expires_at") or 0.0)) or (lease.get("state") != "LEADER")
        cur_holder = (lease or {}).get("holder_node")

        if local_node == primary:
            # Primary yields only when sibling explicitly holds manual_leader AND its lease is still fresh
            if manual and manual != local_node and not expired:
                if _read_pid(_oc_pid_file()):
                    cmd_stop(cfg, release_lease=False)
            else:
                cmd_start(cfg, force=expired, manual_leader=None)
        else:
            primary_up = _probe_node_peer(cfg, primary)
            if primary_up:
                failures = 0
                if manual == local_node:
                    cmd_start(cfg, force=False, manual_leader=local_node)
                elif _read_pid(_oc_pid_file()) or cur_holder == local_node:
                    cmd_stop(cfg, release_lease=True)
            else:
                failures += 1
                if (manual == local_node) or (failures >= need_fails and expired):
                    cmd_start(cfg, force=True, manual_leader=manual)

        if once:
            break
        time.sleep(interval_s)


def start_sentinel_daemon(cfg):
    pid = _read_pid(_sentinel_pid_file())
    if pid:
        return {"ok": True, "already_running": True, "pid": pid}
    log_path = _sentinel_log_file()
    with open(log_path, "a", encoding="utf-8") as lf:
        proc = subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "sentinel"],
            stdin=subprocess.DEVNULL,
            stdout=lf,
            stderr=lf,
            env=_node_env(),
            start_new_session=True,
        )
    with open(_sentinel_pid_file(), "w", encoding="utf-8") as f:
        f.write(str(proc.pid))
    return {"ok": True, "pid": proc.pid, "log": log_path}


def stop_sentinel_daemon():
    pid = _read_pid(_sentinel_pid_file())
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    if os.path.isfile(_sentinel_pid_file()):
        try:
            os.remove(_sentinel_pid_file())
        except OSError:
            pass
    return {"ok": True, "stopped_pid": pid}


def main(argv=None):
    ap = argparse.ArgumentParser(description="OpenClaw WSL installer & Active-Standby HA controller")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pi = sub.add_parser("install-runtime")
    pi.add_argument("--force", action="store_true")
    sub.add_parser("configure")
    sub.add_parser("start")
    sub.add_parser("stop")
    sub.add_parser("fence")
    sub.add_parser("takeover")
    sub.add_parser("status")
    ps = sub.add_parser("sentinel")
    ps.add_argument("--daemon", action="store_true")
    ps.add_argument("--once", action="store_true")
    sub.add_parser("sentinel-stop")
    pt = sub.add_parser("telegram-reply")
    pt.add_argument("--message", required=True)
    pt.add_argument("--chat-id", default=None)

    args = ap.parse_args(argv)
    cfg = cluster_lib.load_cluster_config(REPO_ROOT)

    if args.cmd == "install-runtime":
        res = install_runtime(force=args.force)
    elif args.cmd == "configure":
        res = configure_openclaw(cfg)
    elif args.cmd == "start":
        res = cmd_start(cfg)
    elif args.cmd == "stop":
        res = cmd_stop(cfg, release_lease=True)
    elif args.cmd == "fence":
        stop_sentinel_daemon()
        res = cmd_stop(cfg, release_lease=True)
    elif args.cmd == "takeover":
        res = cmd_takeover(cfg)
    elif args.cmd == "status":
        res = cmd_status(cfg)
    elif args.cmd == "sentinel":
        if args.daemon:
            res = start_sentinel_daemon(cfg)
        else:
            run_sentinel_loop(cfg, once=args.once)
            res = {"ok": True}
    elif args.cmd == "sentinel-stop":
        res = stop_sentinel_daemon()
    elif args.cmd == "telegram-reply":
        res = send_telegram_reply(args.message, chat_id=args.chat_id)
    else:
        return 1

    print(json.dumps(res, indent=2))
    return 0 if res.get("ok", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
