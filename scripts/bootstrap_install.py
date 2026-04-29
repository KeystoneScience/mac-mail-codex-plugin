#!/usr/bin/env python3
"""Install and register the Mac Mail Codex plugin as a home-local plugin."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPO_URL = "https://github.com/KeystoneScience/mac-mail-codex-plugin.git"
PLUGIN_NAME = "mac-mail"
DEFAULT_MARKETPLACE_NAME = "mac-mail-local"
MARKETPLACE_DISPLAY_NAME = "Mac Mail Local"


def run(cmd: list[str], *, cwd: Path | None = None, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=str(cwd) if cwd else None, text=True, capture_output=True, timeout=timeout, check=False)


def plugin_root() -> Path:
    return Path(__file__).resolve().parents[1]


def expand(path: str) -> Path:
    return Path(path).expanduser().resolve()


def load_marketplace(path: Path, preferred_name: str) -> dict[str, Any]:
    if path.exists():
        try:
            payload = json.loads(path.read_text())
            if isinstance(payload, dict):
                payload.setdefault("name", preferred_name)
                payload.setdefault("interface", {"displayName": MARKETPLACE_DISPLAY_NAME})
                payload.setdefault("plugins", [])
                return payload
        except json.JSONDecodeError:
            backup = path.with_suffix(path.suffix + ".bak")
            shutil.copy2(path, backup)
    return {
        "name": preferred_name,
        "interface": {"displayName": MARKETPLACE_DISPLAY_NAME},
        "plugins": [],
    }


def upsert_plugin_entry(marketplace: dict[str, Any]) -> None:
    entry = {
        "name": PLUGIN_NAME,
        "source": {"source": "local", "path": f"./plugins/{PLUGIN_NAME}"},
        "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
        "category": "Productivity",
    }
    plugins = marketplace.setdefault("plugins", [])
    for index, existing in enumerate(plugins):
        if isinstance(existing, dict) and existing.get("name") == PLUGIN_NAME:
            plugins[index] = {**existing, **entry}
            return
    plugins.append(entry)


def ensure_marketplace(home: Path, preferred_name: str) -> tuple[Path, str]:
    marketplace_path = home / ".agents" / "plugins" / "marketplace.json"
    marketplace_path.parent.mkdir(parents=True, exist_ok=True)
    marketplace = load_marketplace(marketplace_path, preferred_name)
    marketplace_name = str(marketplace.get("name") or preferred_name)
    marketplace.setdefault("interface", {}).setdefault("displayName", MARKETPLACE_DISPLAY_NAME)
    upsert_plugin_entry(marketplace)
    marketplace_path.write_text(json.dumps(marketplace, indent=2) + "\n")
    return marketplace_path, marketplace_name


def append_block_once(path: Path, marker: str, block: str) -> bool:
    existing = path.read_text() if path.exists() else ""
    if marker in existing:
        return False
    prefix = "" if not existing or existing.endswith("\n") else "\n"
    path.write_text(existing + prefix + "\n" + block.strip() + "\n")
    return True


def ensure_codex_config(home: Path, marketplace_name: str) -> tuple[Path, list[str]]:
    config_path = home / ".codex" / "config.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    changes: list[str] = []
    plugin_marker = f'[plugins."{PLUGIN_NAME}@{marketplace_name}"]'
    plugin_block = f'''
[plugins."{PLUGIN_NAME}@{marketplace_name}"]
enabled = true
'''
    if append_block_once(config_path, plugin_marker, plugin_block):
        changes.append(f"enabled {PLUGIN_NAME}@{marketplace_name}")

    marketplace_marker = f"[marketplaces.{marketplace_name}]"
    marketplace_block = f'''
[marketplaces.{marketplace_name}]
last_updated = "{datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}"
source_type = "local"
source = "{home}"
'''
    if append_block_once(config_path, marketplace_marker, marketplace_block):
        changes.append(f"registered marketplace {marketplace_name}")
    return config_path, changes


def refresh_codex_caches(home: Path, repo_url: str) -> list[dict[str, Any]]:
    cache_root = home / ".codex" / "plugins" / "cache"
    if not cache_root.exists():
        return []
    refreshed: list[dict[str, Any]] = []
    for candidate in sorted(cache_root.glob(f"*/{PLUGIN_NAME}/*")):
        if not candidate.is_dir():
            continue
        if not (candidate / ".git").exists():
            refreshed.append({"path": str(candidate), "action": "skipped_non_git_cache"})
            continue
        remote = run(["git", "-C", str(candidate), "config", "--get", "remote.origin.url"], timeout=15)
        remote_url = remote.stdout.strip()
        if remote.returncode != 0 or (repo_url not in {remote_url, remote_url.removesuffix(".git")}):
            refreshed.append({"path": str(candidate), "action": "skipped_different_remote", "remote": remote_url or None})
            continue
        dirty = run(["git", "-C", str(candidate), "status", "--short", "--untracked-files=no"], timeout=15)
        if dirty.stdout.strip():
            refreshed.append({"path": str(candidate), "action": "skipped_dirty_cache"})
            continue
        fetch = run(["git", "-C", str(candidate), "fetch", "--quiet", "origin", "main"], timeout=120)
        if fetch.returncode != 0:
            refreshed.append({"path": str(candidate), "action": "fetch_failed", "error": fetch.stderr.strip() or fetch.stdout.strip()})
            continue
        before = run(["git", "-C", str(candidate), "rev-parse", "HEAD"], timeout=15).stdout.strip()
        pull = run(["git", "-C", str(candidate), "pull", "--ff-only", "origin", "main"], timeout=120)
        after = run(["git", "-C", str(candidate), "rev-parse", "HEAD"], timeout=15).stdout.strip()
        refreshed.append(
            {
                "path": str(candidate),
                "action": "updated_cache" if before != after else "cache_already_current",
                "before": before or None,
                "after": after or None,
                "ok": pull.returncode == 0,
                "error": None if pull.returncode == 0 else pull.stderr.strip() or pull.stdout.strip(),
            }
        )
    return refreshed


def ensure_git_checkout(target: Path, repo_url: str, *, replace_existing: bool = False) -> dict[str, Any]:
    source_root = plugin_root().resolve()
    target = target.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)

    if source_root == target:
        if not (target / ".git").exists():
            return {
                "action": "blocked_current_non_git",
                "target": str(target),
                "error": (
                    f"{target} is installed in the right place but is not a git checkout. "
                    "Reinstall from the GitHub repo for self-updating support."
                ),
            }
        return {"action": "already_at_target", "target": str(target)}

    if target.exists() and not (target / ".git").exists():
        if not replace_existing:
            return {
                "action": "blocked_existing_non_git",
                "target": str(target),
                "error": (
                    f"{target} already exists but is not a git checkout. Move it aside or rerun with "
                    "--replace-existing to install the self-updating Git-backed copy."
                ),
            }
        shutil.rmtree(target)

    if (target / ".git").exists():
        fetch = run(["git", "-C", str(target), "fetch", "--quiet", "origin", "main"], timeout=120)
        if fetch.returncode != 0:
            return {"action": "fetch_failed", "target": str(target), "error": fetch.stderr.strip() or fetch.stdout.strip()}
        pull = run(["git", "-C", str(target), "pull", "--ff-only", "origin", "main"], timeout=120)
        if pull.returncode != 0:
            return {"action": "pull_failed", "target": str(target), "error": pull.stderr.strip() or pull.stdout.strip()}
        return {"action": "updated_existing_checkout", "target": str(target)}

    if not shutil.which("git"):
        return {"action": "blocked_missing_git", "target": str(target), "error": "git is required to clone the plugin."}
    clone = run(["git", "clone", repo_url, str(target)], timeout=180)
    if clone.returncode != 0:
        return {"action": "clone_failed", "target": str(target), "error": clone.stderr.strip() or clone.stdout.strip()}
    return {"action": "cloned", "target": str(target)}


def run_doctor(target: Path) -> dict[str, Any]:
    doctor = target / "scripts" / "doctor.py"
    if not doctor.exists():
        return {"ok": False, "error": f"Doctor script not found at {doctor}"}
    python = find_python()
    if not python:
        return {
            "ok": False,
            "error": "Python 3.10+ was not found. Install Python 3.10+ or set MAC_MAIL_PYTHON.",
        }
    result = run([python, str(doctor)], cwd=target, timeout=60)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        payload = {"stdout": result.stdout.strip(), "stderr": result.stderr.strip()}
    return {"ok": result.returncode == 0, "python": python, "result": payload}


def find_python() -> str | None:
    candidates = []
    if os.environ.get("MAC_MAIL_PYTHON"):
        candidates.append(os.environ["MAC_MAIL_PYTHON"])
    candidates.extend(["python3.12", "python3.11", "python3.10", sys.executable, "python3"])
    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        resolved = shutil.which(candidate) if os.sep not in candidate else candidate
        if not resolved:
            continue
        check = run([resolved, "-c", "import sys; raise SystemExit(sys.version_info < (3, 10))"], timeout=15)
        if check.returncode == 0:
            return resolved
    return None


def payload_for(args: argparse.Namespace) -> dict[str, Any]:
    home = expand(args.home)
    target = expand(args.target)
    install = ensure_git_checkout(target, args.repo, replace_existing=args.replace_existing)
    if install.get("error"):
        return {"ok": False, "install": install}
    marketplace_path, marketplace_name = ensure_marketplace(home, args.marketplace_name)
    config_path, config_changes = ensure_codex_config(home, marketplace_name)
    cache_refresh = refresh_codex_caches(home, args.repo)
    doctor_result = None if args.skip_doctor else run_doctor(target)
    ok = install.get("error") is None and (doctor_result is None or doctor_result.get("ok", False))
    return {
        "ok": ok,
        "install": install,
        "plugin_target": str(target),
        "marketplace": {"name": marketplace_name, "path": str(marketplace_path)},
        "codex_config": {"path": str(config_path), "changes": config_changes},
        "codex_cache_refresh": cache_refresh,
        "doctor": doctor_result,
        "permissions_next_steps": [
            "Open System Settings > Privacy & Security > Full Disk Access and enable Codex.",
            "If you test from Terminal or iTerm, grant Full Disk Access to that app too.",
            "Mail.app Automation permission is requested by macOS the first time a draft/open/send tool controls Mail.",
            "Restart Codex after changing permissions.",
            "Restart Codex after plugin installs or updates so the native tool schema reloads.",
        ],
        "try_in_codex": [
            "Run mail_permissions_check. If Full Disk Access is blocked, rerun it with open_full_disk_access=true.",
            "Run mail_list_mailboxes to choose an exact mailbox_id.",
            "Run mail_search_messages with mailbox_id or mailbox_role='inbox' before reading bodies.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=REPO_URL, help="Git repository URL to clone or update.")
    parser.add_argument("--target", default=f"~/plugins/{PLUGIN_NAME}", help="Home-local plugin target directory.")
    parser.add_argument("--home", default=str(Path.home()), help="Home directory whose Codex config should be updated.")
    parser.add_argument("--marketplace-name", default=DEFAULT_MARKETPLACE_NAME, help="Local marketplace name for new installs.")
    parser.add_argument("--replace-existing", action="store_true", help="Replace an existing non-git target directory.")
    parser.add_argument("--skip-doctor", action="store_true", help="Skip the post-install doctor check.")
    parser.add_argument("--json", action="store_true", help="Print JSON only.")
    args = parser.parse_args()

    payload = payload_for(args)
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(json.dumps(payload, indent=2))
        if payload.get("ok"):
            print("\nInstalled. Restart Codex, then run mail_permissions_check from the Mac Mail plugin.")
        else:
            print("\nInstall needs attention. The JSON above includes the exact blocker.", file=sys.stderr)
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
