# Task Brief — Hive Discord Announcements + OpenClaw Cmd Verification

Date: 2026-03-02
Owner: hive-dev
Repo: `/Users/turquoise/Projects/openclaw-hive`

## Objective
Implement optional Discord announcements for hive send/receive events, and verify/regression-test per-instance OpenClaw command path behavior.

## Read First
1. `CLAUDE.md`
2. `docs/protocol.md`
3. `docs/features/discord-announcements-and-openclaw-cmd.md`

## Deliverables
1. Config support for `announcements` block in `hive.toml`
2. Announcement publish path wired into send and receive flow
3. Non-fatal error handling for publish failures
4. Regression coverage for per-instance `openclaw_cmd`
5. Update docs/examples if config shape changes during implementation

## Constraints
- Default behavior unchanged when announcements are unset/disabled
- No secrets in announcement payloads
- Keep messages compact + operator-friendly
- Do not break existing `openclaw` alias compatibility in config parsing

## Candidate Files
- `daemon/hive_daemon/config.py`
- `daemon/hive_daemon/main.py`
- `daemon/hive_daemon/router.py`
- `daemon/hive_daemon/*.py` (where send/receive events are emitted)
- `daemon/tests/*`
- `hive.toml` (example comments only if needed)

## Suggested Test Commands
```bash
cd daemon
pytest tests/test_config.py -v
pytest tests/test_dispatcher.py -v
pytest tests/test_oc_bridge.py -v
# add/execute targeted tests for announcement publish behavior
```

## Done Checklist
- [ ] Send announcements work behind feature flag
- [ ] Receive announcements work behind feature flag
- [ ] Publish errors logged, not fatal
- [ ] `openclaw_cmd` behavior unchanged and covered
- [ ] Docs updated
