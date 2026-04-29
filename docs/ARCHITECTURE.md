# Architecture

## Data Flow

```text
Codex
  -> stdio MCP
  -> scripts/mac_mail_mcp.py
  -> Apple Mail Envelope Index (read-only SQLite)
  -> downloaded .emlx files for selected body reads
  -> optional plugin-owned body-search SQLite cache
  -> AppleScript only for visible draft/open/send operations
  -> git only for explicit plugin update checks/pulls
```

## Read Path

Metadata search opens Apple Mail's `Envelope Index` using SQLite URI
`mode=ro` and sets `PRAGMA query_only=ON`. Searches are indexed by Mail's
existing tables and avoid reading bodies unless a follow-up tool explicitly
requests a message body.

Message bodies are read from downloaded `.emlx` files on disk and capped by
`max_body_chars`.

If Full Disk Access is missing, read tools return permission guidance that
names the macOS pane to enable. The `mail_permissions_check` tool can also open
Full Disk Access or Automation settings when requested.

## Body Search

The FTS body index is a plugin-owned SQLite database under Application Support.
It stores body text locally for search speed and never writes to Mail's private
database. Broad body indexing excludes Sent, Drafts, Junk, and Trash by default.

## Write Path

Draft creation uses AppleScript to create visible outgoing messages, or writes
local `X-Unsent` `.eml` files for review. Sending is a separate gated tool that
requires environment opt-in plus inspected draft hash verification.

## Install And Update Path

`scripts/bootstrap_install.py` installs the plugin as a home-local Codex plugin
at `~/plugins/mac-mail`, updates `~/.agents/plugins/marketplace.json`, and
enables the plugin in `~/.codex/config.toml`.

Git-backed installs expose update status and install tools. Updates use
`git fetch` and `git pull --ff-only`; they do not run arbitrary remote install
scripts. Codex should be restarted after updating so the MCP server reloads.

## Non-Goals

- Provider APIs and OAuth.
- Direct SQLite writes into Apple Mail data.
- Delete/archive/move/mark-read tools.
- Hidden drafts.
- Automatic send.
- Automatic update without explicit confirmation.
- Cloud indexing, embeddings, or remote body search.
