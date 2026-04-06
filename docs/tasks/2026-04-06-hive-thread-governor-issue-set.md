# Hive Thread Governor — Implementation Issue Set

Source design: `~/opt/turq-clawd/docs/cluster/hive-thread-governor-plan.md`

## Epic

Implement Discord thread governance in Hive/admin bot to pause long non-Hugh side conversations in watched parent channels, auto-unlock after cooldown, and provide Hugh overrides.

---

## Issue 1 — Config + wiring scaffold

### Scope
1. Add governor config loading to admin bot startup.
2. Add validation for required fields.
3. Add safe defaults.

### Config keys (MVP)
1. `owner_id`
2. `watched_parents[]`
3. `default_limit`
4. `auto_unlock_minutes`
5. `notice_template`
6. `cleanup.interval_minutes`
7. `cleanup.idle_expiry_days`
8. `cleanup.archived_expiry_days`

### Acceptance criteria
1. Boot succeeds with valid config.
2. Invalid config fails fast with clear error.
3. Defaults apply when optional fields are absent.
4. Effective config is visible in startup logs.

---

## Issue 2 — Durable thread state store (SQLite)

### Scope
1. Add SQLite state DB for tracked threads.
2. Add schema + migration bootstrap.
3. Add CRUD helpers for thread state.

### State fields (MVP)
1. `thread_id` (PK)
2. `parent_id`
3. `count`
4. `limit`
5. `locked`
6. `created_at`
7. `last_owner_at`
8. `last_message_at`
9. `locked_at`
10. `unlock_at`
11. `closed_reason` (nullable)

### Acceptance criteria
1. State persists across restarts.
2. Read/write helpers are idempotent.
3. Missing rows are auto-created on first use.
4. Basic index exists for `locked + unlock_at` scheduler query.

---

## Issue 3 — Message-driven discovery + counting semantics

### Scope
1. Inbound message hook processes only thread messages.
2. Check parent against `watched_parents` allowlist.
3. Auto-create state record when first seen.
4. Apply counting semantics exactly:
   1. Hugh message => reset `count=0`, update `last_owner_at`.
   2. Non-Hugh accepted inbound => increment `count`.
   3. Ignore bot/system/ack noise per policy.

### Acceptance criteria
1. New watched thread is auto-discovered on first message.
2. Unwatched parent threads are ignored.
3. Hugh message reset works without mention requirement.
4. Non-Hugh count increments reliably once per accepted inbound turn.

---

## Issue 4 — Lock trigger + notice

### Scope
1. Trigger when `count >= limit` after processing a non-Hugh message.
2. Lock thread via Discord `ManageThreads` action.
3. Set `locked=true`, `locked_at`, `unlock_at`.
4. Post one short notice using template.
5. Avoid repeated duplicate notices while already locked.

### Acceptance criteria
1. Thread locks exactly at threshold.
2. Notice posts once per lock event.
3. Lock metadata is persisted.
4. Further counting does not continue while locked.

---

## Issue 5 — Auto-unlock scheduler + startup recovery

### Scope
1. Scheduler checks due unlocks (e.g. every minute).
2. On unlock: `locked=false`, `count=0`, clear lock timers.
3. Startup reconciliation:
   1. Unlock overdue locked threads immediately.
   2. Schedule future unlocks for remaining locked threads.
4. No default unlock announcement (silent unlock).

### Acceptance criteria
1. Locked threads auto-unlock at/after `unlock_at`.
2. Restart during cooldown does not strand locks.
3. Overdue locks recover correctly on startup.
4. Post-unlock state is reset and persisted.

---

## Issue 6 — Slash command contract (owner/admin only)

### Scope
1. `/unlock` — unlock + reset.
2. `/pause` — lock now with cooldown timer.
3. `/status` — show lock state, count/limit, ETA, last Hugh activity.
4. `/budget set <n>` — per-thread limit override (persisted).
5. Permission gate to `owner_id` (+ optional admin list).

### Acceptance criteria
1. Unauthorized users cannot execute governor commands.
2. Commands work only in threads (or clearly error otherwise).
3. `/status` output is concise and accurate.
4. Thread budget override survives restart.

---

## Issue 7 — Cleanup job

### Scope
1. Periodic cleanup removes stale state rows only.
2. Delete state when:
   1. Thread inaccessible/nonexistent.
   2. Archived older than `archived_expiry_days`.
   3. Idle older than `idle_expiry_days`.
3. Do not mutate live Discord thread state during cleanup.

### Acceptance criteria
1. Cleanup runs on configured interval.
2. Rows are removed per policy.
3. Cleanup is safe/idempotent and logs deletions.

---

## Issue 8 — Logging + smoke tests

### Scope
1. Add info logs for:
   1. thread discovered
   2. Hugh reset
   3. thread locked
   4. thread unlocked
   5. cleanup deletion
   6. startup overdue unlock recovery
2. Add smoke test plan covering lock/unlock lifecycle.

### Acceptance criteria
1. Logs include thread ID, parent ID, count/limit, timestamps.
2. Manual smoke run passes end-to-end on a watched parent.
3. No lock stranding across restart.

---

## Suggested implementation order

1. Issue 1 (config)
2. Issue 2 (state store)
3. Issue 3 (discovery/counting)
4. Issue 4 (lock + notice)
5. Issue 5 (auto-unlock/recovery)
6. Issue 6 (commands)
7. Issue 7 (cleanup)
8. Issue 8 (logs/tests)

---

## Definition of done (MVP)

1. Governed behavior applies to all threads under watched parent(s).
2. Locking is deterministic at budget threshold.
3. Auto-unlock and restart recovery are reliable.
4. Hugh has immediate manual control via slash commands.
5. State remains bounded via cleanup.
6. Repo includes runnable smoke-test steps + operator notes.
