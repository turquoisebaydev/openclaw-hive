# Task Brief — Session Presence Registry + Deterministic Session Delivery

Date: 2026-03-05
Owner: hive-dev
Repo: `/Users/turquoise/Projects/openclaw-hive`

## Objective
Implement cluster-wide session presence and deterministic session-targeted delivery failures.

## Read First
1. `CLAUDE.md`
2. `docs/protocol.md`
3. `docs/features/session-presence-registry.md`
4. `daemon/hive_daemon/main.py`
5. `daemon/hive_daemon/router.py`
6. `daemon/hive_daemon/oc_bridge.py`

## Non-Negotiable Deterministic Constraints
Must remain model-free deterministic paths:
- presence collection/publication
- presence cache + ttl prune
- session target resolution
- failed target response generation

Do not route these through OC bridge/LLM.

## Deliverables
1. Presence config parsing + defaults (`presence.*`)
2. Presence publisher in daemon loop (session-level records)
3. Presence cache/store with TTL expiry
4. `hive-cli` session-target send flags
5. Deterministic failed-delivery response envelope for stale/missing targets
6. Tests for all above

## Suggested File Targets
- `daemon/hive_daemon/config.py`
- `daemon/hive_daemon/main.py`
- `daemon/hive_daemon/router.py`
- `daemon/hive_daemon/oc_bridge.py`
- `daemon/hive_daemon/presence.py` (new)
- `daemon/tests/test_presence*.py` (new)
- `cli/hive_cli/*.py` for new target flags
- `cli/tests/*` for parser/behavior

## Required Behavior
- Presence publish topic: `<prefix>/presence/<gw>/<agent>/<session>`
- Include status + task summary fields (sanitized/truncated)
- TTL expiry default 300s
- On stale/missing target for session send: deterministic delivery_error response with code + target + corr/replyTo

## Test Commands
```bash
cd daemon
.venv/bin/pytest tests/test_config.py tests/test_presence*.py tests/test_main.py -v

cd ../cli
.venv/bin/pytest tests/ -v

# full daemon suite
cd ../daemon
.venv/bin/pytest tests/ -v
```

## Done Checklist
- [ ] Presence records published with ttl
- [ ] Cache prune deterministic
- [ ] Session-target send resolves correctly
- [ ] Missing/stale target returns deterministic error envelope
- [ ] No LLM invocation in presence/delivery-error path
- [ ] Tests green
