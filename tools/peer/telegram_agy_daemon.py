#!/usr/bin/env python3
"""Lightweight Active-Standby Telegram -> Antigravity (`agy`) responder daemon.

Polls Telegram Bot API `getUpdates` for allowlisted users, persists per-chat
`conversation_id` across turns and HA failovers, and routes incoming messages
through an extensible dispatcher (`route_message`) defaulting to Antigravity
with full autonomous permissions and a project-independent working directory.

Local LM Studio models (`:1234` / `:1235`) are NOT invoked unless Antigravity
itself chooses to delegate a task to cluster tools/subagents.
"""
import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

PEER_DIR = os.path.dirname(os.path.abspath(__file__))
TOOLS_DIR = os.path.abspath(os.path.join(PEER_DIR, ".."))
REPO_ROOT = os.path.abspath(os.path.join(TOOLS_DIR, ".."))
for _d in (PEER_DIR, TOOLS_DIR):
    if _d not in sys.path:
        sys.path.insert(0, _d)

import cluster_lib  # noqa: E402
import openclaw_ctl  # noqa: E402
import peer_ctl  # noqa: E402


class TelegramConflictError(RuntimeError):
    """Raised when Telegram returns 409 Conflict (another getUpdates poller active)."""


def load_telegram_settings(config_path=None):
    cpath = config_path or openclaw_ctl.openclaw_config_path()
    if not os.path.isfile(cpath):
        raise FileNotFoundError(f"Telegram config not found: {cpath}")
    with open(cpath, "r", encoding="utf-8") as f:
        data = json.load(f)
    tg = (data.get("channels") or {}).get("telegram") or {}
    bot_token = str(tg.get("botToken") or "").strip()
    allow_from = [str(x).strip() for x in (tg.get("allowFrom") or []) if str(x).strip()]
    if not bot_token:
        raise RuntimeError(f"channels.telegram.botToken is missing in {cpath}")
    return {"bot_token": bot_token, "allow_from": allow_from}


def is_authorized_sender(chat_id, user_id, allow_from):
    allow_set = {str(x).strip() for x in (allow_from or []) if str(x).strip()}
    if not allow_set:
        return False
    return (str(chat_id).strip() in allow_set) or (str(user_id).strip() in allow_set)


def chunk_text(text, max_len=3900):
    raw = str(text or "").strip()
    if not raw:
        return []
    if len(raw) <= max_len:
        return [raw]

    chunks = []
    cur = []
    cur_len = 0
    for line in raw.splitlines(keepends=True):
        if len(line) > max_len:
            if cur:
                chunks.append("".join(cur).rstrip("\n"))
                cur = []
                cur_len = 0
            for i in range(0, len(line), max_len):
                part = line[i : i + max_len].strip("\n")
                if part:
                    chunks.append(part)
            continue
        if cur_len + len(line) > max_len and cur:
            chunks.append("".join(cur).rstrip("\n"))
            cur = [line]
            cur_len = len(line)
        else:
            cur.append(line)
            cur_len += len(line)
    if cur:
        tail = "".join(cur).rstrip("\n")
        if tail:
            chunks.append(tail)
    return chunks or [raw[:max_len]]


def tg_api_call(bot_token, method, payload=None, timeout=35):
    url = f"https://api.telegram.org/bot{bot_token}/{method}"
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    hdrs = {"Content-Type": "application/json"} if payload is not None else {}
    req = urllib.request.Request(url, data=data, headers=hdrs, method="POST" if payload is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        err_code = exc.code
        desc = exc.reason or f"HTTP {err_code}"
        try:
            err_body = json.loads(exc.read().decode("utf-8", "replace"))
            err_code = err_body.get("error_code", err_code)
            desc = err_body.get("description") or desc
        except Exception:
            pass
        if int(err_code) == 409:
            raise TelegramConflictError(f"Telegram {method} 409 Conflict: {desc}") from None
        raise RuntimeError(f"Telegram {method} failed ({err_code}): {desc}") from None
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Telegram {method} network error: {exc.reason}") from None
    except Exception as exc:
        raise RuntimeError(f"Telegram {method} error: {type(exc).__name__}") from None

    if not body.get("ok"):
        err_code = body.get("error_code")
        desc = body.get("description") or "unknown error"
        if err_code == 409:
            raise TelegramConflictError(f"Telegram {method} 409 Conflict: {desc}")
        raise RuntimeError(f"Telegram {method} returned ok=false ({err_code}): {desc}")
    return body.get("result")


def send_message(bot_token, chat_id, text):
    sent_ids = []
    for chunk in chunk_text(text, max_len=3900):
        res = tg_api_call(bot_token, "sendMessage", {"chat_id": str(chat_id), "text": chunk}, timeout=15)
        if isinstance(res, dict) and res.get("message_id") is not None:
            sent_ids.append(res["message_id"])
    return sent_ids


def start_typing_loop(bot_token, chat_id):
    stop_event = threading.Event()

    def _worker():
        while not stop_event.is_set():
            try:
                tg_api_call(bot_token, "sendChatAction", {"chat_id": str(chat_id), "action": "typing"}, timeout=5)
            except Exception:
                pass
            stop_event.wait(4.0)

    t = threading.Thread(target=_worker, daemon=True, name="tg-typing")
    t.start()
    return stop_event, t


class SessionStore:
    """Persists Telegram update offset and per-chat Antigravity conversation IDs."""

    def __init__(self, state_path=None, sync_dir=None):
        self.state_path = state_path or os.path.join(openclaw_ctl.state_dir(), "telegram-agy-state.json")
        self.sync_dir = sync_dir or ""
        self.shared_path = os.path.join(self.sync_dir, "openclaw-ha", "telegram-sessions.json") if self.sync_dir else ""
        self._lock = threading.Lock()
        self.data = {"last_update_id": 0, "sessions": {}}
        self._load()

    def _safe_load_json(self, path):
        if not path or not os.path.isfile(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                obj = json.load(f)
            return obj if isinstance(obj, dict) else None
        except Exception:
            return None

    def _atomic_write(self, path, obj):
        if not path:
            return
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = os.path.join(os.path.dirname(path), f".{os.path.basename(path)}.{os.getpid()}.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2)
            f.write("\n")
        os.replace(tmp, path)

    def _load(self):
        local = self._safe_load_json(self.state_path)
        if local:
            self.data["last_update_id"] = int(local.get("last_update_id") or 0)
            if isinstance(local.get("sessions"), dict):
                self.data["sessions"] = dict(local["sessions"])

        shared = self._safe_load_json(self.shared_path)
        if shared:
            shared_sessions = shared.get("sessions") if isinstance(shared.get("sessions"), dict) else shared
            if isinstance(shared_sessions, dict):
                for cid, entry in shared_sessions.items():
                    if not isinstance(entry, dict) or not entry.get("conversation_id"):
                        continue
                    cur = self.data["sessions"].get(cid)
                    if not cur or float(entry.get("updated_at") or 0.0) > float(cur.get("updated_at") or 0.0):
                        self.data["sessions"][cid] = dict(entry)

    def _save(self, sync_sessions=False):
        self._atomic_write(self.state_path, self.data)
        if sync_sessions and self.shared_path:
            try:
                self._atomic_write(self.shared_path, {"sessions": self.data["sessions"], "updated_at": time.time()})
            except OSError:
                pass

    def get_offset(self):
        with self._lock:
            return int(self.data.get("last_update_id") or 0)

    def set_offset(self, update_id):
        with self._lock:
            uid = int(update_id)
            if uid > int(self.data.get("last_update_id") or 0):
                self.data["last_update_id"] = uid
                self._save(sync_sessions=False)

    def get_conversation_id(self, chat_id):
        with self._lock:
            entry = (self.data.get("sessions") or {}).get(str(chat_id)) or {}
            return entry.get("conversation_id")

    def set_conversation_id(self, chat_id, conv_id):
        if not conv_id:
            return
        with self._lock:
            self.data.setdefault("sessions", {})[str(chat_id)] = {
                "conversation_id": str(conv_id),
                "updated_at": time.time(),
            }
            self._save(sync_sessions=True)

    def clear_conversation(self, chat_id):
        with self._lock:
            removed = self.data.setdefault("sessions", {}).pop(str(chat_id), None)
            self._save(sync_sessions=True)
            return removed is not None


def find_agy_bin():
    cands = [
        os.path.join(os.path.expanduser("~"), ".gemini", "bin", "agy"),
        shutil.which("agy") or "",
    ]
    for c in cands:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


def resolve_default_cwd(cfg=None):
    oc_cfg = ((cfg or {}).get("peer") or {}).get("openclaw") or {}
    raw = str(oc_cfg.get("agy_cwd") or "~").strip()
    if not raw or raw == "~":
        return os.path.expanduser("~")
    expanded = os.path.expanduser(raw)
    return expanded if os.path.isdir(expanded) else os.path.expanduser("~")


def build_agy_command(agy_bin, prompt, conversation_id=None, autonomous=True, model=None, mode=None):
    cmd = [agy_bin, "-p", prompt, "--output-format", "json"]
    if autonomous:
        cmd.append("--dangerously-skip-permissions")
    if mode and str(mode).strip().lower() not in ("", "auto", "default"):
        cmd.extend(["--mode", str(mode).strip()])
    if conversation_id:
        cmd.extend(["--conversation", str(conversation_id)])
    if model:
        cmd.extend(["--model", str(model)])
    return cmd


def parse_agy_output(stdout, stderr="", returncode=0):
    raw_out = (stdout or "").strip()
    raw_err = (stderr or "").strip()
    parsed = None
    if raw_out:
        try:
            obj = json.loads(raw_out)
            if isinstance(obj, dict):
                parsed = obj
        except Exception:
            for line in reversed(raw_out.splitlines()):
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict):
                        parsed = obj
                        break
                except Exception:
                    continue

    conv_id = None
    text = ""
    err_msg = ""
    status = ""
    if isinstance(parsed, dict):
        conv_id = parsed.get("conversation_id") or parsed.get("conversationId") or None
        text = str(parsed.get("response") or parsed.get("result") or parsed.get("text") or "").strip()
        err_msg = str(parsed.get("error") or "").strip()
        status = str(parsed.get("status") or "").upper()
    else:
        text = raw_out

    combined_err = f"{err_msg}\n{raw_err}".strip()
    if "authentication required" in combined_err.lower() or "authentication failed" in combined_err.lower():
        return {
            "ok": False,
            "text": "",
            "conversation_id": None,
            "error": (
                "Antigravity CLI needs a one-time OAuth login on this node. "
                "Run '~/.gemini/bin/agy -p \"hi\"' in WSL on this machine and complete the login URL."
            ),
        }

    ok = (returncode == 0) and (status != "ERROR") and not err_msg
    if not ok:
        detail = err_msg or (raw_err.splitlines()[-1] if raw_err else f"agy exited with code {returncode}")
        return {"ok": False, "text": text, "conversation_id": conv_id, "error": detail}
    return {"ok": True, "text": text or "(empty response)", "conversation_id": conv_id, "error": ""}


def build_telegram_prompt(user_text, local_node, is_new_session=True):
    if not is_new_session:
        return user_text
    return (
        f"[Telegram Bridge | Active Node: {local_node} | Context: project-independent]\n"
        "You are Antigravity, the user's primary assistant on Telegram.\n"
        "Respond directly and concisely for mobile reading. Do NOT load or call local cluster LM Studio models "
        "unless you decide delegating to cluster tools/subagents (or a sibling node via peer_ctl) clearly benefits the request.\n\n"
        f"User message:\n{user_text}"
    )


def run_antigravity_turn(
    prompt,
    conversation_id=None,
    cwd=None,
    autonomous=True,
    model=None,
    mode=None,
    timeout_s=300,
    agy_bin=None,
    local_node="torre",
):
    exe = agy_bin or find_agy_bin()
    if not exe:
        return {"ok": False, "text": "", "conversation_id": None, "error": "agy binary not found (~/.gemini/bin/agy)"}

    work_dir = cwd or os.path.expanduser("~")
    if not os.path.isdir(work_dir):
        work_dir = os.path.expanduser("~")

    framed_prompt = build_telegram_prompt(prompt, local_node=local_node, is_new_session=not bool(conversation_id))
    cmd = build_agy_command(exe, framed_prompt, conversation_id=conversation_id, autonomous=autonomous, model=model, mode=mode)
    try:
        proc = subprocess.run(
            cmd,
            cwd=work_dir,
            stdin=subprocess.DEVNULL,
            text=True,
            capture_output=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "text": "", "conversation_id": conversation_id, "error": f"Antigravity timed out after {timeout_s}s"}
    except Exception as exc:
        return {"ok": False, "text": "", "conversation_id": conversation_id, "error": str(exc)}

    res = parse_agy_output(proc.stdout, proc.stderr, proc.returncode)
    if (
        not res["ok"]
        and conversation_id
        and "oauth login" not in res["error"].lower()
        and any(k in res["error"].lower() for k in ("conversation", "session", "not found", "unknown"))
    ):
        fresh_prompt = build_telegram_prompt(prompt, local_node=local_node, is_new_session=True)
        fresh_cmd = build_agy_command(exe, fresh_prompt, conversation_id=None, autonomous=autonomous, model=model, mode=mode)
        try:
            proc2 = subprocess.run(
                fresh_cmd,
                cwd=work_dir,
                stdin=subprocess.DEVNULL,
                text=True,
                capture_output=True,
                timeout=timeout_s,
            )
            return parse_agy_output(proc2.stdout, proc2.stderr, proc2.returncode)
        except Exception:
            pass
    return res


def route_message(text, ctx=None):
    """Extensible inbound router. Defaults to Antigravity; future message/job rules hook in here."""
    cleaned = str(text or "").strip()
    if not cleaned:
        return {"action": "ignore"}

    cmd_tok = cleaned.split()[0].split("@")[0].lower()
    if cmd_tok in ("/help", "/start"):
        return {
            "action": "reply",
            "text": (
                "Antigravity Telegram Bridge is active.\n"
                "Commands:\n"
                "  /new — Start a fresh Antigravity conversation\n"
                "  /status — Show active leader, responder & peer status\n"
                "  /help — Show this help"
            ),
        }
    if cmd_tok in ("/new", "/reset"):
        return {"action": "reset", "text": "Started a fresh Antigravity session."}
    if cmd_tok == "/status":
        return {"action": "status"}
    return {"action": "antigravity", "prompt": cleaned}


def format_status_reply(cfg, store, chat_id):
    local_node = cfg.get("local_node") or "torre"
    conv_id = store.get_conversation_id(chat_id)
    conv_short = f"{conv_id[:8]}..." if conv_id else "none"
    peer_lines = []
    try:
        st = peer_ctl.collect_status(cfg)
        for nname, nd in (st.get("nodes") or {}).items():
            state = "online" if nd.get("reachable") else "offline"
            peer_lines.append(f"- {nname}: {state}")
    except Exception as exc:
        peer_lines.append(f"- status check error: {exc}")
    return (
        f"Active Leader: {local_node}\n"
        "Responder: antigravity (full permissions, cwd=~)\n"
        f"Session: {conv_short}\n"
        "Peers:\n" + "\n".join(peer_lines)
    )


def run_daemon(repo_root=REPO_ROOT, poll_timeout=25):
    cfg = cluster_lib.load_cluster_config(repo_root)
    local_node = cfg.get("local_node") or "torre"
    oc_cfg = ((cfg.get("peer") or {}).get("openclaw") or {})
    autonomous = bool(oc_cfg.get("agy_autonomous", True))
    model = (oc_cfg.get("agy_model") or "").strip() or None
    mode = (oc_cfg.get("agy_mode") or "").strip() or None
    timeout_s = int(oc_cfg.get("agy_timeout_s") or 300)
    cwd = resolve_default_cwd(cfg)
    sync_dir = openclaw_ctl.resolve_sync_dir(cfg)

    tg_cfg = load_telegram_settings()
    bot_token = tg_cfg["bot_token"]
    allow_from = tg_cfg["allow_from"]
    store = SessionStore(sync_dir=sync_dir)

    stop_event = threading.Event()

    def _on_signal(signum, _frame):
        print(f"[telegram-agy] received signal {signum}; shutting down", flush=True)
        stop_event.set()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    print(
        f"[telegram-agy] started node={local_node} cwd={cwd} autonomous={autonomous} allow_count={len(allow_from)}",
        flush=True,
    )

    while not stop_event.is_set():
        offset = store.get_offset()
        req_payload = {
            "timeout": int(poll_timeout),
            "allowed_updates": ["message"],
        }
        if offset > 0:
            req_payload["offset"] = offset + 1

        try:
            updates = tg_api_call(bot_token, "getUpdates", req_payload, timeout=int(poll_timeout) + 10) or []
        except TelegramConflictError as exc:
            print(f"[telegram-agy] {exc}; stopping poller", flush=True)
            return 49
        except Exception as exc:
            print(f"[telegram-agy] poll error: {exc}", flush=True)
            stop_event.wait(3.0)
            continue

        for upd in updates:
            if stop_event.is_set():
                break
            if not isinstance(upd, dict):
                continue
            uid = upd.get("update_id")
            if uid is not None:
                store.set_offset(uid)

            msg = upd.get("message") or {}
            chat_id = str((msg.get("chat") or {}).get("id") or "").strip()
            user_id = str((msg.get("from") or {}).get("id") or "").strip()
            if not chat_id:
                continue
            if not is_authorized_sender(chat_id, user_id, allow_from):
                print("[telegram-agy] ignored message from unauthorized sender", flush=True)
                continue

            text = msg.get("text") or msg.get("caption") or ""
            route = route_message(text, ctx={"chat_id": chat_id, "local_node": local_node})
            act = route.get("action")
            if act == "ignore":
                continue
            if act == "reply":
                try:
                    send_message(bot_token, chat_id, route["text"])
                except Exception as exc:
                    print(f"[telegram-agy] send error: {exc}", flush=True)
                continue
            if act == "reset":
                store.clear_conversation(chat_id)
                try:
                    send_message(bot_token, chat_id, route["text"])
                except Exception as exc:
                    print(f"[telegram-agy] send error: {exc}", flush=True)
                continue
            if act == "status":
                try:
                    send_message(bot_token, chat_id, format_status_reply(cfg, store, chat_id))
                except Exception as exc:
                    print(f"[telegram-agy] status send error: {exc}", flush=True)
                continue
            if act == "antigravity":
                conv_id = store.get_conversation_id(chat_id)
                t_stop, t_thread = start_typing_loop(bot_token, chat_id)
                try:
                    res = run_antigravity_turn(
                        route["prompt"],
                        conversation_id=conv_id,
                        cwd=cwd,
                        autonomous=autonomous,
                        model=model,
                        mode=mode,
                        timeout_s=timeout_s,
                        local_node=local_node,
                    )
                finally:
                    t_stop.set()
                    t_thread.join(timeout=1.0)

                if res.get("conversation_id"):
                    store.set_conversation_id(chat_id, res["conversation_id"])
                reply_text = res["text"] if res.get("ok") else f"Antigravity error: {res.get('error') or 'unknown error'}"
                try:
                    send_message(bot_token, chat_id, reply_text)
                except Exception as exc:
                    print(f"[telegram-agy] reply send error: {exc}", flush=True)

    print("[telegram-agy] stopped cleanly", flush=True)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Active-Standby Telegram -> Antigravity responder daemon")
    ap.add_argument("--repo-root", default=REPO_ROOT)
    ap.add_argument("--poll-timeout", type=int, default=25)
    args = ap.parse_args(argv)
    return run_daemon(repo_root=os.path.abspath(args.repo_root), poll_timeout=args.poll_timeout)


if __name__ == "__main__":
    raise SystemExit(main())
