#!/usr/bin/env python3
"""Universal Continue (VS Code / JetBrains) config generator & installer for torre & portatil.

Generates node-tailored `schema: v1` Continue configurations covering all `cluster/*`
(local) + `nim/*` (NVIDIA NIM cloud) virtual aliases (with `tool_use` / `image_input`
capabilities and tuned `contextLength`), cluster rules, context providers, and the
`cluster-hw-llm` Stdio MCP server across both Windows (`%USERPROFILE%\\.continue\\config.yaml`)
and WSL/Linux (`~/.continue/config.yaml`).
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

CONTINUE_MODEL_SPECS = {
    "cluster/code": {
        "name": "Cluster Code (30B MoE -> 14B)",
        "ctx": 16384,
        "roles": ["chat", "edit", "apply"],
        "capabilities": ["tool_use"],
    },
    "cluster/code-fast": {
        "name": "Cluster Code Fast (14B 72 tok/s)",
        "ctx": 12288,
        "roles": ["chat", "edit", "apply"],
        "capabilities": ["tool_use"],
    },
    "cluster/code-heavy": {
        "name": "Cluster Code Heavy (30B-A3B MoE)",
        "ctx": 16384,
        "roles": ["chat", "edit", "apply"],
        "capabilities": ["tool_use"],
    },
    "cluster/agent-heavy": {
        "name": "Cluster Agent Heavy (30B-A3B MoE)",
        "ctx": 16384,
        "roles": ["chat", "edit", "apply"],
        "capabilities": ["tool_use"],
    },
    "cluster/fast-agent": {
        "name": "Cluster Fast Agent (8B 130 tok/s)",
        "ctx": 16384,
        "roles": ["chat", "edit", "apply"],
        "capabilities": ["tool_use"],
    },
    "cluster/vision": {
        "name": "Cluster Vision (Gemma-4-12B MTP-4)",
        "ctx": 16384,
        "roles": ["chat"],
        "capabilities": ["tool_use", "image_input"],
    },
    "cluster/vision-heavy": {
        "name": "Cluster Vision Heavy (26B 32K MTP-3)",
        "ctx": 32768,
        "roles": ["chat"],
        "capabilities": ["tool_use", "image_input"],
    },
    "cluster/reasoning": {
        "name": "Cluster Reasoning (R1-14B Think)",
        "ctx": 8192,
        "roles": ["chat"],
        "capabilities": ["tool_use"],
    },
    "cluster/math": {
        "name": "Cluster Math (Phi-4 14B)",
        "ctx": 8192,
        "roles": ["chat"],
        "capabilities": [],
    },
    "cluster/autocomplete": {
        "name": "Cluster Tab Autocomplete",
        "ctx": 4096,
        "roles": ["autocomplete"],
        "capabilities": [],
    },
    "cluster/embed": {
        "name": "Cluster Embeddings",
        "ctx": 2048,
        "roles": ["embed"],
        "capabilities": [],
    },
}

# NVIDIA NIM cloud resources via the Cluster Router Gateway (:1235); visible when the
# NVIDIA NIM API key is present (mirrors gateway NIM_ALIAS_SPECS context/output windows).
NIM_CONTINUE_SPECS = {
    "nim/ultra": {
        "name": "NVIDIA NIM Ultra (Nemotron-3 550B-A55B Cloud Orchestrator)",
        "ctx": 131072,
        "roles": ["chat", "edit", "apply"],
        "capabilities": ["tool_use"],
    },
    "nim/fast": {
        "name": "NVIDIA NIM Fast (Nemotron-3.5 Lightning 30B-A3B Cloud)",
        "ctx": 131072,
        "roles": ["chat", "edit", "apply"],
        "capabilities": ["tool_use"],
    },
    "nim/vision": {
        "name": "NVIDIA NIM Vision (Nemotron-3.5 Super-VL 120B-A12B Cloud VL)",
        "ctx": 131072,
        "roles": ["chat"],
        "capabilities": ["tool_use", "image_input"],
    },
    "nim/reasoning": {
        "name": "NVIDIA NIM Reasoning (Nemotron-3 Nano Omni 30B-A3B Cloud CoT)",
        "ctx": 131072,
        "roles": ["chat"],
        "capabilities": ["tool_use"],
    },
    "nim/code": {
        "name": "NVIDIA NIM Code (Poolside Laguna-XS 2.1 Cloud Coder)",
        "ctx": 65536,
        "roles": ["chat", "edit", "apply"],
        "capabilities": ["tool_use"],
    },
    "nim/mistral-large": {
        "name": "NVIDIA NIM Mistral Large (Mistral Large 4 1T-A52B Frontier Cloud)",
        "ctx": 131072,
        "roles": ["chat", "edit", "apply"],
        "capabilities": ["tool_use"],
    },
    "nim/kimi": {
        "name": "NVIDIA NIM Kimi (Moonshot K3.1 Pro Long-Context Cloud)",
        "ctx": 131072,
        "roles": ["chat", "edit", "apply"],
        "capabilities": ["tool_use"],
    },
    "nim/step": {
        "name": "NVIDIA NIM Step-5 Preview (Frontier Reasoning Cloud)",
        "ctx": 131072,
        "roles": ["chat"],
        "capabilities": ["tool_use"],
    },
    "nim/glm-flash": {
        "name": "NVIDIA NIM GLM-5.3 Flash (High-Speed Coding/Tool-Calling Cloud)",
        "ctx": 131072,
        "roles": ["chat", "edit", "apply"],
        "capabilities": ["tool_use"],
    },
    "nim/minimax-flash": {
        "name": "NVIDIA NIM MiniMax M3.1 Flash (High-Speed Long-Context Cloud)",
        "ctx": 131072,
        "roles": ["chat", "edit", "apply"],
        "capabilities": ["tool_use"],
    },
    "nim/gemma-31b": {
        "name": "NVIDIA NIM Gemma-4 31B (Instruction/Code Cloud)",
        "ctx": 131072,
        "roles": ["chat", "edit", "apply"],
        "capabilities": ["tool_use"],
    },
    "nim/embed": {
        "name": "NVIDIA NIM Embed (Nemotron-3.5 8B Cloud Embeddings)",
        "ctx": 8192,
        "roles": ["embed"],
        "capabilities": [],
    },
}

CONTINUE_MODEL_SPECS.update(NIM_CONTINUE_SPECS)

NIM_ORDER = [
    "nim/ultra",
    "nim/fast",
    "nim/vision",
    "nim/reasoning",
    "nim/code",
    "nim/mistral-large",
    "nim/kimi",
    "nim/step",
    "nim/glm-flash",
    "nim/minimax-flash",
    "nim/gemma-31b",
    "nim/embed",
]

TORRE_ORDER = [
    "cluster/code",
    "cluster/code-fast",
    "cluster/fast-agent",
    "cluster/vision",
    "cluster/reasoning",
    "cluster/math",
    "cluster/code-heavy",
    "cluster/agent-heavy",
    "cluster/vision-heavy",
    "cluster/autocomplete",
    "cluster/embed",
] + NIM_ORDER

PORTATIL_ORDER = [
    "cluster/code",
    "cluster/code-heavy",
    "cluster/agent-heavy",
    "cluster/vision-heavy",
    "cluster/code-fast",
    "cluster/fast-agent",
    "cluster/vision",
    "cluster/reasoning",
    "cluster/math",
    "cluster/autocomplete",
    "cluster/embed",
] + NIM_ORDER


def _backup_if_needed(path, dry_run=False):
    if dry_run or not os.path.isfile(path):
        return
    stamp = time.strftime("%Y%m%d-%H%M%S")
    shutil.copy2(path, f"{path}.{stamp}.bak")


def resolve_node_name(node=None):
    if node and node.strip().lower() != "auto":
        return node.strip().lower()
    cfg = cluster_lib.load_cluster_config(REPO_ROOT)
    return (cfg.get("local_node") or "torre").strip().lower()


def render_continue_yaml(node, endpoint, mcp_command, mcp_args, repo_str):
    norm_node = (node or "torre").strip().lower()
    v1_url = endpoint.rstrip("/") + "/v1"

    if norm_node == "portatil":
        cfg_name = "Cluster Local Pool — Portatil (Arc 140V + LM Link)"
        order = PORTATIL_ORDER
    elif norm_node == "torre":
        cfg_name = "Cluster Local Pool — Torre (RTX 3080 Ti + LM Link)"
        order = TORRE_ORDER
    else:
        cfg_name = f"Cluster Local Pool — {norm_node} (LM Link)"
        order = TORRE_ORDER

    lines = [
        f"# Managed by cluster-hw-llm-optimization/scripts/install_continue.py (node={norm_node})",
        f"name: {json.dumps(cfg_name)}",
        "version: 1.0.0",
        "schema: v1",
        "",
        "models:",
    ]

    for model_id in order:
        spec = CONTINUE_MODEL_SPECS[model_id]
        lines.extend([
            f"  - name: {spec['name']}",
            "    provider: lmstudio",
            f"    model: {model_id}",
            f"    apiBase: {v1_url}",
            "    defaultCompletionOptions:",
            f"      contextLength: {spec['ctx']}",
            "    roles:",
        ])
        for role in spec["roles"]:
            lines.append(f"      - {role}")
        if spec["capabilities"]:
            lines.append("    capabilities:")
            for cap in spec["capabilities"]:
                lines.append(f"      - {cap}")

    lines.extend([
        "",
        "mcpServers:",
        "  - name: cluster-hw-llm",
        f"    command: {json.dumps(mcp_command)}",
        "    args:",
    ])
    for arg in mcp_args:
        lines.append(f"      - {json.dumps(arg)}")
    lines.extend([
        "    env:",
        f"      CLUSTER_REPO_ROOT: {json.dumps(repo_str)}",
        "",
        "rules:",
        "  - Preserve persisted LM Studio default configs (user-concrete-model-default-config/*.json); use tools/cluster_ctl.py or node_llm_control instead of overwriting defaults.",
        "  - Scope LM Link model loads/unloads strictly by each node's model_prefixes; never run un-scoped lms unload --all.",
        "  - Prefer cluster-hw-llm MCP tools (hw_node_profile, node_llm_status, node_best_configs, cluster_models, cluster_chat) when inspecting hardware or routing prompts.",
        "",
        "context:",
        "  - provider: code",
        "  - provider: docs",
        "  - provider: diff",
        "  - provider: terminal",
        "  - provider: problems",
        "  - provider: folder",
        "  - provider: codebase",
        "",
    ])
    return "\n".join(lines)


def write_profile_templates(endpoint="http://127.0.0.1:1235", dry_run=False):
    for node_name in ("torre", "portatil"):
        out_path = os.path.join(REPO_ROOT, "profiles", node_name, "continue-config.yaml")
        yaml_text = render_continue_yaml(
            node=node_name,
            endpoint=endpoint,
            mcp_command="python3",
            mcp_args=["mcp/cluster_mcp_server.py"],
            repo_str=".",
        )
        print(f"[template:{node_name}] write {out_path}")
        if not dry_run:
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(yaml_text)


def install_continue_for_ctx(ctx, os_tag, endpoint, node="auto", dry_run=False, uninstall=False):
    cont_dir = os.path.join(ctx["home_local"], ".continue")
    cfg_path = os.path.join(cont_dir, "config.yaml")

    if uninstall:
        if os.path.isfile(cfg_path):
            print(f"[{os_tag}:continue] leaving {cfg_path} intact (backups preserved in {cont_dir})")
        return

    resolved_node = resolve_node_name(node)
    yaml_text = render_continue_yaml(
        node=resolved_node,
        endpoint=endpoint,
        mcp_command=ctx["mcp_command"],
        mcp_args=ctx["mcp_args"],
        repo_str=ctx["repo_str"],
    )
    print(f"[{os_tag}:continue] install node={resolved_node} -> {cfg_path} (backup + schema v1 -> {endpoint.rstrip('/')}/v1)")
    if not dry_run:
        os.makedirs(cont_dir, exist_ok=True)
        _backup_if_needed(cfg_path, dry_run=False)
        with open(cfg_path, "w", encoding="utf-8") as f:
            f.write(yaml_text)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Generate and install Continue config.yaml for torre (PC) and portatil (Laptop)")
    ap.add_argument("--node", default="auto", help="Target cluster node profile: auto, torre, portatil (default: auto)")
    ap.add_argument("--os", dest="os_targets", default="auto", help="OS targets: auto,windows,wsl,linux,all")
    ap.add_argument("--endpoint", default="", help="Override gateway base URL (default: http://127.0.0.1:1235)")
    ap.add_argument("--direct", action="store_true", help="Point Continue directly at LM Studio :1234 instead of gateway :1235")
    ap.add_argument("--win-use-wsl-python", action="store_true", help="Configure Windows MCP command via wsl.exe -e python3")
    ap.add_argument("--write-templates", action="store_true", help="Regenerate tracked profiles/{torre,portatil}/continue-config.yaml templates")
    ap.add_argument("--dry-run", action="store_true", help="Print actions without writing files")
    ap.add_argument("--uninstall", action="store_true", help="Preserve backups and skip overwriting Continue config")
    args = ap.parse_args(argv)

    cfg = cluster_lib.load_cluster_config(REPO_ROOT)
    gw = cfg.get("gateway") or {}
    default_gw = f"http://{gw.get('host', '127.0.0.1')}:{int(gw.get('port', 1235))}"
    endpoint = (args.endpoint or (gw.get("upstream_url") if args.direct else default_gw) or "http://127.0.0.1:1235").rstrip("/")

    if args.write_templates:
        write_profile_templates(endpoint=endpoint, dry_run=args.dry_run)

    current_plat = cluster_lib.detect_platform()
    win_ctx = inst.detect_windows_context(current_plat, force_wsl_python=args.win_use_wsl_python)
    posix_ctx = inst.detect_posix_context(current_plat)
    wsl_from_win = inst.wsl_available_from_windows()

    os_list = inst.parse_os_list(
        args.os_targets,
        current_plat=current_plat,
        win_ctx_available=(win_ctx is not None),
        wsl_bridge_available=(posix_ctx is not None or wsl_from_win),
    )

    resolved_node = resolve_node_name(args.node)
    for os_tag in os_list:
        if os_tag == "windows":
            if win_ctx is None:
                print("[warn] Windows host not reachable; skipping --os windows", file=sys.stderr)
                continue
            install_continue_for_ctx(win_ctx, "windows", endpoint, node=resolved_node, dry_run=args.dry_run, uninstall=args.uninstall)
        elif os_tag in ("wsl", "linux"):
            if posix_ctx is not None:
                install_continue_for_ctx(posix_ctx, os_tag, endpoint, node=resolved_node, dry_run=args.dry_run, uninstall=args.uninstall)
            elif current_plat == "windows" and wsl_from_win:
                wsl_bin = cluster_lib.resolve_host_binary("wsl.exe")
                posix_repo = cluster_lib.normalize_local_path(REPO_ROOT, "wsl")
                cmd = [
                    wsl_bin, "-e", "python3", f"{posix_repo}/scripts/install_continue.py",
                    "--node", resolved_node, "--os", "wsl", "--endpoint", endpoint,
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
