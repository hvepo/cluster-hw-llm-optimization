#!/usr/bin/env python3
"""Cross-OS controller & client CLI for the Bidirectional Peer Order Bridge (:1236) and OpenClaw HA."""
import argparse
import json
import os
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


def cmd_init_token(cfg, sync_dir_override=""):
    local_cfg_path = os.path.join(REPO_ROOT, "cluster.local.json")
    if not os.path.isfile(local_cfg_path):
        ex = json.load(open(os.path.join(REPO_ROOT, "cluster.example.json"), encoding="utf-8"))
        with open(local_cfg_path, "w", encoding="utf-8") as f:
            json.dump(ex, f, indent=2)
            f.write("\n")

    raw = json.load(open(local_cfg_path, encoding="utf-8"))
    peer_sec = raw.setdefault("peer", {})
    peer_sec.setdefault("port", 1236)
    peer_sec.setdefault("default_mode", "auto")
    peer_sec.setdefault("openclaw", {
        "enabled": True,
        "primary_node": "torre",
        "responder": "antigravity",
        "agy_autonomous": True,
        "agy_cwd": "~",
        "port": 18789,
        "lease_ttl_s": 30,
        "probe_interval_s": 10,
        "failures_before_failover": 3,
    })
    peer_sec["openclaw"].setdefault("responder", "antigravity")

    sync_dir = sync_dir_override or openclaw_ctl.resolve_sync_dir(raw)
    if sync_dir:
        peer_sec["sync_dir"] = sync_dir

    tok = openclaw_ctl.resolve_peer_token(raw, sync_dir)
    if not tok:
        tok = secrets.token_hex(32)
    peer_sec["token"] = tok

    if sync_dir:
        try:
            os.makedirs(sync_dir, exist_ok=True)
            tok_file = os.path.join(sync_dir, "peer.token")
            with open(tok_file, "w", encoding="utf-8") as f:
                f.write(tok + "\n")
            try:
                os.chmod(tok_file, 0o600)
            except OSError:
                pass
        except OSError:
            pass

    # Auto-populate peer_url from each node's base_url host or local Tailscale IP
    port = int(peer_sec["port"])
    local_node = raw.get("local_node") or "torre"
    local_ts = peer_daemon.detect_local_tailscale_ip(raw)
    for nname, nd in (raw.get("nodes") or {}).items():
        cur_p = (nd.get("peer_url") or "").strip()
        if cur_p and "<" not in cur_p:
            continue
        if nname == local_node and local_ts:
            nd["peer_url"] = f"http://{local_ts}:{port}"
        else:
            burl = (nd.get("base_url") or "").strip()
            m = peer_daemon._TAILSCALE_IP_RE.search(burl)
            if m:
                nd["peer_url"] = f"http://{m.group(0)}:{port}"

    with open(local_cfg_path, "w", encoding="utf-8") as f:
        json.dump(raw, f, indent=2)
        f.write("\n")

    return {"ok": True, "local_node": local_node, "sync_dir": sync_dir, "token_set": bool(tok), "peer_urls": {k: v.get("peer_url") for k, v in (raw.get("nodes") or {}).items()}}


def cmd_start(cfg, wait_s=6.0):
    base_url, port = _local_base(cfg)
    if _is_healthy(base_url):
        return {"ok": True, "already_running": True, "url": base_url}

    daemon_script = os.path.join(PEER_DIR, "peer_daemon.py")
    log_path = _log_file()
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
    pid = None
    if os.path.isfile(_pid_file()):
        try:
            pid = int(open(_pid_file(), "r", encoding="utf-8").read().strip())
            os.kill(pid, signal.SIGTERM)
        except Exception:
            pass
        try:
            os.remove(_pid_file())
        except OSError:
            pass
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


def _write_offline_queue(sync_dir, target_node, kind, payload):
    if not sync_dir:
        raise RuntimeError("Offline Google Drive sync_dir is not configured/mounted")
    job_id = payload.get("job_id") or f"job_{int(time.time())}_{uuid.uuid4().hex[:8]}"
    inbox = os.path.join(sync_dir, "peer-queue", f"to_{target_node}")
    os.makedirs(inbox, exist_ok=True)
    record = {"job_id": job_id, "kind": kind, "queued_at": time.time(), **payload}
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
    return _write_offline_queue(sync_dir, target_node or (cfg.get("local_node") or "torre"), "order", payload)


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
    return _write_offline_queue(sync_dir, target_node or (cfg.get("local_node") or "torre"), "exec", payload)


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
            return {"ok": True, "transport": "drive_queue", "job": json.load(open(cpath, encoding="utf-8"))}
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

    unit_dir = os.path.join(os.path.expanduser("~"), ".config", "systemd", "user")
    unit_path = os.path.join(unit_dir, "cluster-peer-openclaw.service")
    unit = (
        "[Unit]\nDescription=Cluster Peer Order Daemon & OpenClaw HA\nAfter=network.target\n\n"
        f"[Service]\nType=oneshot\nRemainAfterExit=yes\nExecStart={sys.executable} {posix_ctl} autostart-boot\n\n"
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
    else:
        return 1

    print(json.dumps(res, indent=2))
    return 0 if res.get("ok", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
