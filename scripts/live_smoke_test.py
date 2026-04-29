#!/usr/bin/env python3
"""Live read-only smoke checks for the Mac Mail MCP server.

This script intentionally avoids creating drafts, sending mail, exporting
attachments, or printing message bodies.
"""

from __future__ import annotations

import importlib.util
import json
import os
import time
from pathlib import Path
from typing import Any, Callable


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = PLUGIN_ROOT / "scripts" / "mac_mail_mcp.py"


def load_module():
    spec = importlib.util.spec_from_file_location("mac_mail_mcp", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def timed(name: str, fn: Callable[[], Any]) -> tuple[Any, dict[str, Any]]:
    start = time.perf_counter()
    result = fn()
    elapsed_ms = (time.perf_counter() - start) * 1000
    return result, {"name": name, "elapsed_ms": round(elapsed_ms, 2)}


def main() -> None:
    mm = load_module()
    checks: list[dict[str, Any]] = []

    state, timing = timed("get_state", lambda: mm.get_state({"include_mail_app": False}))
    checks.append({**timing, "message_count": state["message_count"], "mailbox_count": len(state["mailboxes"])})

    accounts, timing = timed("list_accounts", lambda: mm.list_accounts({"include_mail_app": False}))
    checks.append({**timing, "account_count": len(accounts["accounts"])})

    mailbox_listing, timing = timed(
        "list_mailboxes_query",
        lambda: mm.list_mailboxes({"query": "inbox", "include_empty": False, "limit": 10}),
    )
    mailbox_choices = [
        item for item in mailbox_listing["mailboxes"]
        if item.get("role") != "junk" and item.get("total_count", 0)
    ]
    checks.append({**timing, "result_count": len(mailbox_listing["mailboxes"]), "has_search_arguments": bool(mailbox_choices and mailbox_choices[0].get("search_arguments"))})

    overview, timing = timed(
        "inbox_overview",
        lambda: mm.inbox_overview({"include_candidates": True, "limit_per_lane": 3}),
    )
    checks.append(
        {
            **timing,
            "account_count": len(overview["accounts"]),
            "candidate_lanes": sorted(overview.get("triage_candidates", {}).keys()),
            "bodies_read": overview["coverage"]["bodies_read"],
        }
    )

    search, timing = timed(
        "search_inbox_unread",
        lambda: mm.search_messages({"mailbox_role": "inbox", "unread_only": True, "limit": 5}),
    )
    messages = search["messages"]
    checks.append({**timing, "result_count": len(messages)})

    mailbox_id_search = {"messages": []}
    mailbox_id = messages[0].get("mailbox_id") if messages else None
    if mailbox_id is None:
        latest_for_mailbox = mm.search_messages({"limit": 1})["messages"]
        mailbox_id = latest_for_mailbox[0].get("mailbox_id") if latest_for_mailbox else None
    if mailbox_id is not None:
        mailbox_id_search, timing = timed(
            "search_by_mailbox_id",
            lambda: mm.search_messages({"mailbox_id": mailbox_id, "limit": 5}),
        )
        checks.append(
            {
                **timing,
                "result_count": len(mailbox_id_search["messages"]),
                "all_results_in_mailbox": all(message.get("mailbox_id") == mailbox_id for message in mailbox_id_search["messages"]),
            }
        )

    future, timing = timed(
        "search_future_empty",
        lambda: mm.search_messages({"date_from": "2999-01-01", "limit": 5}),
    )
    checks.append({**timing, "result_count": len(future["messages"])})

    default_search, timing = timed("search_default_junk_filter", lambda: mm.search_messages({"limit": 100}))
    default_junk_count = sum(1 for message in default_search["messages"] if message.get("mailbox_role") == "junk")
    checks.append({**timing, "result_count": len(default_search["messages"]), "junk_result_count": default_junk_count})

    if messages:
        local_id = messages[0]["local_id"]
        metadata, timing = timed(
            "read_message_metadata",
            lambda: mm.read_message({"local_id": local_id, "include_body": False}),
        )
        checks.append(
            {
                **timing,
                "local_id": local_id,
                "has_subject": bool(metadata.get("subject")),
                "attachment_count": metadata.get("attachment_count", 0),
            }
        )

        thread, timing = timed(
            "read_thread_metadata",
            lambda: mm.read_thread(
                {
                    "conversation_id": metadata["conversation_id"],
                    "include_bodies": False,
                    "limit": 10,
                }
            ),
        )
        checks.append({**timing, "conversation_id": metadata["conversation_id"], "message_count": thread["count"]})

    body_probe = None
    for candidate in mm.search_messages({"limit": 25})["messages"]:
        read = mm.read_message(
            {
                "local_id": candidate["local_id"],
                "include_body": True,
                "max_body_chars": 500,
            }
        )
        if read.get("emlx_path"):
            body_probe = {
                "local_id": candidate["local_id"],
                "body_char_count": read.get("body_char_count"),
                "body_truncated": read.get("body_truncated"),
                "body_sample_returned_chars": len(read.get("body") or ""),
            }
            break
    checks.append({"name": "read_downloaded_body_probe", "found_downloaded_body": body_probe is not None, **(body_probe or {})})

    try:
        mm.send_draft({"draft_id": "not-numeric", "confirm_send": True, "approval_note": "smoke"})
        invalid_draft_id_blocked = False
    except mm.ToolError:
        invalid_draft_id_blocked = True

    old_gate = os.environ.pop("ALLOW_MAC_MAIL_SEND", None)
    try:
        try:
            mm.send_draft({"draft_id": "123", "confirm_send": True, "approval_note": "smoke"})
            send_gate_blocked = False
        except mm.ToolError:
            send_gate_blocked = True
    finally:
        if old_gate is not None:
            os.environ["ALLOW_MAC_MAIL_SEND"] = old_gate

    checks.append(
        {
            "name": "send_safety",
            "invalid_draft_id_blocked": invalid_draft_id_blocked,
            "env_gate_blocked": send_gate_blocked,
        }
    )

    required = {
        "has_indexed_messages": state["message_count"] > 0,
        "has_indexed_accounts": len(accounts["accounts"]) > 0,
        "mailbox_listing_has_search_arguments": bool(mailbox_choices and mailbox_choices[0].get("search_arguments")),
        "mailbox_id_search_scoped": bool(mailbox_id_search["messages"]) and all(message.get("mailbox_id") == mailbox_id for message in mailbox_id_search["messages"]),
        "overview_is_metadata_only": overview["coverage"]["bodies_read"] is False,
        "future_search_empty": len(future["messages"]) == 0,
        "default_search_excludes_junk": default_junk_count == 0,
        "downloaded_body_probe_found": body_probe is not None,
        "invalid_draft_id_blocked": invalid_draft_id_blocked,
        "send_env_gate_blocked": send_gate_blocked,
    }

    print(json.dumps({"ok": all(required.values()), "required": required, "checks": checks}, indent=2))


if __name__ == "__main__":
    main()
