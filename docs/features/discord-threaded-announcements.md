# Feature Spec — Discord Threaded Announcements (Bot API, Corr Threads)

## Status
Draft (implementation-ready)

## Why
Channel-level announcements are noisy. We want correlated Hive conversations (`corr`) grouped into Discord threads, while preserving deterministic/non-fatal daemon behavior.

## Goals
1. Use Discord **bot token API** for robust thread create/reuse.
2. Group messages by `corr` (fallback `id`).
3. Post **thread message body as message text only** (`envelope.text` preview policy still applies).
4. Keep rich metadata in a **local daemon audit log** (not in Discord thread text).
5. Keep announcement failures non-fatal.

---

## Transport

### Primary
Discord bot API (token-based) for:
- parent message send
- thread creation
- message send into thread

### Fallback
Webhook/top-level fallback only when configured and bot path fails.

---

## Config

```toml
[announcements]
enabled = false

[announcements.discord]
enabled = false
channel = "hive-announcements"        # operator label
channel_id = "1477044417012695040"   # required for bot API path
bot_token = "<discord bot token>"     # required for bot API path
webhook_url = "https://discord.com/api/webhooks/..." # optional fallback
publish_send = false
publish_receive = true

[announcements.discord.threading]
enabled = true
mode = "by_corr"
thread_name_prefix = "hive"
max_age_hours = 168
fallback_to_channel = true

[announcements.audit]
enabled = true
path = "~/.local/state/hive/hive-announcements.log"
```

Defaults:
- threading disabled by default until explicitly enabled
- audit enabled by default
- preserve existing publish_send/publish_receive semantics

---

## Thread Grouping Rules
1. `thread_key = corr` when present
2. fallback `thread_key = id`

All events with same thread_key go to one thread.

---

## Discord Content Rules

### Parent message (thread root)
Compact summary allowed (single line), e.g.:
`HIVE FLOW corr=<corr> from=<from> to=<to> action=<action>`

### Thread message body
**Text only**:
- send message text (`envelope.text`) only
- no extra metadata prefix in thread message
- rely on Discord author/time/thread context for display

If text is very large, apply deterministic truncation policy.

---

## Local Audit Log (per daemon)
Write one JSONL/text line per announcement event containing full metadata:
- from/to/ch/action/corr/replyTo/id/gw/ts/status/threadId
- include publish outcome (ok/failure + error)

Suggested defaults:
- macOS: `~/Library/Logs/hive-announcements.log`
- Linux: `~/.local/state/hive/hive-announcements.log`

This log replaces the need for metadata-heavy Discord message bodies.

---

## Mapping Store
Persist thread mappings:
- `~/.local/share/hive/announcement_threads.json`

Fields:
- thread_key
- thread_id
- parent_message_id
- created_at
- last_seen_at

Prune mappings older than `max_age_hours`.

---

## Failure Handling
- Never block core hive routing/dispatch.
- On bot send/thread errors:
  - warn + write audit log
  - fallback behavior per `fallback_to_channel`
- If fallback disabled, drop announcement but keep daemon healthy.

---

## Acceptance Criteria
1. Messages with same corr appear in same Discord thread.
2. Thread message body contains message text only.
3. Full metadata appears in local audit log.
4. Restart preserves thread reuse via mapping store.
5. Invalid/archived thread remaps cleanly.
6. Failures remain non-fatal.

---

## Test Plan
- config parse tests for bot/token/channel/threading/audit
- thread map tests (create/reuse/prune/remap)
- Discord sender tests (bot path + fallback path)
- content tests: thread body is text-only; metadata in audit log
- regression tests for publish_send/publish_receive + heartbeat suppression

---

## Rollout Plan
1. Implement behind config flags.
2. Enable on turq first.
3. Validate thread grouping + text-only body + audit logs.
4. Expand to pg1, then turqette/mini1 path.
