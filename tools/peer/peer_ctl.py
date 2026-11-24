#!/usr/bin/env python3
"""Cross-OS controller & client CLI for the Bidirectional Peer Order Bridge (:1236) and OpenClaw HA."""
import argparse
import base64
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

PEER_DIR = os.path.dirname(os.path.abspath(__file__))
TOOLS_DIR = os.path.abspath(os.path.join(PEER_DIR, ".."))
REPO_ROOT = os.path.abspath(os.path.join(TOOLS_DIR, ".."))
for d in (PEER_DIR, TOOLS_DIR):
    if d not in sys.path:
        sys.path.insert(0, d)

import cluster_lib  # noqa: E402
import openclaw_ctl  # noqa: E402
import peer_daemon  # noqa: E402


def _pid_file():
    return os.path.join(openclaw_ctl.state_dir(), "peer-daemon.pid")


def _log_file():
    return os.path.join(openclaw_ctl.state_dir(), "peer-daemon.log")


def _local_base(cfg):
    port = int(((cfg.get("peer") or {}).get("port") or 1236))
    return f"http://127.0.0.1:{port}", port


def _node_base_url(cfg, target_node=None):
    local_node = cfg.get("local_node") or "torre"
    target = (target_node or local_node).strip()
    if target == local_node:
        url, _ = _local_base(cfg)
        return url, target
    nd = (cfg.get("nodes") or {}).get(target) or {}
    purl = (nd.get("peer_url") or "").strip().rstrip("/")
    if not purl or "<" in purl:
        raise ValueError(f"No peer_url configured for node {target!r} in cluster.local.json")
    return purl, target


def _http_json(url, token=None, method="GET", body=None, timeout=5.0):
    data = None if body is None else json.dumps(body).encode("utf-8")
    hdrs = {}
    if token:
        hdrs["Authorization"] = f"Bearer {token}"
    if body is not None:
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _is_healthy(base_url):
    try:
        d = _http_json(base_url.rstrip("/") + "/health", timeout=1.5)
        return bool(d.get("ok"))
    except Exception:
        return False


def _discover_ssh_and_tailscale_peers(cfg=None):
    """Parse ~/.ssh/config and tailscale status to auto-populate peer_url and ssh_target."""
    alias_map = {
        "rpi": "raspberrypi",
        "raspberrypi": "raspberrypi",
        "arduq": "arduq",
        "torre": "torre",
        "portatil": "portatil",
    }
    custom_aliases = ((cfg or {}).get("peer") or {}).get("host_aliases") or {}
    if isinstance(custom_aliases, dict):
        for k, v in custom_aliases.items():
            if k and v:
                alias_map[str(k).strip().lower()] = str(v).strip()
    discovered = {}
    ssh_cfg = os.path.join(os.path.expanduser("~"), ".ssh", "config")
    if os.path.isfile(ssh_cfg):
        cur_hosts = []
        cur_ip = ""
        cur_user = ""
        def _flush():
            for h in cur_hosts:
                node = alias_map.get(h.lower())
                if node and cur_ip:
                    entry = discovered.setdefault(node, {})
                    if peer_daemon._TAILSCALE_IP_RE.fullmatch(cur_ip):
                        entry["ip"] = cur_ip
                    else:
                        entry["host"] = cur_ip
                    if cur_user:
                        entry["user"] = cur_user
                        entry["ssh_target"] = f"{cur_user}@{cur_ip}"
        with open(ssh_cfg, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                s = line.strip()
                if not s or s.startswith("#"):
                    continue
                parts = s.split(None, 1)
                if len(parts) < 2:
                    continue
                k, v = parts[0].lower(), parts[1].strip()
                if k == "host":
                    _flush()
                    cur_hosts = [x for x in v.split() if "*" not in x]
                    cur_ip = ""
                    cur_user = ""
                elif k == "hostname":
                    cur_ip = v
                elif k == "user":
                    cur_user = v
            _flush()

    for cmd in (["tailscale", "status", "--json"], ["tailscale.exe", "status", "--json"]):
        try:
            r = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=3)
            if r.returncode == 0 and r.stdout.strip():
                ts_data = json.loads(r.stdout)
                peers = list((ts_data.get("Peer") or {}).values())
                if ts_data.get("Self"):
                    peers.append(ts_data["Self"])
                for p in peers:
                    hname = str(p.get("HostName") or "").strip().lower()
                    dns = str(p.get("DNSName") or "").split(".")[0].strip().lower()
                    node = alias_map.get(hname) or alias_map.get(dns)
                    ips = [ip for ip in (p.get("TailscaleIPs") or []) if peer_daemon._TAILSCALE_IP_RE.fullmatch(str(ip))]
                    if node and ips:
                        entry = discovered.setdefault(node, {})
                        entry["ip"] = ips[0]
                        if entry.get("user"):
                            entry["ssh_target"] = f"{entry['user']}@{ips[0]}"
                break
        except Exception:
            continue
    return discovered


def cmd_init_token(cfg, sync_dir_override=""):
    ex_path = os.path.join(REPO_ROOT, "cluster.example.json")
    ex = {}
    if os.path.isfile(ex_path):
        with open(ex_path, encoding="utf-8") as f:
            ex = json.load(f)
    local_cfg_path = os.path.join(REPO_ROOT, "cluster.local.json")
    if not os.path.isfile(local_cfg_path):
        with open(local_cfg_path, "w", encoding="utf-8") as f:
            json.dump(ex, f, indent=2)
            f.write("\n")

    with open(local_cfg_path, encoding="utf-8") as f:
        raw = json.load(f)
    peer_sec = raw.setdefault("peer", {})
    peer_sec.setdefault("port", 1236)
    peer_sec.setdefault("default_mode", "auto")
    peer_sec.setdefault("openclaw", {
        "enabled": True,
        "primary_node": "torre",
        "witness_node": "raspberrypi",
        "responder": "antigravity",
        "agy_autonomous": True,
        "agy_cwd": "~",
        "port": 18789,
        "lease_ttl_s": 30,
        "probe_interval_s": 10,
        "failures_before_failover": 3,
    })
    peer_sec["openclaw"].setdefault("responder", "antigravity")
    peer_sec["openclaw"].setdefault("witness_node", "raspberrypi")

    # Merge any new node skeletons from cluster.example.json (e.g. raspberrypi, arduq)
    nodes_sec = raw.setdefault("nodes", {})
    for nname, ex_nd in (ex.get("nodes") or {}).items():
        if nname not in nodes_sec:
            nodes_sec[nname] = json.loads(json.dumps(ex_nd))

    sync_dir = sync_dir_override or openclaw_ctl.resolve_sync_dir(raw)
    if sync_dir:
        peer_sec["sync_dir"] = sync_dir

    tok = openclaw_ctl.resolve_peer_token(raw, sync_dir)
    if not tok:
        tok = secrets.token_hex(32)
    peer_sec["token"] = tok

    gw_sec = raw.setdefault("gateway", {})
    gw_tok = str(gw_sec.get("token") or "").strip()
    if not gw_tok or "<" in gw_tok:
        gw_sec["token"] = secrets.token_hex(32)

    # Security Decision 2: Never write plaintext peer.token to Google Drive sync_dir.
    # Token lives exclusively in 0600 gitignored cluster.local.json.

    # Auto-populate peer_url and ssh_target from base_url, ~/.ssh/config, or Tailscale status
    port = int(peer_sec["port"])
    local_node = raw.get("local_node") or "torre"
    local_ts = peer_daemon.detect_local_tailscale_ip(raw)
    discovered = _discover_ssh_and_tailscale_peers(raw)
    for nname, nd in nodes_sec.items():
        disc = discovered.get(nname) or {}
        cur_ssh = (nd.get("ssh_target") or "").strip()
        if (not cur_ssh or "<" in cur_ssh or "@" in cur_ssh and not peer_daemon._TAILSCALE_IP_RE.search(cur_ssh)) and disc.get("ssh_target"):
            nd["ssh_target"] = disc["ssh_target"]
        cur_p = (nd.get("peer_url") or "").strip()
        if cur_p and "<" not in cur_p and peer_daemon._TAILSCALE_IP_RE.search(cur_p):
            continue
        if nname == local_node and local_ts:
            nd["peer_url"] = f"http://{local_ts}:{port}"
        elif disc.get("ip"):
            nd["peer_url"] = f"http://{disc['ip']}:{port}"
        else:
            burl = (nd.get("base_url") or "").strip()
            m = peer_daemon._TAILSCALE_IP_RE.search(burl)
            if m:
                nd["peer_url"] = f"http://{m.group(0)}:{port}"

    tmp_cfg = f"{local_cfg_path}.{os.getpid()}.tmp"
    with open(tmp_cfg, "w", encoding="utf-8") as f:
        json.dump(raw, f, indent=2)
        f.write("\n")
    try:
        os.chmod(tmp_cfg, 0o600)
    except OSError:
        pass
    os.replace(tmp_cfg, local_cfg_path)
    try:
        os.chmod(local_cfg_path, 0o600)
    except OSError:
        pass

    return {"ok": True, "local_node": local_node, "sync_dir": sync_dir, "token_set": bool(tok), "peer_urls": {k: v.get("peer_url") for k, v in nodes_sec.items()}}


def cmd_start(cfg, wait_s=6.0):
    base_url, port = _local_base(cfg)
    if _is_healthy(base_url):
        return {"ok": True, "already_running": True, "url": base_url}

    daemon_script = os.path.join(PEER_DIR, "peer_daemon.py")
    log_path = _log_file()
    cluster_lib.rotate_log_if_needed(log_path)
    with open(log_path, "a", encoding="utf-8") as lf:
        proc = subprocess.Popen(
            [sys.executable, daemon_script, "--port", str(port), "--repo-root", REPO_ROOT],
            stdin=subprocess.DEVNULL,
            stdout=lf,
            stderr=lf,
            start_new_session=True,
        )
    with open(_pid_file(), "w", encoding="utf-8") as f:
        f.write(str(proc.pid))

    deadline = time.time() + wait_s
    while time.time() < deadline:
        if _is_healthy(base_url):
            return {"ok": True, "pid": proc.pid, "url": base_url, "log": log_path}
        time.sleep(0.25)
    return {"ok": False, "error": f"peer_daemon did not become healthy within {wait_s}s (see {log_path})"}


def cmd_stop(cfg):
    base_url, _ = _local_base(cfg)
    pid = None
    if os.path.isfile(_pid_file()):
        try:
            with open(_pid_file(), "r", encoding="utf-8") as f:
                pid = int(f.read().strip())
            os.kill(pid, signal.SIGTERM)
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                try:
                    os.kill(pid, 0)
                except OSError:
                    break
                time.sleep(0.15)
            try:
                os.kill(pid, 0)
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        except Exception:
            pass
        try:
            os.remove(_pid_file())
        except OSError:
            pass
    deadline_port = time.monotonic() + 2.0
    while time.monotonic() < deadline_port and _is_healthy(base_url):
        time.sleep(0.15)
    return {"ok": True, "stopped_pid": pid}


def collect_status(cfg, node_filter=None):
    token = openclaw_ctl.resolve_peer_token(cfg)
    sync_dir = openclaw_ctl.resolve_sync_dir(cfg)
    local_node = cfg.get("local_node") or "torre"
    out = {"local_node": local_node, "sync_dir": sync_dir, "token_configured": bool(token), "nodes": {}}

    for nname in (cfg.get("nodes") or {}):
        if node_filter and nname != node_filter:
            continue
        try:
            url, _ = _node_base_url(cfg, nname)
        except ValueError as exc:
            out["nodes"][nname] = {"reachable": False, "error": str(exc)}
            continue
        try:
            if token:
                st = _http_json(url + "/status", token=token, timeout=2.5)
                out["nodes"][nname] = {"reachable": True, "url": url, **st}
            else:
                hl = _http_json(url + "/health", timeout=2.0)
                out["nodes"][nname] = {"reachable": True, "url": url, **hl}
        except Exception as exc:
            out["nodes"][nname] = {"reachable": False, "url": url, "error": str(exc)}

        if sync_dir:
            qdir = os.path.join(sync_dir, "peer-queue", f"to_{nname}")
            pending = len([f for f in os.listdir(qdir) if f.endswith(".json") and not f.startswith(".")]) if os.path.isdir(qdir) else 0
            out["nodes"][nname]["offline_queue_pending"] = pending
    return out


def _write_offline_queue(sync_dir, target_node, kind, payload, token=None):
    if not sync_dir:
        raise RuntimeError("Offline Google Drive sync_dir is not configured/mounted")
    job_id = payload.get("job_id") or f"job_{int(time.time())}_{uuid.uuid4().hex[:8]}"
    inbox = os.path.join(sync_dir, "peer-queue", f"to_{target_node}")
    os.makedirs(inbox, exist_ok=True)
    record = {"job_id": job_id, "kind": kind, "queued_at": time.time(), **payload}
    if token:
        record["hmac_sha256"] = peer_daemon.sign_queue_payload(record, token)
    tmp = os.path.join(inbox, f".{job_id}.tmp")
    dst = os.path.join(inbox, f"{job_id}.json")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(record, f, indent=2)
    os.replace(tmp, dst)
    return {"ok": True, "job_id": job_id, "node": target_node, "status": "queued_offline", "transport": "drive_queue", "queue_file": dst}


def send_order(cfg, target_node, prompt, target_os="auto", mode="auto", agent=None, model=None, conversation_id=None, cwd=None, wait_s=0.0, queue_if_offline=True, reply_telegram=False):
    token = openclaw_ctl.resolve_peer_token(cfg)
    sync_dir = openclaw_ctl.resolve_sync_dir(cfg)
    payload = {
        "prompt": prompt,
        "target_os": target_os,
        "mode": mode or ((cfg.get("peer") or {}).get("default_mode") or "auto"),
        "reply_telegram": bool(reply_telegram),
    }
    if agent:
        payload["agent"] = agent
    if model:
        payload["model"] = model
    if conversation_id:
        payload["conversation_id"] = conversation_id
    if cwd:
        payload["cwd"] = cwd
    if wait_s:
        payload["wait_s"] = float(wait_s)

    try:
        url, resolved = _node_base_url(cfg, target_node)
        if token:
            http_timeout = max(10.0, float(wait_s or 0.0) + 10.0)
            return _http_json(url + "/orders", token=token, method="POST", body=payload, timeout=http_timeout)
    except Exception as exc:
        if not queue_if_offline:
            return {"ok": False, "node": target_node, "error": str(exc)}
    return _write_offline_queue(sync_dir, target_node or (cfg.get("local_node") or "torre"), "order", payload, token=token)


def exec_command(cfg, target_node, command, target_os="auto", cwd=None, wait_s=30.0, timeout_s=120, queue_if_offline=True):
    token = openclaw_ctl.resolve_peer_token(cfg)
    sync_dir = openclaw_ctl.resolve_sync_dir(cfg)
    payload = {
        "command": command,
        "target_os": target_os,
        "timeout_s": int(timeout_s),
        "wait_s": float(wait_s),
    }
    if cwd:
        payload["cwd"] = cwd
    try:
        url, _ = _node_base_url(cfg, target_node)
        if token:
            return _http_json(url + "/exec", token=token, method="POST", body=payload, timeout=max(10.0, float(wait_s) + 10.0))
    except Exception as exc:
        if not queue_if_offline:
            return {"ok": False, "node": target_node, "error": str(exc)}
    return _write_offline_queue(sync_dir, target_node or (cfg.get("local_node") or "torre"), "exec", payload, token=token)


def get_job_status(cfg, job_id, target_node=None):
    token = openclaw_ctl.resolve_peer_token(cfg)
    sync_dir = openclaw_ctl.resolve_sync_dir(cfg)
    try:
        url, _ = _node_base_url(cfg, target_node)
        if token:
            return _http_json(url + f"/jobs/{job_id}", token=token, timeout=5.0)
    except Exception:
        pass
    if sync_dir:
        cpath = os.path.join(sync_dir, "peer-queue", "completed", f"{job_id}.json")
        if os.path.isfile(cpath):
            with open(cpath, encoding="utf-8") as f:
                return {"ok": True, "transport": "drive_queue", "job": json.load(f)}
    return {"ok": False, "error": f"Job {job_id!r} not found"}


def remote_openclaw_control(cfg, action, target_node=None, message=None):
    local_node = cfg.get("local_node") or "torre"
    target = (target_node or local_node).strip()
    if action == "sync_secrets":
        if target == local_node:
            raise ValueError("Specify target sibling node ('portatil' or 'torre') when action='sync_secrets'")
        return openclaw_ctl.cmd_sync_secrets(cfg, target)
    if target == local_node:
        if action == "status":
            return openclaw_ctl.cmd_status(cfg)
        if action == "start":
            return openclaw_ctl.cmd_start(cfg)
        if action == "stop":
            return openclaw_ctl.cmd_stop(cfg, release_lease=True)
        if action == "takeover":
            return openclaw_ctl.cmd_takeover(cfg)
        if action == "telegram_reply":
            return openclaw_ctl.send_telegram_reply(message or "")
        raise ValueError(f"Unknown openclaw action {action!r}")

    token = openclaw_ctl.resolve_peer_token(cfg)
    url, _ = _node_base_url(cfg, target)
    return _http_json(
        url + "/openclaw/control",
        token=token,
        method="POST",
        body={"action": action, "message": message or ""},
        timeout=15.0,
    )


def _windows_startup_vbs_path():
    plat = cluster_lib.detect_platform()
    if plat == "windows":
        app = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Roaming")
    elif plat == "wsl":
        wh = cluster_lib._win_userprofile_from_wsl()
        if not wh:
            return None
        app = os.path.join(wh, "AppData", "Roaming")
    else:
        return None
    return os.path.join(app, "Microsoft", "Windows", "Start Menu", "Programs", "Startup", "cluster-peer-openclaw.vbs")


def cmd_install_autostart(dry_run=False):
    vbs_path = _windows_startup_vbs_path()
    posix_ctl = cluster_lib.normalize_local_path(os.path.abspath(__file__), "wsl")
    if vbs_path:
        cmd_str = f'wsl.exe -e python3 "{posix_ctl}" autostart-boot'
        escaped = cmd_str.replace('"', '""')
        vbs = 'Set WshShell = CreateObject("WScript.Shell")\n' + f'WshShell.Run "{escaped}", 0, False\n'
        print(f"[peer-autostart] write {vbs_path}")
        if not dry_run:
            os.makedirs(os.path.dirname(vbs_path), exist_ok=True)
            with open(vbs_path, "w", encoding="utf-8") as f:
                f.write(vbs)
        return {"ok": True, "vbs": vbs_path}

    cfg = cluster_lib.load_cluster_config(REPO_ROOT)
    port = int(((cfg.get("peer") or {}).get("port") or 1236))
    posix_daemon = cluster_lib.normalize_local_path(os.path.join(PEER_DIR, "peer_daemon.py"), "wsl")
    posix_root = cluster_lib.normalize_local_path(REPO_ROOT, "wsl")
    unit_dir = os.path.join(os.path.expanduser("~"), ".config", "systemd", "user")
    unit_path = os.path.join(unit_dir, "cluster-peer-openclaw.service")
    unit = (
        "[Unit]\nDescription=Cluster Peer Order Daemon & OpenClaw HA\nAfter=network.target\n\n"
        f"[Service]\nType=simple\nRestart=always\nRestartSec=3\nExecStart={sys.executable} {posix_daemon} --port {port} --repo-root {posix_root}\n\n"
        "[Install]\nWantedBy=default.target\n"
    )
    print(f"[peer-autostart] write {unit_path}")
    if not dry_run:
        os.makedirs(unit_dir, exist_ok=True)
        with open(unit_path, "w", encoding="utf-8") as f:
            f.write(unit)
    return {"ok": True, "unit": unit_path}


def cmd_uninstall_autostart(dry_run=False):
    vbs_path = _windows_startup_vbs_path()
    if vbs_path and os.path.isfile(vbs_path):
        print(f"[peer-autostart] remove {vbs_path}")
        if not dry_run:
            os.remove(vbs_path)
    unit_path = os.path.join(os.path.expanduser("~"), ".config", "systemd", "user", "cluster-peer-openclaw.service")
    if os.path.isfile(unit_path):
        print(f"[peer-autostart] remove {unit_path}")
        if not dry_run:
            os.remove(unit_path)
    return {"ok": True}


def cmd_autostart_boot(cfg):
    p_res = cmd_start(cfg)
    s_res = {"ok": True, "skipped": True}
    if openclaw_ctl.can_start_responder(cfg):
        s_res = openclaw_ctl.start_sentinel_daemon(cfg)
    return {"ok": True, "peer": p_res, "sentinel": s_res}


def deploy_edge_node(cfg, target_node):
    """Push zero-dependency peer daemon bundle over SSH to a Linux edge node (raspberrypi / arduq)."""
    nodes = cfg.get("nodes") or {}
    if target_node == "all":
        targets = [n for n, nd in nodes.items() if str(nd.get("role") or "").startswith("edge-")]
        results = {t: deploy_edge_node(cfg, t) for t in targets}
        return {"ok": all(r.get("ok") for r in results.values()), "nodes": results}

    nd = nodes.get(target_node) or {}
    if not nd:
        return {"ok": False, "node": target_node, "error": f"Unknown node {target_node!r}"}
    ssh_target = (nd.get("ssh_target") or "").strip()
    if not ssh_target or "<" in ssh_target:
        disc = _discover_ssh_and_tailscale_peers(cfg).get(target_node) or {}
        ssh_target = disc.get("ssh_target") or target_node

    tok = openclaw_ctl.resolve_peer_token(cfg)
    if not tok:
        return {"ok": False, "node": target_node, "error": "Peer token not configured; run init-token first"}

    port = int(((cfg.get("peer") or {}).get("port") or 1236))
    edge_cfg = {
        "schema_version": 1,
        "cluster_name": cfg.get("cluster_name") or "cluster-hw-llm-optimization",
        "local_node": target_node,
        "peer": {
            "port": port,
            "token": tok,
            "sync_dir": "",
            "default_mode": "auto",
            "openclaw": {
                "enabled": False,
                "primary_node": "torre",
                "witness_node": "raspberrypi",
                "responder": "antigravity",
            },
        },
        "nodes": nodes,
    }

    def _read_txt(p):
        with open(p, encoding="utf-8") as f:
            return f.read()

    files_payload = {
        "cluster.example.json": _read_txt(os.path.join(REPO_ROOT, "cluster.example.json")),
        "cluster.local.json": json.dumps(edge_cfg, indent=2) + "\n",
        "tools/cluster_lib.py": _read_txt(os.path.join(TOOLS_DIR, "cluster_lib.py")),
        "tools/peer/openclaw_ctl.py": _read_txt(os.path.join(PEER_DIR, "openclaw_ctl.py")),
        "tools/peer/peer_daemon.py": _read_txt(os.path.join(PEER_DIR, "peer_daemon.py")),
        "tools/peer/peer_ctl.py": _read_txt(os.path.join(PEER_DIR, "peer_ctl.py")),
    }

    remote_py = (
        "import json, os, subprocess, sys\n"
        "payload = json.loads(sys.stdin.read())\n"
        "root = os.path.expanduser('~/.cluster-peer')\n"
        "for rel, content in payload.items():\n"
        "    p = os.path.join(root, rel)\n"
        "    os.makedirs(os.path.dirname(p), exist_ok=True)\n"
        "    with open(p, 'w', encoding='utf-8') as f:\n"
        "        f.write(content)\n"
        "    if p.endswith('.py'):\n"
        "        os.chmod(p, 0o755)\n"
        "    elif p.endswith('.json'):\n"
        "        os.chmod(p, 0o600)\n"
        "# Retire legacy :1239 cluster_bridge.py if running\n"
        "subprocess.run(['pkill', '-f', 'cluster_bridge.py'], stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "ctl = os.path.join(root, 'tools', 'peer', 'peer_ctl.py')\n"
        "r1 = subprocess.run([sys.executable, ctl, 'restart'], stdin=subprocess.DEVNULL, capture_output=True, text=True)\n"
        "subprocess.run([sys.executable, ctl, 'install-autostart'], stdin=subprocess.DEVNULL, capture_output=True, text=True)\n"
        "subprocess.run(['systemctl', '--user', 'daemon-reload'], stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "subprocess.run(['systemctl', '--user', 'enable', 'cluster-peer-openclaw.service'], stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "# Ensure @reboot crontab fallback for embedded Linux without systemd user session lingering\n"
        "cron_line = f'@reboot /usr/bin/python3 {ctl} start >/dev/null 2>&1'\n"
        "cr = subprocess.run(['crontab', '-l'], stdin=subprocess.DEVNULL, capture_output=True, text=True)\n"
        "lines = [ln for ln in (cr.stdout or '').splitlines() if 'cluster_bridge.py' not in ln and ctl not in ln]\n"
        "lines.append(cron_line)\n"
        "subprocess.run(['crontab', '-'], input='\\n'.join(lines) + '\\n', text=True, capture_output=True)\n"
        "print(r1.stdout.strip() or json.dumps({'ok': r1.returncode == 0, 'stderr': r1.stderr.strip()}))\n"
    )

    script_b64 = base64.b64encode(remote_py.encode("utf-8")).decode("ascii")
    remote_cmd = f"python3 -c \"import base64; exec(base64.b64decode('{script_b64}').decode('utf-8'))\""

    try:
        r = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", ssh_target, remote_cmd],
            input=json.dumps(files_payload),
            text=True,
            capture_output=True,
            timeout=30,
        )
    except Exception as exc:
        return {"ok": False, "node": target_node, "ssh_target": ssh_target, "error": str(exc)}

    if r.returncode != 0:
        return {"ok": False, "node": target_node, "ssh_target": ssh_target, "error": (r.stderr or r.stdout).strip()}

    remote_res = {}
    try:
        remote_res = json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:
        remote_res = {"raw": r.stdout.strip()}

    st = collect_status(cfg, node_filter=target_node)
    node_st = (st.get("nodes") or {}).get(target_node) or {}
    return {
        "ok": bool(node_st.get("reachable")),
        "node": target_node,
        "ssh_target": ssh_target,
        "remote": remote_res,
        "status": node_st,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description="Bidirectional Peer Bridge & OpenClaw controller")
    sub = ap.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("init-token")
    pi.add_argument("--sync-dir", default="")
    sub.add_parser("start")
    sub.add_parser("stop")
    sub.add_parser("restart")
    ps = sub.add_parser("status")
    ps.add_argument("--node", default=None)

    psend = sub.add_parser("send")
    psend.add_argument("prompt")
    psend.add_argument("--to", required=True, help="Target node: torre or portatil")
    psend.add_argument("--os", dest="target_os", choices=("auto", "wsl", "windows"), default="auto")
    psend.add_argument("--mode", choices=("auto", "accept-edits", "plan"), default="auto")
    psend.add_argument("--conversation", default=None)
    psend.add_argument("--agent", default=None)
    psend.add_argument("--model", default=None)
    psend.add_argument("--cwd", default=None)
    psend.add_argument("--wait", type=float, default=0.0)
    psend.add_argument("--no-queue", action="store_true")
    psend.add_argument("--reply-telegram", action="store_true")

    pexec = sub.add_parser("exec")
    pexec.add_argument("command")
    pexec.add_argument("--to", required=True)
    pexec.add_argument("--os", dest="target_os", choices=("auto", "wsl", "windows"), default="auto")
    pexec.add_argument("--cwd", default=None)
    pexec.add_argument("--wait", type=float, default=30.0)
    pexec.add_argument("--timeout", type=int, default=120)
    pexec.add_argument("--no-queue", action="store_true")

    pjob = sub.add_parser("job")
    pjob.add_argument("job_id")
    pjob.add_argument("--node", default=None)

    pjobs = sub.add_parser("jobs")
    pjobs.add_argument("--node", default=None)

    pia = sub.add_parser("install-autostart")
    pia.add_argument("--dry-run", action="store_true")
    pua = sub.add_parser("uninstall-autostart")
    pua.add_argument("--dry-run", action="store_true")
    sub.add_parser("autostart-boot")
    pde = sub.add_parser("deploy-edge")
    pde.add_argument("--to", default="all", help="Edge node name (raspberrypi, arduq, or all)")

    args = ap.parse_args(argv)
    cfg = cluster_lib.load_cluster_config(REPO_ROOT)

    if args.cmd == "init-token":
        res = cmd_init_token(cfg, sync_dir_override=args.sync_dir)
    elif args.cmd == "start":
        res = cmd_start(cfg)
    elif args.cmd == "stop":
        res = cmd_stop(cfg)
    elif args.cmd == "restart":
        cmd_stop(cfg)
        res = cmd_start(cfg)
    elif args.cmd == "status":
        res = collect_status(cfg, node_filter=args.node)
    elif args.cmd == "send":
        res = send_order(
            cfg,
            target_node=args.to,
            prompt=args.prompt,
            target_os=args.target_os,
            mode=args.mode,
            agent=args.agent,
            model=args.model,
            conversation_id=args.conversation,
            cwd=args.cwd,
            wait_s=args.wait,
            queue_if_offline=not args.no_queue,
            reply_telegram=args.reply_telegram,
        )
    elif args.cmd == "exec":
        res = exec_command(
            cfg,
            target_node=args.to,
            command=args.command,
            target_os=args.target_os,
            cwd=args.cwd,
            wait_s=args.wait,
            timeout_s=args.timeout,
            queue_if_offline=not args.no_queue,
        )
    elif args.cmd == "job":
        res = get_job_status(cfg, args.job_id, target_node=args.node)
    elif args.cmd == "jobs":
        token = openclaw_ctl.resolve_peer_token(cfg)
        url, _ = _node_base_url(cfg, args.node)
        res = _http_json(url + "/jobs", token=token, timeout=5.0)
    elif args.cmd == "install-autostart":
        res = cmd_install_autostart(dry_run=args.dry_run)
    elif args.cmd == "uninstall-autostart":
        res = cmd_uninstall_autostart(dry_run=args.dry_run)
    elif args.cmd == "autostart-boot":
        res = cmd_autostart_boot(cfg)
    elif args.cmd == "deploy-edge":
        res = deploy_edge_node(cfg, target_node=args.to)
    else:
        return 1

    print(json.dumps(res, indent=2))
    return 0 if res.get("ok", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
