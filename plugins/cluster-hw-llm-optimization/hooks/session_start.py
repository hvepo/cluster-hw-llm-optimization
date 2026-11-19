#!/usr/bin/env python3
"""SessionStart hook: injects concise detected platform, cluster endpoints, and MCP/tool hints."""
import json
import os
import sys

from common import emit_json, find_repo_root, read_hook_input


def main():
    _ = read_hook_input()
    root = find_repo_root()
    sys.path.insert(0, os.path.join(root, "tools"))
    try:
        import cluster_lib
        cfg = cluster_lib.load_cluster_config(root)
        plat = cluster_lib.detect_platform()
        local_node = cfg.get("local_node", "torre")
        nodes = {n: v.get("base_url", "") for n, v in cfg.get("nodes", {}).items()}
        msg = (
            f"[cluster-hw-llm] host={plat} local_node={local_node} "
            f"endpoints={json.dumps(nodes)} | "
            "Use MCP server `cluster-hw-llm` (or tools/cluster_ctl.py) for prefix-isolated load/unload, "
            "and preserve LM Studio permanent defaults."
        )
    except Exception as exc:
        msg = f"[cluster-hw-llm] repo={root} (config fallback: {exc})"

    emit_json({"injectSteps": [{"systemMessage": {"systemMessage": msg}}]})


if __name__ == "__main__":
    main()
