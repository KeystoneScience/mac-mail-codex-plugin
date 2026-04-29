---
name: mac-mail
description: Use the local Apple Mail plugin to search Mail.app data, read downloaded messages, triage local inboxes, search the private body index, and prepare visible or unsent drafts from this Mac.
---

# Mac Mail

Use this skill when the user asks to work with mail that may exist in local Apple Mail accounts outside a provider connector.

## Operating Rules

- Prefer read-only tools first: inspect state/accounts, list mailboxes if needed, search messages, then read a specific message or local thread.
- If any read/search tool reports Apple Mail storage access problems, call `mail_permissions_check` and offer to open Full Disk Access with `open_full_disk_access=true`. If Mail.app draft/open/send control is blocked, call it with `include_mail_app=true` and `open_automation=true`.
- Before assuming a bug is in search logic, verify permissions and local Mail sync state with `mail_permissions_check`, `mail_get_state`, or `mail_list_accounts`.
- When the user names a specific inbox, account mailbox, folder, or label, use `mail_list_mailboxes` first and then pass the returned `search_arguments.mailbox_id` into `mail_search_messages` for exact targeting.
- Use `mail_inbox_overview` when the user asks for a broad inbox scan or "what matters" across local Apple Mail accounts; it returns metadata-only candidates and avoids body reads by default.
- Use `mail_search_messages` for fast metadata search. Use `mail_rebuild_body_index` plus `mail_search_bodies` only when body text matters; build the body index narrowly by mailbox/date/account when possible.
- Treat Apple Mail as local machine state. Do not assume it matches Gmail or any remote mailbox if Mail has not synced.
- Do not request or store account passwords, app passwords, OAuth tokens, or Apple ID credentials.
- Drafts, reply drafts, forward drafts, and unsent `.eml` draft files are allowed when the user asks for a draft, but actual sending requires explicit approval for the exact sender, recipients, subject, body, and attachments. Apple Mail drafts are always made visible and may still sync to provider Drafts folders.
- Before sending, inspect the outgoing draft with `mail_inspect_outgoing_draft` and pass the returned `draft_sha256` into `mail_send_draft`; sending is still blocked unless `ALLOW_MAC_MAIL_SEND=1` and the user has explicitly approved the exact send.
- Do not delete, archive, move, mark read/unread, or change rules/accounts/signatures unless a future tool supports it and the user explicitly asks.
- For attachments, show filename/path/size before sending or exporting. Export exact reviewed attachment names unless the user explicitly requests all attachments. Never execute attachments.
- Use `mail_plugin_update_status` when the user asks whether the plugin is current. Use `mail_plugin_update_install` only after explicit approval for the update pull, then tell the user to restart Codex.

## Search Flow

1. Call `mail_get_state` or `mail_list_accounts` to confirm local index coverage.
2. Use `mail_inbox_overview` for broad scans, or `mail_list_mailboxes` when the user refers to a specific inbox/account/folder/label and the mailbox role is ambiguous.
3. Use `mail_search_messages` with narrow filters. Prefer exact `mailbox_id`; use `mailbox_ids`, `account_uuid` + `mailbox_role`, `mailbox_name`, or `mailbox_path` when that better matches the request. Junk/Spam is excluded by default on broad searches unless explicitly requested.
4. Inspect a small page of results before expanding. Use `limit: 20` by default unless the task clearly needs a wider pass.
5. Use `mail_read_message` for one promising message, or `mail_read_thread` only when thread context changes the answer, audience, tone, or whether the user is next to act.
6. State the search scope and confidence when summarizing results, especially for broad "anything important?" questions.

## Inbox Triage

For "check my mail," "interesting messages I missed," or "what needs attention," use a Gmail-style bucketed answer:

- `Urgent`: deadlines, commitments, money/legal/customer risk, same-day asks, or direct high-priority messages.
- `Needs reply soon`: direct messages where the user is plausibly next to act.
- `Waiting`: threads where the user is waiting on someone else or no action is needed until they reply.
- `FYI`: useful updates that do not need action.

Workflow:

1. Start with `mail_inbox_overview` for recent local inbox candidates.
2. Shortlist from metadata first: direct senders, unread, flagged, attachments, customer/vendor/legal/finance terms, and recent thread movement.
3. Read bodies only for shortlisted messages. Treat "needs reply" as an inference, not a certainty.
4. For each item, give sender, subject, date, why it matters, likely next action, and whether you read metadata only or full body/thread.
5. Avoid absolute claims like "the only urgent email" unless the scan was comprehensive enough to support it.

## Body Search

- `mail_index_status` shows whether the private body cache exists and how much it covers.
- `mail_rebuild_body_index` reads downloaded `.emlx` bodies into the plugin-owned local FTS cache. Scope it with `mailbox_id`, `account_uuid`, `mailbox_role`, and dates whenever possible.
- Sent, Drafts, Junk, and Trash are excluded from broad body indexing by default. Include them only when the user explicitly asks.
- `mail_search_bodies` returns capped snippets, not full bodies. Follow with `mail_read_message` or `mail_read_thread` for bounded full context.
- `mail_purge_body_index` deletes the private local body cache when the user wants to remove cached body text.

## Draft And Send Flow

- Use `mail_prepare_eml_draft` for rich HTML or when a local unsent `.eml` file is safer than creating a provider-synced Mail draft.
- Use `mail_create_draft`, `mail_create_reply_draft`, or `mail_create_forward_draft` when the user wants a visible Mail compose window.
- Read latest local thread context before drafting replies. Preserve names, dates, links, commitments, attachments, and recipient intent.
- Do not default to reply-all. Use `reply_all` only when the user asks or when the thread clearly requires it; mention the audience choice if it matters.
- Before any send: inspect with `mail_inspect_outgoing_draft`, show the relevant draft facts to the user, and use `mail_send_draft` only after exact approval.
