# Phase 1 Plan — Core Daemon + CLI + Skills

**Goal:** Two hive daemons on separate machines can exchange messages, dispatch handlers, inject system events into local OpenClaw instances, and provide full protocol awareness to OC agents via skills.

**Status:** ✅ Complete (2026-02-16). 127 tests passing. Live on turq + pg1.

## Milestone 1: Message Envelope & Core Models ✅

- [x] `Envelope` dataclass with all fields (v, id, ts, from, to, ch, urgency, corr, replyTo, ttl, text, action)
- [x] JSON serialization/deserialization with validation
- [x] `create_envelope()` factory (auto-generate id, ts)
- [x] `create_reply()` factory (auto-set corr, replyTo from original)
- [x] Unit tests for all envelope operations

## Milestone 2: Daemon MQTT Subscriber ✅

- [x] Config model (TOML): node identity, MQTT broker, topic prefix, OC instances, handler dir, heartbeat settings
- [x] MQTT connection with auto-reconnect
- [x] Subscribe to `{prefix}/{mynode}/+`, `{prefix}/all/+`, and `{prefix}/+/command`
- [x] Parse inbound messages into Envelope objects
- [x] Route by channel to appropriate handler (router module)

## Milestone 3: Handler Dispatch ✅

- [x] Discover handlers from `hive-daemon.d/` directory on startup
- [x] Dispatch: pipe envelope JSON to handler stdin, capture stdout/stderr/exit code
- [x] Timeout handling (kill handler after configurable timeout)
- [x] On missing handler: escalate to OC via system event

## Milestone 4: Machine-to-Machine Heartbeat ✅

- [x] Publish heartbeat on configurable interval (default 5s)
- [x] Heartbeat payload: node identity, uptime, load, OC instance status
- [x] Track known peers, detect missed heartbeats (configurable threshold)
- [x] On missed heartbeats: alert callback (OC bridge injection)
- [x] Publish node state to meta channel (retained)

## Milestone 5: OC Bridge (System Event Injection) ✅

- [x] Config: list of local OC instances with profile/port
- [x] Inject system events via `openclaw [--profile X] system event --text "..."`
- [x] Route commands and alerts through bridge (alerts with URGENT prefix)
- [x] Include hive metadata in injected text `[hive:from->to ch:channel]`
- [x] Handle OC not running gracefully (log error, don't crash)

## Milestone 6: CLI (hive-cli) ✅

- [x] `hive-cli send` — publish envelope to MQTT topic
- [x] `hive-cli send --wait N` — synchronous send, block for correlated response
- [x] `hive-cli reply` — publish response with correlation (from JSON string or file)
- [x] `hive-cli status` — read meta/state retained messages, display cluster overview
- [x] `hive-cli roster` — read meta/roster retained messages, show capabilities

## Milestone 7: Correlation ✅

- [x] CLI `--wait`: subscribe to response topic, filter by `corr` match, return envelope JSON
- [x] Daemon `CorrelationStore`: track outbound commands (from `+/command` subscription)
- [x] Response enrichment: prepend original command text to OC system event
- [x] 1-hour TTL with automatic pruning

## Milestone 8: OC Skills ✅

- [x] `hive-member` SKILL.md: full protocol reference — envelope schema, channels, correlation IDs, urgency, action dispatch, CLI usage, response patterns
- [x] `hive-master` SKILL.md: coordinator skill — delegation, fan-out, `--wait` vs fire-and-forget, correlation tracking, roster awareness, escalation rules, action vs command decision framework

## Milestone 9: Example Handlers + Service Units ✅

- [x] `contrib/handlers/echo` — echo envelope text back
- [x] `contrib/handlers/ping` — health-check, returns pong
- [x] `contrib/handlers/git-sync` — pull across configured workspaces
- [x] `contrib/handlers/health-check` — check system metrics
- [x] `systemd/openclaw-hive-daemon.service` — Linux service unit template
- [x] `launchd/ai.openclaw.hive-daemon.plist` — macOS launchd plist template

## Live Verification (2026-02-16)

- [x] turq daemon running (Mac mini, `~/Projects/openclaw-hive/`)
- [x] pg1 daemon running (<host>, `$HOME/openclaw-hive/`)
- [x] Mutual heartbeat discovery (turq ↔ pg1 within seconds)
- [x] turq → pg1 command: routed through OC bridge, injected into pg1 OpenClaw ✅
- [x] pg1 → turq command: received by turq daemon, injected into turq OpenClaw ✅
- [x] `--wait` synchronous round-trip: send, block, receive matched response ✅
- [x] Daemon correlation enrichment: async response includes original command text ✅
- [x] `hive-cli status` shows both nodes online ✅
- [x] 127 tests passing (105 daemon + 22 CLI)
