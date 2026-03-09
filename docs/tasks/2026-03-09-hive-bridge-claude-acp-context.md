# Task context — Claude ACP run: Hive plugin bridge scaffold

Date: 2026-03-09
Owner: Hugh/Turq
Repo: `openclaw-hive`

## Objective

Implement the first pass of a plugin-based OpenClaw runtime bridge so Hive can
publish low-latency session presence and real-time event telemetry without
modifying OpenClaw core.

## Constraints

- Prefer extension package over OC core changes.
- Keep daemon/CLI as external transport/control plane.
- Keep skills as behavior layer (no transport logic in skills).
- Maintain deterministic routing behavior.
- Default to redacted/summarized telemetry payloads.

## Current repo decision

`openclaw-hive` now formally targets 3 artifacts:

1. `daemon/` + `cli/`
2. `plugin/` (new extension package area)
3. `skills/`

## Proposed plugin scope (phase 1)

### Inputs

- OpenClaw runtime observability event stream (`session`, `run`, `llm`, `tool`, `queue`).

### Outputs

1) Retained presence snapshots
- topic: `turq/hive/presence/<gw>/<agent>/<session>`

2) Non-retained events stream
- topic: `turq/hive/events/<gw>/<agent>/<session>`

### Presence fields to include

- `tool` state/phase/name (where active)
- token usage totals (input/output/total/cache)
- context `used/max`
- compaction counts
- model
- status + busy + lastError + updatedTs

## Expected implementation deliverables

1. plugin package scaffold in `plugin/` (manifest, index, config schema)
2. event listener + in-memory session live-state reducer
3. MQTT publisher abstraction + topic mapping
4. retained presence coalescing/debounce
5. tests for reducer/topic mapping/payload shape
6. docs update with config and rollout notes

## Open questions for the run

1. Plugin publishes MQTT directly vs local IPC to daemon only?
   - preferred initial: direct publish (simpler, lower latency)
2. How to source context `used/max` when not present in each event?
   - likely merge runtime snapshot poll into reducer on interval
3. Presence schema versioning
   - recommend `v: 2` for enriched presence payload

## Definition of done (phase 1)

- Plugin builds and loads via OpenClaw plugin mechanism.
- Emits testable event messages on local broker.
- Updates retained presence on live run/tool/llm transitions.
- No raw sensitive payload leakage at default settings.
- Existing daemon/CLI behavior remains unchanged.
