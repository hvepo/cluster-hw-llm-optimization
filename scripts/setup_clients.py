#!/usr/bin/env python3
"""Universal client provisioner exposing the dual-node LM Link Cluster Gateway across Windows and WSL.

Non-destructively configures:
- opencode   : OpenCode v2.3.2+ provider.cluster (opencode.json on WSL & Windows)
- vscode     : VS Code Copilot BYOK customendpoint entry (chatLanguageModels.json)
- continue   : Continue IDE chat/edit/autocomplete/embed roles (config.yaml, backed up first)
- lmstudio   : LM Studio mcp.json (adds cluster-hw-llm alongside existing servers like browse)
- env        : Opt-in shell & PowerShell helpers (~/.config/cluster-llm/env.sh + env.ps1)
- codex      : Optional Codex CLI profile (~/.codex/config.toml)
"""
import argparse
import os
import shutil
import subprocess
import sys
import time

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(SCRIPTS_DIR, ".."))
TOOLS_DIR = os.path.join(REPO_ROOT, "tools")
for d in (SCRIPTS_DIR, TOOLS_DIR):
    if d not in sys.path:
        sys.path.insert(0, d)

import cluster_lib  # noqa: E402
import install as inst  # noqa: E402

CLIENT_MODELS = [
    {"id": "cluster/code", "name": "Cluster Code (30B MoE -> 14B)", "ctx": 16384, "out": 4096, "tools": True, "vision": False},
    {"id": "cluster/code-fast", "name": "Cluster Code Fast (14B 72 tok/s)", "ctx": 12288, "out": 4096, "tools": True, "vision": False},
    {"id": "cluster/code-heavy", "name": "Cluster Code Heavy (30B-A3B MoE)", "ctx": 16384, "out": 4096, "tools": True, "vision": False},
    {"id": "cluster/agent-heavy", "name": "Cluster Agent Heavy (30B-A3B MoE)", "ctx": 16384, "out": 4096, "tools": True, "vision": False},
    {"id": "cluster/fast-agent", "name": "Cluster Fast Agent (8B 130 tok/s)", "ctx": 16384, "out": 4096, "tools": True, "vision": False},
    {"id": "cluster/vision", "name": "Cluster Vision (Gemma-4-12B MTP-4)", "ctx": 16384, "out": 4096, "tools": True, "vision": True},
    {"id": "cluster/vision-heavy", "name": "Cluster Vision Heavy (26B 32K MTP-3)", "ctx": 32768, "out": 4096, "tools": True, "vision": True},
    {"id": "cluster/reasoning", "name": "Cluster Reasoning (R1-14B Think)", "ctx": 8192, "out": 4096, "tools": True, "vision": False},
    {"id": "cluster/math", "name": "Cluster Math (Phi-4 14B)", "ctx": 8192, "out": 4096, "tools": False, "vision": False},
]

VALID_CLIENTS = {"opencode", "vscode", "continue", "lmstudio", "env", "codex"}


def parse_clients(raw):
    items = {c.strip().lower() for c in (raw or "all").split(",") if c.strip()}
    if not items or "all" in items:
        return {"opencode", "vscode", "continue", "lmstudio", "env"}
    unknown = items - VALID_CLIENTS
    if unknown:
        raise ValueError(f"Unknown client(s): {sorted(unknown)}. Valid: {sorted(VALID_CLIENTS | {'all'})}")
    return items


def _backup_if_needed(path, dry_run=False):
    if dry_run or not os.path.isfile(path):
        return
    stamp = time.strftime("%Y%m%d-%H%M%S")
    bak = f"{path}.{stamp}.bak"
    shutil.copy2(path, bak)


def _opencode_dirs(ctx, os_tag):
    home = ctx["home_local"]
    dirs = []
    if os_tag == "windows":
        dirs.append(os.path.join(home, ".config", "opencode"))
        appdata_oc = os.path.join(ctx["appdata_local"], "opencode")
        if os.path.isdir(appdata_oc):
            dirs.append(appdata_oc)
    else:
        if os.environ.get("CLUSTER_INSTALL_POSIX_HOME"):
            base_cfg = os.path.join(home, ".config")
        else:
            base_cfg = os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
        dirs.append(os.path.join(base_cfg, "opencode"))
    return dirs


def setup_opencode(ctx, os_tag, endpoint, dry_run=False, uninstall=False):
    v1_url = endpoint.rstrip("/") + "/v1"
    for oc_dir in _opencode_dirs(ctx, os_tag):
        oc_cfg = os.path.join(oc_dir, "opencode.json")
        if uninstall:
            if not os.path.isfile(oc_cfg):
                continue
            data = inst.load_json_safe(oc_cfg, {})
            prov = data.get("provider")
            if isinstance(prov, dict) and "cluster" in prov:
                print(f"[{os_tag}:opencode] remove provider.cluster from {oc_cfg}")
                del prov["cluster"]
                if not prov:
                    data.pop("provider", None)
                inst.write_json_atomic(oc_cfg, data, dry_run=dry_run)
            continue

        print(f"[{os_tag}:opencode] merge provider.cluster -> {oc_cfg} ({v1_url})")
        data = inst.load_json_safe(oc_cfg, {})
        if not isinstance(data.get("provider"), dict):
            data["provider"] = {}
        models_map = {}
        for m in CLIENT_MODELS:
            short_id = m["id"].split("/", 1)[1] if m["id"].startswith("cluster/") else m["id"]
            entry = {
                "id": m["id"],
                "name": m["name"],
                "tool_call": m["tools"],
                "limit": {"context": m["ctx"], "output": m["out"]},
            }
            if m["vision"]:
                entry["modalities"] = {"input": ["text", "image"], "output": ["text"]}
            models_map[short_id] = entry

        data["provider"]["cluster"] = {
            "npm": "@ai-sdk/openai-compatible",
            "name": "Local Cluster (Torre + Portatil)",
            "options": {
                "baseURL": v1_url,
                "apiKey": "local-cluster",
            },
            "models": models_map,
        }
        inst.write_json_atomic(oc_cfg, data, dry_run=dry_run)


def _vscode_chat_lm_paths(ctx, os_tag):
    paths = []
    if os_tag == "windows":
        paths.append(os.path.join(ctx["appdata_local"], "Code", "User", "chatLanguageModels.json"))
        ins = os.path.join(ctx["appdata_local"], "Code - Insiders")
        if os.path.isdir(ins):
            paths.append(os.path.join(ins, "User", "chatLanguageModels.json"))
    else:
        code_cfg = os.path.join(ctx["home_local"], ".config", "Code")
        if os.path.isdir(code_cfg) or os.environ.get("CLUSTER_INSTALL_POSIX_HOME"):
            paths.append(os.path.join(code_cfg, "User", "chatLanguageModels.json"))
        vsc_srv = os.path.join(ctx["home_local"], ".vscode-server")
        if os.path.isdir(vsc_srv):
            paths.append(os.path.join(vsc_srv, "data", "User", "chatLanguageModels.json"))
    return paths


def setup_vscode_byok(ctx, os_tag, endpoint, dry_run=False, uninstall=False):
    chat_url = endpoint.rstrip("/") + "/v1/chat/completions"
    provider_name = "Local Cluster (LM Link)"
    for path in _vscode_chat_lm_paths(ctx, os_tag):
        if uninstall:
            if not os.path.isfile(path):
                continue
            arr = inst.load_json_safe(path, [])
            if not isinstance(arr, list):
                continue
            filtered = [item for item in arr if not (isinstance(item, dict) and item.get("name") == provider_name)]
            if len(filtered) != len(arr):
                print(f"[{os_tag}:vscode-byok] remove '{provider_name}' from {path}")
                inst.write_json_atomic(path, filtered, dry_run=dry_run)
            continue

        print(f"[{os_tag}:vscode-byok] merge '{provider_name}' -> {path}")
        arr = inst.load_json_safe(path, [])
        if not isinstance(arr, list):
            arr = []

        # Drop empty stub named 'lmstudio' (id == '') or previous cluster entry, preserving all real providers.
        cleaned = []
        for item in arr:
            if not isinstance(item, dict):
                continue
            if item.get("name") == provider_name:
                continue
            if item.get("name") == "lmstudio" and item.get("vendor") == "customendpoint":
                ms = item.get("models") or []
                if len(ms) == 1 and not (ms[0].get("id") or "").strip() and not (ms[0].get("url") or "").strip():
                    continue
            cleaned.append(item)

        cluster_entry = {
            "name": provider_name,
            "vendor": "customendpoint",
            "apiKey": "local-cluster",
            "apiType": "chat-completions",
            "models": [
                {
                    "id": m["id"],
                    "name": m["name"],
                    "url": chat_url,
                    "toolCalling": m["tools"],
                    "vision": m["vision"],
                    "maxInputTokens": m["ctx"],
                    "maxOutputTokens": m["out"],
                }
                for m in CLIENT_MODELS
            ],
        }
        cleaned.append(cluster_entry)
        inst.write_json_atomic(path, cleaned, dry_run=dry_run)


def setup_continue(ctx, os_tag, endpoint, dry_run=False, uninstall=False):
    cont_dir = os.path.join(ctx["home_local"], ".continue")
    cfg_path = os.path.join(cont_dir, "config.yaml")
    if os_tag != "windows" and not os.path.isdir(cont_dir) and not os.environ.get("CLUSTER_INSTALL_POSIX_HOME"):
        return

    if uninstall:
        if os.path.isfile(cfg_path):
            print(f"[{os_tag}:continue] leaving {cfg_path} intact (backups preserved in {cont_dir})")
        return

    v1_url = endpoint.rstrip("/") + "/v1"
    yaml_text = (
        "# Managed by cluster-hw-llm-optimization/scripts/setup_clients.py\n"
        "name: Cluster Local Pool (Torre + Portatil)\n"
        "version: 1.0.0\n"
        "schema: v1\n\n"
        "models:\n"
        "  - name: Cluster Code (30B MoE -> 14B)\n"
        "    provider: lmstudio\n"
        "    model: cluster/code\n"
        f"    apiBase: {v1_url}\n"
        "    roles:\n"
        "      - chat\n"
        "      - edit\n"
        "      - apply\n"
        "  - name: Cluster Code Fast (14B 72 tok/s)\n"
        "    provider: lmstudio\n"
        "    model: cluster/code-fast\n"
        f"    apiBase: {v1_url}\n"
        "    roles:\n"
        "      - chat\n"
        "      - edit\n"
        "      - apply\n"
        "  - name: Cluster Fast Agent (8B 130 tok/s)\n"
        "    provider: lmstudio\n"
        "    model: cluster/fast-agent\n"
        f"    apiBase: {v1_url}\n"
        "    roles:\n"
        "      - chat\n"
        "      - edit\n"
        "      - apply\n"
        "  - name: Cluster Vision Heavy (26B 32K)\n"
        "    provider: lmstudio\n"
        "    model: cluster/vision-heavy\n"
        f"    apiBase: {v1_url}\n"
        "    roles:\n"
        "      - chat\n"
        "  - name: Cluster Reasoning (R1-14B)\n"
        "    provider: lmstudio\n"
        "    model: cluster/reasoning\n"
        f"    apiBase: {v1_url}\n"
        "    roles:\n"
        "      - chat\n"
        "  - name: Cluster Tab Autocomplete\n"
        "    provider: lmstudio\n"
        "    model: cluster/autocomplete\n"
        f"    apiBase: {v1_url}\n"
        "    roles:\n"
        "      - autocomplete\n"
        "  - name: Cluster Embeddings\n"
        "    provider: lmstudio\n"
        "    model: cluster/embed\n"
        f"    apiBase: {v1_url}\n"
        "    roles:\n"
        "      - embed\n\n"
        "context:\n"
        "  - provider: code\n"
        "  - provider: docs\n"
        "  - provider: diff\n"
        "  - provider: terminal\n"
        "  - provider: problems\n"
        "  - provider: folder\n"
        "  - provider: codebase\n"
    )
    print(f"[{os_tag}:continue] update {cfg_path} (backup + schema v1 roles -> {v1_url})")
    if not dry_run:
        os.makedirs(cont_dir, exist_ok=True)
        _backup_if_needed(cfg_path, dry_run=False)
        with open(cfg_path, "w", encoding="utf-8") as f:
            f.write(yaml_text)


def setup_lmstudio_mcp(ctx, os_tag, dry_run=False, uninstall=False):
    lms_dir = os.path.join(ctx["home_local"], ".lmstudio")
    if os_tag != "windows" and not os.path.isdir(lms_dir) and not os.environ.get("CLUSTER_INSTALL_POSIX_HOME"):
        return
    mcp_path = os.path.join(lms_dir, "mcp.json")
    if uninstall:
        if not os.path.isfile(mcp_path):
            return
        data = inst.load_json_safe(mcp_path, {})
        srvs = data.get("mcpServers")
        if isinstance(srvs, dict) and "cluster-hw-llm" in srvs:
            print(f"[{os_tag}:lmstudio-mcp] remove cluster-hw-llm from {mcp_path}")
            del srvs["cluster-hw-llm"]
            inst.write_json_atomic(mcp_path, data, dry_run=dry_run)
        return

    print(f"[{os_tag}:lmstudio-mcp] merge mcpServers.cluster-hw-llm -> {mcp_path}")
    data = inst.load_json_safe(mcp_path, {})
    if not isinstance(data.get("mcpServers"), dict):
        data["mcpServers"] = {}
    data["mcpServers"]["cluster-hw-llm"] = {
        "command": ctx["mcp_command"],
        "args": ctx["mcp_args"],
        "env": {"CLUSTER_REPO_ROOT": ctx["repo_str"]},
    }
    inst.write_json_atomic(mcp_path, data, dry_run=dry_run)


def setup_shell_helpers(ctx, os_tag, endpoint, dry_run=False, uninstall=False):
    base = endpoint.rstrip("/")
    v1 = base + "/v1"
    api_v1 = base + "/api/v1"
    cfg_dir = os.path.join(ctx["home_local"], ".config", "cluster-llm")

    if os_tag == "windows":
        target = os.path.join(cfg_dir, "env.ps1")
        if uninstall:
            if os.path.isfile(target):
                print(f"[windows:env] remove {target}")
                if not dry_run:
                    os.remove(target)
            return
        win_root = ctx["repo_str"]
        win_cmd = ctx["mcp_command"]
        pre_args = " ".join(f'"{a}"' for a in ctx["mcp_args"][:-1])
        chat_script = win_root.rstrip("\\") + r"\tools\gateway\cluster_chat.py"
        ctl_script = win_root.rstrip("\\") + r"\tools\cluster_ctl.py"
        gw_ctl_script = win_root.rstrip("\\") + r"\tools\gateway\gateway_ctl.py"
        ps_body = (
            "# Opt-in Local Cluster helpers for PowerShell\n"
            f"$env:CLUSTER_API_BASE = '{base}'\n"
            f"$env:CLUSTER_OPENAI_BASE = '{v1}'\n"
            f"$env:CLUSTER_LMS_API_V1 = '{api_v1}'\n"
            "function use-cluster-openai {\n"
            f"  $env:OPENAI_BASE_URL = '{v1}'\n"
            "  $env:OPENAI_API_KEY = 'local-cluster'\n"
            "  Write-Host \"OPENAI_BASE_URL=$env:OPENAI_BASE_URL\"\n"
            "}\n"
            "function cluster-status { & " + f'"{win_cmd}" {pre_args} "{gw_ctl_script}" status @args' + " }\n"
            "function cluster-chat { & " + f'"{win_cmd}" {pre_args} "{chat_script}" @args' + " }\n"
            "function cluster-preset { param([string]$Name='home-dual') & " + f'"{win_cmd}" {pre_args} "{ctl_script}" preset $Name' + " }\n"
            "function cluster-claude {\n"
            "  param([Parameter(ValueFromRemainingArguments=$true)][string[]]$Rest)\n"
            f"  $env:ANTHROPIC_BASE_URL = '{base}'\n"
            "  $env:ANTHROPIC_AUTH_TOKEN = 'local-cluster'\n"
            "  if (-not $env:ANTHROPIC_MODEL) { $env:ANTHROPIC_MODEL = 'cluster/code' }\n"
            "  if (-not $env:ANTHROPIC_SMALL_FAST_MODEL) { $env:ANTHROPIC_SMALL_FAST_MODEL = 'cluster/fast-agent' }\n"
            "  & claude @Rest\n"
            "}\n"
        )
        print(f"[windows:env] write opt-in helpers -> {target}")
        if not dry_run:
            os.makedirs(cfg_dir, exist_ok=True)
            with open(target, "w", encoding="utf-8") as f:
                f.write(ps_body)
        return

    target = os.path.join(cfg_dir, "env.sh")
    if uninstall:
        if os.path.isfile(target):
            print(f"[{os_tag}:env] remove {target}")
            if not dry_run:
                os.remove(target)
        return
    posix_repo = ctx["repo_str"]
    py = ctx["mcp_command"]
    sh_body = (
        "# Opt-in Local Cluster helpers for bash/zsh (source ~/.config/cluster-llm/env.sh)\n"
        f'export CLUSTER_API_BASE="{base}"\n'
        f'export CLUSTER_OPENAI_BASE="{v1}"\n'
        f'export CLUSTER_LMS_API_V1="{api_v1}"\n'
        "use-cluster-openai() {\n"
        f'  export OPENAI_BASE_URL="{v1}"\n'
        '  export OPENAI_API_KEY="local-cluster"\n'
        '  echo "OPENAI_BASE_URL=$OPENAI_BASE_URL"\n'
        "}\n"
        f'cluster-status() {{ "{py}" "{posix_repo}/tools/gateway/gateway_ctl.py" status "$@"; }}\n'
        f'cluster-chat() {{ "{py}" "{posix_repo}/tools/gateway/cluster_chat.py" "$@"; }}\n'
        f'cluster-preset() {{ "{py}" "{posix_repo}/tools/cluster_ctl.py" preset "${{1:-home-dual}}"; }}\n'
        "cluster-claude() {\n"
        f'  ANTHROPIC_BASE_URL="{base}" ANTHROPIC_AUTH_TOKEN="local-cluster" \\\n'
        '  ANTHROPIC_MODEL="${ANTHROPIC_MODEL:-cluster/code}" \\\n'
        '  ANTHROPIC_SMALL_FAST_MODEL="${ANTHROPIC_SMALL_FAST_MODEL:-cluster/fast-agent}" \\\n'
        '  claude "$@"\n'
        "}\n"
        "cluster-codex() {\n"
        '  CLUSTER_API_KEY="local-cluster" codex -c model_provider="cluster" "$@"\n'
        "}\n"
        "cluster-aider() {\n"
        f'  OPENAI_API_BASE="{v1}" OPENAI_API_KEY="local-cluster" \\\n'
        '  aider --model "openai/${CLUSTER_CODE_MODEL:-cluster/code}" --weak-model "openai/cluster/fast-agent" "$@"\n'
        "}\n"
    )
    print(f"[{os_tag}:env] write opt-in helpers -> {target}")
    if not dry_run:
        os.makedirs(cfg_dir, exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            f.write(sh_body)


def setup_codex(ctx, os_tag, endpoint, dry_run=False, uninstall=False):
    codex_dir = os.path.join(ctx["home_local"], ".codex")
    cfg_path = os.path.join(codex_dir, "config.toml")
    marker_start = "# BEGIN cluster-hw-llm"
    marker_end = "# END cluster-hw-llm"

    if uninstall:
        if not os.path.isfile(cfg_path):
            return
        txt = open(cfg_path, encoding="utf-8").read()
        if marker_start in txt and marker_end in txt:
            pre = txt.split(marker_start, 1)[0].rstrip()
            post = txt.split(marker_end, 1)[1].lstrip()
            new_txt = (pre + "\n\n" + post).strip() + "\n" if (pre or post) else ""
            print(f"[{os_tag}:codex] remove cluster block from {cfg_path}")
            if not dry_run:
                with open(cfg_path, "w", encoding="utf-8") as f:
                    f.write(new_txt)
        return

    v1 = endpoint.rstrip("/") + "/v1"
    block = (
        f"{marker_start}\n"
        "[model_providers.cluster]\n"
        'name = "Local Cluster (Torre + Portatil)"\n'
        f'base_url = "{v1}"\n'
        'env_key = "CLUSTER_API_KEY"\n'
        'wire_api = "responses"\n'
        f"{marker_end}\n"
    )
    print(f"[{os_tag}:codex] configure [model_providers.cluster] -> {cfg_path}")
    if not dry_run:
        os.makedirs(codex_dir, exist_ok=True)
        existing = open(cfg_path, encoding="utf-8").read() if os.path.isfile(cfg_path) else ""
        if marker_start in existing and marker_end in existing:
            pre = existing.split(marker_start, 1)[0].rstrip()
            post = existing.split(marker_end, 1)[1].lstrip()
            out = (pre + "\n\n" if pre else "") + block + (("\n" + post) if post else "")
        else:
            out = (existing.rstrip() + "\n\n" if existing.strip() else "") + block
        with open(cfg_path, "w", encoding="utf-8") as f:
            f.write(out)


def apply_clients_for_os(os_tag, ctx, clients, endpoint, dry_run=False, uninstall=False):
    if "opencode" in clients:
        setup_opencode(ctx, os_tag, endpoint, dry_run=dry_run, uninstall=uninstall)
    if "vscode" in clients:
        setup_vscode_byok(ctx, os_tag, endpoint, dry_run=dry_run, uninstall=uninstall)
    if "continue" in clients:
        setup_continue(ctx, os_tag, endpoint, dry_run=dry_run, uninstall=uninstall)
    if "lmstudio" in clients:
        setup_lmstudio_mcp(ctx, os_tag, dry_run=dry_run, uninstall=uninstall)
    if "env" in clients:
        setup_shell_helpers(ctx, os_tag, endpoint, dry_run=dry_run, uninstall=uninstall)
    if "codex" in clients:
        setup_codex(ctx, os_tag, endpoint, dry_run=dry_run, uninstall=uninstall)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Configure IDEs, coding agents, and shell helpers for the Cluster Gateway")
    ap.add_argument("--os", dest="os_targets", default="auto", help="OS targets: auto,windows,wsl,linux,all")
    ap.add_argument("--clients", default="all", help="Comma-separated: opencode,vscode,continue,lmstudio,env,codex,all")
    ap.add_argument("--endpoint", default="", help="Override gateway base URL (default: http://127.0.0.1:1235)")
    ap.add_argument("--direct", action="store_true", help="Point clients directly at LM Studio :1234 instead of gateway :1235")
    ap.add_argument("--win-use-wsl-python", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--uninstall", action="store_true")
    args = ap.parse_args(argv)

    cfg = cluster_lib.load_cluster_config(REPO_ROOT)
    gw = cfg.get("gateway") or {}
    default_gw = f"http://{gw.get('host', '127.0.0.1')}:{int(gw.get('port', 1235))}"
    endpoint = (args.endpoint or (gw.get("upstream_url") if args.direct else default_gw) or "http://127.0.0.1:1235").rstrip("/")

    current_plat = cluster_lib.detect_platform()
    clients = parse_clients(args.clients)
    win_ctx = inst.detect_windows_context(current_plat, force_wsl_python=args.win_use_wsl_python)
    posix_ctx = inst.detect_posix_context(current_plat)
    wsl_from_win = inst.wsl_available_from_windows()

    os_list = inst.parse_os_list(
        args.os_targets,
        current_plat=current_plat,
        win_ctx_available=(win_ctx is not None),
        wsl_bridge_available=(posix_ctx is not None or wsl_from_win),
    )

    for os_tag in os_list:
        if os_tag == "windows":
            if win_ctx is None:
                print("[warn] Windows host not reachable; skipping --os windows", file=sys.stderr)
                continue
            apply_clients_for_os("windows", win_ctx, clients, endpoint, dry_run=args.dry_run, uninstall=args.uninstall)
        elif os_tag in ("wsl", "linux"):
            if posix_ctx is not None:
                apply_clients_for_os(os_tag, posix_ctx, clients, endpoint, dry_run=args.dry_run, uninstall=args.uninstall)
            elif current_plat == "windows" and wsl_from_win:
                wsl_bin = cluster_lib.resolve_host_binary("wsl.exe")
                posix_repo = cluster_lib.normalize_local_path(REPO_ROOT, "wsl")
                cmd = [
                    wsl_bin, "-e", "python3", f"{posix_repo}/scripts/setup_clients.py",
                    "--os", "wsl", "--clients", ",".join(sorted(clients)), "--endpoint", endpoint,
                ]
                if args.dry_run:
                    cmd.append("--dry-run")
                if args.uninstall:
                    cmd.append("--uninstall")
                r = subprocess.run(cmd, stdin=subprocess.DEVNULL)
                if r.returncode != 0:
                    return r.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
