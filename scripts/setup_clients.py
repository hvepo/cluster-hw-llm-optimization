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
import json
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
import install_continue  # noqa: E402

CLIENT_MODELS = [
    {"id": "cluster/code", "name": "Cluster Code (30B MoE -> 14B)", "ctx": 16384, "out": 4096, "tools": True, "vision": False},
    {"id": "cluster/fast-agent", "name": "Cluster Fast Agent (8B 130 tok/s)", "ctx": 16384, "out": 4096, "tools": True, "vision": False},
    {"id": "cluster/vision-heavy", "name": "Cluster Vision Heavy (26B 32K)", "ctx": 32768, "out": 4096, "tools": True, "vision": True},
    {"id": "cluster/reasoning", "name": "Cluster Reasoning (R1-14B Think)", "ctx": 8192, "out": 4096, "tools": True, "vision": False},
    {"id": "cluster/math", "name": "Cluster Math (Phi-4 14B)", "ctx": 8192, "out": 4096, "tools": False, "vision": False},
]

# NVIDIA NIM cloud resources via the Cluster Router Gateway (:1235), gated on the NVIDIA NIM API key.
NIM_CLIENT_MODELS = [
    {"id": "nim/ultra", "upstream_id": "nvidia/nemotron-3-ultra-550b-a55b", "name": "NIM Ultra (550B Cloud Orchestrator)", "ctx": 131072, "out": 16384, "tools": True, "vision": False},
    {"id": "nim/fast", "upstream_id": "nvidia/nemotron-3.5-lightning-30b-a3b", "name": "NIM Fast (30B Cloud Lightning)", "ctx": 131072, "out": 8192, "tools": True, "vision": False},
    {"id": "nim/code", "upstream_id": "poolside/laguna-xs-2.1", "name": "NIM Code (Laguna-XS Cloud Coder)", "ctx": 65536, "out": 8192, "tools": True, "vision": False},
    {"id": "nim/vision", "upstream_id": "nvidia/nemotron-3.5-super-vl-120b-a12b", "name": "NIM Vision (120B Cloud VL)", "ctx": 131072, "out": 8192, "tools": True, "vision": True},
    {"id": "nim/reasoning", "upstream_id": "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning", "name": "NIM Reasoning (30B Cloud CoT)", "ctx": 131072, "out": 8192, "tools": True, "vision": False},
]
NIM_CHAT_MODELS = list(NIM_CLIENT_MODELS)

# Built-in / auto-discovered / legacy OpenCode providers hidden so OpenCode presents pure Cluster Mode.
DENIED_OPENCODE_PROVIDERS = ("lmstudio", "ollama", "opencode", "nvidia", "nvidia-nim", "sglang")
LEGACY_OPENCODE_PROVIDER_KEYS = ("nvidia", "nvidia-nim", "lmstudio", "sglang", "ollama")

NIM_OPENAI_BASE = "https://integrate.api.nvidia.com/v1"
NIM_OPENAI_BASE_CHAT = NIM_OPENAI_BASE + "/chat/completions"

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
            changed = False
            prov = data.get("provider")
            if isinstance(prov, dict):
                for key_name in ("cluster", "nvidia-nim"):
                    if key_name in prov:
                        print(f"[{os_tag}:opencode] remove provider.{key_name} from {oc_cfg}")
                        del prov[key_name]
                        changed = True
                if not prov:
                    data.pop("provider", None)
            if isinstance(data.get("policies"), list):
                kept = [
                    pol for pol in data["policies"]
                    if not (
                        isinstance(pol, dict)
                        and pol.get("action") == "provider.use"
                        and pol.get("effect") == "deny"
                        and pol.get("resource") in DENIED_OPENCODE_PROVIDERS
                    )
                ]
                if len(kept) != len(data["policies"]):
                    changed = True
                    if kept:
                        data["policies"] = kept
                    else:
                        data.pop("policies", None)
            if data.get("model") == "cluster/code":
                data.pop("model", None)
                changed = True
            if changed:
                inst.write_json_atomic(oc_cfg, data, dry_run=dry_run)
            continue

        print(f"[{os_tag}:opencode] enforce unified 10-model Cluster Mode (hide raw/free providers) -> {oc_cfg} ({v1_url})")
        data = inst.load_json_safe(oc_cfg, {})

        # Clean up pre-existing legacy v2 `providers` & duplicate `provider` entries on PC/Laptop.
        data.pop("disabled_providers", None)
        data.pop("enabled_providers", None)
        legacy_provs = data.get("providers")
        if isinstance(legacy_provs, dict):
            for lk in LEGACY_OPENCODE_PROVIDER_KEYS:
                legacy_provs.pop(lk, None)
            if not legacy_provs:
                data.pop("providers", None)

        if not isinstance(data.get("provider"), dict):
            data["provider"] = {}
        for lk in LEGACY_OPENCODE_PROVIDER_KEYS:
            data["provider"].pop(lk, None)

        cur_model = str(data.get("model") or "").strip()
        if not cur_model or cur_model.startswith(("lmstudio/", "ollama/", "nvidia/", "nvidia-nim/", "sglang/", "opencode/")):
            data["model"] = "cluster/code"

        def _oc_entry(m, model_id=None):
            entry = {
                "id": model_id or m["id"],
                "name": m["name"],
                "tool_call": m["tools"],
                "limit": {"context": m["ctx"], "output": m["out"]},
            }
            if m["vision"]:
                entry["modalities"] = {"input": ["text", "image"], "output": ["text"]}
            return entry

        models_map = {}
        for m in CLIENT_MODELS:
            short_id = m["id"].split("/", 1)[1] if m["id"].startswith("cluster/") else m["id"]
            models_map[short_id] = _oc_entry(m)

        nim_key = cluster_lib.resolve_nvidia_api_key()
        if nim_key:
            for m in NIM_CHAT_MODELS:
                models_map[m["id"]] = _oc_entry(m)

        data["provider"]["cluster"] = {
            "npm": "@ai-sdk/openai-compatible",
            "name": "Cluster (Local + NIM)",
            "options": {
                "baseURL": v1_url,
                "apiKey": "local-cluster",
            },
            "models": models_map,
        }

        existing_policies = data.get("policies") if isinstance(data.get("policies"), list) else []
        cleaned_policies = [
            pol for pol in existing_policies
            if not (
                isinstance(pol, dict)
                and pol.get("action") == "provider.use"
                and pol.get("resource") in DENIED_OPENCODE_PROVIDERS
            )
        ]
        for prov_id in DENIED_OPENCODE_PROVIDERS:
            cleaned_policies.append({"action": "provider.use", "resource": prov_id, "effect": "deny"})
        data["policies"] = cleaned_policies

        inst.write_json_atomic(oc_cfg, data, dry_run=dry_run)
    _reload_opencode(ctx, os_tag, dry_run=dry_run)


def _reload_opencode(ctx, os_tag, dry_run=False):
    """Best-effort reload of any running OpenCode server after config changes."""
    if dry_run or os.environ.get("CLUSTER_INSTALL_POSIX_HOME") or os.environ.get("CLUSTER_INSTALL_WIN_HOME"):
        return
    try:
        if os_tag == "windows":
            oc_cmd = os.path.join(ctx["home_local"], "bin", "opencode.cmd")
            if not os.path.isfile(oc_cmd):
                return
            win_cmd = cluster_lib.normalize_local_path(oc_cmd, "windows")
            cmd_bin = cluster_lib.resolve_host_binary("cmd.exe")
            subprocess.run([cmd_bin, "/c", win_cmd, "reload"], stdin=subprocess.DEVNULL, capture_output=True, timeout=5)
        else:
            oc_bin = os.path.join(ctx["home_local"], ".opencode", "bin", "opencode")
            if not (os.path.isfile(oc_bin) and os.access(oc_bin, os.X_OK)):
                oc_bin = shutil.which("opencode") or ""
            if not oc_bin:
                return
            subprocess.run([oc_bin, "reload"], stdin=subprocess.DEVNULL, capture_output=True, timeout=5)
    except Exception:
        pass


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
            filtered = [
                item
                for item in arr
                if not (isinstance(item, dict) and item.get("name") in (provider_name, "NVIDIA NIM Cloud"))
            ]
            if len(filtered) != len(arr):
                print(f"[{os_tag}:vscode-byok] remove '{provider_name}' (+ 'NVIDIA NIM Cloud') from {path}")
                inst.write_json_atomic(path, filtered, dry_run=dry_run)
            continue

        print(f"[{os_tag}:vscode-byok] merge '{provider_name}' -> {path}")
        arr = inst.load_json_safe(path, [])
        if not isinstance(arr, list):
            arr = []

        # Drop empty stub entries ('lmstudio', 'NVIDIA NIM', 'OpenRouter') or previous cluster entries, preserving real providers.
        cleaned = []
        for item in arr:
            if not isinstance(item, dict):
                continue
            item_name = item.get("name")
            if item_name in (provider_name, "NVIDIA NIM Cloud", "Cluster (Local + NIM)"):
                continue
            ms = item.get("models") or []
            if item_name in ("lmstudio", "NVIDIA NIM", "OpenRouter"):
                if not ms or (len(ms) == 1 and not (ms[0].get("id") or "").strip() and not (ms[0].get("url") or "").strip()):
                    continue
            cleaned.append(item)

        nim_key = cluster_lib.resolve_nvidia_api_key()
        byok_models = CLIENT_MODELS + (NIM_CHAT_MODELS if nim_key else [])
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
                for m in byok_models
            ],
        }
        cleaned.append(cluster_entry)
        inst.write_json_atomic(path, cleaned, dry_run=dry_run)


def setup_continue(ctx, os_tag, endpoint, dry_run=False, uninstall=False):
    install_continue.install_continue_for_ctx(
        ctx=ctx,
        os_tag=os_tag,
        endpoint=endpoint,
        node="auto",
        dry_run=dry_run,
        uninstall=uninstall,
    )


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


GLOBAL_CLI_SHIMS = (
    ("cluster-peer", "tools/peer/peer_ctl.py"),
    ("cluster-telegram", "tools/peer/openclaw_ctl.py"),
    ("cluster-llm", "tools/cluster_ctl.py"),
    ("cluster-gateway", "tools/gateway/gateway_ctl.py"),
    ("cluster-chat", "tools/gateway/cluster_chat.py"),
    ("cluster-hw", "tools/hw/hardware_probe.py"),
    ("cluster-doctor", "scripts/doctor.py"),
)
SHIM_MARKER = "# managed-by: cluster-hw-llm-optimization"


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
        agy_script = win_root.rstrip("\\") + r"\tools\gateway\agy_local_agent.py"
        ctl_script = win_root.rstrip("\\") + r"\tools\cluster_ctl.py"
        gw_ctl_script = win_root.rstrip("\\") + r"\tools\gateway\gateway_ctl.py"
        peer_script = win_root.rstrip("\\") + r"\tools\peer\peer_ctl.py"
        tg_script = win_root.rstrip("\\") + r"\tools\peer\openclaw_ctl.py"
        ps_body = (
            "# Opt-in Local Cluster helpers for PowerShell\n"
            f"$env:CLUSTER_API_BASE = '{base}'\n"
            f"$env:CLUSTER_OPENAI_BASE = '{v1}'\n"
            f"$env:CLUSTER_LMS_API_V1 = '{api_v1}'\n"
            "# NVIDIA NIM cloud helpers (requires %USERPROFILE%\\.config\\nvidia_api_key or $env:NVIDIA_API_KEY)\n"
            "if (-not $env:NVIDIA_API_KEY) {\n"
            "  $nvKeyFile = Join-Path $env:USERPROFILE '.config\\nvidia_api_key'\n"
            "  if (Test-Path $nvKeyFile) { $env:NVIDIA_API_KEY = (Get-Content -Raw $nvKeyFile).Trim() }\n"
            "}\n"
            "function use-nvidia-nim {\n"
            "  if (-not $env:NVIDIA_API_KEY) { Write-Host 'use-nvidia-nim: NVIDIA_API_KEY not set' -ForegroundColor Yellow; return 1 }\n"
            "  $env:OPENAI_BASE_URL = 'https://integrate.api.nvidia.com/v1'\n"
            "  $env:OPENAI_API_KEY = $env:NVIDIA_API_KEY\n"
            "  Write-Host \"OPENAI_BASE_URL=$env:OPENAI_BASE_URL (NVIDIA NIM cloud)\"\n"
            "}\n"
            "function nim-chat { cluster-chat --model nim/ultra @args }\n"
            "function nim-orchestrate { cluster-chat --orchestrate --model nim/ultra @args }\n"
            "function nim-aider {\n"
            "  if (-not $env:NVIDIA_API_KEY) { Write-Host 'nim-aider: NVIDIA_API_KEY not set' -ForegroundColor Yellow; return 1 }\n"
            "  $env:OPENAI_API_BASE = 'https://integrate.api.nvidia.com/v1'\n"
            "  $env:OPENAI_API_KEY = $env:NVIDIA_API_KEY\n"
            "  aider --model 'openai/nvidia/nemotron-3-ultra-550b-a55b' @args\n"
            "}\n"
            "function use-cluster-openai {\n"
            f"  $env:OPENAI_BASE_URL = '{v1}'\n"
            "  $env:OPENAI_API_KEY = 'local-cluster'\n"
            "  Write-Host \"OPENAI_BASE_URL=$env:OPENAI_BASE_URL\"\n"
            "}\n"
            "function cluster-status { & " + f'"{win_cmd}" {pre_args} "{gw_ctl_script}" status @args' + " }\n"
            "function cluster-chat { & " + f'"{win_cmd}" {pre_args} "{chat_script}" @args' + " }\n"
            "function cluster-agy { & " + f'"{win_cmd}" {pre_args} "{agy_script}" @args' + " }\n"
            "function cluster-preset { param([string]$Name='home-dual') & " + f'"{win_cmd}" {pre_args} "{ctl_script}" preset $Name' + " }\n"
            "function cluster-peer { & " + f'"{win_cmd}" {pre_args} "{peer_script}" @args' + " }\n"
            "function cluster-telegram { & " + f'"{win_cmd}" {pre_args} "{tg_script}" @args' + " }\n"
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
    bin_dir = os.path.join(ctx["home_local"], ".local", "bin")
    if uninstall:
        if os.path.isfile(target):
            print(f"[{os_tag}:env] remove {target}")
            if not dry_run:
                os.remove(target)
        for name, _ in GLOBAL_CLI_SHIMS:
            shim_path = os.path.join(bin_dir, name)
            if os.path.isfile(shim_path):
                try:
                    if SHIM_MARKER in open(shim_path, encoding="utf-8", errors="ignore").read():
                        print(f"[{os_tag}:cli] remove {shim_path}")
                        if not dry_run:
                            os.remove(shim_path)
                except OSError:
                    pass
        return
    posix_repo = ctx["repo_str"].rstrip("/")
    py = ctx["mcp_command"]
    sh_body = (
        "# Opt-in Local Cluster helpers for bash/zsh (source ~/.config/cluster-llm/env.sh)\n"
        f'export CLUSTER_API_BASE="{base}"\n'
        f'export CLUSTER_OPENAI_BASE="{v1}"\n'
        f'export CLUSTER_LMS_API_V1="{api_v1}"\n'
        "# NVIDIA NIM cloud helpers (requires ~/.config/nvidia_api_key or NVIDIA_API_KEY)\n"
        'if [ -z "$NVIDIA_API_KEY" ] && [ -f "$HOME/.config/nvidia_api_key" ]; then\n'
        '  _nim_key="$(tr -d \'\\r\\n\' < "$HOME/.config/nvidia_api_key" 2>/dev/null)"\n'
        '  [ -n "$_nim_key" ] && export NVIDIA_API_KEY="$_nim_key"\n'
        "  unset _nim_key\n"
        "fi\n"
        "use-nvidia-nim() {\n"
        '  if [ -z "$NVIDIA_API_KEY" ]; then echo "use-nvidia-nim: NVIDIA_API_KEY not set (~/.config/nvidia_api_key or NVIDIA_API_KEY)" >&2; return 1; fi\n'
        '  export OPENAI_BASE_URL="https://integrate.api.nvidia.com/v1"\n'
        '  export OPENAI_API_KEY="$NVIDIA_API_KEY"\n'
        '  echo "OPENAI_BASE_URL=$OPENAI_BASE_URL (NVIDIA NIM cloud)"\n'
        "}\n"
        "nim-chat() { cluster-chat --model nim/ultra \"$@\"; }\n"
        "nim-orchestrate() { cluster-chat --orchestrate --model nim/ultra \"$@\"; }\n"
        "nim-aider() {\n"
        '  if [ -z "$NVIDIA_API_KEY" ]; then echo "nim-aider: NVIDIA_API_KEY not set" >&2; return 1; fi\n'
        '  OPENAI_API_BASE="https://integrate.api.nvidia.com/v1" OPENAI_API_KEY="$NVIDIA_API_KEY" aider --model "openai/nvidia/nemotron-3-ultra-550b-a55b" "$@"\n'
        "}\n"
        "use-cluster-openai() {\n"
        f'  export OPENAI_BASE_URL="{v1}"\n'
        '  export OPENAI_API_KEY="local-cluster"\n'
        '  echo "OPENAI_BASE_URL=$OPENAI_BASE_URL"\n'
        "}\n"
        f'cluster-status() {{ "{py}" "{posix_repo}/tools/gateway/gateway_ctl.py" status "$@"; }}\n'
        f'cluster-chat() {{ "{py}" "{posix_repo}/tools/gateway/cluster_chat.py" "$@"; }}\n'
        f'cluster-agy() {{ "{py}" "{posix_repo}/tools/gateway/agy_local_agent.py" "$@"; }}\n'
        f'cluster-preset() {{ "{py}" "{posix_repo}/tools/cluster_ctl.py" preset "${{1:-home-dual}}"; }}\n'
        f'cluster-peer() {{ "{py}" "{posix_repo}/tools/peer/peer_ctl.py" "$@"; }}\n'
        f'cluster-telegram() {{ "{py}" "{posix_repo}/tools/peer/openclaw_ctl.py" "$@"; }}\n'
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
    print(f"[{os_tag}:env] write opt-in helpers -> {target} and PATH shims -> {bin_dir}/cluster-*")
    if not dry_run:
        os.makedirs(cfg_dir, exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            f.write(sh_body)
        os.makedirs(bin_dir, exist_ok=True)
        for name, rel_script in GLOBAL_CLI_SHIMS:
            shim_path = os.path.join(bin_dir, name)
            shim_body = f'#!/usr/bin/env sh\n{SHIM_MARKER}\nexec "{py}" "{posix_repo}/{rel_script}" "$@"\n'
            with open(shim_path, "w", encoding="utf-8") as sf:
                sf.write(shim_body)
            os.chmod(shim_path, 0o755)


def _strip_block(txt, start, end):
    if start in txt and end in txt:
        pre = txt.split(start, 1)[0].rstrip()
        post = txt.split(end, 1)[1].lstrip()
        return (pre + "\n\n" + post).strip() + "\n" if (pre or post) else ""
    return txt


def setup_codex(ctx, os_tag, endpoint, dry_run=False, uninstall=False):
    codex_dir = os.path.join(ctx["home_local"], ".codex")
    cfg_path = os.path.join(codex_dir, "config.toml")
    marker_start = "# BEGIN cluster-hw-llm"
    marker_end = "# END cluster-hw-llm"
    nim_marker_start = "# BEGIN cluster-nim"
    nim_marker_end = "# END cluster-nim"

    if uninstall:
        if not os.path.isfile(cfg_path):
            return
        txt = open(cfg_path, encoding="utf-8").read()
        new_txt = _strip_block(txt, marker_start, marker_end)
        new_txt = _strip_block(new_txt, nim_marker_start, nim_marker_end)
        if new_txt != txt:
            print(f"[{os_tag}:codex] remove cluster blocks from {cfg_path}")
            if not dry_run:
                with open(cfg_path, "w", encoding="utf-8") as f:
                    f.write(new_txt)
        return

    v1 = endpoint.rstrip("/") + "/v1"
    blocks = [
        (
            f"{marker_start}\n"
            "[model_providers.cluster]\n"
            'name = "Local Cluster (Torre + Portatil)"\n'
            f'base_url = "{v1}"\n'
            'env_key = "CLUSTER_API_KEY"\n'
            'wire_api = "responses"\n'
            f"{marker_end}\n"
        )
    ]
    nim_key = cluster_lib.resolve_nvidia_api_key()
    if nim_key:
        blocks.append(
            (
                f"{nim_marker_start}\n"
                "[model_providers.nvidia]\n"
                'name = "NVIDIA NIM Cloud (Zero Local VRAM)"\n'
                f'base_url = "{NIM_OPENAI_BASE}"\n'
                'env_key = "NVIDIA_API_KEY"\n'
                'wire_api = "chat"\n'
                f"{nim_marker_end}\n"
            )
        )
    print(f"[{os_tag}:codex] configure [model_providers.cluster]{' + [model_providers.nvidia]' if nim_key else ''} -> {cfg_path}")
    if not dry_run:
        os.makedirs(codex_dir, exist_ok=True)
        existing = open(cfg_path, encoding="utf-8").read() if os.path.isfile(cfg_path) else ""
        pre = _strip_block(existing, marker_start, marker_end)
        pre = _strip_block(pre, nim_marker_start, nim_marker_end)
        out = (pre.rstrip() + "\n\n" if pre.strip() else "") + "\n\n".join(blocks)
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
