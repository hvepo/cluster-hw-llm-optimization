#!/usr/bin/env python3
"""Bidirectional Peer Order & Execution Daemon (:1236 Tailscale + Google Drive Offline Queue).

Binds strictly to `127.0.0.1` and the local Tailscale IP (never `0.0.0.0`), requires a
256-bit `Bearer` token on all control/execution routes, executes Antigravity (`agy -p`)
and cross-OS (`wsl` / `windows`) commands asynchronously, and polls the shared offline
queue (`<sync_dir>/peer-queue/to_<local_node>/`).
"""
import argparse
import hashlib
import hmac
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PEER_DIR = os.path.dirname(os.path.abspath(__file__))
TOOLS_DIR = os.path.abspath(os.path.join(PEER_DIR, ".."))
REPO_ROOT = os.path.abspath(os.path.join(TOOLS_DIR, ".."))
for d in (PEER_DIR, TOOLS_DIR):
    if d not in sys.path:
        sys.path.insert(0, d)

import cluster_lib  # noqa: E402
import openclaw_ctl  # noqa: E402

_TAILSCALE_IP_RE = re.compile(r"\b100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}\b")


def detect_local_tailscale_ip(cfg=None):
    cfg = cfg or cluster_lib.load_cluster_config(REPO_ROOT)
    local_node = cfg.get("local_node") or "torre"
    purl = (((cfg.get("nodes") or {}).get(local_node) or {}).get("peer_url") or "")
    m = _TAILSCALE_IP_RE.search(purl)
    if m:
        return m.group(0)

    plat = cluster_lib.detect_platform()
    if plat == "wsl":
        ts_cands = [
            "/mnt/c/Program Files/Tailscale/tailscale.exe",
            shutil.which("tailscale") or "",
        ]
        for cand in ts_cands:
            if cand and os.path.isfile(cand):
                try:
                    out = subprocess.check_output(
                        [cand, "ip", "-4"],
                        stdin=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        text=True,
                        timeout=4,
                    ).strip()
                    m2 = _TAILSCALE_IP_RE.search(out)
                    if m2:
                        return m2.group(0)
                except Exception:
                    pass
    elif plat in ("linux", "macos"):
        ts = shutil.which("tailscale")
        if ts:
            try:
                out = subprocess.check_output([ts, "ip", "-4"], stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True, timeout=4).strip()
                m2 = _TAILSCALE_IP_RE.search(out)
                if m2:
                    return m2.group(0)
            except Exception:
                pass
    return None


def find_agy_bin():
    cands = [
        os.path.join(os.path.expanduser("~"), ".gemini", "bin", "agy"),
        shutil.which("agy") or "",
    ]
    for c in cands:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


def sign_queue_payload(payload, token):
    if not token:
        return ""
    body = {k: v for k, v in payload.items() if k != "hmac_sha256"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hmac.new(token.encode("utf-8"), canonical, hashlib.sha256).hexdigest()


def verify_queue_payload(payload, token):
    if not token:
        return True
    got = str(payload.get("hmac_sha256") or "").strip()
    if not got:
        return False
    expected = sign_queue_payload(payload, token)
    return hmac.compare_digest(got, expected)


class PeerRuntime:
    def __init__(self, repo_root=REPO_ROOT, token=None, sync_dir=None, default_mode=None):
        self.repo_root = repo_root
        self.cfg = cluster_lib.load_cluster_config(repo_root)
        self.local_node = (self.cfg.get("local_node") or "torre").strip()
        self.sync_dir = sync_dir if sync_dir is not None else openclaw_ctl.resolve_sync_dir(self.cfg)
        self.token = token if token is not None else openclaw_ctl.resolve_peer_token(self.cfg, self.sync_dir)
        self.default_mode = default_mode or ((self.cfg.get("peer") or {}).get("default_mode") or "auto")
        self.jobs_dir = os.path.join(openclaw_ctl.state_dir(), "peer-jobs")
        os.makedirs(self.jobs_dir, exist_ok=True)
        self._jobs_lock = threading.Lock()
        self._jobs = {}
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="peer-worker")
        self._stop_event = threading.Event()

    def _save_job(self, job):
        with self._jobs_lock:
            self._jobs[job["job_id"]] = dict(job)
        jpath = os.path.join(self.jobs_dir, f"{job['job_id']}.json")
        with open(jpath, "w", encoding="utf-8") as f:
            json.dump(job, f, indent=2)
        try:
            os.chmod(jpath, 0o600)
        except OSError:
            pass
        if self.sync_dir and job.get("transport") == "drive_queue" and job.get("status") in ("completed", "failed"):
            comp_dir = os.path.join(self.sync_dir, "peer-queue", "completed")
            try:
                os.makedirs(comp_dir, exist_ok=True)
                tmp = os.path.join(comp_dir, f".{job['job_id']}.tmp")
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(job, f, indent=2)
                os.replace(tmp, os.path.join(comp_dir, f"{job['job_id']}.json"))
            except OSError:
                pass

    def get_job(self, job_id):
        with self._jobs_lock:
            if job_id in self._jobs:
                return dict(self._jobs[job_id])
        jpath = os.path.join(self.jobs_dir, f"{job_id}.json")
        if os.path.isfile(jpath):
            return json.load(open(jpath, encoding="utf-8"))
        if self.sync_dir:
            cpath = os.path.join(self.sync_dir, "peer-queue", "completed", f"{job_id}.json")
            if os.path.isfile(cpath):
                return json.load(open(cpath, encoding="utf-8"))
        return None

    def list_jobs(self, limit=25):
        items = []
        if os.path.isdir(self.jobs_dir):
            files = sorted(
                (os.path.join(self.jobs_dir, fn) for fn in os.listdir(self.jobs_dir) if fn.endswith(".json")),
                key=os.path.getmtime,
                reverse=True,
            )
            for p in files[:limit]:
                try:
                    items.append(json.load(open(p, encoding="utf-8")))
                except Exception:
                    pass
        return items

    def _run_order_job(self, job):
        job["status"] = "running"
        job["started_at"] = time.time()
        self._save_job(job)

        agy_bin = find_agy_bin()
        if not agy_bin:
            job["status"] = "failed"
            job["error"] = "agy CLI binary not found on this machine (~/.gemini/bin/agy)"
            job["finished_at"] = time.time()
            self._save_job(job)
            return

        prompt = job["prompt"]
        target_os = job.get("target_os") or "auto"
        mode = job.get("mode") or self.default_mode
        raw_cwd = str(job.get("cwd") or "").strip()
        cwd = os.path.expanduser(raw_cwd) if raw_cwd else os.path.expanduser("~")
        if not os.path.isdir(cwd):
            cwd = os.path.expanduser("~")

        if target_os == "windows":
            prompt = f"[Target OS: Windows via powershell.exe -NoProfile -Command ... < /dev/null]\n{prompt}"

        cmd = [agy_bin, "-p", prompt, "--output-format", "json"]
        if mode and mode != "auto":
            cmd.extend(["--mode", mode])
        if mode == "auto":
            cmd.append("--dangerously-skip-permissions")
        if job.get("conversation_id"):
            cmd.extend(["--conversation", str(job["conversation_id"])])
        if job.get("agent"):
            cmd.extend(["--agent", str(job["agent"])])
        if job.get("model"):
            cmd.extend(["--model", str(job["model"])])

        timeout_s = int(job.get("timeout_s") or 600)
        try:
            r = subprocess.run(
                cmd,
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                text=True,
                capture_output=True,
                timeout=timeout_s,
            )
            job["returncode"] = r.returncode
            job["stderr"] = (r.stderr or "")[-8000:]
            raw_out = (r.stdout or "").strip()
            parsed = None
            if raw_out:
                try:
                    parsed = json.loads(raw_out)
                except Exception:
                    for line in reversed(raw_out.splitlines()):
                        try:
                            parsed = json.loads(line)
                            break
                        except Exception:
                            continue
            if isinstance(parsed, dict):
                job["conversation_id"] = parsed.get("conversation_id") or parsed.get("conversationId") or job.get("conversation_id")
                job["response_text"] = parsed.get("result") or parsed.get("response") or parsed.get("text") or raw_out
            else:
                job["response_text"] = raw_out
            job["status"] = "completed" if r.returncode == 0 else "failed"
        except Exception as exc:
            job["status"] = "failed"
            job["error"] = str(exc)
        finally:
            job["finished_at"] = time.time()
            self._save_job(job)

        if job.get("reply_telegram"):
            summary = f"[{self.local_node} · {job['status']}] {job.get('response_text') or job.get('error') or '(no output)'}"
            openclaw_ctl.send_telegram_reply(summary)

    def _run_exec_job(self, job):
        job["status"] = "running"
        job["started_at"] = time.time()
        self._save_job(job)

        target_os = job.get("target_os") or "auto"
        command = job["command"]
        raw_cwd = str(job.get("cwd") or "").strip()
        cwd = os.path.expanduser(raw_cwd) if raw_cwd else os.path.expanduser("~")
        if not os.path.isdir(cwd):
            cwd = os.path.expanduser("~")
        timeout_s = int(job.get("timeout_s") or 120)

        plat = cluster_lib.detect_platform()
        if target_os == "windows" and plat != "windows":
            ps_bin = cluster_lib.resolve_host_binary("powershell.exe")
            run_cmd = [ps_bin, "-NoProfile", "-Command", command]
        elif plat == "windows":
            if target_os == "wsl":
                wsl_bin = cluster_lib.resolve_host_binary("wsl.exe")
                run_cmd = [wsl_bin, "-e", "bash", "-lc", command]
            else:
                ps_bin = cluster_lib.resolve_host_binary("powershell.exe")
                run_cmd = [ps_bin, "-NoProfile", "-Command", command]
        else:
            run_cmd = ["bash", "-lc", command]

        try:
            r = subprocess.run(
                run_cmd,
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                text=True,
                capture_output=True,
                timeout=timeout_s,
            )
            job["returncode"] = r.returncode
            job["stdout"] = (r.stdout or "")[-16000:]
            job["stderr"] = (r.stderr or "")[-8000:]
            job["status"] = "completed" if r.returncode == 0 else "failed"
        except Exception as exc:
            job["status"] = "failed"
            job["error"] = str(exc)
        finally:
            job["finished_at"] = time.time()
            self._save_job(job)

    def submit_job(self, kind, payload, transport="http", wait_s=0.0):
        job_id = payload.get("job_id") or f"job_{int(time.time())}_{uuid.uuid4().hex[:8]}"
        job = {
            "job_id": job_id,
            "kind": kind,
            "node": self.local_node,
            "transport": transport,
            "status": "queued",
            "created_at": time.time(),
            **{k: v for k, v in payload.items() if k != "job_id"},
        }
        self._save_job(job)
        fn = self._run_order_job if kind == "order" else self._run_exec_job
        fut = self._executor.submit(fn, job)
        if wait_s and float(wait_s) > 0:
            try:
                fut.result(timeout=float(wait_s))
            except Exception:
                pass
            return self.get_job(job_id) or job
        return job

    def poll_offline_queue_once(self):
        if not self.sync_dir:
            return 0
        inbox = os.path.join(self.sync_dir, "peer-queue", f"to_{self.local_node}")
        if not os.path.isdir(inbox):
            return 0
        processed = 0
        for fn in sorted(os.listdir(inbox)):
            if not fn.endswith(".json") or fn.startswith("."):
                continue
            src = os.path.join(inbox, fn)
            claimed = os.path.join(inbox, f".{fn}.processing")
            try:
                os.replace(src, claimed)
            except OSError:
                continue
            try:
                payload = json.load(open(claimed, encoding="utf-8"))
                if self.token and not verify_queue_payload(payload, self.token):
                    print(f"[peer-daemon] rejected unsigned/tampered offline queue file: {fn}", file=sys.stderr)
                    continue
                payload.pop("hmac_sha256", None)
                kind = payload.pop("kind", "order")
                self.submit_job(kind, payload, transport="drive_queue", wait_s=0.0)
                processed += 1
            except Exception:
                pass
            finally:
                try:
                    os.remove(claimed)
                except OSError:
                    pass
        return processed

    def start_queue_poller(self, interval_s=3.0):
        def _loop():
            while not self._stop_event.is_set():
                try:
                    self.poll_offline_queue_once()
                except Exception:
                    pass
                self._stop_event.wait(interval_s)

        t = threading.Thread(target=_loop, daemon=True, name="peer-queue-poller")
        t.start()
        return t

    def stop(self):
        self._stop_event.set()
        self._executor.shutdown(wait=False)


class _ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def make_handler(runtime: PeerRuntime):
    class PeerHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            return

        def _send_json(self, code, obj):
            raw = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _check_auth(self):
            if not runtime.token:
                self._send_json(401, {"ok": False, "error": "Peer token not configured on this node"})
                return False
            auth = (self.headers.get("Authorization") or "").strip()
            x_tok = (self.headers.get("X-Peer-Token") or "").strip()
            got = auth[7:].strip() if auth.lower().startswith("bearer ") else x_tok
            if not got or not hmac.compare_digest(got, runtime.token):
                self._send_json(401, {"ok": False, "error": "Unauthorized"})
                return False
            return True

        MAX_BODY_BYTES = 10 * 1024 * 1024

        def _read_json(self):
            ln = int(self.headers.get("Content-Length") or 0)
            if ln <= 0:
                return {}
            if ln > self.MAX_BODY_BYTES:
                raise ValueError(f"Request body too large ({ln} bytes > {self.MAX_BODY_BYTES})")
            return json.loads(self.rfile.read(ln).decode("utf-8"))

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/health":
                self._send_json(200, {
                    "ok": True,
                    "node": runtime.local_node,
                    "platform": cluster_lib.detect_platform(),
                    "agy_available": bool(find_agy_bin()),
                })
                return
            if not self._check_auth():
                return
            if path == "/status":
                cfg = cluster_lib.load_cluster_config(runtime.repo_root)
                self._send_json(200, {
                    "ok": True,
                    "node": runtime.local_node,
                    "platform": cluster_lib.detect_platform(),
                    "agy_bin": find_agy_bin(),
                    "sync_dir": runtime.sync_dir,
                    "recent_jobs": len(runtime.list_jobs(limit=25)),
                    "openclaw": openclaw_ctl.cmd_status(cfg),
                })
                return
            if path == "/jobs":
                self._send_json(200, {"ok": True, "node": runtime.local_node, "jobs": runtime.list_jobs(limit=25)})
                return
            if path.startswith("/jobs/"):
                jid = path[len("/jobs/"):].strip("/")
                job = runtime.get_job(jid)
                if not job:
                    self._send_json(404, {"ok": False, "error": f"Job {jid!r} not found"})
                    return
                self._send_json(200, {"ok": True, "job": job})
                return
            self._send_json(404, {"ok": False, "error": f"Unknown path {path}"})

        def do_POST(self):
            if not self._check_auth():
                return
            path = self.path.split("?", 1)[0]
            try:
                body = self._read_json()
            except Exception as exc:
                self._send_json(400, {"ok": False, "error": f"Invalid JSON: {exc}"})
                return

            if path == "/orders":
                if not body.get("prompt"):
                    self._send_json(400, {"ok": False, "error": "Missing 'prompt'"})
                    return
                wait_s = float(body.pop("wait_s", 0.0) or 0.0)
                job = runtime.submit_job("order", body, transport="http", wait_s=wait_s)
                self._send_json(200, {"ok": True, **job})
                return

            if path == "/exec":
                if not body.get("command"):
                    self._send_json(400, {"ok": False, "error": "Missing 'command'"})
                    return
                wait_s = float(body.pop("wait_s", 30.0) if body.get("wait_s") is not None else 30.0)
                job = runtime.submit_job("exec", body, transport="http", wait_s=wait_s)
                self._send_json(200, {"ok": True, **job})
                return

            if path == "/openclaw/control":
                act = (body.get("action") or "status").strip().lower()
                cfg = cluster_lib.load_cluster_config(runtime.repo_root)
                if act == "status":
                    res = openclaw_ctl.cmd_status(cfg)
                elif act == "start":
                    res = openclaw_ctl.cmd_start(cfg)
                elif act == "stop":
                    res = openclaw_ctl.cmd_stop(cfg, release_lease=True)
                elif act == "fence":
                    res = openclaw_ctl.cmd_stop(cfg, release_lease=True)
                elif act == "takeover":
                    res = openclaw_ctl.cmd_takeover(cfg)
                elif act == "telegram_reply":
                    res = openclaw_ctl.send_telegram_reply(body.get("message") or "")
                else:
                    self._send_json(400, {"ok": False, "error": f"Unknown openclaw action {act!r}"})
                    return
                self._send_json(200, res)
                return

            if path == "/openclaw/sync-secrets":
                res = openclaw_ctl.merge_openclaw_secrets(body.get("secrets") or {})
                self._send_json(200, res)
                return

            self._send_json(404, {"ok": False, "error": f"Unknown path {path}"})

    return PeerHandler


def start_servers(runtime: PeerRuntime, bind_hosts, port=1236):
    handler = make_handler(runtime)
    servers = []
    seen = set()
    for host in bind_hosts:
        if not host or host in seen:
            continue
        seen.add(host)
        try:
            srv = _ReusableThreadingHTTPServer((host, int(port)), handler)
            t = threading.Thread(target=srv.serve_forever, daemon=True)
            t.start()
            servers.append(srv)
        except OSError as exc:
            print(f"[peer-daemon] warn: could not bind {host}:{port}: {exc}", file=sys.stderr)
    if not servers:
        raise RuntimeError(f"Failed to bind any peer listener on port {port} ({bind_hosts})")
    runtime.start_queue_poller()
    return servers


def main(argv=None):
    ap = argparse.ArgumentParser(description="Bidirectional Peer Order & OpenClaw Control Daemon")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--bind", action="append", default=None, help="Bind address (defaults to 127.0.0.1 + Tailscale IP)")
    ap.add_argument("--repo-root", default=REPO_ROOT)
    args = ap.parse_args(argv)

    runtime = PeerRuntime(repo_root=os.path.abspath(args.repo_root))
    port = args.port if args.port is not None else int(((runtime.cfg.get("peer") or {}).get("port") or 1236))
    if args.bind:
        bind_hosts = args.bind
    else:
        bind_hosts = ["127.0.0.1"]
        ts_ip = detect_local_tailscale_ip(runtime.cfg)
        if ts_ip:
            bind_hosts.append(ts_ip)

    servers = start_servers(runtime, bind_hosts, port=port)
    bound_desc = ", ".join(f"{s.server_address[0]}:{s.server_address[1]}" for s in servers)
    print(f"[peer-daemon] listening on {bound_desc} (node={runtime.local_node})", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        runtime.stop()
        for s in servers:
            s.shutdown()
            s.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
