# Feature Spec — Hive Session Presence Registry + Deterministic Session Delivery

## Status
Draft (implementation-ready)

## Why
Hive currently coordinates node-level messaging but lacks a cluster-wide, fresh registry of active gateway/agent/sessions. Operators need to target specific sessions reliably and receive deterministic failure responses when targets are stale/unavailable.

## Goals
1. Publish a cluster-wide list of active `gw/agent/session` identities.
2. Include lightweight status + current task summary metadata.
3. Use TTL-expiring presence so stale data naturally disappears.
4. Support session-targeted delivery from `hive-cli`.
5. Ensure stale/missing target delivery failures are handled **deterministically** (no LLM path).

---

## Hard Requirements (Determinism)
The following must be daemon-only deterministic code paths:

- Presence/heartbeat collection and publication
- Presence cache updates and TTL expiry/pruning
- Session target resolution (`gw/agent/session`)
- Delivery failure generation for stale/missing/unreachable targets

No model invocation for the above.

---

## Identity Model
Canonical target identity:
- `gw`: gateway/node id (e.g. `turq`, `pg1`, `turqette`, `mini1`)
- `agent`: local agent id (`main`, etc.)
- `session`: OpenClaw session key (or stable unique id)

Composite key:
`<gw>/<agent>/<session>`

Optional shorthand (CLI convenience):
`<gw>/<session>` if agent can be uniquely inferred.

---

## Presence Topics & Payload

### MQTT Topic
`<topic_prefix>/presence/<gw>/<agent>/<session>`

Example:
`turq/hive/presence/pg1/main/agent:dev:discord:channel:147...`

### Payload
```json
{
  "v": 1,
  "kind": "session_presence",
  "gw": "pg1",
  "agent": "main",
  "session": "agent:dev:discord:channel:147...",
  "state": "active",
  "status": "idle",
  "task": {
    "summary": "Investigating VRAM OOM in qmd",
    "cmdline": "qmd --index index vsearch ...",
    "cwd": "/home/turq/clawd-pg1",
    "url": "https://github.com/..."
  },
  "updatedTs": 1772665000,
  "ttlSec": 300
}
```

### TTL
Use MQTT message expiry = `ttlSec` (default 300 seconds).

---

## Local Presence Cache (Per Daemon)
Each daemon maintains a cache of remote/local presence entries keyed by `<gw>/<agent>/<session>`:
- last payload
- last seen ts
- expiry ts

Cache behavior:
- update on valid presence message
- prune expired entries
- ignore malformed/invalid presence messages (warn)

---

## CLI / Routing Changes

### New target mode
Add session-specific routing options to `hive-cli send`:
- `--to-session <gw>/<agent>/<session>`
- or split form: `--to-gw`, `--to-agent`, `--to-session-id`

### Resolution
When session-targeted send is requested:
1. Resolve fresh presence entry from deterministic cache.
2. If found and fresh: deliver.
3. Else: emit deterministic error response (below).

---

## Deterministic Failure Response Contract
If target is stale/missing/unreachable, sender gets immediate deterministic response envelope:

```json
{
  "v": 1,
  "kind": "delivery_error",
  "code": "SESSION_NOT_FOUND",
  "target": {
    "gw": "pg1",
    "agent": "main",
    "session": "..."
  },
  "detail": "No fresh presence record within ttl window",
  "corr": "<original corr>",
  "replyTo": "<original id>"
}
```

Error codes:
- `SESSION_NOT_FOUND`
- `SESSION_STALE`
- `SESSION_UNREACHABLE`
- `AGENT_MISMATCH`
- `TARGET_AMBIGUOUS`

Must be generated in daemon logic only (no LLM fallback).

---

## Task Summary Collection
Task metadata should be best-effort and deterministic:
1. active deterministic handler/action context (if running)
2. current oc-bridge request metadata (cmdline/cwd/url if available)
3. fallback to last known summary

Privacy/safety:
- no secrets/tokens/full prompts in task summary
- truncate long values

---

## Config Additions
```toml
[presence]
enabled = true
interval_sec = 30
ttl_sec = 300
retain = true
publish_task_details = true

[presence.discovery]
accept_remote = true
prune_stale = true
```

Defaults:
- enabled true for cluster ops
- ttl_sec 300
- deterministic behavior mandatory regardless of toggles

---

## Acceptance Criteria
1. Each node publishes session presence records with 5-minute TTL.
2. Remote caches prune stale entries deterministically.
3. `hive-cli` can target a specific session via `gw/agent/session`.
4. Missing/stale targets produce deterministic delivery_error response (no model call).
5. Presence/task summary collection remains deterministic.
6. Cluster continues normal command/response behavior when presence disabled.

---

## Test Plan
- Unit tests:
  - presence payload validation + keying
  - cache insert/update/prune with ttl
  - deterministic resolver outcomes
  - deterministic error envelope generation
- Integration tests:
  - publish/consume presence across two daemon instances
  - targeted send success with fresh session
  - stale target returns deterministic failure
- Regression:
  - existing non-session routing unchanged
  - heartbeat channel behavior unchanged

---

## Rollout Plan
1. Implement behind config flags, default enabled in dev only.
2. Deploy to turq + pg1, observe cache churn and payload size.
3. Enable session-target send in cli.
4. Deploy to turqette/mini1.
5. Promote to default-on after stability window.
