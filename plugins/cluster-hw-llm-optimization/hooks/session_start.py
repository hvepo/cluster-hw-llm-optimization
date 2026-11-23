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
        gw = cfg.get("gateway") or {}
        gw_host = gw.get("host") or "127.0.0.1"
        gw_port = int(gw.get("port") or 1235)
        peer_cfg = cfg.get("peer") or {}
        peer_port = int(peer_cfg.get("port") or 1236)
        responder = str((peer_cfg.get("openclaw") or {}).get("responder") or "antigravity").strip().lower()
        msg = (
            f"[cluster-hw-llm] host={plat} local_node={local_node} "
            f"gateway=http://{gw_host}:{gw_port} peer=http://127.0.0.1:{peer_port} "
            f"telegram_responder={responder} | "
            "Horizontal resources available from any project: MCP 'cluster-hw-llm' "
            "(hw_*, llm_*, node_*, cluster_*, peer_*, openclaw_control) and global CLIs "
            "(cluster-peer, cluster-telegram, cluster-llm, cluster-gateway, cluster-chat, cluster-hw)."
        )
    except Exception as exc:
        msg = f"[cluster-hw-llm] repo={root} (config fallback: {exc})"

    emit_json({"injectSteps": [{"systemMessage": {"systemMessage": msg}}]})


if __name__ == "__main__":
    main()
