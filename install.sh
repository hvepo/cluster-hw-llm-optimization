#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY3="$(command -v python3 || true)"
[ -n "$PY3" ] || { echo "ERROR: python3 not found in PATH" >&2; exit 1; }

NODE="torre"
TARGETS="agy,opencode,vscode"
TORRE_URL=""
PORTATIL_URL=""
DRY_RUN=0
UNINSTALL=0
RUN_DOCTOR=0

while [ $# -gt 0 ]; do
  case "$1" in
    --node) NODE="${2:?}"; shift 2 ;;
    --targets) TARGETS="${2:?}"; shift 2 ;;
    --torre-url) TORRE_URL="${2:?}"; shift 2 ;;
    --portatil-url) PORTATIL_URL="${2:?}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    --doctor) RUN_DOCTOR=1; shift ;;
    -h|--help)
      cat <<EOF
Usage: bash install.sh [options]
  --node <node_name>            Set local_node in cluster.local.json (default: torre)
  --targets <agy,opencode,vscode> Comma-separated client targets (default: agy,opencode,vscode)
  --torre-url <url>             Set nodes.torre.base_url in cluster.local.json
  --portatil-url <url>          Set nodes.portatil.base_url in cluster.local.json
  --dry-run                     Print actions without modifying files
  --uninstall                   Remove global plugin/MCP/skills entries
  --doctor                      Run scripts/doctor.py after installation
EOF
      exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

export REPO_ROOT PY3 NODE TARGETS TORRE_URL PORTATIL_URL DRY_RUN UNINSTALL

"$PY3" - <<'PY'
import json
import os
import shutil

repo_root = os.environ["REPO_ROOT"]
py3 = os.environ["PY3"]
node = os.environ["NODE"]
targets = {t.strip().lower() for t in os.environ["TARGETS"].split(",") if t.strip()}
torre_url = os.environ["TORRE_URL"]
portatil_url = os.environ["PORTATIL_URL"]
dry_run = os.environ["DRY_RUN"] == "1"
uninstall = os.environ["UNINSTALL"] == "1"

home = os.path.expanduser("~")
agy_dest = os.path.join(home, ".gemini", "config", "plugins", "cluster-hw-llm-optimization")
oc_dir = os.path.join(home, ".config", "opencode")
oc_cfg = os.path.join(oc_dir, "opencode.json")
oc_skills = os.path.join(oc_dir, "skills")
mcp_script = os.path.join(repo_root, "mcp", "cluster_mcp_server.py")

all_known_skills = (
    "hw-node-profiler",
    "llm-node-finetune",
    "cluster-install-migrate",
    "cluster-router",
    "hw-tuning-stability",
    "llm-inference-hillclimb",
    "cluster-migrate-install",
)

if uninstall:
    if "agy" in targets and os.path.exists(agy_dest):
        print(f"[uninstall] remove {agy_dest}")
        if not dry_run:
            shutil.rmtree(agy_dest)
    if "opencode" in targets:
        if os.path.isfile(oc_cfg):
            try:
                data = json.load(open(oc_cfg, encoding="utf-8"))
                srvs = ((data.get("mcp") or {}).get("servers") or {})
                if "cluster-hw-llm" in srvs:
                    print(f"[uninstall] remove mcp.servers.cluster-hw-llm from {oc_cfg}")
                    if not dry_run:
                        del srvs["cluster-hw-llm"]
                        with open(oc_cfg, "w", encoding="utf-8") as f:
                            json.dump(data, f, indent=2)
            except Exception as e:
                print(f"[warn] opencode.json cleanup skipped: {e}")
        for sname in all_known_skills:
            sp = os.path.join(oc_skills, sname)
            if os.path.exists(sp):
                print(f"[uninstall] remove {sp}")
                if not dry_run:
                    shutil.rmtree(sp)
    raise SystemExit(0)

# 1. Create or gently update gitignored cluster.local.json
local_cfg_path = os.path.join(repo_root, "cluster.local.json")
ex_cfg = json.load(open(os.path.join(repo_root, "cluster.example.json"), encoding="utf-8"))
if os.path.isfile(local_cfg_path):
    cfg = json.load(open(local_cfg_path, encoding="utf-8"))
else:
    cfg = ex_cfg
cfg.pop("routing_presets", None)
cfg["local_node"] = node
if node not in cfg.setdefault("nodes", {}):
    cfg["nodes"][node] = {
        "label": node,
        "role": "custom-cluster-node",
        "os": "auto",
        "base_url": "http://127.0.0.1:1234",
        "model_prefixes": [],
        "hardware": {},
        "paths": {},
    }
if torre_url:
    cfg["nodes"].setdefault("torre", {})["base_url"] = torre_url
if portatil_url:
    cfg["nodes"].setdefault("portatil", {})["base_url"] = portatil_url
print(f"[config] {local_cfg_path} (local_node={node})")
if not dry_run:
    with open(local_cfg_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)

# 2. Sync workspace mirrors (.github/skills and plugins/cluster-hw-llm-optimization/skills)
src_skills = os.path.join(repo_root, "skills")
for mirror in (
    os.path.join(repo_root, ".github", "skills"),
    os.path.join(repo_root, "plugins", "cluster-hw-llm-optimization", "skills"),
):
    if not dry_run:
        if os.path.exists(mirror):
            shutil.rmtree(mirror)
        shutil.copytree(src_skills, mirror)

# 3. Antigravity user plugin bundle (never touches ~/.gemini/config/config.json)
if "agy" in targets:
    src_plugin = os.path.join(repo_root, "plugins", "cluster-hw-llm-optimization")
    print(f"[agy] install plugin -> {agy_dest}")
    if not dry_run:
        if os.path.exists(agy_dest):
            shutil.rmtree(agy_dest)
        shutil.copytree(src_plugin, agy_dest)
        with open(os.path.join(agy_dest, "repo_root.txt"), "w", encoding="utf-8") as f:
            f.write(repo_root + "\n")
        mcp_json = {
            "mcpServers": {
                "cluster-hw-llm": {
                    "command": py3,
                    "args": [mcp_script],
                    "env": {"CLUSTER_REPO_ROOT": repo_root},
                }
            }
        }
        with open(os.path.join(agy_dest, "mcp_config.json"), "w", encoding="utf-8") as f:
            json.dump(mcp_json, f, indent=2)
        hooks_json = {
            "cluster-safety-guard": {
                "SessionStart": [
                    {"type": "command", "command": f'"{py3}" ./hooks/session_start.py', "timeout": 10}
                ],
                "PreToolUse": [
                    {
                        "matcher": "run_command",
                        "hooks": [
                            {"type": "command", "command": f'"{py3}" ./hooks/pre_tool_guard.py', "timeout": 10}
                        ],
                    }
                ],
            }
        }
        with open(os.path.join(agy_dest, "hooks.json"), "w", encoding="utf-8") as f:
            json.dump(hooks_json, f, indent=2)

# 4. OpenCode v2.3.2+ global MCP + skills
if "opencode" in targets:
    print(f"[opencode] update {oc_cfg} and sync skills -> {oc_skills}")
    if not dry_run:
        os.makedirs(oc_skills, exist_ok=True)
        oc_data = {}
        if os.path.isfile(oc_cfg):
            try:
                oc_data = json.load(open(oc_cfg, encoding="utf-8"))
            except Exception:
                oc_data = {}
        oc_data.setdefault("mcp", {}).setdefault("servers", {})["cluster-hw-llm"] = {
            "type": "local",
            "command": [py3, mcp_script],
            "environment": {"CLUSTER_REPO_ROOT": repo_root},
        }
        with open(oc_cfg, "w", encoding="utf-8") as f:
            json.dump(oc_data, f, indent=2)
        for old_s in all_known_skills:
            p_old = os.path.join(oc_skills, old_s)
            if os.path.exists(p_old):
                shutil.rmtree(p_old)
        for sname in os.listdir(src_skills):
            sdir = os.path.join(src_skills, sname)
            if os.path.isdir(sdir):
                shutil.copytree(sdir, os.path.join(oc_skills, sname))

if "vscode" in targets:
    print(f"[vscode] synced workspace skills -> {os.path.join(repo_root, '.github', 'skills')}")
PY

if [ "$RUN_DOCTOR" -eq 1 ] && [ "$DRY_RUN" -eq 0 ] && [ "$UNINSTALL" -eq 0 ]; then
  exec "$PY3" "$REPO_ROOT/scripts/doctor.py" --all --check-privacy
fi
