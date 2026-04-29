#!/usr/bin/env python3
"""Benchmark common Mac Mail plugin read/search paths without printing mail bodies."""

from __future__ import annotations

import importlib.util
import json
import statistics
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


def run_case(name: str, fn: Callable[[], Any], iterations: int = 5) -> dict[str, Any]:
    timings: list[float] = []
    result_shape: dict[str, Any] = {}
    for _ in range(iterations):
        start = time.perf_counter()
        result = fn()
        timings.append((time.perf_counter() - start) * 1000)
    if isinstance(result, dict):
        if "messages" in result:
            result_shape["message_count"] = len(result["messages"])
        if "accounts" in result:
            result_shape["account_count"] = len(result["accounts"])
        if "count" in result:
            result_shape["count"] = result["count"]
        if "body_char_count" in result:
            result_shape["body_char_count"] = result["body_char_count"]
            result_shape["body_truncated"] = result.get("body_truncated")
    return {
        "name": name,
        "iterations": iterations,
        "median_ms": round(statistics.median(timings), 2),
        "min_ms": round(min(timings), 2),
        "max_ms": round(max(timings), 2),
        **result_shape,
    }


def downloaded_sample_local_id(mm: Any) -> int | None:
    for candidate in mm.search_messages({"limit": 50})["messages"]:
        read = mm.read_message(
            {
                "local_id": candidate["local_id"],
                "include_body": True,
                "max_body_chars": 0,
            }
        )
        if read.get("emlx_path"):
            return int(candidate["local_id"])
    return None


def main() -> None:
    mm = load_module()
    latest = mm.search_messages({"limit": 1})["messages"]
    sample_local_id = latest[0]["local_id"] if latest else None
    sample_conversation_id = latest[0]["conversation_id"] if latest else None
    downloaded_local_id = downloaded_sample_local_id(mm)
    sample_mailbox_id = latest[0].get("mailbox_id") if latest else None

    cases: list[dict[str, Any]] = [
        run_case("search_latest", lambda: mm.search_messages({"limit": 20})),
        run_case(
            "search_inbox_unread",
            lambda: mm.search_messages({"mailbox_role": "inbox", "unread_only": True, "limit": 20}),
        ),
        run_case(
            "search_inbox_attachments",
            lambda: mm.search_messages({"mailbox_role": "inbox", "has_attachments": True, "limit": 20}),
        ),
        run_case(
            "search_subject_invoice",
            lambda: mm.search_messages({"subject": "invoice", "limit": 20}),
        ),
        run_case("inbox_overview", lambda: mm.inbox_overview({"limit_per_lane": 5})),
    ]
    if sample_mailbox_id is not None:
        cases.append(
            run_case(
                "search_by_mailbox_id",
                lambda: mm.search_messages({"mailbox_id": sample_mailbox_id, "limit": 20}),
            )
        )
    if sample_local_id is not None:
        cases.append(
            run_case(
                "read_message_metadata",
                lambda: mm.read_message({"local_id": sample_local_id, "include_body": False}),
            )
        )
    if downloaded_local_id is not None:
        cases.append(
            run_case(
                "read_message_body_capped",
                lambda: mm.read_message({"local_id": downloaded_local_id, "include_body": True, "max_body_chars": 1000}),
            )
        )
    if sample_conversation_id is not None:
        cases.append(
            run_case(
                "read_thread_metadata",
                lambda: mm.read_thread({"conversation_id": sample_conversation_id, "include_bodies": False, "limit": 20}),
            )
        )

    print(
        json.dumps(
            {
                "mail_version_dir": str(mm.latest_mail_version()) if mm.latest_mail_version() else None,
                "cases": cases,
                "privacy": "Benchmarks print counts and timings only, not message bodies.",
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
