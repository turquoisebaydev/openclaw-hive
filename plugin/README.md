# plugin/

OpenClaw extension package(s) for Hive integration.

## Goal

Keep Hive transport/control-plane concerns in external daemon/CLI, while using
an in-process OpenClaw plugin to surface low-latency runtime events.

## Planned package

- `hive-bridge` (name TBD)
  - Subscribes to OpenClaw runtime/observability event stream.
  - Maintains per-session live state (status, tool phase, model, token totals,
    compactions, errors, last activity).
  - Publishes:
    - retained session presence snapshots
    - non-retained real-time event stream
  - Keeps payloads redacted/summarized by default.

## Ownership boundaries

- Daemon/CLI own: MQTT transport, deterministic handlers, cross-gateway routing,
  retained cache behavior, ops lifecycle.
- Plugin owns: in-process event tap + low-latency publish of runtime deltas.
- Skills own: LLM behavior conventions for using hive flows.
