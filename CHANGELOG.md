# Changelog

## 0.6.0 - 2026-04-29

- Added a copy/paste Codex install prompt at the top of the README.
- Added `scripts/bootstrap_install.py` to install/register the plugin at `~/plugins/mac-mail`, update the home-local Codex marketplace, and enable the plugin in Codex config.
- Added `mail_permissions_check` plus doctor flags to explain missing Full Disk Access or Mail Automation and optionally open the correct macOS System Settings panes.
- Added GitHub update tools: `mail_plugin_update_status`, `mail_plugin_update_install`, and `scripts/update_plugin.py`.
- Added Python launcher scripts so Codex uses Python 3.10+ even on Macs where bare `python3` is older.
- Bumped release checks and tests for the public, self-updating install path.

## 0.5.0 - 2026-04-29

- Added public repo hygiene: license, security policy, contributing guide, release verification, packaging script, GitHub Actions CI, and doctor script.
- Added `mail_purge_body_index` to delete the private body-search cache.
- Changed broad body-index builds to skip Sent, Drafts, Junk, and Trash unless explicitly included.
- Removed user-specific wording from the shareable skill and default prompts.
- Kept the stdlib-only runtime and existing guarded send model.

## 0.4.0 - 2026-04-29

- Added plugin-owned private FTS body search with `mail_index_status`, `mail_rebuild_body_index`, and `mail_search_bodies`.
- Added `mail_prepare_eml_draft` for local `X-Unsent` `.eml` draft files with optional HTML alternatives.
- Added `mail_inspect_outgoing_draft`.
- Strengthened `mail_send_draft` to require a matching inspected `draft_sha256`.
- Expanded Gmail-style skill guidance for search, triage, body search, draft flow, and scope/confidence reporting.

## 0.3.1 - 2026-04-29

- Added exact `mailbox_id` and multiple `mailbox_ids` targeting.
- Added easier mailbox filters and `search_arguments` in mailbox results.
- Added tests and benchmarks for exact mailbox targeting.

## 0.3.0 - 2026-04-29

- Added deeper tests, live smoke checks, and benchmarks.
- Hardened body caps, logging, attachment export, and blocked-send behavior.

## 0.2.0 - 2026-04-29

- Expanded the initial tool surface with accounts, mailboxes, inbox overview, richer metadata search, message/thread reads, attachment listing/export, visible drafts, reply/forward drafts, and guarded draft send.
