# openclaw-hive

Inter-gateway coordination layer for [OpenClaw](https://github.com/openclaw/openclaw) clusters.

Hive connects independent OpenClaw gateway instances via MQTT, enabling cross-gateway commands, event-driven file sync, sentinel monitoring, and machine-to-machine heartbeats — all without burning LLM tokens on deterministic operations.

## Why

Running multiple OpenClaw gateways creates coordination problems:

- **Heartbeat tax** — each gateway wakes its LLM every 30 minutes to check if anything happened. 90%+ of the time, nothing did. That's thousands of wasted tokens per day.
- **File divergence** — boot files, memory files, and configs drift across instances without a sync mechanism.
- **No cross-gateway communication** — gateways can't delegate work to each other.
- **Redundant monitoring** — every gateway independently checks the same things (email, calendar, services).

Hive solves these by putting a thin daemon on each box that handles coordination deterministically, and only wakes the LLM when something actually needs judgment.

## Architecture

```
  MQTT (hive topics)
       ↕
  [hive-daemon]              ← thin sidecar, one per box
       ↕              ↕
  system event    hive-cli
       ↕              ↕
  [OpenClaw instance(s)]
```

- **Inbound to OC:** daemon → `openclaw system event` → LLM wakes with specific context
- **Outbound from OC:** LLM → `hive-cli send` → daemon → MQTT
- **Deterministic work:** daemon → `hive-daemon.d/` handler scripts → no LLM involved

## Components

| Component | Purpose |
|-----------|---------|
| `daemon/` | Always-running sidecar — MQTT routing, heartbeats, handler dispatch |
| `cli/` | Stateless CLI — send commands, check status, reply to requests |
| `skills/` | OpenClaw skills — teach the LLM how to participate in the hive |
| `contrib/handlers/` | Example `hive-daemon.d/` scripts (git-sync, health-check, etc.) |
| `contrib/shared-memory/` | Git-based cluster memory pattern for multi-gateway deployments |

## The Sentinel Pattern

Instead of waking an LLM every 30 minutes to ask "anything happening?", hive uses zero-cost daemon scripts that monitor continuously and only wake the LLM when something needs a brain:

```
Before: every 30 min → wake LLM (18k tokens) → "anything?" → "no" → waste
After:  every 1-5 min → daemon script (0 tokens) → "no" → free
        ...until something actually happens → wake LLM with context
```

See [docs/protocol.md](docs/protocol.md) for the full design.

## Quick Start

*Coming soon — Phase 1 implementation in progress.*

## License

MIT
