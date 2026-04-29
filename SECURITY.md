# Security Policy

## Local Data Model

This plugin is local-first. It reads Apple Mail data already present on the
machine and does not ask for email credentials, Apple ID credentials, OAuth
tokens, or app passwords.

The server opens Apple Mail's `Envelope Index` with SQLite read-only mode and
`PRAGMA query_only=ON`. It never writes to Mail's private database.

## Sensitive Local Caches

The optional body-search index stores downloaded email body text locally at:

```text
~/Library/Application Support/Codex Mac Mail/body-search.sqlite3
```

The database is created with owner-only permissions where supported. Users can
delete it through `mail_purge_body_index` or by removing the file directly.

## Send Safety

Sending is disabled unless all of the following are true:

- `ALLOW_MAC_MAIL_SEND=1` is set in the MCP server environment.
- The tool call includes `confirm_send=true`.
- The tool call includes an approval note.
- The tool call includes the current `draft_sha256` from
  `mail_inspect_outgoing_draft`.

The server re-inspects the draft immediately before sending and blocks if the
draft changed after approval.

## Permissions

Read-only search needs macOS Full Disk Access for the app running the MCP server
because Apple Mail stores local mail under `~/Library/Mail`. Draft/open/send
tools may also need Automation permission to control Mail.app. The
`mail_permissions_check` tool and `scripts/doctor.py` report missing permission
state and can open the relevant System Settings panes when explicitly requested.

## Updates

Automatic updates are limited to Git-backed installs. Update checks use the
configured `origin` remote and update installs use `git pull --ff-only`; the MCP
tool requires `confirm_update=true`. Restart Codex after updating so the new
server code is loaded.

## Mutation Policy

The plugin does not expose tools for deleting, archiving, moving, marking
read/unread, changing rules, changing signatures, or changing accounts.

## Reporting Issues

Please report security issues privately to the repository owner rather than
opening a public issue with sensitive details. Include:

- macOS version
- Mail version if relevant
- plugin version
- reproduction steps that avoid message bodies, credentials, and private email
  addresses when possible
