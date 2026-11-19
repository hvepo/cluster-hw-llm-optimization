#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY3="$(command -v python3 || command -v python || true)"
[ -n "$PY3" ] || { echo "ERROR: python3 not found in PATH" >&2; exit 1; }

exec "$PY3" "$REPO_ROOT/scripts/install.py" "$@"
