# `plugin/`

OpenClaw extension package for the Hive runtime bridge.

## Package

- `hive-bridge`
  - subscribes to OpenClaw runtime observability events
  - maintains per-session in-memory live state
  - publishes retained presence snapshots to `turq/hive/presence/...`
  - publishes non-retained event summaries to `turq/hive/events/...`
  - redacts/truncates sensitive fields by default

## Layout

- `openclaw.plugin.json` — plugin manifest with inline JSON Schema
- `package.json` — OpenClaw extension pack metadata
- `index.js` — plugin entrypoint + background service registration
- `src/config-schema.js` — shared config schema object
- `src/config.js` — runtime config normalization
- `src/extract.js` — runtime event field extraction helpers
- `src/reducer.js` — in-memory session state reducer
- `src/sanitizer.js` — redacted event/presence payload shaping
- `src/publisher.js` — MQTT publish abstraction
- `src/listener.js` — event subscription + debounce/coalescing bridge
- `tests/` — focused unit tests for schema, reducer, publisher, listener, and sanitization

## Runtime contract

The bridge listens to OpenClaw observability domains described in
`docs/tasks/2026-03-09-hive-bridge-claude-acp-context.md` and
`docs/features/hive-plugin-bridge.md`:

- `session`
- `run`
- `llm`
- `tool`
- `queue`

From those events it emits two Hive data products:

- retained presence snapshots on `turq/hive/presence/<server>/gw/<gateway>/<session>`
- non-retained runtime event summaries on `turq/hive/events/<server>/gw/<gateway>/<session>`

Gateway identity inside the payload remains `gw/agent/session`; only the MQTT path
is grouped by server (`pg`, `turq`, `turqette`) and nested under `gw/`.

Presence payloads use the enriched `v: 2` schema and include live status, busy
state, phase, model, token totals, context usage, compaction counts, optional
channel/task metadata, deterministic `activityType`, and the latest redacted
error summary.

`activityType` is inferred without model calls using explicit event fields first,
then session/agent identity patterns (`codex`, `claude`, `acp`, `local-llm`,
fallback `gw`).

## Config shape

Configure under `plugins.entries.hive-bridge.config`:

```json5
{
  plugins: {
    entries: {
      "hive-bridge": {
        enabled: true,
        config: {
          topicPrefix: "turq/hive",
          mqtt: {
            url: "mqtt://127.0.0.1:1883",
            clientIdPrefix: "hive-bridge"
          },
          identity: {
            gatewayId: "turq",
            agentId: "main"
          },
          presence: {
            ttlSec: 300,
            debounceMs: 750,
            maxDelayMs: 5000,
            snapshotRefreshSec: 60,
            publishTaskDetails: true
          },
          events: {
            enabled: true,
            redact: true,
            includeQueue: true,
            summaryMaxLength: 160
          }
        }
      }
    }
  }
}
```

## Rollout notes

- Enable OpenClaw observability events before enabling the plugin.
- Start with `events.redact = true` and only disable it for local debugging.
- Use direct MQTT publish from the plugin for lower latency; keep daemon polling
  as the periodic reconciler/fallback described in the feature brief.
- Presence snapshots are retained and published with MQTT expiry via `ttlSec`.
- The bridge seeds v2 presence from `openclaw sessions --json` on startup and refreshes it on `snapshotRefreshSec`, so idle sessions still publish model/thinking/context fields.
- Set `presence.publishTaskDetails = false` if session task/cwd/cmdline metadata
  should be omitted entirely.
- Event payloads stay summary-only by default; raw prompts/tool payloads are not
  forwarded.
- Gateway bridge topics are grouped by physical server and publish to
  `<topicPrefix>/{presence|events}/<server>/gw/<gateway>/<session>`.
- The bridge stays idle if both outputs are disabled, and it skips MQTT connect if no
  runtime observability source is available.

## Tests

```bash
cd plugin
npm test
```
