# Mac Mail Codex Plugin

## Copy/Paste Install Prompt For Codex

Paste this into Codex on a Mac:

```text
Install the Mac Mail Codex plugin from https://github.com/KeystoneScience/mac-mail-codex-plugin and make it ready to use.

Please run:

mkdir -p ~/plugins
if [ -d ~/plugins/mac-mail/.git ]; then
  git -C ~/plugins/mac-mail pull --ff-only origin main
elif [ -e ~/plugins/mac-mail ]; then
  echo "~/plugins/mac-mail already exists but is not a git checkout. Move it aside before installing the self-updating copy." >&2
  exit 1
else
  git clone https://github.com/KeystoneScience/mac-mail-codex-plugin.git ~/plugins/mac-mail
fi
python3 ~/plugins/mac-mail/scripts/bootstrap_install.py
~/plugins/mac-mail/scripts/run_doctor.sh

Then ask me to grant the required macOS permissions:
- Full Disk Access for Codex so the plugin can read Apple Mail's local index and downloaded messages.
- Full Disk Access for Terminal or iTerm too if you tested the plugin from a shell.
- Mail.app Automation permission when I first ask you to create/open/send a Mail draft.

If the plugin reports missing permissions, call mail_permissions_check. If Full Disk Access is blocked, call it with open_full_disk_access=true so System Settings opens to the right page. If Mail Automation is blocked, call it with include_mail_app=true and open_automation=true.

Do not send email during setup. Verify read-only setup by listing mailboxes and searching one exact mailbox_id.
```

Local-first Codex plugin for Apple Mail on macOS. It exposes Mail.app data through a stdio MCP server with fast read-only SQLite metadata search, bounded `.emlx` body reads, optional private FTS body search, safe attachment handling, visible draft preparation, and a strict send gate.

## Highlights

- No cloud email API or credentials.
- Reads Apple Mail's local `Envelope Index` in SQLite read-only mode.
- Searches by exact `mailbox_id`, multiple `mailbox_ids`, account UUID, role, mailbox name/path, sender, recipient, subject, dates, unread/flagged state, and attachments.
- Permission diagnostics can explain missing Full Disk Access or Automation and optionally open the right macOS System Settings pane.
- Git-backed installs can check GitHub for updates and pull the latest plugin code in the background.
- Excludes Junk/Spam from broad metadata search by default.
- Reads downloaded `.emlx` bodies only when needed and caps body output.
- Optional private FTS body index under `~/Library/Application Support/Codex Mac Mail/body-search.sqlite3`.
- Broad body-index builds skip Sent, Drafts, Junk, and Trash unless explicitly included.
- Can purge the private body index with `mail_purge_body_index`.
- Creates visible Apple Mail drafts and local `X-Unsent` `.eml` files.
- Sending requires `ALLOW_MAC_MAIL_SEND=1`, explicit approval fields, and a matching inspected `draft_sha256`.

## Requirements

- macOS with Apple Mail configured.
- Python 3.10+.
- Full Disk Access for the app that launches Codex or the MCP server if macOS blocks `~/Library/Mail` reads.
- Mail.app Automation permission only for draft/open/send operations. Read-only SQLite search does not need to control Mail.app.

No third-party Python packages are required.
The MCP launcher uses `python3.12`, `python3.11`, `python3.10`, then `python3`, or the `MAC_MAIL_PYTHON` environment variable if set.

## Install

For the easiest self-updating install, clone the repository directly into Codex's home-local plugin path and run the bootstrap script:

```bash
mkdir -p ~/plugins
git clone https://github.com/KeystoneScience/mac-mail-codex-plugin.git ~/plugins/mac-mail
python3 ~/plugins/mac-mail/scripts/bootstrap_install.py
```

The bootstrap script:

- keeps the plugin at `~/plugins/mac-mail`;
- creates or updates `~/.agents/plugins/marketplace.json`;
- enables `mac-mail@<local-marketplace>` in `~/.codex/config.toml`;
- runs a non-destructive doctor check;
- prints the exact permission steps to finish setup.

If you are developing from another checkout, you can sync it as a home-local plugin:

```bash
./scripts/install_local_plugin.sh
```

By default that syncs to:

```text
~/plugins/mac-mail
```

You can pass a different target:

```bash
./scripts/install_local_plugin.sh /path/to/plugins/mac-mail
```

The development sync script is useful while editing, but it intentionally excludes `.git`. Use `bootstrap_install.py` or clone directly into `~/plugins/mac-mail` for automatic GitHub update checks.

If you use a local Codex marketplace manually, add or keep an entry that points at the installed plugin directory. The plugin manifest is:

```text
.codex-plugin/plugin.json
```

The MCP config is:

```text
.mcp.json
```

## Quick Check

Run a non-destructive doctor check:

```bash
scripts/run_doctor.sh
```

If macOS permissions are blocked, open the relevant pane:

```bash
scripts/run_doctor.sh --open-full-disk-access
scripts/run_doctor.sh --open-automation
```

From Codex, use `mail_permissions_check`. It can open the same settings panes with `open_full_disk_access=true` or `open_automation=true`.

List tools over JSON-RPC:

```bash
printf '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}\n' | bash scripts/run_mcp.sh
```

Search metadata without launching Mail.app:

```bash
printf '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"mail_search_messages","arguments":{"query":"invoice","limit":5}}}\n' | bash scripts/run_mcp.sh
```

Find a mailbox, then search only that exact mailbox:

```bash
printf '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"mail_list_mailboxes","arguments":{"query":"inbox","include_empty":false,"limit":10}}}\n' | bash scripts/run_mcp.sh
printf '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"mail_search_messages","arguments":{"mailbox_id":123,"limit":5}}}\n' | bash scripts/run_mcp.sh
```

## Body Search

Metadata search is always the fastest path. Use body search only when message body text matters.

Build a narrow private body index:

```bash
printf '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"mail_rebuild_body_index","arguments":{"mailbox_role":"inbox","date_from":"2026-04-01","max_messages":100}}}\n' | bash scripts/run_mcp.sh
```

Search the private body index:

```bash
printf '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"mail_search_bodies","arguments":{"query":"contract deadline","mailbox_role":"inbox","limit":5}}}\n' | bash scripts/run_mcp.sh
```

Remove the private body cache:

```bash
printf '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"mail_purge_body_index","arguments":{"confirm_purge":true}}}\n' | bash scripts/run_mcp.sh
```

## Drafts And Sending

Prepare a local unsent rich draft file without creating a Mail draft:

```bash
printf '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"mail_prepare_eml_draft","arguments":{"to":["person@example.com"],"subject":"Hello","text_body":"Plain fallback","html_body":"<p>Plain <strong>fallback</strong></p>","open_in_mail":false}}}\n' | bash scripts/run_mcp.sh
```

Visible Apple Mail drafts can be created with `mail_create_draft`, `mail_create_reply_draft`, and `mail_create_forward_draft`.

Sending is intentionally hard to trigger:

1. Create or open a visible outgoing draft.
2. Inspect it with `mail_inspect_outgoing_draft`.
3. Get explicit user approval for the exact draft.
4. Set `ALLOW_MAC_MAIL_SEND=1` in the MCP server environment.
5. Call `mail_send_draft` with `confirm_send=true`, `approval_note`, and the matching `draft_sha256`.

The server re-inspects the outgoing draft immediately before sending and refuses to send if the hash changed.

## Updates

Git-backed installs expose two update tools:

- `mail_plugin_update_status` checks the current checkout against GitHub.
- `mail_plugin_update_install` pulls a fast-forward update after `confirm_update=true`; by default it runs in the background and writes a local update log under `~/Library/Application Support/Codex Mac Mail/update.log`.

Restart Codex after an update so the MCP server reloads the new code.

## Tool Surface

- `mail_get_state`
- `mail_permissions_check`
- `mail_plugin_update_status`
- `mail_plugin_update_install`
- `mail_list_accounts`
- `mail_list_mailboxes`
- `mail_inbox_overview`
- `mail_search_messages`
- `mail_index_status`
- `mail_purge_body_index`
- `mail_rebuild_body_index`
- `mail_search_bodies`
- `mail_read_message`
- `mail_read_thread`
- `mail_list_attachments`
- `mail_export_attachments`
- `mail_prepare_eml_draft`
- `mail_create_draft`
- `mail_create_reply_draft`
- `mail_create_forward_draft`
- `mail_inspect_outgoing_draft`
- `mail_send_draft`

## Test

Unit tests:

```bash
python3 -m unittest discover -s tests -v
```

Live read-only smoke checks:

```bash
python3 scripts/live_smoke_test.py
```

Benchmarks that print timings and counts only:

```bash
python3 scripts/benchmark_mail.py
```

Release verification:

```bash
scripts/verify_release.sh
scripts/verify_release.sh --live
```

## Package

Create a distributable archive in `dist/`:

```bash
scripts/package_plugin.sh
```

The package excludes caches, private local data, and generated artifacts.

## Safety Notes

- This plugin does not request or store email credentials.
- It does not write to Apple Mail's SQLite database.
- It does not implement delete, archive, move, mark-read/unread, rules, signatures, or account mutation tools.
- Attachment export requires reviewed names unless `export_all=true`.
- Outbound attachments must be regular files, are size/count capped, and block common executable/package/script extensions.
- Logs are redacted and live under `~/Library/Application Support/Codex Mac Mail/operation-log.jsonl`.
- Local Mail data may lag remote providers until Mail.app syncs.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the data-flow model and non-goals.

## License

MIT. See [LICENSE](LICENSE).
