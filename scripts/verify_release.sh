#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PYTHON_BIN="${PYTHON_BIN:-python3}"

run_live=false
if [[ "${1:-}" == "--live" ]]; then
  run_live=true
fi

"$PYTHON_BIN" -m py_compile \
  scripts/mac_mail_mcp.py \
  scripts/bootstrap_install.py \
  scripts/doctor.py \
  scripts/update_plugin.py \
  scripts/live_smoke_test.py \
  scripts/benchmark_mail.py \
  tests/test_mac_mail_mcp.py

bash -n scripts/run_mcp.sh scripts/run_doctor.sh

"$PYTHON_BIN" -m unittest discover -s tests -v
"$PYTHON_BIN" scripts/doctor.py

printf '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}\n' \
  | "$PYTHON_BIN" scripts/mac_mail_mcp.py \
  | "$PYTHON_BIN" -c 'import json,sys; lines=sys.stdin.read().splitlines(); init=json.loads(lines[0]); tools=json.loads(lines[1])["result"]["tools"]; assert init["result"]["serverInfo"]["version"] == "0.6.0"; assert len(tools) >= 22; print(f"JSON-RPC OK: {len(tools)} tools")'

if [[ "$run_live" == "true" ]]; then
  "$PYTHON_BIN" scripts/doctor.py --require-mail
  "$PYTHON_BIN" scripts/live_smoke_test.py
  "$PYTHON_BIN" scripts/benchmark_mail.py
fi

echo "Release verification passed."
