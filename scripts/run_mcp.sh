#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

candidates=()
if [[ -n "${MAC_MAIL_PYTHON:-}" ]]; then
  candidates+=("$MAC_MAIL_PYTHON")
fi
candidates+=(python3.12 python3.11 python3.10 python3)

for candidate in "${candidates[@]}"; do
  if ! command -v "$candidate" >/dev/null 2>&1; then
    continue
  fi
  if "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3, 10))' >/dev/null 2>&1; then
    exec "$candidate" "$ROOT/mac_mail_mcp.py" "$@"
  fi
done

echo "Mac Mail Codex plugin requires Python 3.10+. Install Python 3.10+ or set MAC_MAIL_PYTHON=/path/to/python." >&2
exit 1
