#!/usr/bin/env bash
# Print total RTX 3080 Ti VRAM used in bytes for bench.py's memory watchdog.
set -euo pipefail
mib=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits | head -1 | tr -d ' \r')
echo $(( mib * 1048576 ))
