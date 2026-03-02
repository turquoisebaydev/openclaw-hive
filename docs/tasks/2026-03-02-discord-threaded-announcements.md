# Task Brief — Threaded Discord Announcements (for Claude)

Date: 2026-03-02
Owner: hive-dev
Repo: `/Users/turquoise/Projects/openclaw-hive`

## Objective
Implement threaded Discord announcements so correlated Hive events are grouped into one Discord thread, with safe fallback to current channel-post behavior.

## Read First
1. `CLAUDE.md`
2. `docs/protocol.md`
3. `docs/features/discord-threaded-announcements.md`
4. `daemon/hive_daemon/announcer.py`
5. `daemon/hive_daemon/main.py`

## Current State (important)
- Non-threaded announcements already exist and are deployed.
- Heartbeat announcements are suppressed.
- Compact text preview is included.
- Non-fatal policy is mandatory.

## Deliverables
1. Config parsing support for:
   - `[announcements.discord.threading]`
   - `enabled`, `mode`, `thread_name_prefix`, `max_age_hours`, `fallback_to_channel`
2. Thread keying and mapping layer:
   - `corr` primary key, fallback to `id`
   - persistent map on disk with TTL pruning
3. Discord threaded sender path:
   - create parent + thread when new key
   - reuse thread for subsequent events in same key
4. Failure handling:
   - warn and fallback to channel post when configured
   - never break core Hive routing
5. Tests:
   - config parse defaults/overrides
   - map create/reuse/prune/remap
   - fallback behavior on API/thread errors
   - regression: existing non-thread behavior still works when threading disabled

## Constraints
- Default-off feature.
- No secrets in announcement content.
- Keep message format compact and operator-readable.
- Must preserve existing send/recv toggles and heartbeat suppression.
- Must preserve non-fatal behavior.

## Suggested File Targets
- `daemon/hive_daemon/config.py`
- `daemon/hive_daemon/announcer.py`
- `daemon/hive_daemon/main.py` (only if needed)
- `daemon/hive_daemon/*thread*` (new helper module allowed)
- `daemon/tests/test_config.py`
- `daemon/tests/test_announcer.py`
- `daemon/tests/test_*thread*.py` (new)

## Implementation Guidance
- Keep this phase narrowly scoped to Discord.
- Reuse existing durable-map patterns where possible.
- Avoid broad refactors.
- Keep compatibility with current deployed config.

## Test Commands
```bash
cd daemon
.venv/bin/pytest tests/test_config.py tests/test_announcer.py -v
# plus new threaded tests
.venv/bin/pytest tests/test_*thread*.py -v
# then full suite
.venv/bin/pytest tests/ -v
```

## Done Checklist
- [ ] Threading config parsed with sane defaults
- [ ] Correlated events post into one thread
- [ ] Restart-safe mapping works
- [ ] Fallback path proven on thread/API failure
- [ ] Existing non-thread mode unchanged
- [ ] Tests added and passing
- [ ] Docs updated where needed
