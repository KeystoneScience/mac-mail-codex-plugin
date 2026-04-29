#!/usr/bin/env python3
"""Minimal stdio MCP server for local Apple Mail.

The first pass deliberately avoids third-party dependencies. Read-only search
uses Mail's local Envelope Index in readonly mode; write operations use
AppleScript and are guarded.
"""

from __future__ import annotations

import email
import hashlib
import html
import json
import os
import plistlib
import re
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import formatdate, make_msgid
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse


SERVER_NAME = "mac-mail"
SERVER_VERSION = "0.6.1"
UPDATE_REPO_URL = os.environ.get("MAC_MAIL_PLUGIN_REPO", "https://github.com/KeystoneScience/mac-mail-codex-plugin.git")
UPDATE_BRANCH = os.environ.get("MAC_MAIL_PLUGIN_BRANCH", "main")
MAIL_ROOT = Path.home() / "Library" / "Mail"
APP_SUPPORT = Path.home() / "Library" / "Application Support" / "Codex Mac Mail"
OP_LOG = APP_SUPPORT / "operation-log.jsonl"
BODY_SEARCH_DB = APP_SUPPORT / "body-search.sqlite3"
DEFAULT_BODY_CHARS = 20_000
DEFAULT_THREAD_BODY_CHARS = 8_000
DEFAULT_BODY_INDEX_LIMIT = 500
MAX_BODY_INDEX_LIMIT = 2_000
MAX_DRAFT_ATTACHMENTS = 10
MAX_DRAFT_ATTACHMENT_BYTES = 25 * 1024 * 1024
SAFE_EXPORT_ROOT = APP_SUPPORT / "Exports"
EML_DRAFT_ROOT = APP_SUPPORT / "Draft Files"
DANGEROUS_ATTACHMENT_SUFFIXES = {
    ".app",
    ".command",
    ".dmg",
    ".pkg",
    ".scpt",
    ".sh",
    ".terminal",
    ".workflow",
}
SENSITIVE_LOG_KEYS = {
    "approval_note",
    "attachment_names",
    "body",
    "content",
    "destination_dir",
    "exported_path",
    "from",
    "html_body",
    "path",
    "recipients",
    "sender",
    "source_path",
    "subject",
    "text_body",
    "to",
    "cc",
    "bcc",
}


class ToolError(Exception):
    pass


def latest_mail_version() -> Path | None:
    persistence = MAIL_ROOT / "PersistenceInfo.plist"
    if persistence.exists():
        try:
            with persistence.open("rb") as handle:
                payload = plistlib.load(handle)
            version_name = payload.get("LastUsedVersionDirectoryName")
            if isinstance(version_name, str) and (MAIL_ROOT / version_name).exists():
                return MAIL_ROOT / version_name
        except Exception:
            pass
    versions = sorted(
        [p for p in MAIL_ROOT.iterdir() if p.is_dir() and re.fullmatch(r"V\d+", p.name)],
        key=lambda p: int(p.name[1:]),
        reverse=True,
    ) if MAIL_ROOT.exists() else []
    return versions[0] if versions else None


def envelope_index_path() -> Path:
    version = latest_mail_version()
    if not version:
        raise ToolError(
            f"Mail root not found at {MAIL_ROOT}. "
            "Open Apple Mail at least once, make sure accounts are configured, "
            "and grant Full Disk Access to the app running this plugin if macOS blocks Mail storage."
        )
    path = version / "MailData" / "Envelope Index"
    if not path.exists():
        raise ToolError(
            f"Envelope Index not found at {path}. "
            "Open Apple Mail and let it sync, then grant Full Disk Access to the app running this plugin."
        )
    return path


def full_disk_access_guidance(detail: str) -> str:
    return (
        f"{detail}\n\n"
        "Permission fix: open System Settings > Privacy & Security > Full Disk Access, "
        "then enable the app that runs this plugin, usually Codex and sometimes Terminal/iTerm "
        "if you are testing from a shell. After changing permissions, restart Codex or the shell."
    )


def db_connect() -> sqlite3.Connection:
    path = envelope_index_path()
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        return conn
    except sqlite3.Error as exc:
        raise ToolError(full_disk_access_guidance(f"Could not open Apple Mail's Envelope Index in read-only mode: {exc}")) from exc


def json_text(payload: Any) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(payload, indent=2, default=str)}]}


def error_text(message: str) -> dict[str, Any]:
    return {"isError": True, "content": [{"type": "text", "text": message}]}


def clamp_limit(value: Any, default: int = 20, maximum: int = 100) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError):
        limit = default
    return max(1, min(limit, maximum))


def parse_offset_arg(value: Any, name: str = "offset") -> int:
    if value in (None, ""):
        return 0
    try:
        offset = int(value)
    except (TypeError, ValueError) as exc:
        raise ToolError(f"{name} must be a non-negative integer") from exc
    if offset < 0:
        raise ToolError(f"{name} must be a non-negative integer")
    return offset


def parse_int_arg(value: Any, name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ToolError(f"{name} must be an integer") from exc


def parse_int_list_arg(value: Any, name: str) -> list[int]:
    if value in (None, "", []):
        return []
    if isinstance(value, str) and "," in value:
        values = [item.strip() for item in value.split(",") if item.strip()]
    else:
        values = value if isinstance(value, list) else [value]
    parsed: list[int] = []
    for item in values:
        parsed.append(parse_int_arg(item, name))
    return parsed


def validate_draft_id(value: Any) -> str:
    draft_id = str(value or "").strip()
    if not draft_id:
        raise ToolError("draft_id is required.")
    if not re.fullmatch(r"\d+", draft_id):
        raise ToolError("draft_id must contain only digits.")
    return draft_id


def stable_hash(value: Any) -> str:
    text = json.dumps(value, sort_keys=True, default=str) if not isinstance(value, str) else value
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def redact_for_log(payload: dict[str, Any]) -> dict[str, Any]:
    scrubbed: dict[str, Any] = {}
    for key, value in payload.items():
        if key in SENSITIVE_LOG_KEYS:
            if value in (None, "", [], {}):
                scrubbed[f"{key}_present"] = False
            elif isinstance(value, (list, tuple, set)):
                scrubbed[f"{key}_count"] = len(value)
                scrubbed[f"{key}_sha256"] = stable_hash(list(value))
            else:
                scrubbed[f"{key}_present"] = True
                scrubbed[f"{key}_sha256"] = stable_hash(str(value))
            continue
        scrubbed[key] = value
    return scrubbed


def mailbox_url_to_local_dir(mailbox_url: str | None) -> Path | None:
    if not mailbox_url:
        return None
    parsed = urlparse(mailbox_url)
    if parsed.scheme not in {"imap", "pop", "local"}:
        return None
    account_uuid = parsed.netloc
    version = latest_mail_version()
    if not account_uuid or not version:
        return None
    base = version / account_uuid
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    current = base
    for part in parts:
        current = current / f"{part}.mbox"
    return current


def parse_mailbox_url(mailbox_url: str | None) -> dict[str, Any]:
    if not mailbox_url:
        return {
            "scheme": None,
            "account_uuid": None,
            "mailbox_path": None,
            "mailbox_name": None,
            "role": "unknown",
        }
    parsed = urlparse(mailbox_url)
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    mailbox_path = "/".join(parts) if parts else None
    mailbox_name = parts[-1] if parts else None
    lowered_parts = [part.lower() for part in parts]
    role = "other"
    if mailbox_name and mailbox_name.lower() == "inbox":
        role = "inbox"
    elif any(part in {"sent", "sent mail", "sent messages", "sent items"} for part in lowered_parts):
        role = "sent"
    elif any("draft" in part for part in lowered_parts):
        role = "drafts"
    elif any(part in {"spam", "junk", "junk e-mail", "junk email"} for part in lowered_parts):
        role = "junk"
    elif any(part in {"trash", "deleted messages", "deleted items"} for part in lowered_parts):
        role = "trash"
    elif any(part in {"all mail", "archive", "archives"} for part in lowered_parts):
        role = "archive"
    elif any(part == "outbox" for part in lowered_parts):
        role = "outbox"
    return {
        "scheme": parsed.scheme or None,
        "account_uuid": parsed.netloc or None,
        "mailbox_path": mailbox_path,
        "mailbox_name": mailbox_name,
        "role": role,
    }


def enrich_message(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    payload = dict(row)
    mailbox = parse_mailbox_url(payload.get("mailbox_url"))
    payload.update(
        {
            "account_uuid": mailbox["account_uuid"],
            "mailbox_name": mailbox["mailbox_name"],
            "mailbox_role": mailbox["role"],
        }
    )
    return payload


def mailbox_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = [
        dict(row)
        for row in conn.execute(
            """
            SELECT ROWID AS mailbox_id, url, total_count, unread_count, deleted_count
            FROM mailboxes
            ORDER BY url
            """
        )
    ]
    for row in rows:
        row.update(parse_mailbox_url(row["url"]))
    return rows


def filtered_mailbox_ids(
    conn: sqlite3.Connection,
    *,
    mailbox_id: Any = None,
    mailbox_ids: Any = None,
    account_uuid: str = "",
    mailbox_role: str = "",
    mailbox_filter: str = "",
    mailbox_name: str = "",
    mailbox_path: str = "",
    include_spam: bool = False,
) -> list[int] | None:
    rows = mailbox_rows(conn)
    requested_ids = set(parse_int_list_arg(mailbox_ids, "mailbox_ids"))
    if mailbox_id not in (None, ""):
        requested_ids.add(parse_int_arg(mailbox_id, "mailbox_id"))
    role = mailbox_role.lower().strip()
    if role and role not in {"inbox", "sent", "drafts", "junk", "trash", "archive", "outbox", "other"}:
        raise ToolError(f"Unsupported mailbox_role: {mailbox_role}")

    name_filter = mailbox_name.lower().strip()
    path_filter = mailbox_path.lower().strip()
    url_filter = mailbox_filter.lower().strip()
    explicit_mailbox_target = bool(requested_ids or name_filter or path_filter or url_filter)
    explicit_filter = bool(account_uuid or role or explicit_mailbox_target or (not include_spam))
    if not explicit_filter:
        return None

    ids: list[int] = []
    for row in rows:
        row_id = int(row["mailbox_id"])
        if account_uuid and row.get("account_uuid") != account_uuid:
            continue
        if requested_ids and row_id not in requested_ids:
            continue
        if role and row.get("role") != role:
            continue
        row_name = (row.get("mailbox_name") or "").lower()
        row_path = (row.get("mailbox_path") or "").lower()
        decoded_url = unquote(row.get("url", "")).lower()
        if name_filter and row_name != name_filter:
            continue
        if path_filter and path_filter not in row_path:
            continue
        if url_filter and url_filter not in decoded_url and url_filter not in row_path and url_filter not in row_name:
            continue
        if not include_spam and role != "junk" and not explicit_mailbox_target and row.get("role") == "junk":
            continue
        ids.append(row_id)
    return ids


def placeholders(values: list[Any]) -> str:
    return ", ".join(["?"] * len(values))


def emlx_bucket(rowid: int) -> list[str]:
    bucket = str(max(rowid // 1000, 0))
    return list(reversed(bucket)) if bucket != "0" else ["0"]


def locate_emlx(rowid: int, mailbox_url: str | None) -> Path | None:
    filename = f"{rowid}.emlx"
    bucket_parts = emlx_bucket(rowid)
    mailbox_dir = mailbox_url_to_local_dir(mailbox_url)
    search_roots: list[Path] = []
    if mailbox_dir and mailbox_dir.exists():
        search_roots.append(mailbox_dir)
    version = latest_mail_version()
    if version:
        search_roots.append(version)

    for root in search_roots:
        data_suffix = Path("Data").joinpath(*bucket_parts) / "Messages" / filename
        for candidate in root.glob(f"*/{data_suffix}"):
            if candidate.exists():
                return candidate
        direct = root / data_suffix
        if direct.exists():
            return direct

    if mailbox_dir and mailbox_dir.exists():
        matches = list(mailbox_dir.rglob(filename))
        if matches:
            return matches[0]
    return None


def locate_attachment_files(rowid: int, mailbox_url: str | None, names: list[str] | None = None) -> list[dict[str, Any]]:
    wanted = {name for name in names or [] if name}
    bucket_parts = emlx_bucket(rowid)
    mailbox_dir = mailbox_url_to_local_dir(mailbox_url)
    search_roots: list[Path] = []
    if mailbox_dir and mailbox_dir.exists():
        search_roots.append(mailbox_dir)
    version = latest_mail_version()
    if version:
        search_roots.append(version)

    candidates: list[Path] = []
    attachment_suffix = Path("Data").joinpath(*bucket_parts) / "Attachments" / str(rowid)
    for root in search_roots:
        direct = root / attachment_suffix
        if direct.exists():
            candidates.extend([path for path in direct.rglob("*") if path.is_file()])
        candidates.extend([path for path in root.glob(f"*/{attachment_suffix}/**/*") if path.is_file()])

    seen: set[Path] = set()
    files: list[dict[str, Any]] = []
    for path in candidates:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if wanted and path.name not in wanted:
            continue
        try:
            size = path.stat().st_size
        except OSError:
            size = None
        files.append({"name": path.name, "path": str(path), "size": size})
    return sorted(files, key=lambda item: (item["name"], item["path"]))


def strip_html(value: str) -> str:
    value = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", value)
    value = re.sub(r"(?s)<br\s*/?>", "\n", value)
    value = re.sub(r"(?s)</p\s*>", "\n\n", value)
    value = re.sub(r"(?s)<.*?>", " ", value)
    value = html.unescape(value)
    return re.sub(r"[ \t]+", " ", value).strip()


def extract_body(message: email.message.EmailMessage, preferred: str = "plain") -> str:
    if preferred not in {"plain", "html", "raw"}:
        preferred = "plain"
    if preferred == "raw":
        return message.as_string()

    if message.is_multipart():
        plain_parts: list[str] = []
        html_parts: list[str] = []
        for part in message.walk():
            if part.get_content_disposition() == "attachment":
                continue
            ctype = part.get_content_type()
            try:
                content = part.get_content()
            except Exception:
                continue
            if ctype == "text/plain" and isinstance(content, str):
                plain_parts.append(content)
            elif ctype == "text/html" and isinstance(content, str):
                html_parts.append(content)
        if preferred == "html" and html_parts:
            return "\n\n".join(strip_html(part) for part in html_parts)
        if plain_parts:
            return "\n\n".join(part.strip() for part in plain_parts if part.strip())
        if html_parts:
            return "\n\n".join(strip_html(part) for part in html_parts)
        return ""

    try:
        content = message.get_content()
    except Exception:
        payload = message.get_payload(decode=True) or b""
        return payload.decode("utf-8", errors="replace")
    if isinstance(content, str) and message.get_content_type() == "text/html":
        return strip_html(content)
    return content if isinstance(content, str) else str(content)


def parse_emlx(path: Path) -> email.message.EmailMessage:
    data = path.read_bytes()
    first_newline = data.find(b"\n")
    if first_newline < 0:
        raise ToolError(f"Malformed emlx file: {path}")
    try:
        message_size = int(data[:first_newline].strip())
        message_bytes = data[first_newline + 1:first_newline + 1 + message_size]
    except ValueError:
        message_bytes = data[first_newline + 1:]
    return BytesParser(policy=policy.default).parsebytes(message_bytes)


def parse_epoch(value: Any, *, end_of_day: bool = False) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            suffix = "T23:59:59" if end_of_day else "T00:00:00"
            dt = datetime.fromisoformat(text + suffix)
        else:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ToolError(f"Invalid date/time value: {text}. Use YYYY-MM-DD or ISO datetime.") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return int(dt.timestamp())


def truncate_text(value: str | None, max_chars: Any) -> str | None:
    if value is None:
        return None
    try:
        limit = int(max_chars)
    except (TypeError, ValueError):
        return value
    if limit <= 0:
        return ""
    if len(value) <= limit:
        return value
    return value[:limit].rstrip() + "\n[truncated]"


def recipient_rows(conn: sqlite3.Connection, local_id: int) -> list[dict[str, Any]]:
    role_by_type = {0: "to", 1: "cc", 2: "bcc"}
    return [
        {
            **dict(row),
            "role": role_by_type.get(row["type"], f"type_{row['type']}"),
        }
        for row in conn.execute(
            """
            SELECT r.type, r.position, a.address, a.comment
            FROM recipients r
            LEFT JOIN addresses a ON a.ROWID = r.address
            WHERE r.message = ?
            ORDER BY r.type, r.position
            """,
            (local_id,),
        )
    ]


def messages_metadata(conn: sqlite3.Connection, local_ids: list[int]) -> list[dict[str, Any]]:
    if not local_ids:
        return []
    rows = [
        enrich_message(row)
        for row in conn.execute(
        f"""
        SELECT
          m.ROWID AS local_id,
          m.message_id AS apple_message_id,
          gd.message_id_header AS rfc_message_id,
          m.conversation_id,
          datetime(m.date_received, 'unixepoch') AS date_received,
          datetime(m.date_sent, 'unixepoch') AS date_sent,
          a.address AS sender_address,
          a.comment AS sender_name,
          s.subject AS subject,
          sm.summary AS summary,
          m.mailbox AS mailbox_id,
          mb.url AS mailbox_url,
          m.read AS read,
          m.flagged AS flagged,
          m.deleted AS deleted,
          m.size AS size,
          (SELECT count(*) FROM attachments att_count WHERE att_count.message = m.ROWID) AS attachment_count
        FROM messages m
        LEFT JOIN addresses a ON a.ROWID = m.sender
        LEFT JOIN subjects s ON s.ROWID = m.subject
        LEFT JOIN summaries sm ON sm.ROWID = m.summary
        LEFT JOIN message_global_data gd ON gd.ROWID = m.global_message_id
        LEFT JOIN mailboxes mb ON mb.ROWID = m.mailbox
        WHERE m.ROWID IN ({placeholders(local_ids)})
        """,
            local_ids,
        )
    ]
    by_id: dict[int, dict[str, Any]] = {}
    for row in rows:
        row["recipients"] = []
        row["attachments"] = []
        by_id[int(row["local_id"])] = row

    id_sql = placeholders(local_ids)
    for row in conn.execute(
        f"""
        SELECT r.message AS local_id, r.type, r.position, a.address, a.comment
        FROM recipients r
        LEFT JOIN addresses a ON a.ROWID = r.address
        WHERE r.message IN ({id_sql})
        ORDER BY r.message, r.type, r.position
        """,
        local_ids,
    ):
        target = by_id.get(int(row["local_id"]))
        if not target:
            continue
        recipient = dict(row)
        recipient["role"] = {0: "to", 1: "cc", 2: "bcc"}.get(row["type"], f"type_{row['type']}")
        del recipient["local_id"]
        target["recipients"].append(recipient)

    for row in conn.execute(
        f"""
        SELECT message AS local_id, attachment_id, name
        FROM attachments
        WHERE message IN ({id_sql})
        ORDER BY message, name
        """,
        local_ids,
    ):
        target = by_id.get(int(row["local_id"]))
        if not target:
            continue
        attachment = dict(row)
        del attachment["local_id"]
        target["attachments"].append(attachment)

    return [by_id[local_id] for local_id in local_ids if local_id in by_id]


def message_metadata(conn: sqlite3.Connection, local_id: int) -> dict[str, Any]:
    messages = messages_metadata(conn, [local_id])
    if not messages:
        raise ToolError(f"No message found for local_id {local_id}")
    return messages[0]


def load_message_body(result: dict[str, Any], body_format: str, max_body_chars: Any = None) -> None:
    path = locate_emlx(int(result["local_id"]), result.get("mailbox_url"))
    result["emlx_path"] = str(path) if path else None
    if path:
        parsed = parse_emlx(path)
        result["headers"] = {
            "from": parsed.get("from"),
            "to": parsed.get("to"),
            "cc": parsed.get("cc"),
            "date": parsed.get("date"),
            "message_id": parsed.get("message-id"),
            "in_reply_to": parsed.get("in-reply-to"),
            "references": parsed.get("references"),
        }
        body = extract_body(parsed, body_format)
        limited_body = truncate_text(body, max_body_chars)
        result["body"] = limited_body
        result["body_truncated"] = limited_body != body
        result["body_char_count"] = len(body)
    else:
        result["body"] = None
        result["warning"] = "Message body is not present as a downloaded .emlx file in local Mail storage."


def message_details(
    local_id: int,
    *,
    include_body: bool = True,
    body_format: str = "plain",
    include_attachment_paths: bool = False,
    max_body_chars: Any = None,
) -> dict[str, Any]:
    with db_connect() as conn:
        result = message_metadata(conn, local_id)
    if include_body:
        load_message_body(result, body_format, max_body_chars)
    if include_attachment_paths:
        result["attachment_files"] = locate_attachment_files(local_id, result.get("mailbox_url"))
    return result


def log_operation(action: str, payload: dict[str, Any]) -> None:
    APP_SUPPORT.mkdir(parents=True, exist_ok=True)
    os.chmod(APP_SUPPORT, 0o700)
    scrubbed = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "action": action,
        "plugin": f"{SERVER_NAME}@{SERVER_VERSION}",
        **redact_for_log(payload),
    }
    with OP_LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(scrubbed, default=str) + "\n")
    os.chmod(OP_LOG, 0o600)


def applescript_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def run_osascript(script: str, timeout: int = 30) -> str:
    result = subprocess.run(
        ["osascript"],
        input=script,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise ToolError(detail)
    return result.stdout.strip()


def open_system_settings_pane(kind: str) -> dict[str, Any]:
    urls = {
        "full_disk_access": "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles",
        "automation": "x-apple.systempreferences:com.apple.preference.security?Privacy_Automation",
        "privacy": "x-apple.systempreferences:com.apple.preference.security",
    }
    target = urls.get(kind, urls["privacy"])
    result = subprocess.run(["open", target], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise ToolError(result.stderr.strip() or result.stdout.strip() or f"Could not open settings pane: {kind}")
    return {"opened": True, "kind": kind, "url": target}


def mail_app_accounts() -> list[dict[str, Any]]:
    script = r'''
tell application "Mail"
  set oldDelims to AppleScript's text item delimiters
  set rows to {}
  repeat with acct in accounts
    set acctName to ""
    set acctEmails to ""
    try
      set acctName to name of acct as text
    end try
    try
      set AppleScript's text item delimiters to ","
      set acctEmails to (email addresses of acct) as text
    end try
    set end of rows to acctName & tab & acctEmails
  end repeat
  set AppleScript's text item delimiters to linefeed
  set out to rows as text
  set AppleScript's text item delimiters to oldDelims
  return out
end tell
'''
    output = run_osascript(script, timeout=15)
    accounts: list[dict[str, Any]] = []
    for line in output.splitlines():
        name, _, emails = line.partition("\t")
        accounts.append(
            {
                "name": name,
                "email_addresses": [item.strip() for item in emails.split(",") if item.strip()],
            }
        )
    return accounts


def local_email_addresses() -> set[str]:
    try:
        return {
            address.lower()
            for account in mail_app_accounts()
            for address in account.get("email_addresses", [])
        }
    except Exception:
        return set()


def permissions_check(args: dict[str, Any]) -> dict[str, Any]:
    include_mail_app = bool(args.get("include_mail_app", False))
    open_full_disk_access = bool(args.get("open_full_disk_access", False))
    open_automation = bool(args.get("open_automation", False))
    payload: dict[str, Any] = {
        "permissions": {
            "full_disk_access": {
                "required_for": "Reading Apple Mail's local Envelope Index and downloaded .emlx files.",
                "ok": False,
            },
            "mail_automation": {
                "required_for": "Creating visible drafts, opening Mail windows, and sending approved drafts.",
                "ok": None,
                "checked": include_mail_app,
            },
        },
        "settings_opened": [],
        "instructions": [
            "Grant Full Disk Access to Codex. If testing from a shell, also grant it to Terminal or iTerm.",
            "Apple Mail Automation permission is requested by macOS the first time draft/open/send tools control Mail.app.",
            "After changing permissions, restart Codex or the shell that launches the MCP server.",
        ],
    }
    try:
        with db_connect() as conn:
            message_count = conn.execute("SELECT count(*) FROM messages").fetchone()[0]
            mailbox_count = conn.execute("SELECT count(*) FROM mailboxes").fetchone()[0]
        payload["permissions"]["full_disk_access"].update(
            {"ok": True, "message_count": message_count, "mailbox_count": mailbox_count}
        )
    except Exception as exc:
        payload["permissions"]["full_disk_access"].update({"ok": False, "error": str(exc)})

    if include_mail_app:
        try:
            version = run_osascript('tell application "Mail" to get version', timeout=10)
            payload["permissions"]["mail_automation"].update({"ok": True, "mail_version": version})
        except Exception as exc:
            payload["permissions"]["mail_automation"].update(
                {
                    "ok": False,
                    "error": str(exc),
                    "fix": "Grant Automation permission allowing the app running this plugin to control Mail.app.",
                }
            )

    if open_full_disk_access:
        payload["settings_opened"].append(open_system_settings_pane("full_disk_access"))
    if open_automation:
        payload["settings_opened"].append(open_system_settings_pane("automation"))
    payload["ok"] = bool(payload["permissions"]["full_disk_access"]["ok"]) and (
        not include_mail_app or bool(payload["permissions"]["mail_automation"].get("ok"))
    )
    return payload


def list_accounts(args: dict[str, Any]) -> dict[str, Any]:
    include_mail_app = bool(args.get("include_mail_app", False))
    with db_connect() as conn:
        rows = mailbox_rows(conn)
    accounts: dict[str, dict[str, Any]] = {}
    for row in rows:
        account_uuid = row["account_uuid"] or "(local)"
        account = accounts.setdefault(
            account_uuid,
            {
                "account_uuid": account_uuid,
                "scheme": row["scheme"],
                "mailbox_count": 0,
                "total_count": 0,
                "unread_count": 0,
                "roles": {},
            },
        )
        account["mailbox_count"] += 1
        account["total_count"] += row.get("total_count") or 0
        account["unread_count"] += row.get("unread_count") or 0
        role = row["role"]
        account["roles"][role] = account["roles"].get(role, 0) + 1
    payload: dict[str, Any] = {
        "mail_root": str(MAIL_ROOT),
        "mail_version_dir": str(latest_mail_version()) if latest_mail_version() else None,
        "accounts": sorted(accounts.values(), key=lambda item: item["account_uuid"]),
        "notes": ["Account UUIDs come from local Apple Mail mailbox URLs, not provider account names."],
    }
    if include_mail_app:
        payload["mail_app_accounts"] = mail_app_accounts()
    return payload


def list_mailboxes(args: dict[str, Any]) -> dict[str, Any]:
    account_uuid = str(args.get("account_uuid") or args.get("account") or "").strip()
    role_filter = str(args.get("role") or args.get("mailbox_role") or "").strip().lower()
    query = str(args.get("query") or "").strip().lower()
    name_filter = str(args.get("name") or args.get("mailbox_name") or "").strip().lower()
    path_filter = str(args.get("path") or args.get("mailbox_path") or "").strip().lower()
    include_empty = bool(args.get("include_empty", True))
    limit = clamp_limit(args.get("limit"), 100, 250)
    with db_connect() as conn:
        rows = mailbox_rows(conn)
    mailboxes: list[dict[str, Any]] = []
    for row in rows:
        if account_uuid and row["account_uuid"] != account_uuid:
            continue
        if role_filter and row["role"] != role_filter:
            continue
        row_name = (row.get("mailbox_name") or "").lower()
        row_path = (row.get("mailbox_path") or "").lower()
        decoded_url = unquote(row.get("url", "")).lower()
        if name_filter and row_name != name_filter:
            continue
        if path_filter and path_filter not in row_path:
            continue
        if query and not any(query in value for value in [row_name, row_path, decoded_url, row["role"]]):
            continue
        if not include_empty and not (row.get("total_count") or row.get("unread_count")):
            continue
        payload = dict(row)
        payload["search_arguments"] = {"mailbox_id": row["mailbox_id"], "limit": 20}
        if row.get("account_uuid"):
            payload["account_scoped_role_arguments"] = {
                "account_uuid": row["account_uuid"],
                "mailbox_role": row["role"],
                "limit": 20,
            }
        mailboxes.append(payload)
        if len(mailboxes) >= limit:
            break
    return {
        "mailboxes": mailboxes,
        "count": len(mailboxes),
        "limit": limit,
        "notes": [
            "Use search_arguments.mailbox_id for exact mailbox searches.",
            "Use account_uuid plus mailbox_role for all matching role mailboxes within one local account.",
            "Use query/name/path here to find a mailbox before searching it.",
        ],
    }


def candidate_from_message(message: dict[str, Any], reason_codes: list[str]) -> dict[str, Any]:
    return {
        "conversation_id": message.get("conversation_id"),
        "latest_local_id": message.get("local_id"),
        "account_uuid": message.get("account_uuid"),
        "mailbox_id": message.get("mailbox_id"),
        "mailbox_role": message.get("mailbox_role"),
        "mailbox_name": message.get("mailbox_name"),
        "date_received": message.get("date_received"),
        "date_sent": message.get("date_sent"),
        "from": {
            "name": message.get("sender_name"),
            "address": message.get("sender_address"),
        },
        "subject": message.get("subject"),
        "summary": message.get("summary"),
        "unread": message.get("read") == 0,
        "flagged": message.get("flagged") != 0,
        "attachment_count": message.get("attachment_count", 0),
        "reason_codes": reason_codes,
        "next_read": {
            "tool": "mail_read_thread",
            "arguments": {
                "conversation_id": message.get("conversation_id"),
                "include_bodies": False,
                "limit": 20,
            },
        },
    }


def newest_message_at(conn: sqlite3.Connection, mailbox_ids: list[int]) -> str | None:
    if not mailbox_ids:
        return None
    row = conn.execute(
        f"""
        SELECT datetime(max(date_received), 'unixepoch') AS newest
        FROM messages
        WHERE mailbox IN ({placeholders(mailbox_ids)}) AND deleted = 0
        """,
        mailbox_ids,
    ).fetchone()
    return row["newest"] if row else None


def inbox_overview(args: dict[str, Any]) -> dict[str, Any]:
    since = args.get("since")
    if not since:
        since = (datetime.now().astimezone() - timedelta(days=7)).date().isoformat()
    account_uuid = str(args.get("account_uuid") or args.get("account") or "").strip()
    include_candidates = bool(args.get("include_candidates", True))
    include_mail_app = bool(args.get("include_mail_app", False))
    include_mailboxes = bool(args.get("include_mailboxes", False))
    limit_per_lane = clamp_limit(args.get("limit_per_lane"), 10, 50)

    with db_connect() as conn:
        rows = mailbox_rows(conn)
        accounts: dict[str, dict[str, Any]] = {}
        for row in rows:
            uuid = row["account_uuid"] or "(local)"
            if account_uuid and uuid != account_uuid:
                continue
            account = accounts.setdefault(
                uuid,
                {
                    "account_uuid": uuid,
                    "scheme": row["scheme"],
                    "mailboxes": [],
                    "_mailbox_ids": [],
                    "roles": {},
                    "total_count": 0,
                    "unread_count": 0,
                    "sync_diagnostics": {},
                },
            )
            account["_mailbox_ids"].append(int(row["mailbox_id"]))
            account["mailboxes"].append(
                {
                    "mailbox_id": row["mailbox_id"],
                    "role": row["role"],
                    "name": row["mailbox_name"],
                    "path": row["mailbox_path"],
                    "total_count": row["total_count"],
                    "unread_count": row["unread_count"],
                }
            )
            account["roles"][row["role"]] = account["roles"].get(row["role"], 0) + 1
            account["total_count"] += row.get("total_count") or 0
            account["unread_count"] += row.get("unread_count") or 0
        for account in accounts.values():
            mailbox_ids = account["_mailbox_ids"]
            roles = account["roles"]
            account["sync_diagnostics"] = {
                "latest_indexed_message_at": newest_message_at(conn, mailbox_ids),
                "has_inbox": roles.get("inbox", 0) > 0,
                "has_sent": roles.get("sent", 0) > 0,
                "has_drafts": roles.get("drafts", 0) > 0,
                "junk_filter_supported": roles.get("junk", 0) > 0,
                "local_index_only": True,
            }
            account["mailbox_count"] = len(mailbox_ids)
            del account["_mailbox_ids"]
            if not include_mailboxes:
                del account["mailboxes"]

    payload: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "coverage": {
            "mail_root": str(MAIL_ROOT),
            "mail_version_dir": str(latest_mail_version()) if latest_mail_version() else None,
            "local_index_only": True,
            "bodies_read": False,
            "warnings": [
                "Apple Mail's local index may lag remote providers.",
                "Message bodies are not read by this overview; use next_read only for relevant candidates.",
            ],
        },
        "accounts": sorted(accounts.values(), key=lambda item: item["account_uuid"]),
    }
    if include_mail_app:
        try:
            payload["mail_app_accounts"] = mail_app_accounts()
        except Exception as exc:
            payload["mail_app_accounts_error"] = str(exc)

    if include_candidates:
        candidate_args = {
            "date_from": since,
            "limit": limit_per_lane,
            "include_spam": False,
        }
        if account_uuid:
            candidate_args["account_uuid"] = account_uuid
        unread = search_messages({**candidate_args, "mailbox_role": "inbox", "unread_only": True})["messages"]
        flagged = search_messages({**candidate_args, "flagged_only": True})["messages"]
        attachments = search_messages({**candidate_args, "mailbox_role": "inbox", "has_attachments": True})["messages"]
        recent = search_messages({**candidate_args, "mailbox_role": "inbox"})["messages"]
        payload["triage_candidates"] = {
            "unread_recent": [candidate_from_message(item, ["unread", "recent", "inbox"]) for item in unread],
            "flagged": [candidate_from_message(item, ["flagged", "recent"]) for item in flagged],
            "attachments": [candidate_from_message(item, ["has_attachment", "recent", "inbox"]) for item in attachments],
            "recent_inbox": [candidate_from_message(item, ["recent", "inbox"]) for item in recent],
        }
    return payload


def search_messages(args: dict[str, Any]) -> dict[str, Any]:
    query = str(args.get("query") or "").strip()
    limit = clamp_limit(args.get("max_results", args.get("limit")), 20, 100)
    offset = parse_offset_arg(
        args.get("offset", args.get("page_token") or args.get("next_page_token")),
        "offset/page_token",
    )
    unread_only = bool(args.get("unread_only", False))
    flagged_only = bool(args.get("flagged_only", False))
    include_deleted = bool(args.get("include_deleted", False))
    include_spam = bool(args.get("include_spam", False))
    mailbox_id = args.get("mailbox_id")
    mailbox_ids = args.get("mailbox_ids")
    mailbox_filter = str(args.get("mailbox") or "").strip()
    mailbox_name = str(args.get("mailbox_name") or args.get("mailboxName") or "").strip()
    mailbox_path = str(args.get("mailbox_path") or args.get("mailboxPath") or "").strip()
    mailbox_role = str(args.get("mailbox_role") or "").strip().lower()
    account_uuid = str(args.get("account_uuid") or args.get("account") or "").strip()
    sender = str(args.get("sender") or "").strip()
    recipient = str(args.get("recipient") or args.get("to") or "").strip()
    subject = str(args.get("subject") or "").strip()
    has_attachments = args.get("has_attachments")
    date_from = parse_epoch(args.get("date_from"))
    date_to = parse_epoch(args.get("date_to"), end_of_day=True)

    where = []
    params: list[Any] = []
    with db_connect() as conn:
        mailbox_ids = filtered_mailbox_ids(
            conn,
            mailbox_id=mailbox_id,
            mailbox_ids=mailbox_ids,
            account_uuid=account_uuid,
            mailbox_role=mailbox_role,
            mailbox_filter=mailbox_filter,
            mailbox_name=mailbox_name,
            mailbox_path=mailbox_path,
            include_spam=include_spam,
        )
        if mailbox_ids == []:
            return {
                "messages": [],
                "limit": limit,
                "offset": offset,
                "next_page_token": None,
                "query": query,
                "notes": ["No local Apple Mail mailboxes matched the requested filters."],
            }
        if not include_deleted:
            where.append("m.deleted = 0")
        if mailbox_ids is not None:
            where.append(f"m.mailbox IN ({placeholders(mailbox_ids)})")
            params.extend(mailbox_ids)
        if unread_only:
            where.append("m.read = 0")
        if flagged_only:
            where.append("m.flagged != 0")
        if sender:
            where.append("(a.address LIKE ? OR a.comment LIKE ?)")
            params.extend([f"%{sender}%", f"%{sender}%"])
        if recipient:
            where.append(
                """
                EXISTS (
                  SELECT 1
                  FROM recipients r
                  LEFT JOIN addresses ra ON ra.ROWID = r.address
                  WHERE r.message = m.ROWID
                    AND (ra.address LIKE ? OR ra.comment LIKE ?)
                )
                """
            )
            params.extend([f"%{recipient}%", f"%{recipient}%"])
        if subject:
            where.append("s.subject LIKE ?")
            params.append(f"%{subject}%")
        if has_attachments is not None:
            if bool(has_attachments):
                where.append("EXISTS (SELECT 1 FROM attachments att_filter WHERE att_filter.message = m.ROWID)")
            else:
                where.append("NOT EXISTS (SELECT 1 FROM attachments att_filter WHERE att_filter.message = m.ROWID)")
        if date_from is not None:
            where.append("m.date_received >= ?")
            params.append(date_from)
        if date_to is not None:
            where.append("m.date_received <= ?")
            params.append(date_to)
        if query:
            like = f"%{query}%"
            where.append(
                "(s.subject LIKE ? OR a.address LIKE ? OR a.comment LIKE ? OR sm.summary LIKE ? "
                "OR mb.url LIKE ? OR gd.message_id_header LIKE ?)"
            )
            params.extend([like, like, like, like, like, like])

        where_sql = "WHERE " + " AND ".join(where) if where else ""
        sql = f"""
            SELECT
              m.ROWID AS local_id,
              m.message_id AS apple_message_id,
              gd.message_id_header AS rfc_message_id,
              m.conversation_id,
              datetime(m.date_received, 'unixepoch') AS date_received,
              datetime(m.date_sent, 'unixepoch') AS date_sent,
              a.address AS sender_address,
              a.comment AS sender_name,
              s.subject AS subject,
              sm.summary AS summary,
              m.mailbox AS mailbox_id,
              mb.url AS mailbox_url,
              m.read AS read,
              m.flagged AS flagged,
              m.deleted AS deleted,
              m.size AS size,
              (SELECT count(*) FROM attachments att_count WHERE att_count.message = m.ROWID) AS attachment_count
            FROM messages m
            LEFT JOIN addresses a ON a.ROWID = m.sender
            LEFT JOIN subjects s ON s.ROWID = m.subject
            LEFT JOIN summaries sm ON sm.ROWID = m.summary
            LEFT JOIN message_global_data gd ON gd.ROWID = m.global_message_id
            LEFT JOIN mailboxes mb ON mb.ROWID = m.mailbox
            {where_sql}
            ORDER BY m.date_received DESC, m.ROWID DESC
            LIMIT ? OFFSET ?
        """
        params.extend([limit + 1, offset])
        fetched = [enrich_message(row) for row in conn.execute(sql, params)]
    has_more = len(fetched) > limit
    rows = fetched[:limit]
    next_offset = offset + limit if has_more else None
    return {
        "messages": rows,
        "count": len(rows),
        "limit": limit,
        "offset": offset,
        "next_offset": next_offset,
        "next_page_token": str(next_offset) if next_offset is not None else None,
        "query": query,
        "notes": [
            "Search uses Apple Mail's local readonly Envelope Index.",
            "Default searches exclude local Junk/Spam mailboxes unless include_spam=true or mailbox_role='junk'.",
            "Target a specific mailbox with mailbox_id from mail_list_mailboxes for the least ambiguous search.",
            "Friendly mailbox filters are also available: mailbox_name, mailbox_path, mailbox_role, account_uuid, and mailbox.",
            "For Gmail-style paging, pass max_results and then pass next_page_token as page_token on the next call.",
            "Date filtering and ordering use Mail's date_received field for indexed performance.",
            "Bodies require mail_read_message and are only available for downloaded .emlx files.",
        ],
    }


def get_state(args: dict[str, Any]) -> dict[str, Any]:
    include_mail_app = bool(args.get("include_mail_app", False))
    state: dict[str, Any] = {
        "mail_root": str(MAIL_ROOT),
        "mail_version_dir": str(latest_mail_version()) if latest_mail_version() else None,
        "envelope_index": str(envelope_index_path()),
        "capabilities": {
            "search_local_index": True,
            "search_plugin_body_index": BODY_SEARCH_DB.exists(),
            "read_downloaded_emlx": True,
            "prepare_eml_draft_file": True,
            "create_visible_draft": True,
            "send_draft": os.environ.get("ALLOW_MAC_MAIL_SEND") == "1",
            "permissions_check": True,
            "git_self_update": True,
        },
        "warnings": [
            "Local Apple Mail index may lag remote providers until Mail.app syncs.",
            "AppleScript calls may trigger macOS Automation permission prompts.",
        ],
    }
    with db_connect() as conn:
        state["message_count"] = conn.execute("SELECT count(*) FROM messages").fetchone()[0]
        state["mailboxes"] = [
            dict(row)
            for row in conn.execute(
                """
                SELECT ROWID AS mailbox_id, url, total_count, unread_count, deleted_count
                FROM mailboxes
                ORDER BY url
                LIMIT 250
                """
            )
        ]
        account_ids = set()
        for mailbox in state["mailboxes"]:
            parsed = urlparse(mailbox["url"])
            if parsed.netloc:
                account_ids.add(parsed.netloc)
            mailbox.update(parse_mailbox_url(mailbox["url"]))
        state["account_uuids"] = sorted(account_ids)

    if include_mail_app:
        try:
            state["mail_app"] = {
                "application_version": run_osascript('tell application "Mail" to get application version', timeout=10)
            }
        except Exception as exc:
            state["mail_app"] = {"error": str(exc)}
    return state


def read_message(args: dict[str, Any]) -> dict[str, Any]:
    local_id = args.get("local_id")
    if local_id is None:
        raise ToolError("local_id is required. Use mail_search_messages first.")
    try:
        local_id = int(local_id)
    except (TypeError, ValueError) as exc:
        raise ToolError("local_id must be an integer") from exc

    include_body = bool(args.get("include_body", True))
    body_format = str(args.get("body_format") or "plain")
    return message_details(
        local_id,
        include_body=include_body,
        body_format=body_format,
        include_attachment_paths=bool(args.get("include_attachment_paths", False)),
        max_body_chars=args.get("max_body_chars", DEFAULT_BODY_CHARS),
    )


def read_thread(args: dict[str, Any]) -> dict[str, Any]:
    local_id = args.get("local_id")
    conversation_id = args.get("conversation_id")
    if local_id is None and conversation_id is None:
        raise ToolError("local_id or conversation_id is required.")
    try:
        if local_id is not None:
            local_id = int(local_id)
        if conversation_id is not None:
            conversation_id = int(conversation_id)
    except (TypeError, ValueError) as exc:
        raise ToolError("local_id and conversation_id must be integers") from exc

    include_bodies = bool(args.get("include_bodies", False))
    body_format = str(args.get("body_format") or "plain")
    limit = clamp_limit(args.get("limit"), 20, 100)
    if conversation_id is None:
        with db_connect() as conn:
            row = conn.execute("SELECT conversation_id FROM messages WHERE ROWID = ?", (local_id,)).fetchone()
        if not row:
            raise ToolError(f"No message found for local_id {local_id}")
        conversation_id = int(row[0])

    with db_connect() as conn:
        ids = [
            int(row[0])
            for row in conn.execute(
                """
                SELECT ROWID
                FROM messages
                WHERE conversation_id = ?
                ORDER BY date_received, ROWID
                LIMIT ?
                """,
                (conversation_id, limit),
            )
        ]
        messages = messages_metadata(conn, ids)
    if include_bodies:
        for message in messages:
            load_message_body(message, body_format, args.get("max_body_chars", DEFAULT_THREAD_BODY_CHARS))
    return {
        "conversation_id": conversation_id,
        "messages": messages,
        "count": len(messages),
        "note": "Threading uses Apple Mail's local conversation_id, which may differ from a provider thread id.",
    }


def list_attachments(args: dict[str, Any]) -> dict[str, Any]:
    try:
        local_id = int(args.get("local_id"))
    except (TypeError, ValueError) as exc:
        raise ToolError("local_id is required and must be an integer") from exc
    with db_connect() as conn:
        result = message_metadata(conn, local_id)
    files = locate_attachment_files(local_id, result.get("mailbox_url"))
    return {
        "local_id": local_id,
        "subject": result.get("subject"),
        "attachment_index": result.get("attachments", []),
        "attachment_files": files,
        "note": "attachment_index comes from Mail's database; attachment_files are files currently downloaded on disk.",
    }


def validate_outbound_attachments(attachments: list[Path]) -> None:
    if len(attachments) > MAX_DRAFT_ATTACHMENTS:
        raise ToolError(f"Too many attachments. Maximum allowed: {MAX_DRAFT_ATTACHMENTS}.")
    missing = [str(path) for path in attachments if not path.exists()]
    if missing:
        raise ToolError(f"Attachment path(s) do not exist: {missing}")
    invalid: list[str] = []
    oversized: list[str] = []
    total_size = 0
    for path in attachments:
        try:
            resolved = path.resolve(strict=True)
            stat = resolved.stat()
        except OSError:
            invalid.append(str(path))
            continue
        if path.is_symlink() or not resolved.is_file():
            invalid.append(str(path))
            continue
        if resolved.suffix.lower() in DANGEROUS_ATTACHMENT_SUFFIXES:
            invalid.append(resolved.name)
            continue
        total_size += stat.st_size
        if stat.st_size > MAX_DRAFT_ATTACHMENT_BYTES:
            oversized.append(resolved.name)
    if invalid:
        raise ToolError(f"Refusing unsafe attachment path(s): {invalid}")
    if oversized:
        raise ToolError(f"Attachment file(s) exceed {MAX_DRAFT_ATTACHMENT_BYTES} bytes: {oversized}")
    if total_size > MAX_DRAFT_ATTACHMENT_BYTES:
        raise ToolError(f"Total attachment size exceeds {MAX_DRAFT_ATTACHMENT_BYTES} bytes.")


def export_attachments(args: dict[str, Any]) -> dict[str, Any]:
    try:
        local_id = int(args.get("local_id"))
    except (TypeError, ValueError) as exc:
        raise ToolError("local_id is required and must be an integer") from exc
    destination = Path(str(args.get("destination_dir") or SAFE_EXPORT_ROOT)).expanduser()
    destination.mkdir(parents=True, exist_ok=True)
    names = [str(item) for item in args.get("names", []) if str(item).strip()]
    export_all = bool(args.get("export_all", False))
    if not names and not export_all:
        raise ToolError("Provide exact attachment names, or set export_all=true after reviewing mail_list_attachments.")
    with db_connect() as conn:
        result = message_metadata(conn, local_id)
    files = locate_attachment_files(local_id, result.get("mailbox_url"), names or None)
    if names and not files:
        raise ToolError(f"No downloaded attachments matched requested names: {names}")
    if names:
        found_names = {item["name"] for item in files}
        missing_names = sorted(set(names) - found_names)
        if missing_names:
            raise ToolError(f"Requested attachment names were not downloaded or did not match exactly: {missing_names}")
    exported: list[dict[str, Any]] = []
    for item in files:
        source = Path(item["path"])
        if source.suffix.lower() in DANGEROUS_ATTACHMENT_SUFFIXES:
            raise ToolError(f"Refusing to export potentially executable attachment by default: {source.name}")
        target = destination / source.name
        if target.exists():
            stem = target.stem
            suffix = target.suffix
            counter = 2
            while target.exists():
                target = destination / f"{stem}-{counter}{suffix}"
                counter += 1
        shutil.copy2(source, target)
        exported.append({"name": source.name, "source_path": str(source), "exported_path": str(target)})
    log_operation(
        "export_attachments",
        {"local_id": local_id, "destination_dir": str(destination), "export_count": len(exported)},
    )
    return {"local_id": local_id, "exported": exported, "count": len(exported)}


def private_app_support_path(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)


def body_index_connect() -> sqlite3.Connection:
    private_app_support_path(BODY_SEARCH_DB)
    is_new = not BODY_SEARCH_DB.exists()
    conn = sqlite3.connect(BODY_SEARCH_DB, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    if is_new:
        try:
            os.chmod(BODY_SEARCH_DB, 0o600)
        except OSError:
            pass
    ensure_body_index_schema(conn)
    return conn


def ensure_body_index_schema(conn: sqlite3.Connection) -> bool:
    fts_existed = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='body_documents_fts'"
    ).fetchone() is not None
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS body_documents (
          local_id INTEGER PRIMARY KEY,
          rfc_message_id TEXT,
          conversation_id INTEGER,
          date_received TEXT,
          date_sent TEXT,
          sender_address TEXT,
          sender_name TEXT,
          subject TEXT,
          mailbox_id INTEGER,
          mailbox_url TEXT,
          account_uuid TEXT,
          mailbox_name TEXT,
          mailbox_role TEXT,
          body TEXT NOT NULL,
          body_char_count INTEGER NOT NULL,
          body_truncated INTEGER NOT NULL DEFAULT 0,
          indexed_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_body_documents_date
          ON body_documents(date_received DESC);
        CREATE INDEX IF NOT EXISTS idx_body_documents_mailbox
          ON body_documents(account_uuid, mailbox_id, mailbox_role);
        """
    )
    try:
        conn.executescript(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS body_documents_fts USING fts5(
              subject,
              sender_address,
              sender_name,
              body,
              content='body_documents',
              content_rowid='local_id',
              tokenize='porter unicode61'
            );
            CREATE TRIGGER IF NOT EXISTS body_documents_ai AFTER INSERT ON body_documents BEGIN
              INSERT INTO body_documents_fts(rowid, subject, sender_address, sender_name, body)
              VALUES (new.local_id, new.subject, new.sender_address, new.sender_name, new.body);
            END;
            CREATE TRIGGER IF NOT EXISTS body_documents_ad AFTER DELETE ON body_documents BEGIN
              INSERT INTO body_documents_fts(body_documents_fts, rowid, subject, sender_address, sender_name, body)
              VALUES('delete', old.local_id, old.subject, old.sender_address, old.sender_name, old.body);
            END;
            CREATE TRIGGER IF NOT EXISTS body_documents_au AFTER UPDATE ON body_documents BEGIN
              INSERT INTO body_documents_fts(body_documents_fts, rowid, subject, sender_address, sender_name, body)
              VALUES('delete', old.local_id, old.subject, old.sender_address, old.sender_name, old.body);
              INSERT INTO body_documents_fts(rowid, subject, sender_address, sender_name, body)
              VALUES (new.local_id, new.subject, new.sender_address, new.sender_name, new.body);
            END;
            """
        )
        if not fts_existed:
            conn.execute("INSERT INTO body_documents_fts(body_documents_fts) VALUES('rebuild')")
        return True
    except sqlite3.OperationalError:
        return False


def body_index_has_fts(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='body_documents_fts'"
    ).fetchone()
    return row is not None


def body_index_existing_ids(conn: sqlite3.Connection, local_ids: list[int]) -> set[int]:
    if not local_ids:
        return set()
    existing: set[int] = set()
    for start in range(0, len(local_ids), 400):
        chunk = local_ids[start:start + 400]
        existing.update(
            int(row["local_id"])
            for row in conn.execute(
                f"SELECT local_id FROM body_documents WHERE local_id IN ({placeholders(chunk)})",
                chunk,
            )
        )
    return existing


def fts_quote_token(token: str) -> str:
    if token.upper() in {"AND", "OR", "NOT"}:
        return token.upper()
    if token.startswith('"') and token.endswith('"') and len(token) >= 2:
        inner = token[1:-1].replace('"', '""')
        return f'"{inner}"'
    wildcard = token.endswith("*") and len(token) > 1
    core = token[:-1] if wildcard else token
    if not core:
        return ""
    if re.search(r"[^A-Za-z0-9_]", core):
        safe = '"' + core.replace('"', '""') + '"'
        return safe + ("*" if wildcard else "")
    return core + ("*" if wildcard else "")


def sanitize_fts_query(query: str) -> str:
    tokens = re.findall(r'"[^"]+"|\S+', query.strip())
    safe = [fts_quote_token(token) for token in tokens]
    return " ".join(token for token in safe if token)


def date_filter_text(value: Any, *, end_of_day: bool = False) -> str | None:
    epoch = parse_epoch(value, end_of_day=end_of_day)
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S")


def sql_filter_for_body_documents(args: dict[str, Any], params: list[Any], alias: str = "d") -> str:
    where: list[str] = []
    account_uuid = str(args.get("account_uuid") or args.get("account") or "").strip()
    mailbox_role = str(args.get("mailbox_role") or "").strip().lower()
    mailbox_id = args.get("mailbox_id")
    mailbox_ids = parse_int_list_arg(args.get("mailbox_ids"), "mailbox_ids")
    if mailbox_id not in (None, ""):
        mailbox_ids.append(parse_int_arg(mailbox_id, "mailbox_id"))
    if account_uuid:
        where.append(f"{alias}.account_uuid = ?")
        params.append(account_uuid)
    if mailbox_role:
        where.append(f"{alias}.mailbox_role = ?")
        params.append(mailbox_role)
    if mailbox_ids:
        where.append(f"{alias}.mailbox_id IN ({placeholders(mailbox_ids)})")
        params.extend(mailbox_ids)
    date_from = date_filter_text(args.get("date_from"))
    date_to = date_filter_text(args.get("date_to"), end_of_day=True)
    if date_from:
        where.append(f"{alias}.date_received >= ?")
        params.append(date_from)
    if date_to:
        where.append(f"{alias}.date_received <= ?")
        params.append(date_to)
    return (" AND " + " AND ".join(where)) if where else ""


def body_index_status(args: dict[str, Any]) -> dict[str, Any]:
    exists = BODY_SEARCH_DB.exists()
    payload: dict[str, Any] = {
        "path": str(BODY_SEARCH_DB),
        "exists": exists,
        "stores_message_bodies_locally": True,
        "privacy": "The body index is a private local cache under Application Support with chmod 0600 on the SQLite file.",
    }
    if not exists:
        payload.update({"document_count": 0, "fts_enabled": None})
        return payload
    with body_index_connect() as conn:
        payload["fts_enabled"] = body_index_has_fts(conn)
        payload["document_count"] = conn.execute("SELECT count(*) FROM body_documents").fetchone()[0]
        payload["latest_indexed_at"] = conn.execute("SELECT max(indexed_at) FROM body_documents").fetchone()[0]
        payload["latest_message_at"] = conn.execute("SELECT max(date_received) FROM body_documents").fetchone()[0]
        payload["by_role"] = [
            dict(row)
            for row in conn.execute(
                """
                SELECT mailbox_role, count(*) AS count, max(date_received) AS latest_message_at
                FROM body_documents
                GROUP BY mailbox_role
                ORDER BY count DESC
                """
            )
        ]
    return payload


def purge_body_index(args: dict[str, Any]) -> dict[str, Any]:
    confirm = bool(args.get("confirm_purge", False))
    if not confirm:
        raise ToolError("confirm_purge=true is required to delete the private body-search cache.")
    removed: list[str] = []
    for path in [
        BODY_SEARCH_DB,
        Path(str(BODY_SEARCH_DB) + "-wal"),
        Path(str(BODY_SEARCH_DB) + "-shm"),
    ]:
        if path.exists():
            path.unlink()
            removed.append(str(path))
    log_operation("purge_body_index", {"removed_count": len(removed)})
    return {
        "purged": True,
        "removed_count": len(removed),
        "removed_paths": removed,
        "note": "Only the plugin-owned body-search cache was deleted. Apple Mail data was not modified.",
    }


def plugin_root() -> Path:
    return Path(__file__).resolve().parents[1]


def git_json_error(message: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "update_supported": False, "error": message, **extra}


def run_git(args: list[str], *, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(plugin_root()), *args],
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def plugin_update_status(args: dict[str, Any]) -> dict[str, Any]:
    check_remote = bool(args.get("check_remote", True))
    root = plugin_root()
    if not shutil.which("git"):
        return git_json_error("git is not installed or not on PATH.", plugin_root=str(root))
    inside = run_git(["rev-parse", "--is-inside-work-tree"])
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return git_json_error(
            "This plugin install is not a git checkout, so automatic update pull is unavailable. Reinstall with scripts/bootstrap_install.py from the GitHub repo.",
            plugin_root=str(root),
        )
    local_commit = run_git(["rev-parse", "HEAD"]).stdout.strip()
    branch = run_git(["branch", "--show-current"]).stdout.strip() or UPDATE_BRANCH
    remote_url = run_git(["config", "--get", "remote.origin.url"]).stdout.strip()
    upstream = run_git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
    upstream_name = upstream.stdout.strip() if upstream.returncode == 0 else f"origin/{branch}"
    fetch_error = None
    if check_remote:
        fetch = run_git(["fetch", "--quiet", "origin", branch], timeout=60)
        if fetch.returncode != 0:
            fetch_error = fetch.stderr.strip() or fetch.stdout.strip() or f"git fetch exited {fetch.returncode}"
    remote_commit = ""
    remote = run_git(["rev-parse", upstream_name])
    if remote.returncode == 0:
        remote_commit = remote.stdout.strip()
    dirty = run_git(["status", "--short", "--untracked-files=no"])
    update_available = bool(remote_commit and remote_commit != local_commit)
    return {
        "ok": fetch_error is None,
        "update_supported": True,
        "plugin_root": str(root),
        "repository": remote_url or UPDATE_REPO_URL,
        "branch": branch,
        "upstream": upstream_name,
        "current_version": SERVER_VERSION,
        "local_commit": local_commit,
        "remote_commit": remote_commit or None,
        "update_available": update_available,
        "dirty_tracked_files": bool(dirty.stdout.strip()) if dirty.returncode == 0 else None,
        "fetch_error": fetch_error,
        "note": "Updates pull the git checkout in place. Restart Codex or the MCP server after updating so new code is loaded.",
    }


def plugin_update_install(args: dict[str, Any]) -> dict[str, Any]:
    confirm_update = bool(args.get("confirm_update", False))
    background = bool(args.get("background", True))
    if not confirm_update:
        raise ToolError("confirm_update=true is required to pull and install updates.")
    status = plugin_update_status({"check_remote": True})
    if not status.get("update_supported"):
        raise ToolError(status.get("error", "Automatic update is not supported for this install."))
    if status.get("fetch_error"):
        raise ToolError(f"Could not fetch updates: {status['fetch_error']}")
    script = plugin_root() / "scripts" / "update_plugin.py"
    if not script.exists():
        raise ToolError(f"Update helper is missing: {script}")
    if background:
        APP_SUPPORT.mkdir(parents=True, exist_ok=True)
        os.chmod(APP_SUPPORT, 0o700)
        log_path = APP_SUPPORT / "update.log"
        handle = log_path.open("a", encoding="utf-8")
        process = subprocess.Popen(
            [sys.executable, str(script), "--install", "--json"],
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        handle.close()
        return {
            "started": True,
            "background": True,
            "pid": process.pid,
            "log_path": str(log_path),
            "pre_update_status": status,
            "note": "Update pull started in the background. Restart Codex or the MCP server after it completes.",
        }
    result = subprocess.run(
        [sys.executable, str(script), "--install", "--json"],
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )
    if result.returncode != 0:
        raise ToolError(result.stderr.strip() or result.stdout.strip() or f"Update exited {result.returncode}")
    try:
        update_payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        update_payload = {"raw": result.stdout.strip()}
    return {
        "started": False,
        "background": False,
        "pre_update_status": status,
        "update_result": update_payload,
        "note": "Restart Codex or the MCP server so new code is loaded.",
    }


def body_index_candidate_ids(args: dict[str, Any]) -> tuple[list[int], list[str]]:
    limit = clamp_limit(args.get("max_messages"), DEFAULT_BODY_INDEX_LIMIT, MAX_BODY_INDEX_LIMIT)
    include_deleted = bool(args.get("include_deleted", False))
    include_junk = bool(args.get("include_junk", False) or args.get("include_spam", False))
    include_trash = bool(args.get("include_trash", False))
    include_drafts = bool(args.get("include_drafts", False))
    include_sent = bool(args.get("include_sent", False))
    mailbox_id = args.get("mailbox_id")
    mailbox_ids_arg = args.get("mailbox_ids")
    mailbox_filter = str(args.get("mailbox") or "").strip()
    mailbox_name = str(args.get("mailbox_name") or args.get("mailboxName") or "").strip()
    mailbox_path = str(args.get("mailbox_path") or args.get("mailboxPath") or "").strip()
    mailbox_role = str(args.get("mailbox_role") or "").strip().lower()
    account_uuid = str(args.get("account_uuid") or args.get("account") or "").strip()
    query = str(args.get("query") or "").strip()
    sender = str(args.get("sender") or "").strip()
    subject = str(args.get("subject") or "").strip()
    date_from = parse_epoch(args.get("date_from"))
    date_to = parse_epoch(args.get("date_to"), end_of_day=True)

    notes: list[str] = []
    with db_connect() as conn:
        requested_mailbox_ids = filtered_mailbox_ids(
            conn,
            mailbox_id=mailbox_id,
            mailbox_ids=mailbox_ids_arg,
            account_uuid=account_uuid,
            mailbox_role=mailbox_role,
            mailbox_filter=mailbox_filter,
            mailbox_name=mailbox_name,
            mailbox_path=mailbox_path,
            include_spam=include_junk,
        )
        rows_by_id = {int(row["mailbox_id"]): row for row in mailbox_rows(conn)}
        candidate_mailbox_ids = requested_mailbox_ids
        if candidate_mailbox_ids is None:
            candidate_mailbox_ids = list(rows_by_id)
        excluded_roles = set()
        if not include_junk:
            excluded_roles.add("junk")
        if not include_trash:
            excluded_roles.add("trash")
        if not include_drafts:
            excluded_roles.add("drafts")
        if not include_sent:
            excluded_roles.add("sent")
        if excluded_roles:
            before = len(candidate_mailbox_ids)
            candidate_mailbox_ids = [
                mailbox
                for mailbox in candidate_mailbox_ids
                if rows_by_id.get(int(mailbox), {}).get("role") not in excluded_roles
            ]
            if before != len(candidate_mailbox_ids):
                notes.append("Skipped Sent/Drafts/Junk/Trash roles unless their include_* flags were enabled.")
        if not candidate_mailbox_ids:
            return [], notes + ["No local Apple Mail mailboxes matched the body-index filters."]

        where = [f"m.mailbox IN ({placeholders(candidate_mailbox_ids)})"]
        params: list[Any] = list(candidate_mailbox_ids)
        if not include_deleted:
            where.append("m.deleted = 0")
        if sender:
            where.append("(a.address LIKE ? OR a.comment LIKE ?)")
            params.extend([f"%{sender}%", f"%{sender}%"])
        if subject:
            where.append("s.subject LIKE ?")
            params.append(f"%{subject}%")
        if query:
            like = f"%{query}%"
            where.append("(s.subject LIKE ? OR a.address LIKE ? OR a.comment LIKE ? OR sm.summary LIKE ?)")
            params.extend([like, like, like, like])
        if date_from is not None:
            where.append("m.date_received >= ?")
            params.append(date_from)
        if date_to is not None:
            where.append("m.date_received <= ?")
            params.append(date_to)

        params.append(limit)
        ids = [
            int(row["local_id"])
            for row in conn.execute(
                f"""
                SELECT m.ROWID AS local_id
                FROM messages m
                LEFT JOIN addresses a ON a.ROWID = m.sender
                LEFT JOIN subjects s ON s.ROWID = m.subject
                LEFT JOIN summaries sm ON sm.ROWID = m.summary
                WHERE {" AND ".join(where)}
                ORDER BY m.date_received DESC, m.ROWID DESC
                LIMIT ?
                """,
                params,
            )
        ]
    return ids, notes


def index_body_messages(args: dict[str, Any]) -> dict[str, Any]:
    start = datetime.now()
    refresh = bool(args.get("refresh", False))
    reset = bool(args.get("reset", False))
    max_body_chars = clamp_limit(args.get("max_body_chars"), 100_000, 500_000)
    candidate_ids, notes = body_index_candidate_ids(args)
    indexed = 0
    skipped_existing = 0
    skipped_missing_body = 0
    errors: list[dict[str, Any]] = []

    with body_index_connect() as body_conn:
        fts_enabled = body_index_has_fts(body_conn)
        if reset:
            body_conn.execute("DELETE FROM body_documents")
        existing_ids = set() if refresh or reset else body_index_existing_ids(body_conn, candidate_ids)
        with db_connect() as mail_conn:
            candidates_by_id: dict[int, dict[str, Any]] = {}
            for start_idx in range(0, len(candidate_ids), 400):
                for message in messages_metadata(mail_conn, candidate_ids[start_idx:start_idx + 400]):
                    candidates_by_id[int(message["local_id"])] = message
        for local_id in candidate_ids:
            if local_id in existing_ids:
                skipped_existing += 1
                continue
            try:
                message = candidates_by_id.get(local_id)
                if not message:
                    skipped_missing_body += 1
                    continue
                load_message_body(message, "plain", max_body_chars)
                body = str(message.get("body") or "")
                if not body.strip():
                    skipped_missing_body += 1
                    continue
                indexed_at = datetime.now(timezone.utc).isoformat()
                body_conn.execute(
                    """
                    INSERT INTO body_documents (
                      local_id, rfc_message_id, conversation_id, date_received, date_sent,
                      sender_address, sender_name, subject, mailbox_id, mailbox_url,
                      account_uuid, mailbox_name, mailbox_role, body, body_char_count,
                      body_truncated, indexed_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(local_id) DO UPDATE SET
                      rfc_message_id=excluded.rfc_message_id,
                      conversation_id=excluded.conversation_id,
                      date_received=excluded.date_received,
                      date_sent=excluded.date_sent,
                      sender_address=excluded.sender_address,
                      sender_name=excluded.sender_name,
                      subject=excluded.subject,
                      mailbox_id=excluded.mailbox_id,
                      mailbox_url=excluded.mailbox_url,
                      account_uuid=excluded.account_uuid,
                      mailbox_name=excluded.mailbox_name,
                      mailbox_role=excluded.mailbox_role,
                      body=excluded.body,
                      body_char_count=excluded.body_char_count,
                      body_truncated=excluded.body_truncated,
                      indexed_at=excluded.indexed_at
                    """,
                    (
                        local_id,
                        message.get("rfc_message_id"),
                        message.get("conversation_id"),
                        message.get("date_received"),
                        message.get("date_sent"),
                        message.get("sender_address"),
                        message.get("sender_name"),
                        message.get("subject"),
                        message.get("mailbox_id"),
                        message.get("mailbox_url"),
                        message.get("account_uuid"),
                        message.get("mailbox_name"),
                        message.get("mailbox_role"),
                        body,
                        message.get("body_char_count") or len(body),
                        1 if message.get("body_truncated") else 0,
                        indexed_at,
                    ),
                )
                indexed += 1
            except Exception as exc:
                if len(errors) < 10:
                    errors.append({"local_id": local_id, "error": str(exc)})
        if fts_enabled:
            try:
                body_conn.execute("INSERT INTO body_documents_fts(body_documents_fts) VALUES('optimize')")
            except sqlite3.OperationalError:
                pass
        body_conn.commit()

    elapsed_ms = round((datetime.now() - start).total_seconds() * 1000, 2)
    log_operation(
        "rebuild_body_index",
        {
            "candidate_count": len(candidate_ids),
            "indexed": indexed,
            "skipped_existing": skipped_existing,
            "skipped_missing_body": skipped_missing_body,
            "error_count": len(errors),
            "elapsed_ms": elapsed_ms,
        },
    )
    return {
        "candidate_count": len(candidate_ids),
        "indexed": indexed,
        "skipped_existing": skipped_existing,
        "skipped_missing_body": skipped_missing_body,
        "errors": errors,
        "elapsed_ms": elapsed_ms,
        "fts_enabled": body_index_status({}).get("fts_enabled"),
        "path": str(BODY_SEARCH_DB),
            "notes": notes
        + [
            "Body indexing reads downloaded .emlx files and writes only to the plugin-owned local cache.",
            "Sent, Drafts, Junk, and Trash are excluded unless include_sent/include_drafts/include_junk/include_trash is enabled.",
        ],
    }


def snippet_around(text: str, needle: str, max_chars: int = 280) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    if not compact:
        return ""
    idx = compact.lower().find(needle.lower())
    if idx < 0:
        return truncate_text(compact, max_chars) or ""
    start = max(0, idx - max_chars // 3)
    end = min(len(compact), start + max_chars)
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(compact) else ""
    return prefix + compact[start:end].strip() + suffix


def search_body_index(args: dict[str, Any]) -> dict[str, Any]:
    query = str(args.get("query") or "").strip()
    if not query:
        raise ToolError("query is required.")
    limit = clamp_limit(args.get("limit"), 20, 100)
    index_if_needed = bool(args.get("index_if_needed", False))
    if index_if_needed:
        index_body_messages({**args, "refresh": False})
    if not BODY_SEARCH_DB.exists():
        return {
            "messages": [],
            "count": 0,
            "query": query,
            "notes": ["Body index does not exist yet. Run mail_rebuild_body_index first."],
        }

    with body_index_connect() as conn:
        fts_enabled = body_index_has_fts(conn)
        params: list[Any] = []
        filter_sql = sql_filter_for_body_documents(args, params, alias="d")
        if fts_enabled:
            safe_query = sanitize_fts_query(query)
            if not safe_query:
                raise ToolError("query did not contain searchable terms.")
            sql = f"""
                SELECT
                  d.local_id, d.rfc_message_id, d.conversation_id, d.date_received,
                  d.date_sent, d.sender_address, d.sender_name, d.subject,
                  d.mailbox_id, d.mailbox_url, d.account_uuid, d.mailbox_name,
                  d.mailbox_role, d.body_char_count, d.body_truncated,
                  d.indexed_at,
                  snippet(body_documents_fts, 3, '[', ']', ' ... ', 18) AS body_snippet,
                  -bm25(body_documents_fts, 1.0, 0.7, 0.7, 2.0) AS score
                FROM body_documents_fts
                JOIN body_documents d ON d.local_id = body_documents_fts.rowid
                WHERE body_documents_fts MATCH ?{filter_sql}
                ORDER BY score DESC, d.date_received DESC
                LIMIT ?
            """
            rows = [dict(row) for row in conn.execute(sql, [safe_query, *params, limit])]
        else:
            like = f"%{query}%"
            sql = f"""
                SELECT
                  d.local_id, d.rfc_message_id, d.conversation_id, d.date_received,
                  d.date_sent, d.sender_address, d.sender_name, d.subject,
                  d.mailbox_id, d.mailbox_url, d.account_uuid, d.mailbox_name,
                  d.mailbox_role, d.body_char_count, d.body_truncated,
                  d.indexed_at, d.body
                FROM body_documents d
                WHERE (d.body LIKE ? OR d.subject LIKE ? OR d.sender_address LIKE ? OR d.sender_name LIKE ?){filter_sql}
                ORDER BY d.date_received DESC
                LIMIT ?
            """
            rows = [dict(row) for row in conn.execute(sql, [like, like, like, like, *params, limit])]
            for row in rows:
                body = row.pop("body", "")
                row["body_snippet"] = snippet_around(body, query)
                row["score"] = None
    for row in rows:
        row["next_read"] = {
            "tool": "mail_read_message",
            "arguments": {"local_id": row["local_id"], "include_body": True, "max_body_chars": DEFAULT_BODY_CHARS},
        }
        row["next_thread"] = {
            "tool": "mail_read_thread",
            "arguments": {"conversation_id": row["conversation_id"], "include_bodies": False, "limit": 20},
        }
    return {
        "messages": rows,
        "count": len(rows),
        "limit": limit,
        "query": query,
        "fts_enabled": fts_enabled,
        "notes": [
            "Body search uses the plugin-owned local cache, not Mail's Envelope Index.",
            "Results include capped snippets only. Use next_read for the full bounded local message read.",
            "Run mail_rebuild_body_index with mailbox_id/account_uuid/date filters to improve coverage before broad body searches.",
        ],
    }


def address_list(args: dict[str, Any], key: str) -> list[str]:
    value = args.get(key, [])
    if isinstance(value, str):
        value = [item.strip() for item in value.split(",") if item.strip()]
    return [str(item).strip() for item in value if str(item).strip()]


def safe_filename(value: str, default: str = "draft") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._ -]+", " ", value).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)[:80].strip(" .")
    return cleaned or default


def prepare_eml_draft(args: dict[str, Any]) -> dict[str, Any]:
    to = address_list(args, "to")
    cc = address_list(args, "cc")
    bcc = address_list(args, "bcc")
    subject = str(args.get("subject") or "").strip()
    text_body = str(args.get("text_body") or args.get("body") or "")
    html_body = str(args.get("html_body") or "")
    sender = str(args.get("sender") or "").strip()
    reply_to = str(args.get("reply_to") or "").strip()
    allow_incomplete = bool(args.get("allow_incomplete", False))
    open_in_mail = bool(args.get("open_in_mail", False))
    output_dir = Path(str(args.get("output_dir") or EML_DRAFT_ROOT)).expanduser()
    attachments = [Path(str(item)).expanduser() for item in args.get("attachments", [])]

    if not allow_incomplete and not (to or cc or bcc):
        raise ToolError("At least one recipient is required unless allow_incomplete=true.")
    if not allow_incomplete and not subject:
        raise ToolError("subject is required unless allow_incomplete=true.")
    if not allow_incomplete and not (text_body.strip() or html_body.strip()):
        raise ToolError("text_body/body or html_body is required unless allow_incomplete=true.")
    validate_outbound_attachments(attachments)

    message = EmailMessage()
    message["X-Unsent"] = "1"
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain="codex.local")
    if sender:
        message["From"] = sender
    if to:
        message["To"] = ", ".join(to)
    if cc:
        message["Cc"] = ", ".join(cc)
    if bcc:
        message["Bcc"] = ", ".join(bcc)
    if subject:
        message["Subject"] = subject
    if reply_to:
        message["Reply-To"] = reply_to

    if html_body.strip():
        fallback = text_body.strip() or strip_html(html_body)
        message.set_content(fallback)
        message.add_alternative(html_body, subtype="html")
    else:
        message.set_content(text_body)

    for attachment in attachments:
        data = attachment.read_bytes()
        message.add_attachment(
            data,
            maintype="application",
            subtype="octet-stream",
            filename=attachment.name,
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(output_dir, 0o700)
    filename = safe_filename(subject or "unsent-draft") + "-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".eml"
    path = output_dir / filename
    path.write_bytes(message.as_bytes(policy=policy.SMTP))
    os.chmod(path, 0o600)
    opened = False
    if open_in_mail:
        result = subprocess.run(["open", "-a", "Mail", str(path)], capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise ToolError(result.stderr.strip() or result.stdout.strip() or "Failed to open draft in Mail.")
        opened = True
    log_operation(
        "prepare_eml_draft",
        {
            "recipient_count": len(to),
            "cc_count": len(cc),
            "bcc_count": len(bcc),
            "subject": subject,
            "text_body": text_body,
            "html_body": html_body,
            "attachment_count": len(attachments),
            "open_in_mail": opened,
            "path": str(path),
        },
    )
    return {
        "draft_file": str(path),
        "open_in_mail": opened,
        "to": to,
        "cc": cc,
        "bcc_count": len(bcc),
        "subject": subject,
        "attachment_names": [path.name for path in attachments],
        "warning": "This creates a local X-Unsent .eml draft file. It is not sent. If opened in Mail, review the compose window before any send.",
    }


def create_draft(args: dict[str, Any]) -> dict[str, Any]:
    to = [str(item).strip() for item in args.get("to", []) if str(item).strip()]
    cc = [str(item).strip() for item in args.get("cc", []) if str(item).strip()]
    bcc = [str(item).strip() for item in args.get("bcc", []) if str(item).strip()]
    subject = str(args.get("subject") or "").strip()
    body = str(args.get("body") or "")
    sender = str(args.get("sender") or "").strip()
    requested_visible = bool(args.get("visible", True))
    visible = True
    attachments = [Path(str(item)).expanduser() for item in args.get("attachments", [])]

    if not to and not cc and not bcc:
        raise ToolError("At least one recipient is required.")
    if not subject:
        raise ToolError("subject is required.")
    validate_outbound_attachments(attachments)

    lines = [
        'tell application "Mail"',
        "set newMessage to make new outgoing message with properties "
        + "{subject:" + applescript_quote(subject)
        + ", content:" + applescript_quote(body)
        + ", visible:" + ("true" if visible else "false")
        + "}",
        "tell newMessage",
    ]
    if sender:
        lines.append("set sender to " + applescript_quote(sender))
    for recipient in to:
        lines.append("make new to recipient at end of to recipients with properties {address:" + applescript_quote(recipient) + "}")
    for recipient in cc:
        lines.append("make new cc recipient at end of cc recipients with properties {address:" + applescript_quote(recipient) + "}")
    for recipient in bcc:
        lines.append("make new bcc recipient at end of bcc recipients with properties {address:" + applescript_quote(recipient) + "}")
    for attachment in attachments:
        lines.append(
            "make new attachment with properties {file name:(POSIX file "
            + applescript_quote(str(attachment.resolve()))
            + ")} at after the last paragraph"
        )
    lines.extend([
        "save",
        "set draftId to id",
        "end tell",
        "return draftId",
        "end tell",
    ])
    draft_id = run_osascript("\n".join(lines), timeout=30)
    log_operation(
        "create_draft",
        {
            "draft_id": draft_id,
            "recipient_count": len(to),
            "cc_count": len(cc),
            "bcc_count": len(bcc),
            "subject": subject,
            "attachment_count": len(attachments),
            "visible": visible,
            "requested_visible": requested_visible,
        },
    )
    return {
        "draft_id": draft_id,
        "subject": subject,
        "to": to,
        "cc": cc,
        "bcc_count": len(bcc),
        "attachment_names": [path.name for path in attachments],
        "visible": visible,
        "warning": "Draft created visibly in Apple Mail. Drafts may sync to the provider Drafts mailbox; do not send without explicit approval.",
    }


def prefixed_subject(subject: str | None, prefix: str) -> str:
    subject = (subject or "").strip()
    if not subject:
        return prefix.rstrip(":")
    if subject.lower().startswith(prefix.lower()):
        return subject
    return f"{prefix} {subject}"


def address_label(message: dict[str, Any]) -> str:
    sender = message.get("sender_name") or message.get("sender_address") or "sender"
    date = message.get("date_sent") or message.get("date_received") or "an earlier date"
    return f"On {date}, {sender} wrote:"


def create_reply_draft(args: dict[str, Any]) -> dict[str, Any]:
    try:
        local_id = int(args.get("local_id"))
    except (TypeError, ValueError) as exc:
        raise ToolError("local_id is required and must be an integer") from exc
    body = str(args.get("body") or "")
    if not body.strip():
        raise ToolError("body is required.")
    reply_all = bool(args.get("reply_all", False))
    include_original = bool(args.get("include_original", False))
    sender_override = str(args.get("sender") or "").strip()
    message = message_details(
        local_id,
        include_body=include_original,
        body_format="plain",
        include_attachment_paths=False,
        max_body_chars=args.get("max_original_chars", 6000),
    )
    to = [message["sender_address"]] if message.get("sender_address") else []
    cc: list[str] = []
    if reply_all:
        my_addresses = local_email_addresses()
        for recipient in message.get("recipients", []):
            address = (recipient.get("address") or "").strip()
            if not address or address.lower() in my_addresses or address in to:
                continue
            if recipient.get("role") == "cc":
                cc.append(address)
            elif recipient.get("role") == "to":
                to.append(address)
    draft_body = body
    if include_original and message.get("body"):
        quoted = "\n".join("> " + line for line in str(message["body"]).splitlines())
        draft_body = f"{body.rstrip()}\n\n{address_label(message)}\n{quoted}"
    result = create_draft(
        {
            "to": to,
            "cc": cc,
            "subject": prefixed_subject(message.get("subject"), "Re:"),
            "body": draft_body,
            "sender": sender_override,
            "attachments": args.get("attachments", []),
            "visible": args.get("visible", True),
        }
    )
    result["source_local_id"] = local_id
    result["threading_warning"] = "Reply draft is addressed from the local message but may not preserve provider-native thread headers."
    return result


def create_forward_draft(args: dict[str, Any]) -> dict[str, Any]:
    try:
        local_id = int(args.get("local_id"))
    except (TypeError, ValueError) as exc:
        raise ToolError("local_id is required and must be an integer") from exc
    to = [str(item).strip() for item in args.get("to", []) if str(item).strip()]
    if not to:
        raise ToolError("At least one forward recipient is required.")
    note = str(args.get("note") or "")
    include_attachments = bool(args.get("include_attachments", False))
    sender_override = str(args.get("sender") or "").strip()
    message = message_details(
        local_id,
        include_body=True,
        body_format="plain",
        include_attachment_paths=include_attachments,
        max_body_chars=args.get("max_original_chars", 10000),
    )
    forwarded = [
        note.rstrip(),
        "",
        "---------- Forwarded message ----------",
        f"From: {message.get('sender_name') or ''} <{message.get('sender_address') or ''}>",
        f"Date: {message.get('date_sent') or message.get('date_received') or ''}",
        f"Subject: {message.get('subject') or ''}",
        "",
        str(message.get("body") or ""),
    ]
    attachments = args.get("attachments", [])
    if include_attachments:
        attachments = attachments + [item["path"] for item in message.get("attachment_files", [])]
    result = create_draft(
        {
            "to": to,
            "cc": args.get("cc", []),
            "bcc": args.get("bcc", []),
            "subject": prefixed_subject(message.get("subject"), "Fwd:"),
            "body": "\n".join(forwarded).strip(),
            "sender": sender_override,
            "attachments": attachments,
            "visible": args.get("visible", True),
        }
    )
    result["source_local_id"] = local_id
    result["included_original_attachments"] = include_attachments
    return result


def inspect_outgoing_draft_payload(draft_id: str) -> dict[str, Any]:
    sep = "\x1e"
    item_sep = "\x1f"
    script = f"""
tell application "Mail"
  set targetMessage to first outgoing message whose id is {draft_id}
  set fieldSep to ASCII character 30
  set itemSep to ASCII character 31
  set oldDelims to AppleScript's text item delimiters
  set toRows to {{}}
  set ccRows to {{}}
  set bccRows to {{}}
  set attachmentRows to {{}}
  repeat with r in to recipients of targetMessage
    try
      set end of toRows to address of r as text
    end try
  end repeat
  repeat with r in cc recipients of targetMessage
    try
      set end of ccRows to address of r as text
    end try
  end repeat
  repeat with r in bcc recipients of targetMessage
    try
      set end of bccRows to address of r as text
    end try
  end repeat
  repeat with att in mail attachments of targetMessage
    try
      set end of attachmentRows to name of att as text
    end try
  end repeat
  set AppleScript's text item delimiters to itemSep
  set toText to toRows as text
  set ccText to ccRows as text
  set bccText to bccRows as text
  set attachmentText to attachmentRows as text
  set AppleScript's text item delimiters to oldDelims
  set senderText to ""
  try
    set senderText to sender of targetMessage as text
  end try
  return (id of targetMessage as text) & fieldSep & senderText & fieldSep & (subject of targetMessage as text) & fieldSep & (content of targetMessage as text) & fieldSep & toText & fieldSep & ccText & fieldSep & bccText & fieldSep & attachmentText
end tell
"""
    raw = run_osascript(script, timeout=20)
    parts = raw.split(sep)
    if len(parts) < 8:
        raise ToolError("Could not inspect outgoing draft metadata.")
    body = parts[3]
    payload = {
        "draft_id": parts[0],
        "sender": parts[1],
        "subject": parts[2],
        "body_sha256": stable_hash(body),
        "body_char_count": len(body),
        "body_preview": truncate_text(body, 600),
        "to": [item for item in parts[4].split(item_sep) if item],
        "cc": [item for item in parts[5].split(item_sep) if item],
        "bcc": [item for item in parts[6].split(item_sep) if item],
        "attachment_names": [item for item in parts[7].split(item_sep) if item],
    }
    payload["draft_sha256"] = stable_hash(
        {
            "draft_id": payload["draft_id"],
            "sender": payload["sender"],
            "subject": payload["subject"],
            "body_sha256": payload["body_sha256"],
            "to": payload["to"],
            "cc": payload["cc"],
            "bcc": payload["bcc"],
            "attachment_names": payload["attachment_names"],
        }
    )
    return payload


def inspect_outgoing_draft(args: dict[str, Any]) -> dict[str, Any]:
    draft_id = validate_draft_id(args.get("draft_id"))
    payload = inspect_outgoing_draft_payload(draft_id)
    payload["send_approval_instruction"] = (
        "Before mail_send_draft, the user must approve this exact draft. "
        "Pass draft_sha256 unchanged so send can detect edits between inspection and send."
    )
    return payload


def send_draft(args: dict[str, Any]) -> dict[str, Any]:
    draft_id = validate_draft_id(args.get("draft_id"))
    confirm_send = bool(args.get("confirm_send", False))
    approval_note = str(args.get("approval_note") or "").strip()
    if os.environ.get("ALLOW_MAC_MAIL_SEND") != "1":
        log_operation("send_blocked", {"draft_id": draft_id, "reason": "ALLOW_MAC_MAIL_SEND not set"})
        raise ToolError("Sending is disabled. Set ALLOW_MAC_MAIL_SEND=1 only after explicit approval.")
    if not confirm_send or not approval_note:
        log_operation("send_blocked", {"draft_id": draft_id, "reason": "missing confirmation"})
        raise ToolError("confirm_send=true and approval_note are required.")
    expected_hash = str(args.get("draft_sha256") or "").strip()
    if not expected_hash:
        log_operation("send_blocked", {"draft_id": draft_id, "reason": "missing draft hash"})
        raise ToolError("draft_sha256 is required. Run mail_inspect_outgoing_draft immediately before approval/send.")
    draft_snapshot = inspect_outgoing_draft_payload(draft_id)
    if draft_snapshot.get("draft_sha256") != expected_hash:
        log_operation("send_blocked", {"draft_id": draft_id, "reason": "draft hash mismatch"})
        raise ToolError("Draft contents changed or did not match approval. Re-inspect and re-approve before sending.")
    script = f"""
tell application "Mail"
  set targetMessage to first outgoing message whose id is {draft_id}
  send targetMessage
end tell
"""
    run_osascript(script, timeout=30)
    log_operation("send_draft", {"draft_id": draft_id, "approval_note_present": bool(approval_note)})
    return {
        "sent": True,
        "draft_id": draft_id,
        "draft_sha256": expected_hash,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


TOOLS: dict[str, dict[str, Any]] = {
    "mail_permissions_check": {
        "description": "Check local Apple Mail permissions and optionally open the macOS privacy panes for Full Disk Access or Mail Automation.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "include_mail_app": {
                    "type": "boolean",
                    "default": False,
                    "description": "If true, also test AppleScript Automation access to Mail.app. This may trigger a macOS permission prompt.",
                },
                "open_full_disk_access": {
                    "type": "boolean",
                    "default": False,
                    "description": "Open System Settings to Full Disk Access.",
                },
                "open_automation": {
                    "type": "boolean",
                    "default": False,
                    "description": "Open System Settings to Automation permissions.",
                },
            },
            "additionalProperties": False,
        },
        "handler": permissions_check,
    },
    "mail_plugin_update_status": {
        "description": "Check whether this plugin git checkout has updates available from GitHub.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "check_remote": {"type": "boolean", "default": True},
            },
            "additionalProperties": False,
        },
        "handler": plugin_update_status,
    },
    "mail_plugin_update_install": {
        "description": "Pull and install the latest plugin update from GitHub. Requires confirm_update=true and usually runs in the background.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "confirm_update": {"type": "boolean"},
                "background": {"type": "boolean", "default": True},
            },
            "required": ["confirm_update"],
            "additionalProperties": False,
        },
        "handler": plugin_update_install,
    },
    "mail_get_state": {
        "description": "Inspect local Apple Mail index coverage and optional Mail.app AppleScript status.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "include_mail_app": {
                    "type": "boolean",
                    "description": "If true, also query Mail.app through AppleScript, which may trigger macOS Automation permission.",
                    "default": False,
                }
            },
            "additionalProperties": False,
        },
        "handler": get_state,
    },
    "mail_list_accounts": {
        "description": "List Apple Mail account UUID coverage from the local index, optionally including Mail.app account names and email addresses.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "include_mail_app": {
                    "type": "boolean",
                    "description": "If true, also query Mail.app account names/email addresses through AppleScript.",
                    "default": False,
                }
            },
            "additionalProperties": False,
        },
        "handler": list_accounts,
    },
    "mail_list_mailboxes": {
        "description": "List local Apple Mail mailboxes across accounts, with friendly role/account filters and exact search arguments for each mailbox.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "account_uuid": {"type": "string"},
                "account": {"type": "string", "description": "Alias for account_uuid."},
                "role": {
                    "type": "string",
                    "enum": ["inbox", "sent", "drafts", "junk", "trash", "archive", "outbox", "other"],
                },
                "mailbox_role": {
                    "type": "string",
                    "enum": ["inbox", "sent", "drafts", "junk", "trash", "archive", "outbox", "other"],
                    "description": "Alias for role, useful when copying filters from mail_search_messages.",
                },
                "query": {"type": "string", "description": "Case-insensitive fuzzy match against mailbox name, path, URL, or role."},
                "name": {"type": "string", "description": "Exact case-insensitive mailbox display name, for example Inbox or Sent Mail."},
                "mailbox_name": {"type": "string", "description": "Alias for name."},
                "path": {"type": "string", "description": "Case-insensitive substring match against decoded mailbox path."},
                "mailbox_path": {"type": "string", "description": "Alias for path."},
                "include_empty": {"type": "boolean", "default": True},
                "limit": {"type": "integer", "minimum": 1, "maximum": 250, "default": 100},
            },
            "additionalProperties": False,
        },
        "handler": list_mailboxes,
    },
    "mail_inbox_overview": {
        "description": "Return a metadata-only overview of local inbox coverage, freshness diagnostics, and bounded triage candidates without reading message bodies.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "since": {"type": "string", "description": "YYYY-MM-DD or ISO datetime. Defaults to the last 7 days."},
                "account_uuid": {"type": "string"},
                "account": {"type": "string", "description": "Alias for account_uuid."},
                "include_candidates": {"type": "boolean", "default": True},
                "include_mail_app": {"type": "boolean", "default": False},
                "include_mailboxes": {"type": "boolean", "default": False},
                "limit_per_lane": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
            },
            "additionalProperties": False,
        },
        "handler": inbox_overview,
    },
    "mail_search_messages": {
        "description": "Search local Apple Mail metadata newest-first using Mail's readonly Envelope Index. Use mailbox_role='inbox' for Gmail-style inbox searches, mailbox_id for exact targeting, and page_token for follow-up pages.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
                "max_results": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "default": 20,
                    "description": "Alias for limit; matches Gmail connector naming.",
                },
                "offset": {"type": "integer", "minimum": 0, "default": 0},
                "page_token": {
                    "type": "string",
                    "description": "Numeric offset token from next_page_token in a prior result.",
                },
                "next_page_token": {
                    "type": "string",
                    "description": "Alias for page_token when replaying a returned token.",
                },
                "mailbox_id": {"type": "integer", "description": "Exact mailbox_id from mail_list_mailboxes. Best for unambiguous mailbox targeting."},
                "mailbox_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "One or more exact mailbox IDs from mail_list_mailboxes.",
                },
                "mailbox": {"type": "string", "description": "Fuzzy substring match against decoded Mail mailbox URL/path/name."},
                "mailbox_name": {"type": "string", "description": "Exact case-insensitive mailbox display name, for example Inbox or Sent Mail."},
                "mailboxName": {"type": "string", "description": "Alias for mailbox_name."},
                "mailbox_path": {"type": "string", "description": "Case-insensitive substring match against decoded mailbox path."},
                "mailboxPath": {"type": "string", "description": "Alias for mailbox_path."},
                "mailbox_role": {
                    "type": "string",
                    "enum": ["inbox", "sent", "drafts", "junk", "trash", "archive", "outbox", "other"],
                },
                "account_uuid": {"type": "string", "description": "Account UUID from mail_get_state."},
                "account": {"type": "string", "description": "Alias for account_uuid."},
                "sender": {"type": "string"},
                "recipient": {"type": "string"},
                "to": {"type": "string", "description": "Alias for recipient."},
                "subject": {"type": "string"},
                "date_from": {"type": "string", "description": "YYYY-MM-DD or ISO datetime."},
                "date_to": {"type": "string", "description": "YYYY-MM-DD or ISO datetime."},
                "has_attachments": {"type": "boolean"},
                "unread_only": {"type": "boolean", "default": False},
                "flagged_only": {"type": "boolean", "default": False},
                "include_deleted": {"type": "boolean", "default": False},
                "include_spam": {"type": "boolean", "default": False},
            },
            "additionalProperties": False,
        },
        "handler": search_messages,
    },
    "mail_index_status": {
        "description": "Inspect the plugin-owned private body-search index status without reading Mail message bodies.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        "handler": body_index_status,
    },
    "mail_purge_body_index": {
        "description": "Delete the plugin-owned private body-search cache. This does not modify Apple Mail data.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "confirm_purge": {"type": "boolean", "description": "Must be true to delete the body-search cache."},
            },
            "required": ["confirm_purge"],
            "additionalProperties": False,
        },
        "handler": purge_body_index,
    },
    "mail_rebuild_body_index": {
        "description": "Build or refresh the plugin-owned private FTS body index from downloaded .emlx files. Writes only to the plugin cache, never to Mail's database.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "max_messages": {"type": "integer", "minimum": 1, "maximum": MAX_BODY_INDEX_LIMIT, "default": DEFAULT_BODY_INDEX_LIMIT},
                "max_body_chars": {"type": "integer", "minimum": 1, "maximum": 500000, "default": 100000},
                "refresh": {"type": "boolean", "default": False, "description": "Re-read matching messages even if they are already indexed."},
                "reset": {"type": "boolean", "default": False, "description": "Delete existing body-index rows before indexing the requested scope."},
                "query": {"type": "string", "description": "Metadata prefilter before body indexing."},
                "sender": {"type": "string"},
                "subject": {"type": "string"},
                "mailbox_id": {"type": "integer", "description": "Exact mailbox_id from mail_list_mailboxes."},
                "mailbox_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "One or more exact mailbox IDs from mail_list_mailboxes.",
                },
                "mailbox": {"type": "string", "description": "Fuzzy mailbox path/name/url filter."},
                "mailbox_name": {"type": "string"},
                "mailbox_path": {"type": "string"},
                "mailbox_role": {
                    "type": "string",
                    "enum": ["inbox", "sent", "drafts", "junk", "trash", "archive", "outbox", "other"],
                },
                "account_uuid": {"type": "string"},
                "account": {"type": "string", "description": "Alias for account_uuid."},
                "date_from": {"type": "string", "description": "YYYY-MM-DD or ISO datetime."},
                "date_to": {"type": "string", "description": "YYYY-MM-DD or ISO datetime."},
                "include_deleted": {"type": "boolean", "default": False},
                "include_sent": {"type": "boolean", "default": False},
                "include_drafts": {"type": "boolean", "default": False},
                "include_junk": {"type": "boolean", "default": False},
                "include_spam": {"type": "boolean", "default": False},
                "include_trash": {"type": "boolean", "default": False},
            },
            "additionalProperties": False,
        },
        "handler": index_body_messages,
    },
    "mail_search_bodies": {
        "description": "Search downloaded message bodies using the plugin-owned private FTS cache. Returns capped snippets; use mail_read_message for full bounded reads.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
                "index_if_needed": {"type": "boolean", "default": False, "description": "If true, index matching metadata candidates before searching."},
                "max_messages": {"type": "integer", "minimum": 1, "maximum": MAX_BODY_INDEX_LIMIT, "default": DEFAULT_BODY_INDEX_LIMIT},
                "mailbox_id": {"type": "integer"},
                "mailbox_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "One or more exact mailbox IDs from mail_list_mailboxes.",
                },
                "mailbox_role": {
                    "type": "string",
                    "enum": ["inbox", "sent", "drafts", "junk", "trash", "archive", "outbox", "other"],
                },
                "account_uuid": {"type": "string"},
                "account": {"type": "string", "description": "Alias for account_uuid."},
                "date_from": {"type": "string", "description": "YYYY-MM-DD or ISO datetime."},
                "date_to": {"type": "string", "description": "YYYY-MM-DD or ISO datetime."},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        "handler": search_body_index,
    },
    "mail_read_message": {
        "description": "Read one local Apple Mail message by local_id from search results. Body requires a downloaded .emlx file.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "local_id": {"type": "integer"},
                "include_body": {"type": "boolean", "default": True},
                "body_format": {"type": "string", "enum": ["plain", "html", "raw"], "default": "plain"},
                "include_attachment_paths": {"type": "boolean", "default": False},
                "max_body_chars": {"type": "integer", "minimum": 0, "default": DEFAULT_BODY_CHARS},
            },
            "required": ["local_id"],
            "additionalProperties": False,
        },
        "handler": read_message,
    },
    "mail_read_thread": {
        "description": "Read all locally indexed messages in an Apple Mail conversation.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "local_id": {"type": "integer"},
                "conversation_id": {"type": "integer"},
                "include_bodies": {"type": "boolean", "default": False},
                "body_format": {"type": "string", "enum": ["plain", "html", "raw"], "default": "plain"},
                "max_body_chars": {"type": "integer", "minimum": 0, "default": 8000},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
            },
            "additionalProperties": False,
        },
        "handler": read_thread,
    },
    "mail_list_attachments": {
        "description": "List attachment metadata and downloaded local attachment files for one message.",
        "inputSchema": {
            "type": "object",
            "properties": {"local_id": {"type": "integer"}},
            "required": ["local_id"],
            "additionalProperties": False,
        },
        "handler": list_attachments,
    },
    "mail_export_attachments": {
        "description": "Copy downloaded Apple Mail attachments for one message to a local destination folder.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "local_id": {"type": "integer"},
                "destination_dir": {"type": "string"},
                "names": {"type": "array", "items": {"type": "string"}},
                "export_all": {"type": "boolean", "default": False},
            },
            "required": ["local_id"],
            "additionalProperties": False,
        },
        "handler": export_attachments,
    },
    "mail_prepare_eml_draft": {
        "description": "Create a local X-Unsent .eml draft file, optionally with HTML, without sending. Can be opened in Mail for visible review.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "to": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Recipients. Runtime also accepts a comma-separated string for manual JSON-RPC calls.",
                },
                "cc": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Cc recipients. Runtime also accepts a comma-separated string for manual JSON-RPC calls.",
                },
                "bcc": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Bcc recipients. Runtime also accepts a comma-separated string for manual JSON-RPC calls.",
                },
                "subject": {"type": "string"},
                "body": {"type": "string", "description": "Alias for text_body."},
                "text_body": {"type": "string"},
                "html_body": {"type": "string", "description": "Optional HTML alternative body. A text fallback is generated when omitted."},
                "sender": {"type": "string"},
                "reply_to": {"type": "string"},
                "attachments": {"type": "array", "items": {"type": "string"}},
                "output_dir": {"type": "string", "description": "Optional local output folder. Defaults to the plugin draft-file cache."},
                "open_in_mail": {"type": "boolean", "default": False, "description": "If true, open the unsent .eml in Mail.app for visible review."},
                "allow_incomplete": {"type": "boolean", "default": False, "description": "Allow missing recipients/subject/body for manual completion."},
            },
            "additionalProperties": False,
        },
        "handler": prepare_eml_draft,
    },
    "mail_create_draft": {
        "description": "Create a visible Apple Mail draft. This does not send the message; visible=false is ignored for safety.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "to": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                "cc": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                "bcc": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                "subject": {"type": "string"},
                "body": {"type": "string"},
                "sender": {"type": "string"},
                "attachments": {"type": "array", "items": {"type": "string"}},
                "visible": {"type": "boolean", "default": True, "description": "Ignored; drafts are always visible."},
            },
            "required": ["subject", "body"],
            "additionalProperties": False,
        },
        "handler": create_draft,
    },
    "mail_create_reply_draft": {
        "description": "Create a visible addressed reply draft from a local Apple Mail message. This does not send.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "local_id": {"type": "integer"},
                "body": {"type": "string"},
                "reply_all": {"type": "boolean", "default": False},
                "include_original": {"type": "boolean", "default": False},
                "max_original_chars": {"type": "integer", "minimum": 0, "default": 6000},
                "sender": {"type": "string"},
                "attachments": {"type": "array", "items": {"type": "string"}},
                "visible": {"type": "boolean", "default": True},
            },
            "required": ["local_id", "body"],
            "additionalProperties": False,
        },
        "handler": create_reply_draft,
    },
    "mail_create_forward_draft": {
        "description": "Create a visible Apple Mail forward draft from a local message. This does not send.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "local_id": {"type": "integer"},
                "to": {"type": "array", "items": {"type": "string"}},
                "cc": {"type": "array", "items": {"type": "string"}},
                "bcc": {"type": "array", "items": {"type": "string"}},
                "note": {"type": "string"},
                "sender": {"type": "string"},
                "include_attachments": {"type": "boolean", "default": False},
                "attachments": {"type": "array", "items": {"type": "string"}},
                "max_original_chars": {"type": "integer", "minimum": 0, "default": 10000},
                "visible": {"type": "boolean", "default": True},
            },
            "required": ["local_id", "to"],
            "additionalProperties": False,
        },
        "handler": create_forward_draft,
    },
    "mail_inspect_outgoing_draft": {
        "description": "Inspect an Apple Mail outgoing draft before sending and return a draft_sha256 approval token.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "draft_id": {"type": "string"},
            },
            "required": ["draft_id"],
            "additionalProperties": False,
        },
        "handler": inspect_outgoing_draft,
    },
    "mail_send_draft": {
        "description": "Send an inspected Apple Mail outgoing message. Disabled unless ALLOW_MAC_MAIL_SEND=1, confirm_send=true, approval_note, and matching draft_sha256 are present.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "draft_id": {"type": "string"},
                "confirm_send": {"type": "boolean"},
                "approval_note": {"type": "string"},
                "draft_sha256": {"type": "string", "description": "Approval token from mail_inspect_outgoing_draft."},
            },
            "required": ["draft_id", "confirm_send", "approval_note", "draft_sha256"],
            "additionalProperties": False,
        },
        "handler": send_draft,
    },
}


def tool_definitions() -> list[dict[str, Any]]:
    return [
        {
            "name": name,
            "description": entry["description"],
            "inputSchema": entry["inputSchema"],
        }
        for name, entry in TOOLS.items()
    ]


def handle_request(message: dict[str, Any]) -> dict[str, Any] | None:
    method = message.get("method")
    request_id = message.get("id")
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        }
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": tool_definitions()}}
    if method == "tools/call":
        params = message.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        if name not in TOOLS:
            result = error_text(f"Unknown tool: {name}")
        else:
            try:
                result = json_text(TOOLS[name]["handler"](args))
            except ToolError as exc:
                result = error_text(str(exc))
            except Exception as exc:
                result = error_text(f"{type(exc).__name__}: {exc}")
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    if method and method.startswith("notifications/"):
        return None
    if request_id is None:
        return None
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def main() -> None:
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
            response = handle_request(message)
        except Exception as exc:
            response = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": f"Parse or server error: {exc}"},
            }
        if response is not None:
            print(json.dumps(response), flush=True)


if __name__ == "__main__":
    main()
