#!/usr/bin/env python3
"""Check and pull updates for a Git-backed Mac Mail Codex plugin install."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_JSON = PLUGIN_ROOT / ".codex-plugin" / "plugin.json"
DEFAULT_BRANCH = os.environ.get("MAC_MAIL_PLUGIN_BRANCH", "main")


def run_git(args: list[str], *, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(PLUGIN_ROOT), *args],
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def version() -> str | None:
    try:
        return json.loads(PLUGIN_JSON.read_text()).get("version")
    except Exception:
        return None


def git_error(message: str, **extra: Any) -> dict[str, Any]:
    return {
        "ok": False,
        "update_supported": False,
        "plugin_root": str(PLUGIN_ROOT),
        "error": message,
        **extra,
    }


def status(*, check_remote: bool = True) -> dict[str, Any]:
    if not shutil.which("git"):
        return git_error("git is not installed or not on PATH.")

    inside = run_git(["rev-parse", "--is-inside-work-tree"], timeout=15)
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return git_error("This install is not a git checkout. Reinstall with scripts/bootstrap_install.py.")

    local = run_git(["rev-parse", "HEAD"], timeout=15)
    branch = run_git(["branch", "--show-current"], timeout=15)
    remote_url = run_git(["config", "--get", "remote.origin.url"], timeout=15)
    upstream = run_git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], timeout=15)
    status_short = run_git(["status", "--short", "--untracked-files=no"], timeout=15)

    current_branch = branch.stdout.strip() or DEFAULT_BRANCH
    upstream_name = upstream.stdout.strip() if upstream.returncode == 0 else f"origin/{current_branch}"
    fetch_error = None
    if check_remote:
        fetch = run_git(["fetch", "--quiet", "origin", current_branch], timeout=90)
        if fetch.returncode != 0:
            fetch_error = fetch.stderr.strip() or fetch.stdout.strip() or f"git fetch exited {fetch.returncode}"

    remote = run_git(["rev-parse", upstream_name], timeout=15)
    remote_commit = remote.stdout.strip() if remote.returncode == 0 else ""
    local_commit = local.stdout.strip()
    update_available = bool(remote_commit and local_commit and remote_commit != local_commit)

    ahead = behind = None
    if remote_commit:
        counts = run_git(["rev-list", "--left-right", "--count", f"HEAD...{upstream_name}"], timeout=15)
        if counts.returncode == 0:
            parts = counts.stdout.strip().split()
            if len(parts) == 2:
                ahead, behind = int(parts[0]), int(parts[1])

    return {
        "ok": fetch_error is None,
        "update_supported": True,
        "plugin_root": str(PLUGIN_ROOT),
        "version": version(),
        "branch": current_branch,
        "upstream": upstream_name,
        "repository": remote_url.stdout.strip() or None,
        "local_commit": local_commit or None,
        "remote_commit": remote_commit or None,
        "update_available": update_available,
        "ahead": ahead,
        "behind": behind,
        "dirty_tracked_files": bool(status_short.stdout.strip()),
        "fetch_error": fetch_error,
    }


def install() -> dict[str, Any]:
    before = status(check_remote=True)
    if not before.get("update_supported"):
        return before
    if before.get("fetch_error"):
        return {**before, "ok": False, "error": f"Could not fetch updates: {before['fetch_error']}"}
    if before.get("dirty_tracked_files"):
        return {
            **before,
            "ok": False,
            "error": "Tracked plugin files have local changes. Commit, stash, or reinstall before pulling updates.",
        }
    if not before.get("update_available"):
        return {**before, "updated": False, "message": "Already up to date."}

    branch = str(before.get("branch") or DEFAULT_BRANCH)
    pull = run_git(["pull", "--ff-only", "origin", branch], timeout=120)
    if pull.returncode != 0:
        return {
            **before,
            "ok": False,
            "updated": False,
            "error": pull.stderr.strip() or pull.stdout.strip() or f"git pull exited {pull.returncode}",
        }
    after = status(check_remote=False)
    return {
        "ok": after.get("ok", False),
        "updated": True,
        "before": before,
        "after": after,
        "pull_output": pull.stdout.strip(),
        "restart_required": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="Check update status.")
    mode.add_argument("--install", action="store_true", help="Pull the latest fast-forward update.")
    parser.add_argument("--json", action="store_true", help="Print JSON only.")
    args = parser.parse_args()

    payload = install() if args.install else status(check_remote=True)
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        if payload.get("ok"):
            if args.install and payload.get("updated"):
                print("Mac Mail Codex plugin updated. Restart Codex to load the new server.")
            elif payload.get("update_available"):
                print("Update available.")
            else:
                print("Mac Mail Codex plugin is up to date.")
            print(json.dumps(payload, indent=2))
        else:
            print(json.dumps(payload, indent=2), file=sys.stderr)
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
