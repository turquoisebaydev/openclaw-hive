# AGENTS.md — openclaw-hive

Short repo instructions for Codex, Claude Code, and similar coding agents.

If you are Claude Code, also read `CLAUDE.md` for the longer architecture/testing notes.

## What this repo is

`openclaw-hive` is the deterministic coordination layer for the Turq cluster.

Main deliverables:
- `daemon/` — MQTT subscriber/router, heartbeats, deterministic presence cache
- `cli/` — `hive-cli` send/reply/status/roster flows
- `plugin/` — OpenClaw runtime bridge that publishes session presence + runtime events
- `skills/` — agent-facing behavior docs only, no transport logic

## Read first

Before changing behavior, read the minimum relevant docs:
- `docs/protocol.md`
- `docs/features/hive-plugin-bridge.md`
- `docs/features/session-presence-registry.md`
- newest file in `docs/tasks/`

## Current topic truth

Gateway bridge topics are currently server-grouped.

Gateway-published MQTT topics:
- presence: ``<topic_prefix>/presence/<server>/gw/<gateway>/<session>``
- events: ``<topic_prefix>/events/<server>/gw/<gateway>/<session>``

Examples:
- `turq/hive/presence/turq/gw/turq/...`
- `turq/hive/presence/turq/gw/mini1/...`
- `turq/hive/presence/pg/gw/pg1/...`
- `turq/hive/presence/turqette/gw/turqette/...`

Important: payload identity remains logical `gw/agent/session` even though the MQTT path is grouped by physical server.

## Current payload truth

Bridge payloads already support deterministic task/session metadata.

Useful fields already available from deterministic extraction:
- `task.summary`
- `task.activity`
- `task.cmdline`
- `task.cwd`
- `task.url`
- top-level `activityType`

`activityType` is deterministic and inferred without model calls.

## Non-negotiables

- No LLM calls from `daemon/`, `cli/`, or `plugin/`
- Keep routing/failure/presence logic deterministic
- Prefer focused changes over broad refactors
- Do not quietly change topic schemas without updating docs + tests together

## Validation

Prefer targeted validation first.

Useful commands:
```bash
cd plugin && npm test
cd daemon && .venv/bin/pytest tests/test_main.py tests/test_presence.py -q
cd daemon && .venv/bin/pytest tests/ -q
```

## Practical note

If you touch topic formats, update all three together:
- producer/topic builders
- consumer/parsing tests
- docs in `docs/features/`
