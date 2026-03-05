# Task Brief — Discord Bot-Threaded Announcements (Text-Only Thread Messages)

Date: 2026-03-05
Owner: hive-dev
Repo: `/Users/turquoise/Projects/openclaw-hive`

## Objective
Implement Discord bot-token threaded announcements keyed by corr, with text-only thread message bodies and full metadata written to local daemon audit log.

## Read First
1. `CLAUDE.md`
2. `docs/protocol.md`
3. `docs/features/discord-threaded-announcements.md`
4. `daemon/hive_daemon/announcer.py`
5. `daemon/hive_daemon/config.py`

## Requirements
1. Add config support for bot-based Discord announce path:
   - `announcements.discord.channel_id`
   - `announcements.discord.bot_token`
   - keep existing `webhook_url` as optional fallback
2. Implement corr-key thread mapping + reuse:
   - key: corr fallback id
   - persistent map with prune (`max_age_hours`)
3. Thread posting behavior:
   - parent summary message can include compact metadata
   - thread message body must be message text only (`envelope.text` policy)
4. Add per-daemon audit log writer:
   - record full metadata and publish result
   - configurable path (`announcements.audit.path`)
5. Preserve non-fatal policy and publish toggles:
   - `publish_send` / `publish_receive`
   - heartbeat suppression remains
6. Fallback behavior:
   - if bot thread path fails and `fallback_to_channel=true`, send top-level fallback
   - do not break core routing

## Constraints
- Keep implementation deterministic in announce/control path.
- Never log secrets/tokens.
- Keep backward compatibility for existing webhook configs.

## Suggested File Targets
- `daemon/hive_daemon/config.py`
- `daemon/hive_daemon/announcer.py`
- `daemon/hive_daemon/announcement_threads.py` (new, optional)
- `daemon/hive_daemon/announcement_audit.py` (new, optional)
- `daemon/tests/test_config.py`
- `daemon/tests/test_announcer.py`
- `daemon/tests/test_*thread*.py`
- `daemon/tests/test_*audit*.py`

## Test Commands
```bash
cd daemon
.venv/bin/pytest tests/test_config.py tests/test_announcer.py -v
.venv/bin/pytest tests/test_*thread*.py tests/test_*audit*.py -v
.venv/bin/pytest tests/ -v
```

## Return Format
- Summary of changes
- Files changed
- Test commands + outputs
- Commit hash

## Done Checklist
- [ ] Bot-token config parsed + validated
- [ ] Thread grouping/reuse works by corr
- [ ] Thread message body is text-only
- [ ] Metadata written to local audit log
- [ ] Fallback path works and remains non-fatal
- [ ] Tests green
