#!/usr/bin/env python3
"""Cross-OS controller for Cluster Router Gateway (start|stop|restart|status|install-autostart|uninstall-autostart|check-lmlink)."""
import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request

GATEWAY_DIR = os.path.dirname(os.path.abspath(__file__))
TOOLS_DIR = os.path.abspath(os.path.join(GATEWAY_DIR, ".."))
REPO_ROOT = os.path.abspath(os.path.join(TOOLS_DIR, ".."))
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

import cluster_lib  # noqa: E402


def _state_dir():
    plat = cluster_lib.detect_platform()
    if plat == "windows":
        base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
    else:
        base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    d = os.path.join(base, "cluster-llm")
    os.makedirs(d, exist_ok=True)
    return d


def _pid_file():
    return os.path.join(_state_dir(), "gateway.pid")


def _log_file():
    return os.path.join(_state_dir(), "gateway.log")


def _gateway_base(cfg):
    gw = cfg.get("gateway") or {}
    host = os.environ.get("CLUSTER_GATEWAY_HOST") or gw.get("host") or "127.0.0.1"
    port = int(os.environ.get("CLUSTER_GATEWAY_PORT") or gw.get("port") or 1235)
    return f"http://{host}:{port}", host, port


def _http_json(url, timeout=2.0):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _is_healthy(base_url):
    try:
        d = _http_json(base_url + "/health", timeout=1.5)
        return bool(d.get("ok"))
    except Exception:
        return False


def _stop_windows_gateway_if_any():
    plat = cluster_lib.detect_platform()
    if plat not in ("windows", "wsl"):
        return
    ps_bin = cluster_lib.resolve_host_binary("powershell.exe")
    ps = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.CommandLine -match 'cluster_gateway\\.py' } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
    )
    try:
        subprocess.run([ps_bin, "-NoProfile", "-Command", ps], stdin=subprocess.DEVNULL, capture_output=True, timeout=10)
    except Exception:
        pass


def cmd_start(cfg, wait_s=8.0):
    base_url, host, port = _gateway_base(cfg)
    if _is_healthy(base_url):
        print(f"[gateway] already running at {base_url}")
        return 0

    cluster_lib.ensure_gateway_token(REPO_ROOT)
    plat = cluster_lib.detect_platform()
    gw_script = os.path.join(GATEWAY_DIR, "cluster_gateway.py")
    log_path = _log_file()
    cluster_lib.rotate_log_if_needed(log_path)
    pid_path = _pid_file()

    # On Windows+WSL mirrored networking, prefer launching Windows pythonw.exe if installed so
    # the gateway stays alive across WSL terminal restarts and serves both OSes via 127.0.0.1.
    launched_win = False
    if plat in ("windows", "wsl"):
        win_py = cluster_lib.resolve_win_python(cfg)
        win_pyw = None
        if win_py and win_py.lower().endswith("python.exe"):
            cand = win_py[:-10] + "pythonw.exe"
            if os.path.isfile(cand):
                win_pyw = cand
        if plat == "windows" and (win_pyw or (win_py and os.path.isfile(win_py))):
            exe = win_pyw or win_py
            win_script = cluster_lib.normalize_local_path(gw_script, "windows")
            win_root = cluster_lib.normalize_local_path(REPO_ROOT, "windows")
            flags = getattr(subprocess, "DETACHED_PROCESS", 0x00000008) | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            with open(log_path, "a", encoding="utf-8") as lf:
                p = subprocess.Popen(
                    [exe, win_script, "--host", host, "--port", str(port), "--repo-root", win_root],
                    stdin=subprocess.DEVNULL,
                    stdout=lf,
                    stderr=lf,
                    creationflags=flags,
                    close_fds=True,
                )
            with open(pid_path, "w", encoding="utf-8") as f:
                f.write(str(p.pid))
            launched_win = True
        elif plat == "wsl" and win_pyw and os.path.isfile(win_pyw):
            ps_bin = cluster_lib.resolve_host_binary("powershell.exe")
            win_exe = cluster_lib.normalize_local_path(win_pyw, "windows")
            win_script = cluster_lib.normalize_local_path(gw_script, "windows")
            win_root = cluster_lib.normalize_local_path(REPO_ROOT, "windows")
            win_log = cluster_lib.normalize_local_path(log_path, "windows")
            cmd_line = (
                f'"{win_exe}" "{win_script}" --host {host} --port {port} '
                f'--repo-root "{win_root}" --log-file "{win_log}"'
            )
            ps_cmd = (
                "$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
                f"-Arguments @{{ CommandLine = '{cmd_line}' }}; "
                "if ($r.ReturnValue -eq 0 -and $r.ProcessId) { Write-Output $r.ProcessId }"
            )
            r = subprocess.run(
                [ps_bin, "-NoProfile", "-Command", ps_cmd],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=15,
            )
            pid_txt = (r.stdout or "").strip()
            launched_win = (r.returncode == 0 and pid_txt.isdigit())
            if launched_win:
                with open(pid_path, "w", encoding="utf-8") as f:
                    f.write(pid_txt)

    if not launched_win:
        with open(log_path, "a", encoding="utf-8") as lf:
            p = subprocess.Popen(
                [sys.executable, gw_script, "--host", host, "--port", str(port), "--repo-root", REPO_ROOT],
                stdin=subprocess.DEVNULL,
                stdout=lf,
                stderr=lf,
                start_new_session=True,
                close_fds=True,
            )
        with open(pid_path, "w", encoding="utf-8") as f:
            f.write(str(p.pid))

    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if _is_healthy(base_url):
            print(f"[gateway] started cleanly at {base_url}")
            return 0
        time.sleep(0.25)
    print(f"[gateway] ERROR: failed to become healthy at {base_url}; check {log_path}", file=sys.stderr)
    return 1


def cmd_stop(cfg):
    base_url, _, _ = _gateway_base(cfg)
    pid_path = _pid_file()
    if os.path.isfile(pid_path):
        try:
            with open(pid_path, encoding="utf-8") as f:
                pid = int(f.read().strip())
            if cluster_lib.detect_platform() != "windows":
                os.kill(pid, signal.SIGTERM)
        except Exception:
            pass
        try:
            os.remove(pid_path)
        except OSError:
            pass

    if cluster_lib.detect_platform() != "windows":
        try:
            subprocess.run(["pkill", "-f", "tools/gateway/cluster_gateway.py"], stdin=subprocess.DEVNULL, capture_output=True, timeout=5)
        except Exception:
            pass
    _stop_windows_gateway_if_any()
    time.sleep(0.4)
    if _is_healthy(base_url):
        print(f"[gateway] WARN: {base_url} still responding after stop", file=sys.stderr)
        return 1
    print(f"[gateway] stopped ({base_url})")
    return 0


def cmd_status(cfg, as_json=False):
    base_url, _, _ = _gateway_base(cfg)
    try:
        st = _http_json(base_url + "/cluster/status", timeout=3.0)
    except Exception as exc:
        if as_json:
            print(json.dumps({"ok": False, "url": base_url, "error": str(exc)}, indent=2))
        else:
            print(f"Gateway DOWN at {base_url}: {exc}")
        return 1

    if as_json:
        print(json.dumps(st, indent=2))
        return 0

    print(f"Cluster Gateway OK: {base_url} -> upstream {st.get('upstream')} (reachable={st.get('upstream_reachable')})")
    print(f"  local_node={st.get('local_node')} | online_nodes={st.get('online_nodes')} | loaded={st.get('loaded_by_node')}")
    if st.get("torre_heavy_loaded"):
        print(f"  torre_heavy_loaded={st['torre_heavy_loaded']} (anti-thrash FIM/embed diversion active)")
    for alias, info in sorted((st.get("aliases") or {}).items()):
        tag = "LOADED" if info.get("loaded") else ("online" if info.get("online") else "offline")
        div = " [anti-thrash]" if info.get("anti_thrash_diverted") else ""
        print(f"  {alias:<22} -> {info['resolved']:<28} ({info['node']}, {info['context_length']} ctx, {tag}){div}")
    return 0


def _windows_startup_vbs_path():
    plat = cluster_lib.detect_platform()
    if plat == "windows":
        appdata = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Roaming")
        return os.path.join(appdata, "Microsoft", "Windows", "Start Menu", "Programs", "Startup", "cluster-llm-gateway.vbs")
    if plat == "wsl":
        ps_bin = cluster_lib.resolve_host_binary("powershell.exe")
        try:
            raw = subprocess.check_output(
                [ps_bin, "-NoProfile", "-Command", "[Environment]::GetFolderPath('Startup')"],
                stdin=subprocess.DEVNULL,
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=5,
            ).strip()
            if raw:
                return os.path.join(cluster_lib.normalize_local_path(raw, "wsl"), "cluster-llm-gateway.vbs")
        except Exception:
            pass
        win_home = cluster_lib._win_userprofile_from_wsl()
        if win_home:
            return os.path.join(win_home, "AppData", "Roaming", "Microsoft", "Windows", "Start Menu", "Programs", "Startup", "cluster-llm-gateway.vbs")
    return None


def cmd_install_autostart(cfg, dry_run=False):
    _, host, port = _gateway_base(cfg)
    plat = cluster_lib.detect_platform()
    gw_script = os.path.join(GATEWAY_DIR, "cluster_gateway.py")

    if plat in ("windows", "wsl"):
        vbs_path = _windows_startup_vbs_path()
        if vbs_path:
            win_py = cluster_lib.resolve_win_python(cfg)
            win_pyw = None
            if win_py and win_py.lower().endswith("python.exe"):
                cand = win_py[:-10] + "pythonw.exe"
                if os.path.isfile(cand):
                    win_pyw = cand
            if win_pyw and os.path.isfile(win_pyw):
                exe_win = cluster_lib.normalize_local_path(win_pyw, "windows")
                script_win = cluster_lib.normalize_local_path(gw_script, "windows")
                root_win = cluster_lib.normalize_local_path(REPO_ROOT, "windows")
                cmd_str = f'"{exe_win}" "{script_win}" --host {host} --port {port} --repo-root "{root_win}"'
            else:
                posix_script = cluster_lib.normalize_local_path(gw_script, "wsl")
                posix_root = cluster_lib.normalize_local_path(REPO_ROOT, "wsl")
                cmd_str = f'wsl.exe -e python3 "{posix_script}" --host {host} --port {port} --repo-root "{posix_root}"'
            escaped = cmd_str.replace('"', '""')
            vbs = (
                'Set WshShell = CreateObject("WScript.Shell")\n'
                f'WshShell.Run "{escaped}", 0, False\n'
            )
            print(f"[autostart] write {vbs_path}")
            if not dry_run:
                os.makedirs(os.path.dirname(vbs_path), exist_ok=True)
                with open(vbs_path, "w", encoding="utf-8") as f:
                    f.write(vbs)
            return 0

    # Standalone Linux systemd --user service
    unit_dir = os.path.join(os.path.expanduser("~"), ".config", "systemd", "user")
    unit_path = os.path.join(unit_dir, "cluster-llm-gateway.service")
    unit = (
        "[Unit]\n"
        "Description=Cluster HW & LLM Router Gateway\n"
        "After=network.target\n\n"
        "[Service]\n"
        f"ExecStart={sys.executable} {gw_script} --host {host} --port {port} --repo-root {REPO_ROOT}\n"
        "Restart=on-failure\n"
        "RestartSec=3\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )
    print(f"[autostart] write {unit_path}")
    if not dry_run:
        os.makedirs(unit_dir, exist_ok=True)
        with open(unit_path, "w", encoding="utf-8") as f:
            f.write(unit)
        subprocess.run(["systemctl", "--user", "daemon-reload"], stdin=subprocess.DEVNULL, capture_output=True)
        subprocess.run(["systemctl", "--user", "enable", "cluster-llm-gateway.service"], stdin=subprocess.DEVNULL, capture_output=True)
    return 0


def cmd_uninstall_autostart(dry_run=False):
    vbs_path = _windows_startup_vbs_path()
    if vbs_path and os.path.isfile(vbs_path):
        print(f"[autostart] remove {vbs_path}")
        if not dry_run:
            os.remove(vbs_path)
    unit_path = os.path.join(os.path.expanduser("~"), ".config", "systemd", "user", "cluster-llm-gateway.service")
    if os.path.isfile(unit_path):
        print(f"[autostart] remove {unit_path}")
        if not dry_run:
            subprocess.run(["systemctl", "--user", "disable", "--now", "cluster-llm-gateway.service"], stdin=subprocess.DEVNULL, capture_output=True)
            os.remove(unit_path)
    return 0


def cmd_check_lmlink(cfg):
    lms_home = cluster_lib.resolve_local_lmstudio_home(cfg)
    internal = os.path.join(lms_home, ".internal")
    http_cfg_p = os.path.join(internal, "http-server-config.json")
    settings_p = os.path.join(lms_home, "settings.json")
    lmlink_p = os.path.join(internal, "lm-link-config.json")

    report = {
        "local_node": cfg.get("local_node"),
        "lmstudio_home": lms_home,
        "http_server_config": {},
        "developer_settings": {},
        "lm_link_enabled": None,
        "wsl_mirrored_networking": None,
        "upstream_1234_reachable": False,
        "gateway_reachable": False,
        "ollama_ghost_detected": False,
        "warnings": [],
    }

    if os.path.isfile(http_cfg_p):
        with open(http_cfg_p, encoding="utf-8") as f:
            hc = json.load(f)
        report["http_server_config"] = {
            "autoStartOnLaunch": hc.get("autoStartOnLaunch"),
            "port": hc.get("port"),
            "cors": hc.get("cors"),
            "justInTimeModelLoading": hc.get("justInTimeModelLoading"),
            "networkInterface": hc.get("networkInterface"),
        }
        if not hc.get("cors"):
            report["warnings"].append("Enable CORS in LM Studio Developer Server settings")
        if not hc.get("justInTimeModelLoading"):
            report["warnings"].append("Enable Just-In-Time Model Loading in LM Studio Developer settings")

    if os.path.isfile(settings_p):
        with open(settings_p, encoding="utf-8") as f:
            sd = json.load(f)
        dev = sd.get("developer") or {}
        report["developer_settings"] = {
            "unloadPreviousJITModelOnLoad": dev.get("unloadPreviousJITModelOnLoad"),
            "jitModelTTL": dev.get("jitModelTTL"),
            "separateReasoningContentInAPI": dev.get("separateReasoningContentInAPI"),
        }

    if os.path.isfile(lmlink_p):
        with open(lmlink_p, encoding="utf-8") as f:
            lc = json.load(f).get("json") or {}
        report["lm_link_enabled"] = (not lc.get("forceDisabled", False)) and bool(lc.get("allowLoadingModelsOnThisMachine", True))

    if cluster_lib.detect_platform() in ("windows", "wsl"):
        win_home = os.path.expanduser("~") if cluster_lib.detect_platform() == "windows" else cluster_lib._win_userprofile_from_wsl()
        if win_home:
            wslcfg = os.path.join(win_home, ".wslconfig")
            if os.path.isfile(wslcfg):
                with open(wslcfg, encoding="utf-8", errors="ignore") as f:
                    txt = f.read().lower()
                mirrored = "networkingmode=mirrored" in txt.replace(" ", "")
                report["wsl_mirrored_networking"] = mirrored
                if not mirrored:
                    report["warnings"].append("Set networkingMode=mirrored in Windows .wslconfig so Windows and WSL share 127.0.0.1")

    gw_url, _, _ = _gateway_base(cfg)
    up_url = ((cfg.get("gateway") or {}).get("upstream_url") or "http://127.0.0.1:1234").rstrip("/")
    try:
        ms = cluster_lib.get_api_models(up_url, timeout=3)
        report["upstream_1234_reachable"] = True
        report["upstream_model_count"] = len(ms)
    except Exception:
        report["upstream_1234_reachable"] = False
    report["gateway_reachable"] = _is_healthy(gw_url)

    try:
        ol = _http_json("http://127.0.0.1:11434/api/tags", timeout=1.5)
        ol_models = [m.get("name") or m.get("model") for m in (ol.get("models") or []) if m.get("name") or m.get("model")]
        report["ollama_ghost_detected"] = True
        report["ollama_ghost_models"] = ol_models
        report["warnings"].append(
            "Ollama is still listening on 127.0.0.1:11434 (auto-discovered by OpenCode/VS Code); "
            "run 'python3 scripts/cleanup_ollama.py --apply' to remove it."
        )
    except Exception:
        pass

    print(json.dumps(report, indent=2))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Cluster Router Gateway controller")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("start")
    sub.add_parser("stop")
    sub.add_parser("restart")
    ps = sub.add_parser("status")
    ps.add_argument("--json", action="store_true")
    pi = sub.add_parser("install-autostart")
    pi.add_argument("--dry-run", action="store_true")
    pu = sub.add_parser("uninstall-autostart")
    pu.add_argument("--dry-run", action="store_true")
    sub.add_parser("check-lmlink")

    args = ap.parse_args(argv)
    cfg = cluster_lib.load_cluster_config(REPO_ROOT)

    if args.cmd == "start":
        return cmd_start(cfg)
    if args.cmd == "stop":
        return cmd_stop(cfg)
    if args.cmd == "restart":
        cmd_stop(cfg)
        return cmd_start(cfg)
    if args.cmd == "status":
        return cmd_status(cfg, as_json=args.json)
    if args.cmd == "install-autostart":
        return cmd_install_autostart(cfg, dry_run=args.dry_run)
    if args.cmd == "uninstall-autostart":
        return cmd_uninstall_autostart(dry_run=args.dry_run)
    if args.cmd == "check-lmlink":
        return cmd_check_lmlink(cfg)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
