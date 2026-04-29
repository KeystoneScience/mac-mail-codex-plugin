#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NAME="$(basename "$ROOT")"
cd "$ROOT"
VERSION="$(python3 -c 'import json; print(json.load(open(".codex-plugin/plugin.json"))["version"])' < /dev/null)"
DIST="$ROOT/dist"
ARCHIVE="$DIST/mac-mail-codex-plugin-${VERSION}.tar.gz"

mkdir -p "$DIST"
rm -f "$ARCHIVE"

tar -czf "$ARCHIVE" \
  --exclude="$NAME/.git" \
  --exclude="$NAME/.git/*" \
  --exclude="$NAME/dist" \
  --exclude="$NAME/dist/*" \
  --exclude="$NAME/__pycache__" \
  --exclude="$NAME/*/__pycache__" \
  --exclude="$NAME/*/*/__pycache__" \
  --exclude="$NAME/*.pyc" \
  --exclude="$NAME/*/*.pyc" \
  --exclude="$NAME/*/*/*.pyc" \
  --exclude="$NAME/.DS_Store" \
  -C "$(dirname "$ROOT")" "$NAME"

echo "$ARCHIVE"
