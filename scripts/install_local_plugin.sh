#!/usr/bin/env bash
set -euo pipefail

SOURCE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET_ROOT="${1:-${HOME}/plugins/mac-mail}"

mkdir -p "$(dirname "$TARGET_ROOT")"
rsync -a \
  --delete \
  --exclude '.git/' \
  --exclude '.github/' \
  --exclude 'dist/' \
  --exclude '__pycache__/' \
  --exclude '*.pyc' \
  "$SOURCE_ROOT/" "$TARGET_ROOT/"

echo "Installed mac-mail plugin to $TARGET_ROOT"
