#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = PLUGIN_ROOT / "scripts" / "mac_mail_mcp.py"


def load_module():
    spec = importlib.util.spec_from_file_location("mac_mail_mcp", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MacMailMcpUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_module()
        self.tmp = tempfile.TemporaryDirectory()
        self.old_support = self.module.APP_SUPPORT
        self.old_log = self.module.OP_LOG
        self.old_body_search_db = self.module.BODY_SEARCH_DB
        self.old_eml_draft_root = self.module.EML_DRAFT_ROOT
        self.module.APP_SUPPORT = Path(self.tmp.name) / "support"
        self.module.OP_LOG = self.module.APP_SUPPORT / "operation-log.jsonl"
        self.module.BODY_SEARCH_DB = self.module.APP_SUPPORT / "body-search.sqlite3"
        self.module.EML_DRAFT_ROOT = self.module.APP_SUPPORT / "Draft Files"

    def tearDown(self) -> None:
        self.module.APP_SUPPORT = self.old_support
        self.module.OP_LOG = self.old_log
        self.module.BODY_SEARCH_DB = self.old_body_search_db
        self.module.EML_DRAFT_ROOT = self.old_eml_draft_root
        self.tmp.cleanup()

    def test_mailbox_role_classification_covers_spam_and_junk(self) -> None:
        spam = self.module.parse_mailbox_url("imap://abc/%5BGmail%5D/Spam")
        junk = self.module.parse_mailbox_url("imap://abc/Junk")
        ordinary = self.module.parse_mailbox_url("imap://abc/Presentations")
        self.assertEqual(spam["role"], "junk")
        self.assertEqual(junk["role"], "junk")
        self.assertEqual(ordinary["role"], "other")

    def test_zero_body_limit_means_empty_not_unbounded(self) -> None:
        self.assertEqual(self.module.truncate_text("secret body", 0), "")

    def test_create_draft_schema_requires_a_recipient_bucket(self) -> None:
        schema = self.module.TOOLS["mail_create_draft"]["inputSchema"]
        self.assertEqual(schema["required"], ["subject", "body"])
        self.assertNotIn("anyOf", schema)
        self.assertEqual(schema["properties"]["to"]["minItems"], 1)
        with self.assertRaisesRegex(self.module.ToolError, "recipient"):
            self.module.create_draft({"subject": "Hello", "body": "Body"})

    def test_tool_schemas_avoid_codex_rejected_composition_keywords(self) -> None:
        blocked = {"anyOf", "oneOf", "allOf", "not"}

        def walk(value, path):
            if isinstance(value, dict):
                for key, item in value.items():
                    self.assertNotIn(key, blocked, f"{path}.{key}")
                    walk(item, f"{path}.{key}")
            elif isinstance(value, list):
                for index, item in enumerate(value):
                    walk(item, f"{path}[{index}]")

        for tool_name, entry in self.module.TOOLS.items():
            schema = entry["inputSchema"]
            self.assertEqual(schema.get("type"), "object", tool_name)
            self.assertNotIn("enum", schema, tool_name)
            walk(schema, tool_name)

    def test_search_schema_exposes_easy_mailbox_filters(self) -> None:
        props = self.module.TOOLS["mail_search_messages"]["inputSchema"]["properties"]
        self.assertIn("mailbox_id", props)
        self.assertIn("mailbox_ids", props)
        self.assertIn("mailbox_name", props)
        self.assertIn("mailbox_path", props)
        self.assertIn("account", props)
        self.assertIn("max_results", props)
        self.assertIn("page_token", props)
        self.assertIn("mailbox_role", self.module.TOOLS["mail_list_mailboxes"]["inputSchema"]["properties"])

    def test_search_messages_supports_gmail_style_paging(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE mailboxes (url TEXT, total_count INTEGER, unread_count INTEGER, deleted_count INTEGER);
            CREATE TABLE messages (
              message_id INTEGER,
              conversation_id INTEGER,
              date_received INTEGER,
              date_sent INTEGER,
              sender INTEGER,
              subject INTEGER,
              summary INTEGER,
              mailbox INTEGER,
              read INTEGER,
              flagged INTEGER,
              deleted INTEGER,
              size INTEGER,
              global_message_id INTEGER
            );
            CREATE TABLE addresses (address TEXT, comment TEXT);
            CREATE TABLE subjects (subject TEXT);
            CREATE TABLE summaries (summary TEXT);
            CREATE TABLE message_global_data (message_id_header TEXT);
            CREATE TABLE attachments (message INTEGER, attachment_id INTEGER, name TEXT);
            """
        )
        conn.execute(
            "INSERT INTO mailboxes(rowid, url, total_count, unread_count, deleted_count) VALUES (1, 'imap://acct/Inbox', 3, 3, 0)"
        )
        conn.execute("INSERT INTO addresses(rowid, address, comment) VALUES (1, 'sender@example.com', 'Sender')")
        for index, subject in enumerate(["Newest", "Middle", "Oldest"], start=1):
            conn.execute("INSERT INTO subjects(rowid, subject) VALUES (?, ?)", (index, subject))
            conn.execute("INSERT INTO summaries(rowid, summary) VALUES (?, ?)", (index, subject))
            conn.execute("INSERT INTO message_global_data(rowid, message_id_header) VALUES (?, ?)", (index, f'<{index}@example.com>'))
            conn.execute(
                """
                INSERT INTO messages(
                  rowid, message_id, conversation_id, date_received, date_sent, sender, subject,
                  summary, mailbox, read, flagged, deleted, size, global_message_id
                )
                VALUES (?, ?, ?, ?, ?, 1, ?, ?, 1, 0, 0, 0, 100, ?)
                """,
                (index, index, index, 1000 + (4 - index), 1000 + (4 - index), index, index, index),
            )
        conn.commit()
        try:
            with mock.patch.object(self.module, "db_connect", return_value=conn):
                first = self.module.search_messages({"mailbox_role": "inbox", "max_results": 2})
                second = self.module.search_messages({"mailbox_role": "inbox", "page_token": first["next_page_token"], "max_results": 2})
        finally:
            conn.close()
        self.assertEqual(first["count"], 2)
        self.assertEqual([item["subject"] for item in first["messages"]], ["Newest", "Middle"])
        self.assertEqual(first["next_page_token"], "2")
        self.assertEqual(second["offset"], 2)
        self.assertEqual(second["next_page_token"], None)
        self.assertEqual([item["subject"] for item in second["messages"]], ["Oldest"])

    def test_new_tools_are_registered(self) -> None:
        for tool_name in [
            "mail_permissions_check",
            "mail_plugin_update_status",
            "mail_plugin_update_install",
            "mail_index_status",
            "mail_purge_body_index",
            "mail_rebuild_body_index",
            "mail_search_bodies",
            "mail_prepare_eml_draft",
            "mail_inspect_outgoing_draft",
        ]:
            self.assertIn(tool_name, self.module.TOOLS)

    def test_body_index_search_returns_snippets_from_private_cache(self) -> None:
        with self.module.body_index_connect() as conn:
            conn.execute(
                """
                INSERT INTO body_documents (
                  local_id, rfc_message_id, conversation_id, date_received, date_sent,
                  sender_address, sender_name, subject, mailbox_id, mailbox_url,
                  account_uuid, mailbox_name, mailbox_role, body, body_char_count,
                  body_truncated, indexed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    42,
                    "<test@example.com>",
                    7,
                    "2026-04-29 10:00:00",
                    "2026-04-29 10:00:00",
                    "sender@example.com",
                    "Sender",
                    "Needle subject",
                    3,
                    "imap://acct/Inbox",
                    "acct",
                    "Inbox",
                    "inbox",
                    "This body contains the very specific needle phrase.",
                    51,
                    0,
                    "2026-04-29T10:00:00+00:00",
                ),
            )
            conn.commit()
        result = self.module.search_body_index({"query": "specific needle"})
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["messages"][0]["local_id"], 42)
        self.assertIn("body_snippet", result["messages"][0])

    def test_permissions_check_reports_blocker_and_can_open_settings(self) -> None:
        with mock.patch.object(self.module, "db_connect", side_effect=self.module.ToolError("blocked")), \
             mock.patch.object(
                 self.module,
                 "open_system_settings_pane",
                 return_value={"opened": True, "kind": "full_disk_access"},
             ) as opener:
            result = self.module.permissions_check({"open_full_disk_access": True})
        self.assertFalse(result["ok"])
        self.assertFalse(result["permissions"]["full_disk_access"]["ok"])
        self.assertIn("blocked", result["permissions"]["full_disk_access"]["error"])
        self.assertEqual(result["settings_opened"][0]["kind"], "full_disk_access")
        opener.assert_called_once_with("full_disk_access")

    def test_update_status_reports_non_git_install(self) -> None:
        with mock.patch.object(self.module.shutil, "which", return_value="/usr/bin/git"), \
             mock.patch.object(
                 self.module,
                 "run_git",
                 return_value=subprocess.CompletedProcess(["git"], 1, "", "not a repo"),
             ):
            result = self.module.plugin_update_status({"check_remote": False})
        self.assertFalse(result["ok"])
        self.assertFalse(result["update_supported"])
        self.assertIn("not a git checkout", result["error"])

    def test_update_status_reports_available_remote_commit(self) -> None:
        def fake_run_git(args, *, timeout=30):
            values = {
                ("rev-parse", "--is-inside-work-tree"): subprocess.CompletedProcess(args, 0, "true\n", ""),
                ("rev-parse", "HEAD"): subprocess.CompletedProcess(args, 0, "local123\n", ""),
                ("branch", "--show-current"): subprocess.CompletedProcess(args, 0, "main\n", ""),
                ("config", "--get", "remote.origin.url"): subprocess.CompletedProcess(
                    args, 0, "https://github.com/KeystoneScience/mac-mail-codex-plugin.git\n", ""
                ),
                ("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"): subprocess.CompletedProcess(
                    args, 0, "origin/main\n", ""
                ),
                ("rev-parse", "origin/main"): subprocess.CompletedProcess(args, 0, "remote456\n", ""),
                ("status", "--short", "--untracked-files=no"): subprocess.CompletedProcess(args, 0, "", ""),
            }
            return values[tuple(args)]

        with mock.patch.object(self.module.shutil, "which", return_value="/usr/bin/git"), \
             mock.patch.object(self.module, "run_git", side_effect=fake_run_git):
            result = self.module.plugin_update_status({"check_remote": False})
        self.assertTrue(result["ok"])
        self.assertTrue(result["update_supported"])
        self.assertTrue(result["update_available"])
        self.assertFalse(result["dirty_tracked_files"])

    def test_update_install_requires_confirmation(self) -> None:
        with self.assertRaisesRegex(self.module.ToolError, "confirm_update"):
            self.module.plugin_update_install({})

    def test_purge_body_index_requires_confirmation(self) -> None:
        with self.assertRaisesRegex(self.module.ToolError, "confirm_purge"):
            self.module.purge_body_index({})

    def test_purge_body_index_removes_private_cache_only(self) -> None:
        with self.module.body_index_connect() as conn:
            conn.execute(
                """
                INSERT INTO body_documents (
                  local_id, rfc_message_id, conversation_id, date_received, date_sent,
                  sender_address, sender_name, subject, mailbox_id, mailbox_url,
                  account_uuid, mailbox_name, mailbox_role, body, body_char_count,
                  body_truncated, indexed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    43,
                    "<test@example.com>",
                    7,
                    "2026-04-29 10:00:00",
                    "2026-04-29 10:00:00",
                    "sender@example.com",
                    "Sender",
                    "Subject",
                    3,
                    "imap://acct/Inbox",
                    "acct",
                    "Inbox",
                    "inbox",
                    "body",
                    4,
                    0,
                    "2026-04-29T10:00:00+00:00",
                ),
            )
            conn.commit()
        self.assertTrue(self.module.BODY_SEARCH_DB.exists())
        result = self.module.purge_body_index({"confirm_purge": True})
        self.assertTrue(result["purged"])
        self.assertFalse(self.module.BODY_SEARCH_DB.exists())

    def test_body_index_skips_sent_by_default(self) -> None:
        rows = [
            {
                "mailbox_id": 1,
                "account_uuid": "acct-a",
                "role": "inbox",
                "mailbox_name": "Inbox",
                "mailbox_path": "Inbox",
                "url": "imap://acct-a/Inbox",
            },
            {
                "mailbox_id": 2,
                "account_uuid": "acct-a",
                "role": "sent",
                "mailbox_name": "Sent Mail",
                "mailbox_path": "Sent Mail",
                "url": "imap://acct-a/Sent%20Mail",
            },
        ]

        class DummyConn:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def execute(self, sql, params=()):
                self.last_params = params
                return [{"local_id": 99}]

        dummy = DummyConn()
        with mock.patch.object(self.module, "db_connect", return_value=dummy), \
             mock.patch.object(self.module, "mailbox_rows", return_value=rows):
            ids, notes = self.module.body_index_candidate_ids({"max_messages": 10})
        self.assertEqual(ids, [99])
        self.assertEqual(dummy.last_params[0], 1)
        self.assertIn("Sent/Drafts/Junk/Trash", " ".join(notes))

    def test_prepare_eml_draft_writes_unsent_file_without_opening_mail(self) -> None:
        result = self.module.prepare_eml_draft(
            {
                "to": ["person@example.com"],
                "subject": "Hello",
                "text_body": "Draft body",
                "open_in_mail": False,
            }
        )
        draft_path = Path(result["draft_file"])
        self.assertTrue(draft_path.exists())
        raw = draft_path.read_text(errors="replace")
        self.assertIn("X-Unsent: 1", raw)
        self.assertIn("Subject: Hello", raw)
        self.assertEqual(oct(draft_path.stat().st_mode & 0o777), "0o600")

    def test_prepare_eml_draft_blocks_executable_attachment(self) -> None:
        attachment = Path(self.tmp.name) / "bad.sh"
        attachment.write_text("#!/bin/sh\n")
        with self.assertRaisesRegex(self.module.ToolError, "unsafe attachment"):
            self.module.prepare_eml_draft(
                {
                    "to": ["person@example.com"],
                    "subject": "Hello",
                    "text_body": "Draft body",
                    "attachments": [str(attachment)],
                }
            )

    def test_filtered_mailbox_ids_supports_exact_and_friendly_filters(self) -> None:
        rows = [
            {
                "mailbox_id": 1,
                "account_uuid": "acct-a",
                "role": "inbox",
                "mailbox_name": "Inbox",
                "mailbox_path": "Inbox",
                "url": "imap://acct-a/Inbox",
            },
            {
                "mailbox_id": 2,
                "account_uuid": "acct-a",
                "role": "sent",
                "mailbox_name": "Sent Mail",
                "mailbox_path": "[Gmail]/Sent Mail",
                "url": "imap://acct-a/%5BGmail%5D/Sent%20Mail",
            },
            {
                "mailbox_id": 3,
                "account_uuid": "acct-a",
                "role": "junk",
                "mailbox_name": "Spam",
                "mailbox_path": "[Gmail]/Spam",
                "url": "imap://acct-a/%5BGmail%5D/Spam",
            },
        ]
        with mock.patch.object(self.module, "mailbox_rows", return_value=rows):
            self.assertEqual(self.module.filtered_mailbox_ids(mock.Mock(), mailbox_id="2"), [2])
            self.assertEqual(self.module.filtered_mailbox_ids(mock.Mock(), mailbox_ids="1,2"), [1, 2])
            self.assertEqual(self.module.filtered_mailbox_ids(mock.Mock(), mailbox_name="inbox"), [1])
            self.assertEqual(self.module.filtered_mailbox_ids(mock.Mock(), mailbox_path="sent mail"), [2])
            self.assertEqual(self.module.filtered_mailbox_ids(mock.Mock()), [1, 2])
            self.assertEqual(self.module.filtered_mailbox_ids(mock.Mock(), mailbox_id=3), [3])

    def test_nonnumeric_draft_id_is_rejected_before_send_gate(self) -> None:
        with self.assertRaisesRegex(self.module.ToolError, "digits"):
            self.module.send_draft(
                {
                    "draft_id": "1; display dialog \"bad\"",
                    "confirm_send": True,
                    "approval_note": "approved",
                }
            )

    def test_send_gate_remains_blocked_without_env(self) -> None:
        old = os.environ.pop("ALLOW_MAC_MAIL_SEND", None)
        try:
            with self.assertRaisesRegex(self.module.ToolError, "Sending is disabled"):
                self.module.send_draft(
                    {"draft_id": "123", "confirm_send": True, "approval_note": "approved"}
                )
        finally:
            if old is not None:
                os.environ["ALLOW_MAC_MAIL_SEND"] = old

    def test_send_gate_requires_inspected_draft_hash(self) -> None:
        old = os.environ.get("ALLOW_MAC_MAIL_SEND")
        os.environ["ALLOW_MAC_MAIL_SEND"] = "1"
        try:
            with self.assertRaisesRegex(self.module.ToolError, "draft_sha256"):
                self.module.send_draft(
                    {"draft_id": "123", "confirm_send": True, "approval_note": "approved"}
                )
        finally:
            if old is None:
                os.environ.pop("ALLOW_MAC_MAIL_SEND", None)
            else:
                os.environ["ALLOW_MAC_MAIL_SEND"] = old

    def test_send_schema_requires_draft_hash(self) -> None:
        required = self.module.TOOLS["mail_send_draft"]["inputSchema"]["required"]
        self.assertIn("draft_sha256", required)

    def test_operation_log_redacts_sensitive_fields_and_is_private(self) -> None:
        self.module.log_operation(
            "unit",
            {
                "subject": "Sensitive subject",
                "approval_note": "Sensitive approval note",
                "body": "Sensitive body",
                "destination_dir": "/tmp/sensitive/path",
                "safe_count": 2,
            },
        )
        raw = self.module.OP_LOG.read_text()
        self.assertNotIn("Sensitive subject", raw)
        self.assertNotIn("Sensitive approval note", raw)
        self.assertNotIn("Sensitive body", raw)
        payload = json.loads(raw)
        self.assertTrue(payload["subject_present"])
        self.assertTrue(payload["approval_note_present"])
        self.assertEqual(payload["safe_count"], 2)
        self.assertEqual(oct(self.module.OP_LOG.stat().st_mode & 0o777), "0o600")

    def test_export_attachments_requires_every_requested_name(self) -> None:
        class DummyConn:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        with mock.patch.object(self.module, "db_connect", return_value=DummyConn()), \
             mock.patch.object(self.module, "message_metadata", return_value={"mailbox_url": None}), \
             mock.patch.object(
                 self.module,
                 "locate_attachment_files",
                 return_value=[{"name": "a.pdf", "path": __file__, "size": 1}],
             ):
            with self.assertRaisesRegex(self.module.ToolError, "did not match exactly"):
                self.module.export_attachments(
                    {
                        "local_id": 1,
                        "destination_dir": str(Path(self.tmp.name) / "exports"),
                        "names": ["a.pdf", "missing.pdf"],
                    }
                )


if __name__ == "__main__":
    unittest.main()
