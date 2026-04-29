#!/usr/bin/env python3
"""Non-destructive environment check for the Mac Mail Codex plugin."""

from __future__ import annotations

import importlib.util
import json
import platform
import sqlite3
import sys
from pathlib import Path
from typing import Any


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = PLUGIN_ROOT / "scripts" / "mac_mail_mcp.py"
PLUGIN_JSON = PLUGIN_ROOT / ".codex-plugin" / "plugin.json"
MCP_JSON = PLUGIN_ROOT / ".mcp.json"


def load_module():
    spec = importlib.util.spec_from_file_location("mac_mail_mcp", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check_fts5() -> bool:
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE VIRTUAL TABLE test_fts USING fts5(body)")
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        conn.close()


def schema_compatibility_issues(tools: dict[str, dict[str, Any]]) -> list[str]:
    blocked_anywhere = {"anyOf", "oneOf", "allOf", "not"}
    issues: list[str] = []

    def walk(value: Any, path: str) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key in blocked_anywhere:
                    issues.append(f"{path}.{key}")
                walk(item, f"{path}.{key}")
        elif isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, f"{path}[{index}]")

    for tool_name, entry in tools.items():
        schema = entry.get("inputSchema", {})
        if schema.get("type") != "object":
            issues.append(f"{tool_name}.type")
        for key in ["anyOf", "oneOf", "allOf", "enum", "not"]:
            if key in schema:
                issues.append(f"{tool_name}.{key}")
        walk(schema, tool_name)
    return sorted(set(issues))


def main() -> int:
    require_mail = "--require-mail" in sys.argv
    open_full_disk_access = "--open-full-disk-access" in sys.argv
    open_automation = "--open-automation" in sys.argv
    checks: list[dict[str, Any]] = []
    failures: list[str] = []

    def add(name: str, ok: bool, **details: Any) -> None:
        checks.append({"name": name, "ok": ok, **details})
        if not ok:
            failures.append(name)

    add("platform_macos", platform.system() == "Darwin", platform=platform.platform())
    add(
        "python_version",
        sys.version_info >= (3, 10),
        version=".".join(map(str, sys.version_info[:3])),
    )
    add("plugin_json_exists", PLUGIN_JSON.exists(), path=str(PLUGIN_JSON))
    add("mcp_json_exists", MCP_JSON.exists(), path=str(MCP_JSON))
    add("sqlite_fts5", check_fts5())

    try:
        mm = load_module()
        manifest = json.loads(PLUGIN_JSON.read_text())
        add(
            "version_match",
            manifest.get("version") == mm.SERVER_VERSION,
            manifest_version=manifest.get("version"),
            server_version=mm.SERVER_VERSION,
        )
        tool_names = sorted(mm.TOOLS)
        add("tool_count", len(tool_names) >= 22, count=len(tool_names))
        schema_issues = schema_compatibility_issues(mm.TOOLS)
        add(
            "codex_tool_schema_compatible",
            not schema_issues,
            issues=schema_issues,
            note="Avoids root composition keywords and nested union combinators that can block Codex plugin loading.",
        )
        add("has_permissions_tool", "mail_permissions_check" in mm.TOOLS)
        add("has_update_tools", {"mail_plugin_update_status", "mail_plugin_update_install"}.issubset(mm.TOOLS))
        add("has_purge_tool", "mail_purge_body_index" in mm.TOOLS)
        add("send_disabled_by_default", not mm.os.environ.get("ALLOW_MAC_MAIL_SEND"))
        if open_full_disk_access:
            try:
                add("opened_full_disk_access_settings", True, **mm.open_system_settings_pane("full_disk_access"))
            except Exception as exc:
                add("opened_full_disk_access_settings", False, error=str(exc))
        if open_automation:
            try:
                add("opened_automation_settings", True, **mm.open_system_settings_pane("automation"))
            except Exception as exc:
                add("opened_automation_settings", False, error=str(exc))

        try:
            version_dir = mm.latest_mail_version()
            add(
                "mail_version_dir",
                version_dir is not None or not require_mail,
                path=str(version_dir) if version_dir else None,
                required=require_mail,
            )
            if version_dir is not None:
                envelope = mm.envelope_index_path()
                add("envelope_index_exists", envelope.exists(), path=str(envelope), required=require_mail)
                with mm.db_connect() as conn:
                    message_count = conn.execute("SELECT count(*) FROM messages").fetchone()[0]
                    mailbox_count = conn.execute("SELECT count(*) FROM mailboxes").fetchone()[0]
                add(
                    "mail_index_readable",
                    (message_count > 0 and mailbox_count > 0) or not require_mail,
                    message_count=message_count,
                    mailbox_count=mailbox_count,
                    required=require_mail,
                )
        except Exception as exc:
            add("mail_index_readable", not require_mail, error=str(exc), required=require_mail)
    except Exception as exc:
        add("module_load", False, error=str(exc))

    payload = {
        "ok": not failures,
        "checks": checks,
        "failures": failures,
        "privacy": "Doctor checks print environment status and counts only, not message bodies.",
    }
    print(json.dumps(payload, indent=2))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
