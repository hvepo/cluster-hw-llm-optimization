#!/usr/bin/env python3
"""Cross-OS Ollama ghost detector and cleaner across Windows and Ubuntu WSL.

Detects why Ollama models (e.g. ollama/*) keep appearing in OpenCode, VS Code, etc.
after uninstalling the Windows app (most commonly an active WSL systemd `ollama.service`
exposed back to Windows via `.wslconfig` `networkingMode=mirrored`, plus Windows PATH
or leftover user directories).
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(SCRIPTS_DIR, ".."))
TOOLS_DIR = os.path.join(REPO_ROOT, "tools")
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

import cluster_lib  # noqa: E402

ROOT_WSL_CLEANUP_CMD = (
    "sudo systemctl disable --now ollama.service 2>/dev/null || true; "
    "sudo rm -f /etc/systemd/system/ollama.service "
    "/etc/systemd/system/default.target.wants/ollama.service "
    "/usr/local/bin/ollama; "
    "sudo rm -rf /usr/share/ollama /usr/lib/ollama; "
    "sudo userdel ollama 2>/dev/null || true; "
    "sudo groupdel ollama 2>/dev/null || true; "
    "sudo systemctl daemon-reload 2>/dev/null || true"
)


def probe_ollama_api(url="http://127.0.0.1:11434/api/tags", timeout=2.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        models = []
        for m in data.get("models") or []:
            name = m.get("name") or m.get("model")
            if name:
                models.append(name)
        return True, models
    except Exception:
        return False, []


def _run_ps_json(ps_script):
    ps_bin = cluster_lib.resolve_host_binary("powershell.exe")
    try:
        r = subprocess.run(
            [ps_bin, "-NoProfile", "-Command", ps_script],
            stdin=subprocess.DEVNULL,
            text=True,
            capture_output=True,
            timeout=15,
        )
        if r.returncode == 0 and r.stdout.strip():
            return json.loads(r.stdout.strip().splitlines()[-1])
    except Exception as exc:
        return {"error": str(exc)}
    return {}


def _inspect_windows_ollama():
    ps = (
        "$procs = @(Get-Process -ErrorAction SilentlyContinue | "
        "Where-Object { $_.ProcessName -match '(?i)ollama' } | "
        "Select-Object -ExpandProperty ProcessName); "
        "$svcs = @(Get-Service -ErrorAction SilentlyContinue | "
        "Where-Object { $_.Name -match '(?i)ollama' -or $_.DisplayName -match '(?i)ollama' } | "
        "Select-Object Name, Status); "
        "$dirs = @(); "
        "foreach ($p in @("
        "(Join-Path $env:USERPROFILE '.ollama'), "
        "(Join-Path $env:LOCALAPPDATA 'Programs\\Ollama'), "
        "(Join-Path $env:LOCALAPPDATA 'Ollama'))) { "
        "if ($p -and (Test-Path $p)) { $dirs += $p } }; "
        "$userPath = [Environment]::GetEnvironmentVariable('Path', 'User'); "
        "$pathEntries = @(); "
        "if ($userPath) { $pathEntries = @($userPath -split ';' | Where-Object { $_ -and ($_ -match '(?i)ollama') }) }; "
        "@{processes=$procs; services=$svcs; dirs=$dirs; user_path_entries=$pathEntries; "
        "userprofile=$env:USERPROFILE; appdata=$env:APPDATA} | ConvertTo-Json -Depth 4 -Compress"
    )
    res = _run_ps_json(ps)
    if not res:
        return {"reachable": False}
    res["reachable"] = True
    return res


def _wsl_audit_script():
    return (
        "python3 -c '\n"
        "import json, os, subprocess\n"
        "def run(cmd):\n"
        "    r = subprocess.run(cmd, stdin=subprocess.DEVNULL, text=True, capture_output=True)\n"
        "    return r.returncode, (r.stdout or \"\").strip()\n"
        "paths = [\n"
        "    \"/etc/systemd/system/ollama.service\",\n"
        "    \"/etc/systemd/system/default.target.wants/ollama.service\",\n"
        "    \"/usr/local/bin/ollama\",\n"
        "    \"/usr/share/ollama\",\n"
        "    \"/usr/lib/ollama\",\n"
        "]\n"
        "existing_root = [p for p in paths if os.path.lexists(p)]\n"
        "user_dir = os.path.expanduser(\"~/.ollama\")\n"
        "user_dir_exists = os.path.exists(user_dir)\n"
        "du_share = None\n"
        "if os.path.exists(\"/usr/share/ollama\"):\n"
        "    rc, out = run([\"du\", \"-sh\", \"/usr/share/ollama\"])\n"
        "    if rc == 0 and out:\n"
        "        du_share = out.split()[0]\n"
        "_, svc_active = run([\"systemctl\", \"is-active\", \"ollama.service\"])\n"
        "_, svc_enabled = run([\"systemctl\", \"is-enabled\", \"ollama.service\"])\n"
        "rc_u, _ = run([\"id\", \"ollama\"])\n"
        "rc_g, _ = run([\"getent\", \"group\", \"ollama\"])\n"
        "rc_p, procs = run([\"pgrep\", \"-af\", \"ollama\"])\n"
        "proc_lines = [ln for ln in procs.splitlines() if ln and \"cleanup_ollama\" not in ln] if rc_p == 0 else []\n"
        "rc_sn, _ = run([\"sudo\", \"-n\", \"true\"])\n"
        "print(json.dumps({\n"
        "    \"reachable\": True,\n"
        "    \"home\": os.path.expanduser(\"~\"),\n"
        "    \"root_paths\": existing_root,\n"
        "    \"user_ollama_dir\": user_dir if user_dir_exists else None,\n"
        "    \"share_ollama_size\": du_share,\n"
        "    \"service_active\": svc_active == \"active\",\n"
        "    \"service_enabled\": svc_enabled == \"enabled\",\n"
        "    \"service_state\": {\"active\": svc_active, \"enabled\": svc_enabled},\n"
        "    \"user_exists\": rc_u == 0,\n"
        "    \"group_exists\": rc_g == 0,\n"
        "    \"processes\": proc_lines,\n"
        "    \"sudo_nopasswd\": rc_sn == 0,\n"
        "}))\n"
        "'"
    )


def _inspect_wsl_ollama(plat):
    script = _wsl_audit_script()
    try:
        if plat in ("wsl", "linux", "macos"):
            r = subprocess.run(["bash", "-c", script], stdin=subprocess.DEVNULL, text=True, capture_output=True, timeout=15)
        elif plat == "windows":
            wsl_bin = cluster_lib.resolve_host_binary("wsl.exe")
            r = subprocess.run([wsl_bin, "-e", "bash", "-c", script], stdin=subprocess.DEVNULL, text=True, capture_output=True, timeout=15)
        else:
            return {"reachable": False}
        if r.returncode == 0 and r.stdout.strip():
            return json.loads(r.stdout.strip().splitlines()[-1])
    except Exception as exc:
        return {"reachable": False, "error": str(exc)}
    return {"reachable": False}


def _inspect_ide_configs(plat, win_info, wsl_info):
    candidates = []
    if plat in ("wsl", "linux", "macos"):
        posix_home = os.path.expanduser("~")
        candidates.extend([
            os.path.join(posix_home, ".config", "opencode", "opencode.json"),
            os.path.join(posix_home, ".continue", "config.yaml"),
        ])
    elif wsl_info.get("reachable") and wsl_info.get("home"):
        # Inspect WSL home from Windows via wsl.exe normalized path when reachable
        for rel in (".config/opencode/opencode.json", ".continue/config.yaml"):
            p = cluster_lib.normalize_local_path(wsl_info["home"].rstrip("/") + "/" + rel, "windows")
            if p:
                candidates.append(p)

    if win_info.get("reachable"):
        win_up = win_info.get("userprofile")
        win_app = win_info.get("appdata")
        if win_up:
            up_local = cluster_lib.normalize_local_path(win_up, plat)
            candidates.extend([
                os.path.join(up_local, ".config", "opencode", "opencode.json"),
                os.path.join(up_local, ".continue", "config.yaml"),
            ])
        if win_app:
            app_local = cluster_lib.normalize_local_path(win_app, plat)
            candidates.extend([
                os.path.join(app_local, "opencode", "opencode.json"),
                os.path.join(app_local, "Code", "User", "chatLanguageModels.json"),
            ])

    hits = []
    seen = set()
    pat = re.compile(r"ollama|11434", re.IGNORECASE)
    for path in candidates:
        if not path or path in seen or not os.path.isfile(path):
            continue
        seen.add(path)
        try:
            txt = open(path, encoding="utf-8", errors="ignore").read()
            if pat.search(txt):
                hits.append(path)
        except OSError:
            pass
    return hits


def _list_opencode_ollama_models():
    oc_bin = shutil.which("opencode")
    if not oc_bin:
        cand = os.path.expanduser("~/.opencode/bin/opencode")
        if os.path.isfile(cand):
            oc_bin = cand
    if not oc_bin:
        return None
    try:
        r = subprocess.run([oc_bin, "models"], stdin=subprocess.DEVNULL, text=True, capture_output=True, timeout=15)
        if r.returncode == 0:
            return [ln.strip() for ln in r.stdout.splitlines() if ln.strip().lower().startswith("ollama/")]
    except Exception:
        pass
    return None


def _clean_windows_leftovers():
    ps = (
        "$actions = @(); "
        "$procs = @(Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.ProcessName -match '(?i)ollama' }); "
        "foreach ($pr in $procs) { Stop-Process -Id $pr.Id -Force -ErrorAction SilentlyContinue; $actions += ('stopped:' + $pr.ProcessName) }; "
        "foreach ($p in @("
        "(Join-Path $env:USERPROFILE '.ollama'), "
        "(Join-Path $env:LOCALAPPDATA 'Programs\\Ollama'), "
        "(Join-Path $env:LOCALAPPDATA 'Ollama'))) { "
        "if ($p -and (Test-Path $p)) { Remove-Item -LiteralPath $p -Recurse -Force -ErrorAction SilentlyContinue; $actions += ('removed_dir:' + $p) } }; "
        "$userPath = [Environment]::GetEnvironmentVariable('Path', 'User'); "
        "if ($userPath) { "
        "$parts = @($userPath -split ';'); "
        "$kept = @($parts | Where-Object { $_ -and ($_ -notmatch '(?i)ollama') }); "
        "if ($kept.Count -ne @($parts | Where-Object { $_ }).Count) { "
        "[Environment]::SetEnvironmentVariable('Path', ($kept -join ';'), 'User'); "
        "$actions += 'cleaned_user_path' } }; "
        "@{actions=$actions} | ConvertTo-Json -Depth 3 -Compress"
    )
    return _run_ps_json(ps)


def _clean_wsl_user_dir(plat):
    removed = []
    if plat in ("wsl", "linux", "macos"):
        u = os.path.expanduser("~/.ollama")
        if os.path.exists(u):
            shutil.rmtree(u, ignore_errors=True)
            removed.append(u)
    elif plat == "windows":
        wsl_bin = cluster_lib.resolve_host_binary("wsl.exe")
        r = subprocess.run(
            [wsl_bin, "-e", "bash", "-c", "[ -e \"$HOME/.ollama\" ] && rm -rf \"$HOME/.ollama\" && echo removed || true"],
            stdin=subprocess.DEVNULL,
            text=True,
            capture_output=True,
            timeout=15,
        )
        if "removed" in (r.stdout or ""):
            removed.append("~/.ollama")
    return removed


def _has_root_wsl_artifacts(wsl_info):
    if not wsl_info.get("reachable"):
        return False
    return bool(
        wsl_info.get("root_paths")
        or wsl_info.get("service_active")
        or wsl_info.get("service_enabled")
        or wsl_info.get("user_exists")
        or wsl_info.get("group_exists")
        or wsl_info.get("processes")
    )


def run_audit(plat=None):
    plat = plat or cluster_lib.detect_platform()
    api_live, api_models = probe_ollama_api()
    win_info = _inspect_windows_ollama() if plat in ("windows", "wsl") else {"reachable": False}
    wsl_info = _inspect_wsl_ollama(plat) if plat in ("windows", "wsl", "linux") else {"reachable": False}
    ide_hits = _inspect_ide_configs(plat, win_info, wsl_info)
    oc_ollama = _list_opencode_ollama_models()

    report = {
        "platform": plat,
        "api_11434_live": api_live,
        "api_models": api_models,
        "windows": win_info,
        "wsl": wsl_info,
        "ide_config_hits": ide_hits,
        "opencode_ollama_models": oc_ollama,
        "wsl_root_cleanup_pending": _has_root_wsl_artifacts(wsl_info),
        "wsl_root_cleanup_command": ROOT_WSL_CLEANUP_CMD,
    }
    return report


def run_apply(plat=None, quiet=False, allow_interactive_sudo=True):
    plat = plat or cluster_lib.detect_platform()
    before = run_audit(plat)
    actions = []
    root_pending = False

    if plat in ("windows", "wsl"):
        win_res = _clean_windows_leftovers()
        actions.extend(win_res.get("actions") or [])

    actions.extend([f"removed_wsl_user:{p}" for p in _clean_wsl_user_dir(plat)])

    wsl_info = before.get("wsl") or {}
    if _has_root_wsl_artifacts(wsl_info):
        can_run_tty = allow_interactive_sudo and sys.stdin.isatty() and sys.stdout.isatty()
        can_run_nopw = bool(wsl_info.get("sudo_nopasswd"))
        if can_run_tty or can_run_nopw:
            if plat in ("wsl", "linux", "macos"):
                if not quiet:
                    print("[cleanup] removing WSL systemd Ollama service & /usr/share/ollama (sudo)...")
                stdin_arg = None if can_run_tty else subprocess.DEVNULL
                r = subprocess.run(["bash", "-c", ROOT_WSL_CLEANUP_CMD], stdin=stdin_arg)
                if r.returncode == 0:
                    actions.append("removed_wsl_root_ollama")
                else:
                    root_pending = True
            elif plat == "windows" and can_run_tty:
                if not quiet:
                    print("[cleanup] removing WSL systemd Ollama service via wsl.exe (sudo)...")
                wsl_bin = cluster_lib.resolve_host_binary("wsl.exe")
                r = subprocess.run([wsl_bin, "-e", "bash", "-c", ROOT_WSL_CLEANUP_CMD])
                if r.returncode == 0:
                    actions.append("removed_wsl_root_ollama")
                else:
                    root_pending = True
            else:
                root_pending = True
        else:
            root_pending = True

    after = run_audit(plat)
    after["apply_actions"] = actions
    after["wsl_root_cleanup_pending"] = root_pending or _has_root_wsl_artifacts(after.get("wsl") or {})
    return after


def print_human_report(rep):
    print(f"=== Ollama Ghost Audit ({rep['platform']}) ===")
    if rep["api_11434_live"]:
        print(f"  [FOUND] Live API on http://127.0.0.1:11434 serving: {rep['api_models']}")
    else:
        print("  [CLEAN] http://127.0.0.1:11434 is closed (no live Ollama server)")

    w = rep.get("windows") or {}
    if w.get("reachable"):
        has_win = bool(w.get("processes") or w.get("services") or w.get("dirs") or w.get("user_path_entries"))
        if has_win:
            print(f"  [FOUND] Windows leftovers: procs={w.get('processes')} svcs={w.get('services')} dirs={w.get('dirs')} path={w.get('user_path_entries')}")
        else:
            print("  [CLEAN] Windows processes, services, dirs, and User PATH have no Ollama leftovers")

    wl = rep.get("wsl") or {}
    if wl.get("reachable"):
        has_wsl = bool(wl.get("root_paths") or wl.get("user_ollama_dir") or wl.get("service_active") or wl.get("service_enabled") or wl.get("processes"))
        if has_wsl:
            sz = f" ({wl['share_ollama_size']})" if wl.get("share_ollama_size") else ""
            print(
                f"  [FOUND] WSL Ollama: active={wl.get('service_active')} enabled={wl.get('service_enabled')} "
                f"paths={wl.get('root_paths')}{sz} user_dir={wl.get('user_ollama_dir')} procs={len(wl.get('processes') or [])}"
            )
        else:
            print("  [CLEAN] WSL has no Ollama service, binary, or model directories")

    if rep.get("ide_config_hits"):
        print(f"  [FOUND] IDE configs mentioning ollama/11434: {rep['ide_config_hits']}")
    else:
        print("  [CLEAN] OpenCode / VS Code BYOK / Continue configs have 0 Ollama references")

    if rep.get("opencode_ollama_models") is not None:
        if rep["opencode_ollama_models"]:
            print(f"  [FOUND] `opencode models` still shows: {rep['opencode_ollama_models']}")
        else:
            print("  [CLEAN] `opencode models` lists 0 ollama/* models")

    if rep.get("apply_actions"):
        print(f"  [APPLIED] {rep['apply_actions']}")

    if rep.get("wsl_root_cleanup_pending"):
        print("\nRoot privileges are required to remove WSL systemd Ollama. Run this in an interactive terminal:")
        if rep["platform"] == "windows":
            print(f"  wsl.exe -e bash -c '{ROOT_WSL_CLEANUP_CMD}'")
        else:
            print(f"  {ROOT_WSL_CLEANUP_CMD}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Audit or remove leftover Windows/WSL Ollama installations exposing models on :11434")
    ap.add_argument("--check", action="store_true", help="Read-only audit across Windows and WSL (default)")
    ap.add_argument("--apply", action="store_true", help="Clean user-level leftovers and remove WSL systemd Ollama (uses sudo if interactive/nopasswd)")
    ap.add_argument("--no-sudo", action="store_true", help="Clean user-level leftovers without prompting for interactive sudo; print the root WSL cleanup command instead")
    ap.add_argument("--json", action="store_true", help="Print JSON report")
    args = ap.parse_args(argv)

    plat = cluster_lib.detect_platform()
    rep = run_apply(plat=plat, quiet=args.json, allow_interactive_sudo=not args.no_sudo) if args.apply else run_audit(plat=plat)

    if args.json:
        print(json.dumps(rep, indent=2))
    else:
        print_human_report(rep)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
