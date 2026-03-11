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
- `tests/` — focused unit tests for schema, reducer, publisher, and listener

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
            maxDelayMs: 5000
          },
          events: {
            redact: true,
            summaryMaxLength: 160
          }
        }
      }
    }
  }
}
```

## Notes

- MQTT publishes happen directly from the plugin for lower latency.
- Presence snapshots are retained and published with MQTT expiry.
- Event payloads are summary-only unless redaction is disabled explicitly.

## Tests

```bash
cd plugin
npm test
```
