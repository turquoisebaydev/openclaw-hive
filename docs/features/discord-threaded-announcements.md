# Feature Spec — Discord Threaded Announcements (Correlation Flows)

## Status
Draft (ready for implementation)

## Why
Current webhook announcements are visible but noisy in-channel. Operators want each request/response flow grouped in one Discord thread so they can follow a single conversation chain.

## Goal
When Hive events share the same correlation chain (`corr`), post them into one Discord thread under a single parent message.

---

## Scope
1. Threaded delivery for Discord announcements.
2. Stable thread keying by correlation id.
3. Local mapping store to reuse thread ids across process restarts.
4. Non-fatal fallback to normal channel post if thread operations fail.

Out of scope (this round):
- Migrating historical messages into threads
- Cross-guild threading
- Rich embeds/components
- Provider-agnostic threading abstraction

---

## Important Implementation Note
Discord **webhook-only posting cannot reliably create/reuse normal channel threads** for this use-case.

Therefore this phase should use a **Discord bot/API sender path** (token-based) for thread operations, while keeping existing webhook path as fallback.

---

## Proposed Config

```toml
[announcements]
enabled = false

[announcements.discord]
enabled = false
channel = "hive-announcements"
webhook_url = "https://discord.com/api/webhooks/..." # existing fallback path
publish_send = true
publish_receive = true

[announcements.discord.threading]
enabled = false
mode = "by_corr"                # only mode in phase 1
thread_name_prefix = "hive"
max_age_hours = 168              # map entry reuse window
fallback_to_channel = true       # if thread post fails, post top-level
```

### Defaults
- `threading.enabled = false`
- `mode = "by_corr"`
- `max_age_hours = 168`
- `fallback_to_channel = true`

---

## Thread Key / Grouping Rules
1. `thread_key = envelope.corr` when present
2. fallback: `thread_key = envelope.id`

All send/recv events with same `thread_key` should be posted into the same thread.

---

## Thread Lifecycle
1. Resolve existing thread from local map by `(channel, thread_key)`.
2. If missing/stale/invalid:
   - create parent summary message in configured channel
   - create a thread from parent
   - store mapping
3. Post event message into thread.
4. Update `last_seen`.

If thread posting fails:
- log warning
- if `fallback_to_channel=true`, publish top-level non-thread message
- never impact core Hive send/recv processing

---

## Mapping Store
Add local JSON map (similar to session map durability):

Suggested file:
- `~/.local/share/hive/announcement_threads.json`

Record shape:
```json
{
  "discord:hive-announcements:corr:ab12...": {
    "threadId": "...",
    "parentMessageId": "...",
    "createdAt": 1772430000,
    "lastSeenAt": 1772433600
  }
}
```

Cleanup:
- prune entries older than `max_age_hours`

---

## Runtime Behavior
### Parent message format
Compact opener for thread root:

`HIVE FLOW | corr=... | from=turq | to=pg1 | action=ping`

### Thread message format
Reuse existing announcement line format (send/recv, metadata, preview text).

---

## Safety / Reliability
- Keep non-fatal policy (announce failures do not block routing/dispatch)
- Never include secrets/tokens
- Keep text preview truncation
- Heartbeat channel remains suppressed from announcements

---

## Acceptance Criteria
1. New flow creates one thread and posts events inside it.
2. Matching `corr` events reuse same thread.
3. Restart daemon -> mapping still works (same thread reused when valid).
4. Invalid/archived thread remaps by creating a fresh one.
5. Failure path falls back to channel post when configured.
6. Existing non-thread behavior remains unchanged when `threading.enabled=false`.

---

## Test Plan
- Unit tests:
  - key selection (`corr` vs `id` fallback)
  - map store read/write/prune behavior
  - thread resolution create/reuse/remap
  - fallback path on API error
- Integration smoke:
  - send command with `--wait` and verify SEND/RECV in same thread
  - repeat same corr chain and verify same thread id
  - restart daemon and verify reuse

---

## Rollout Plan
1. Ship disabled by default.
2. Enable on turq only.
3. Run ping + command/response smoke tests.
4. Validate no message loss on failures.
5. Enable on pg1.
6. Optionally enable on remaining gateways.
