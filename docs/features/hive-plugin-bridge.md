# Hive plugin bridge (OpenClaw extension)

## Summary

Add an optional OpenClaw plugin that bridges runtime observability into Hive MQTT.

This avoids core forks and keeps `openclaw-hive` transport decoupled from OpenClaw
runtime internals.

## Why this exists

Current presence publication is deterministic but periodic (runtime API polling).
The plugin adds low-latency event-driven updates for better routing decisions and
live visibility.

## Architecture

- `daemon/` + `cli/` remain external control-plane components.
- `plugin/` provides in-process runtime event ingestion.
- `skills/` remains agent behavior/prompt layer.

## Data products

### 1) Retained session presence (snapshot)

Topic pattern:

- `turq/hive/presence/<gw>/<agent>/<session>`

Suggested fields (v2):

- identity: `gw`, `agent`, `session`
- channel: `provider`, `chatType`, `chatId` (if available)
- model/thinking: active model metadata
- status: `idle|queued|running|llm|tool|stuck|error`
- busy flag + live phase
- token totals (rolling per run/session where available)
- context: `used/max` (from runtime snapshot where available)
- compactions: per-attempt and rolling counters
- last error summary + last activity timestamp
- ttl and updated timestamp

### 2) Non-retained events (timeline)

Topic pattern:

- `turq/hive/events/<gw>/<agent>/<session>`

Payload (sanitized) from OpenClaw observability event bus:

- domain/event/phase
- runId/sessionKey/agentId
- status/duration/error
- data summaries (tool/model/token/compaction as available)

## Event source expectations

OpenClaw observability already emits key runtime domains:

- `session` (state/stuck)
- `run` (attempt lifecycle)
- `llm` (call start/end/error + usage + compactionCount)
- `tool` (call start/update/end/error)
- `queue` (enqueue/dequeue lane metrics)

## Config posture

- observability events enabled
- capture depth at summary by default
- redaction enabled

## Non-goals

- No blind raw prompt/tool payload forwarding.
- No replacement of daemon/CLI transport responsibilities.
- No OpenClaw core patch dependency for MQTT sink behavior.

## Rollout plan

1. Scaffold plugin package in `plugin/`.
2. Implement event listener + session-state aggregator.
3. Publish to MQTT with debounce/coalescing for retained presence.
4. Keep daemon polling as periodic reconciler/fallback.
5. Add integration tests and sample dashboard consumer.
