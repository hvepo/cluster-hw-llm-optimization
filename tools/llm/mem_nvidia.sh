#!/usr/bin/env bash
# Print total RTX 3080 Ti VRAM used in bytes for bench.py's memory watchdog.
set -euo pipefail
SMI="$(command -v nvidia-smi || true)"
if [ -z "$SMI" ] && [ -x /usr/lib/wsl/lib/nvidia-smi ]; then
  SMI="/usr/lib/wsl/lib/nvidia-smi"
fi
mib=$("$SMI" --query-gpu=memory.used --format=csv,noheader,nounits | head -1 | tr -d ' \r')
echo $(( mib * 1048576 ))
