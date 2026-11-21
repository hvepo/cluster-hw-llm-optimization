#!/usr/bin/env python3
"""Universal cross-OS & multi-client installer for cluster-hw-llm-optimization.

Supports any combination of:
- OS environments (--os): auto, windows, wsl, linux, all (or comma-separated list)
- Agent clients (--targets): agy, vscode, opencode, all (or comma-separated list)
- Scopes (--scope): all, user, workspace
"""
import argparse
import json
import os
import shutil
import subprocess
import sys

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO_ROOT, "tools"))

import cluster_lib  # noqa: E402

ALL_KNOWN_SKILLS = (
    "hw-node-profiler",
    "llm-node-finetune",
    "cluster-install-migrate",
    "cluster-router",
    "hw-tuning-stability",
    "llm-inference-hillclimb",
    "cluster-migrate-install",
)

TARGET_ALIASES = {
    "agy": "agy",
    "antigravity": "agy",
    "vscode": "vscode",
    "code": "vscode",
    "vs-code": "vscode",
    "opencode": "opencode",
    "oc": "opencode",
}


def parse_targets(raw):
    items = {t.strip().lower() for t in (raw or "").split(",") if t.strip()}
    if not items or "all" in items:
        return {"agy", "vscode", "opencode"}
    out = set()
    for it in items:
        if it not in TARGET_ALIASES:
            raise ValueError(f"Unknown target {it!r}. Valid targets: agy, vscode, opencode, all")
        out.add(TARGET_ALIASES[it])
    return out


def parse_os_list(raw, current_plat, win_ctx_available, wsl_bridge_available):
    items = {o.strip().lower() for o in (raw or "auto").split(",") if o.strip()}
    if not items or "auto" in items:
        if current_plat == "wsl":
            res = ["wsl"]
            if win_ctx_available:
                res.append("windows")
            return res
        if current_plat == "windows":
            res = ["windows"]
            if wsl_bridge_available:
                res.append("wsl")
            return res
        return ["linux"]
    if "all" in items:
        res = []
        if current_plat == "windows":
            res.append("windows")
            res.append("wsl")
        elif current_plat == "wsl":
            res.append("wsl")
            res.append("windows")
        else:
            res.append("linux")
            if win_ctx_available:
                res.append("windows")
        return res
    out = []
    for it in items:
        if it in ("wsl", "linux", "posix"):
            norm = "wsl" if current_plat in ("wsl", "windows") and it != "linux" else ("wsl" if it == "wsl" else "linux")
            if norm not in out:
                out.append(norm)
        elif it in ("win", "windows"):
            if "windows" not in out:
                out.append("windows")
        else:
            raise ValueError(f"Unknown --os value {it!r}. Valid values: auto, windows, wsl, linux, all")
    return out


def load_json_safe(path, default=None):
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {} if default is None else default


def write_json_atomic(path, data, dry_run=False):
    if dry_run:
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def sync_skills_dir(src_skills, dest_skills, dry_run=False):
    if dry_run:
        return
    os.makedirs(dest_skills, exist_ok=True)
    for old_s in ALL_KNOWN_SKILLS:
        p_old = os.path.join(dest_skills, old_s)
        if os.path.exists(p_old):
            shutil.rmtree(p_old)
    for sname in sorted(os.listdir(src_skills)):
        sdir = os.path.join(src_skills, sname)
        if os.path.isdir(sdir):
            shutil.copytree(sdir, os.path.join(dest_skills, sname))


def remove_skills_dir(dest_skills, dry_run=False):
    if not os.path.isdir(dest_skills):
        return
    for sname in ALL_KNOWN_SKILLS:
        sp = os.path.join(dest_skills, sname)
        if os.path.exists(sp):
            print(f"[uninstall] remove skill {sp}")
            if not dry_run:
                shutil.rmtree(sp)


def detect_windows_context(current_plat, force_wsl_python=False):
    """Return Windows host paths & execution command specification (works from native Windows, WSL, or test sandbox)."""
    sandbox_win_home = os.environ.get("CLUSTER_INSTALL_WIN_HOME")
    sandbox_win_appdata = os.environ.get("CLUSTER_INSTALL_WIN_APPDATA")

    win_userprofile_local = None
    win_appdata_local = None
    win_userprofile_winstr = None

    if sandbox_win_home:
        win_userprofile_local = sandbox_win_home
        win_appdata_local = sandbox_win_appdata or os.path.join(sandbox_win_home, "AppData", "Roaming")
        win_userprofile_winstr = r"C:\Users\<USER>"
    elif current_plat == "windows":
        win_userprofile_local = os.path.expanduser("~")
        win_appdata_local = os.environ.get("APPDATA") or os.path.join(win_userprofile_local, "AppData", "Roaming")
        win_userprofile_winstr = win_userprofile_local
    elif current_plat == "wsl":
        try:
            cmd_bin = cluster_lib.resolve_host_binary("cmd.exe")
            raw_up = subprocess.check_output(
                [cmd_bin, "/c", "echo", "%USERPROFILE%"],
                stdin=subprocess.DEVNULL,
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=5,
            ).strip()
            if raw_up and "%" not in raw_up:
                win_userprofile_winstr = raw_up
                win_userprofile_local = cluster_lib.normalize_local_path(raw_up, "wsl")
            raw_app = subprocess.check_output(
                [cmd_bin, "/c", "echo", "%APPDATA%"],
                stdin=subprocess.DEVNULL,
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=5,
            ).strip()
            if raw_app and "%" not in raw_app:
                win_appdata_local = cluster_lib.normalize_local_path(raw_app, "wsl")
            elif win_userprofile_local:
                win_appdata_local = os.path.join(win_userprofile_local, "AppData", "Roaming")
        except Exception:
            pass

    if not win_userprofile_local or not os.path.isdir(win_userprofile_local):
        return None

    # Resolve Windows repo path and Python command (or wsl.exe -e python3 fallback)
    posix_repo = cluster_lib.normalize_local_path(REPO_ROOT, "wsl")
    win_repo_str = cluster_lib.normalize_local_path(REPO_ROOT, "windows")
    if current_plat == "wsl" and not sandbox_win_home:
        try:
            wslpath_bin = cluster_lib.resolve_host_binary("wslpath")
            w = subprocess.check_output([wslpath_bin, "-w", REPO_ROOT], text=True, stderr=subprocess.DEVNULL, timeout=5).strip()
            if w:
                win_repo_str = w
        except Exception:
            pass

    win_py_winstr = None
    if not force_wsl_python:
        if sandbox_win_home:
            win_py_winstr = os.environ.get("CLUSTER_INSTALL_WIN_PYTHON") or (win_userprofile_winstr + r"\AppData\Local\Python\bin\python.exe")
        elif current_plat == "windows":
            exe = sys.executable
            if exe and "WindowsApps" not in exe and os.path.isfile(exe):
                win_py_winstr = exe
        elif current_plat == "wsl" and win_userprofile_local and win_userprofile_winstr:
            for rel_posix, rel_win in (
                ("AppData/Local/Python/bin/python.exe", r"AppData\Local\Python\bin\python.exe"),
                ("AppData/Local/Programs/Python/Python313/python.exe", r"AppData\Local\Programs\Python\Python313\python.exe"),
                ("AppData/Local/Programs/Python/Python312/python.exe", r"AppData\Local\Programs\Python\Python312\python.exe"),
                ("AppData/Local/Programs/Python/Python311/python.exe", r"AppData\Local\Programs\Python\Python311\python.exe"),
            ):
                if os.path.isfile(os.path.join(win_userprofile_local, rel_posix)):
                    win_py_winstr = win_userprofile_winstr.rstrip("\\") + "\\" + rel_win
                    break

    if win_py_winstr:
        win_mcp_script = win_repo_str.rstrip("\\") + r"\mcp\cluster_mcp_server.py"
        return {
            "mode": "native_win_python",
            "home_local": win_userprofile_local,
            "appdata_local": win_appdata_local,
            "repo_str": win_repo_str,
            "mcp_command": win_py_winstr,
            "mcp_args": [win_mcp_script],
            "opencode_command": [win_py_winstr, win_mcp_script],
            "hook_session_cmd": f'"{win_py_winstr}" ./hooks/session_start.py',
            "hook_pre_tool_cmd": f'"{win_py_winstr}" ./hooks/pre_tool_guard.py',
        }

    # Fallback: Windows host runs MCP and hooks through wsl.exe -e python3
    posix_mcp_script = f"{posix_repo}/mcp/cluster_mcp_server.py"
    return {
        "mode": "wsl_python_bridge",
        "home_local": win_userprofile_local,
        "appdata_local": win_appdata_local,
        "repo_str": posix_repo,
        "mcp_command": "wsl.exe",
        "mcp_args": ["-e", "python3", posix_mcp_script],
        "opencode_command": ["wsl.exe", "-e", "python3", posix_mcp_script],
        "hook_session_cmd": "wsl.exe -e python3 ./hooks/session_start.py",
        "hook_pre_tool_cmd": "wsl.exe -e python3 ./hooks/pre_tool_guard.py",
    }


def detect_posix_context(current_plat):
    sandbox_posix_home = os.environ.get("CLUSTER_INSTALL_POSIX_HOME")
    if sandbox_posix_home:
        posix_home = sandbox_posix_home
    elif current_plat in ("wsl", "linux", "macos"):
        posix_home = os.path.expanduser("~")
    else:
        return None

    posix_py = os.environ.get("CLUSTER_INSTALL_POSIX_PYTHON") or (sys.executable if current_plat != "windows" else "/usr/bin/python3")
    posix_repo = cluster_lib.normalize_local_path(REPO_ROOT, "wsl")
    posix_mcp_script = f"{posix_repo}/mcp/cluster_mcp_server.py"
    return {
        "mode": "posix_python",
        "home_local": posix_home,
        "repo_str": posix_repo,
        "mcp_command": posix_py,
        "mcp_args": [posix_mcp_script],
        "opencode_command": [posix_py, posix_mcp_script],
        "hook_session_cmd": f'"{posix_py}" ./hooks/session_start.py',
        "hook_pre_tool_cmd": f'"{posix_py}" ./hooks/pre_tool_guard.py',
    }


def wsl_available_from_windows():
    if cluster_lib.detect_platform() != "windows":
        return False
    try:
        wsl_bin = cluster_lib.resolve_host_binary("wsl.exe")
        r = subprocess.run(
            [wsl_bin, "-e", "python3", "-c", "print('ok')"],
            stdin=subprocess.DEVNULL,
            text=True,
            capture_output=True,
            timeout=5,
        )
        return r.returncode == 0 and "ok" in (r.stdout or "")
    except Exception:
        return False


def configure_cluster_local_json(node, node_explicit, torre_url, portatil_url, os_label, dry_run=False):
    if os.environ.get("CLUSTER_INSTALL_SKIP_CONFIG") == "1":
        return
    local_cfg_path = os.path.join(REPO_ROOT, "cluster.local.json")
    ex_cfg = load_json_safe(os.path.join(REPO_ROOT, "cluster.example.json"), {})
    existed = os.path.isfile(local_cfg_path)
    cfg = load_json_safe(local_cfg_path, ex_cfg) if existed else ex_cfg
    cfg.pop("routing_presets", None)
    if node_explicit or not existed or not cfg.get("local_node"):
        cfg["local_node"] = node
    active_node = cfg["local_node"]
    if active_node not in cfg.setdefault("nodes", {}):
        cfg["nodes"][active_node] = {
            "label": active_node,
            "role": "custom-cluster-node",
            "os": os_label,
            "base_url": "http://127.0.0.1:1234",
            "model_prefixes": [],
            "hardware": {},
            "paths": {},
        }
    if torre_url:
        cfg["nodes"].setdefault("torre", {})["base_url"] = torre_url
    if portatil_url:
        cfg["nodes"].setdefault("portatil", {})["base_url"] = portatil_url
    print(f"[config] {local_cfg_path} (local_node={active_node})")
    write_json_atomic(local_cfg_path, cfg, dry_run=dry_run)


def sync_workspace_mirrors(dry_run=False):
    src_skills = os.path.join(REPO_ROOT, "skills")
    for mirror in (
        os.path.join(REPO_ROOT, ".github", "skills"),
        os.path.join(REPO_ROOT, "plugins", "cluster-hw-llm-optimization", "skills"),
    ):
        print(f"[workspace] sync skills -> {mirror}")
        if not dry_run:
            if os.path.exists(mirror):
                shutil.rmtree(mirror)
            shutil.copytree(src_skills, mirror)
    agents_skills_json = os.path.join(REPO_ROOT, ".agents", "skills.json")
    write_json_atomic(agents_skills_json, {"entries": [{"path": "skills"}]}, dry_run=dry_run)


def install_agy(ctx, os_tag, dry_run=False, uninstall=False):
    agy_dest = os.path.join(ctx["home_local"], ".gemini", "config", "plugins", "cluster-hw-llm-optimization")
    if uninstall:
        if os.path.exists(agy_dest):
            print(f"[{os_tag}:agy] uninstall {agy_dest}")
            if not dry_run:
                shutil.rmtree(agy_dest)
        return

    src_plugin = os.path.join(REPO_ROOT, "plugins", "cluster-hw-llm-optimization")
    print(f"[{os_tag}:agy] install plugin -> {agy_dest} ({ctx['mode']})")
    if not dry_run:
        os.makedirs(os.path.dirname(agy_dest), exist_ok=True)
        if os.path.exists(agy_dest):
            shutil.rmtree(agy_dest)
        shutil.copytree(src_plugin, agy_dest)
        with open(os.path.join(agy_dest, "repo_root.txt"), "w", encoding="utf-8") as f:
            f.write(ctx["repo_str"] + "\n")

        mcp_json = {
            "mcpServers": {
                "cluster-hw-llm": {
                    "command": ctx["mcp_command"],
                    "args": ctx["mcp_args"],
                    "env": {"CLUSTER_REPO_ROOT": ctx["repo_str"]},
                }
            }
        }
        write_json_atomic(os.path.join(agy_dest, "mcp_config.json"), mcp_json, dry_run=False)

        hooks_json = {
            "cluster-safety-guard": {
                "SessionStart": [
                    {"type": "command", "command": ctx["hook_session_cmd"], "timeout": 10}
                ],
                "PreToolUse": [
                    {
                        "matcher": "run_command",
                        "hooks": [
                            {"type": "command", "command": ctx["hook_pre_tool_cmd"], "timeout": 10}
                        ],
                    }
                ],
            }
        }
        write_json_atomic(os.path.join(agy_dest, "hooks.json"), hooks_json, dry_run=False)


def _merge_or_remove_vscode_mcp(mcp_path, ctx, os_tag, dry_run=False, uninstall=False):
    if uninstall:
        if not os.path.isfile(mcp_path):
            return
        data = load_json_safe(mcp_path, {})
        srvs = data.get("servers")
        if isinstance(srvs, dict) and "cluster-hw-llm" in srvs:
            print(f"[{os_tag}:vscode] uninstall servers.cluster-hw-llm from {mcp_path}")
            del srvs["cluster-hw-llm"]
            write_json_atomic(mcp_path, data, dry_run=dry_run)
        return

    print(f"[{os_tag}:vscode] merge servers.cluster-hw-llm -> {mcp_path}")
    data = load_json_safe(mcp_path, {})
    if not isinstance(data.get("servers"), dict):
        data["servers"] = {}
    data["servers"]["cluster-hw-llm"] = {
        "type": "stdio",
        "command": ctx["mcp_command"],
        "args": ctx["mcp_args"],
        "env": {"CLUSTER_REPO_ROOT": ctx["repo_str"]},
    }
    write_json_atomic(mcp_path, data, dry_run=dry_run)


def install_vscode(ctx, os_tag, dry_run=False, uninstall=False):
    src_skills = os.path.join(REPO_ROOT, "skills")
    home = ctx["home_local"]
    mcp_targets = []

    if os_tag == "windows":
        appdata = ctx["appdata_local"]
        mcp_targets.append(os.path.join(appdata, "Code", "User", "mcp.json"))
        insiders_dir = os.path.join(appdata, "Code - Insiders")
        if os.path.isdir(insiders_dir):
            mcp_targets.append(os.path.join(insiders_dir, "User", "mcp.json"))
    else:
        mcp_targets.append(os.path.join(home, ".config", "Code", "User", "mcp.json"))
        insiders_cfg = os.path.join(home, ".config", "Code - Insiders")
        if os.path.isdir(insiders_cfg):
            mcp_targets.append(os.path.join(insiders_cfg, "User", "mcp.json"))
        if os_tag == "wsl" or os.path.isdir(os.path.join(home, ".vscode-server")):
            mcp_targets.append(os.path.join(home, ".vscode-server", "data", "User", "mcp.json"))
        if os.path.isdir(os.path.join(home, ".vscode-server-insiders")):
            mcp_targets.append(os.path.join(home, ".vscode-server-insiders", "data", "User", "mcp.json"))

    for mcp_path in mcp_targets:
        _merge_or_remove_vscode_mcp(mcp_path, ctx, os_tag, dry_run=dry_run, uninstall=uninstall)

    copilot_skills = os.path.join(home, ".copilot", "skills")
    if uninstall:
        remove_skills_dir(copilot_skills, dry_run=dry_run)
    else:
        print(f"[{os_tag}:vscode] sync user skills -> {copilot_skills}")
        sync_skills_dir(src_skills, copilot_skills, dry_run=dry_run)


def _merge_or_remove_opencode_dir(oc_dir, ctx, os_tag, dry_run=False, uninstall=False):
    oc_cfg = os.path.join(oc_dir, "opencode.json")
    oc_skills = os.path.join(oc_dir, "skills")
    src_skills = os.path.join(REPO_ROOT, "skills")

    if uninstall:
        if os.path.isfile(oc_cfg):
            data = load_json_safe(oc_cfg, {})
            srvs = (data.get("mcp") or {}).get("servers")
            if isinstance(srvs, dict) and "cluster-hw-llm" in srvs:
                print(f"[{os_tag}:opencode] uninstall mcp.servers.cluster-hw-llm from {oc_cfg}")
                del srvs["cluster-hw-llm"]
                write_json_atomic(oc_cfg, data, dry_run=dry_run)
        remove_skills_dir(oc_skills, dry_run=dry_run)
        return

    print(f"[{os_tag}:opencode] merge mcp.servers.cluster-hw-llm -> {oc_cfg} and sync skills -> {oc_skills}")
    data = load_json_safe(oc_cfg, {})
    if not isinstance(data.get("mcp"), dict):
        data["mcp"] = {}
    if not isinstance(data["mcp"].get("servers"), dict):
        data["mcp"]["servers"] = {}
    data["mcp"]["servers"]["cluster-hw-llm"] = {
        "type": "local",
        "command": ctx["opencode_command"],
        "environment": {"CLUSTER_REPO_ROOT": ctx["repo_str"]},
    }
    write_json_atomic(oc_cfg, data, dry_run=dry_run)
    sync_skills_dir(src_skills, oc_skills, dry_run=dry_run)


def install_opencode(ctx, os_tag, dry_run=False, uninstall=False):
    home = ctx["home_local"]
    oc_dirs = []
    if os_tag == "windows":
        oc_dirs.append(os.path.join(home, ".config", "opencode"))
        appdata_oc = os.path.join(ctx["appdata_local"], "opencode")
        if os.path.isdir(appdata_oc):
            oc_dirs.append(appdata_oc)
    else:
        if os.environ.get("CLUSTER_INSTALL_POSIX_HOME"):
            base_cfg = os.path.join(home, ".config")
        else:
            base_cfg = os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
        oc_dirs.append(os.path.join(base_cfg, "opencode"))

    for oc_dir in oc_dirs:
        _merge_or_remove_opencode_dir(oc_dir, ctx, os_tag, dry_run=dry_run, uninstall=uninstall)


def apply_for_os(os_tag, ctx, targets, dry_run=False, uninstall=False):
    if "agy" in targets:
        install_agy(ctx, os_tag, dry_run=dry_run, uninstall=uninstall)
    if "vscode" in targets:
        install_vscode(ctx, os_tag, dry_run=dry_run, uninstall=uninstall)
    if "opencode" in targets:
        install_opencode(ctx, os_tag, dry_run=dry_run, uninstall=uninstall)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Universal installer for cluster-hw-llm-optimization")
    ap.add_argument("--node", default=None, help="Cluster node name in cluster.local.json (default: torre)")
    ap.add_argument("--targets", default="agy,opencode,vscode", help="Comma-separated client targets: agy,vscode,opencode,all")
    ap.add_argument("--os", dest="os_targets", default="auto", help="Comma-separated OS targets: auto,windows,wsl,linux,all")
    ap.add_argument("--scope", choices=("all", "user", "workspace"), default="all", help="Install scope (default: all)")
    ap.add_argument("--torre-url", default="", help="Override nodes.torre.base_url in cluster.local.json")
    ap.add_argument("--portatil-url", default="", help="Override nodes.portatil.base_url in cluster.local.json")
    ap.add_argument("--win-use-wsl-python", action="store_true", help="Configure Windows targets to bridge via wsl.exe -e python3")
    ap.add_argument("--serving", "--with-gateway", dest="serving", action="store_true", help="Configure Cluster Router Gateway (:1235), autostart, and IDE/CLI clients")
    ap.add_argument("--clients", default="all", help="Clients for --serving (default: all -> opencode,vscode,continue,lmstudio,env)")
    ap.add_argument("--dry-run", action="store_true", help="Print actions without modifying files")
    ap.add_argument("--uninstall", action="store_true", help="Remove installed global plugin/MCP/skills entries")
    ap.add_argument("--doctor", action="store_true", help="Run scripts/doctor.py after installation")
    args = ap.parse_args(argv)

    current_plat = cluster_lib.detect_platform()
    targets = parse_targets(args.targets)
    win_ctx = detect_windows_context(current_plat, force_wsl_python=args.win_use_wsl_python)
    posix_ctx = detect_posix_context(current_plat)
    wsl_from_win = wsl_available_from_windows()

    os_list = parse_os_list(
        args.os_targets,
        current_plat=current_plat,
        win_ctx_available=(win_ctx is not None),
        wsl_bridge_available=(posix_ctx is not None or wsl_from_win),
    )

    node_explicit = args.node is not None
    node_name = args.node or "torre"

    if not args.uninstall:
        if args.scope in ("all", "workspace"):
            configure_cluster_local_json(
                node=node_name,
                node_explicit=node_explicit,
                torre_url=args.torre_url,
                portatil_url=args.portatil_url,
                os_label="+".join(os_list),
                dry_run=args.dry_run,
            )
            sync_workspace_mirrors(dry_run=args.dry_run)

    if args.scope in ("all", "user"):
        for os_tag in os_list:
            if os_tag == "windows":
                if win_ctx is None:
                    print("[warn] Windows host environment not reachable from this machine; skipping --os windows", file=sys.stderr)
                    continue
                apply_for_os("windows", win_ctx, targets, dry_run=args.dry_run, uninstall=args.uninstall)
            elif os_tag in ("wsl", "linux"):
                if posix_ctx is not None:
                    apply_for_os(os_tag, posix_ctx, targets, dry_run=args.dry_run, uninstall=args.uninstall)
                elif current_plat == "windows" and wsl_from_win:
                    wsl_bin = cluster_lib.resolve_host_binary("wsl.exe")
                    posix_repo = cluster_lib.normalize_local_path(REPO_ROOT, "wsl")
                    cmd = [
                        wsl_bin,
                        "-e",
                        "python3",
                        f"{posix_repo}/scripts/install.py",
                        "--os",
                        "wsl",
                        "--targets",
                        ",".join(sorted(targets)),
                        "--scope",
                        "user",
                    ]
                    if args.dry_run:
                        cmd.append("--dry-run")
                    if args.uninstall:
                        cmd.append("--uninstall")
                    print(f"[bridge:windows->wsl] {' '.join(cmd)}")
                    r = subprocess.run(cmd, stdin=subprocess.DEVNULL)
                    if r.returncode != 0:
                        return r.returncode
                else:
                    print(f"[warn] POSIX/WSL environment not reachable for --os {os_tag}; skipping", file=sys.stderr)

    if args.serving:
        setup_script = os.path.join(REPO_ROOT, "scripts", "setup_clients.py")
        gw_ctl = os.path.join(REPO_ROOT, "tools", "gateway", "gateway_ctl.py")
        sc_args = [sys.executable, setup_script, "--os", args.os_targets, "--clients", args.clients]
        if args.win_use_wsl_python:
            sc_args.append("--win-use-wsl-python")
        if args.dry_run:
            sc_args.append("--dry-run")
        if args.uninstall:
            sc_args.append("--uninstall")
        r = subprocess.run(sc_args, stdin=subprocess.DEVNULL)
        if r.returncode != 0:
            return r.returncode

        if args.uninstall:
            if not args.dry_run:
                subprocess.run([sys.executable, gw_ctl, "stop"], stdin=subprocess.DEVNULL)
                subprocess.run([sys.executable, gw_ctl, "uninstall-autostart"], stdin=subprocess.DEVNULL)
        else:
            if args.dry_run:
                subprocess.run([sys.executable, gw_ctl, "install-autostart", "--dry-run"], stdin=subprocess.DEVNULL)
            else:
                subprocess.run([sys.executable, gw_ctl, "install-autostart"], stdin=subprocess.DEVNULL, check=True)
                subprocess.run([sys.executable, gw_ctl, "restart"], stdin=subprocess.DEVNULL, check=True)
                subprocess.run([sys.executable, gw_ctl, "check-lmlink"], stdin=subprocess.DEVNULL)

    if args.doctor and not args.dry_run and not args.uninstall:
        doctor_script = os.path.join(REPO_ROOT, "scripts", "doctor.py")
        r = subprocess.run([sys.executable, doctor_script, "--all", "--check-privacy"], stdin=subprocess.DEVNULL)
        return r.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
