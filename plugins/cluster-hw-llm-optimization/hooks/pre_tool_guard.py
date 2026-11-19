#!/usr/bin/env python3
"""PreToolUse safety guard for Antigravity (`run_command`).

1. Denies direct rm/del/Remove-Item/truncate inside LM Studio's user-concrete-model-default-config.
2. Asks confirmation before running un-scoped `lms unload --all`.
3. Asks confirmation if invoking Windows `.exe` from WSL without stdin redirection (< /dev/null).
"""
import re
from common import emit_json, read_hook_input

DESTRUCTIVE_RE = re.compile(r"\b(rm|rmdir|del|erase|remove-item|clear-content)\b|(^|[^2])>\s*", re.IGNORECASE)
WIN_EXE_RE = re.compile(r"\b(lms|powershell|python)\.exe\b", re.IGNORECASE)


def main():
    payload = read_hook_input()
    tc = payload.get("toolCall") or {}
    if tc.get("name") != "run_command":
        emit_json({"decision": "allow"})
        return

    cmd = str((tc.get("args") or {}).get("CommandLine") or "")
    low = cmd.lower()

    if "user-concrete-model-default-config" in low and DESTRUCTIVE_RE.search(cmd):
        emit_json({
            "decision": "deny",
            "reason": "Blocked destructive modification of LM Studio user-concrete-model-default-config. Use profiles/torre/apply_defaults.py or tools/llm/lmctl_bench.py instead.",
        })
        return

    if re.search(r"\blms(?:\.exe)?\s+unload\b.*--all\b", low):
        emit_json({
            "decision": "ask",
            "reason": "lms unload --all may unload models on the sibling LM Link machine. Prefer tools/cluster_ctl.py unload --node <torre|portatil>.",
        })
        return

    if WIN_EXE_RE.search(cmd):
        has_devnull = "< /dev/null" in cmd or "</dev/null" in cmd or "stdin=" in cmd
        if not has_devnull and ("&" in cmd or "|" in cmd or ";" in cmd):
            emit_json({
                "decision": "ask",
                "reason": "Calling Windows .exe from WSL without `< /dev/null` can hang background commands. Redirect stdin with `< /dev/null`.",
            })
            return

    emit_json({"decision": "allow"})


if __name__ == "__main__":
    main()
