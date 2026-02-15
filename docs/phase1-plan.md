# Phase 1 Plan — Proof of Concept

**Goal:** Two hive daemons on separate machines can exchange messages, dispatch handlers, and inject system events into local OpenClaw instances.

## Milestone 1: Message Envelope & Core Models

**Tasks:**
- [x] Define `Envelope` dataclass with all fields (v, id, ts, from, to, ch, urgency, corr, replyTo, ttl, text, action)
- [x] JSON serialization/deserialization with validation
- [x] Factory method for creating new envelopes (auto-generate id, ts)
- [x] Factory method for creating reply envelopes (auto-set corr, replyTo)
- [x] Unit tests for all envelope operations

**Verification:** `pytest tests/test_envelope.py` passes

## Milestone 2: Daemon MQTT Subscriber

**Tasks:**
- [x] Config model (TOML): node identity, MQTT broker, topic prefix, OC instances, handler dir
- [x] MQTT connection with auto-reconnect
- [x] Subscribe to `{prefix}/{mynode}/+` and `{prefix}/all/+`
- [x] Parse inbound messages into Envelope objects
- [x] Route by channel to appropriate handler (router module)
- [x] Logging throughout (structured, not print statements)
- [x] Unit tests with mocked MQTT client

**Verification:** Daemon starts, connects to MQTT, logs received messages

## Milestone 3: Handler Dispatch

**Tasks:**
- [x] Discover handlers from `hive-daemon.d/` directory on startup
- [x] Publish discovered handlers in roster
- [x] Dispatch: pipe envelope JSON to handler stdin, capture stdout/stderr/exit code
- [x] Timeout handling (kill handler after configurable timeout)
- [x] Publish result to meta channel (`{prefix}/meta/{node}/{action}`)
- [x] On failure: publish alert to sender
- [x] On missing handler: escalate to OC via system event
- [x] Unit tests with mock handler scripts

**Verification:** Create a test handler script, send an action message, verify result published to meta topic

## Milestone 4: Machine-to-Machine Heartbeat

**Tasks:**
- [x] Publish heartbeat on configurable interval (default 5s)
- [x] Heartbeat payload: node identity, uptime, load, OC instance status
- [x] Subscribe to cluster heartbeats, track known nodes
- [x] Detect missed heartbeats (configurable threshold, default 3)
- [x] On missed heartbeats: publish alert
- [x] Publish node state to meta channel (retained)
- [x] Unit tests for heartbeat tracking and failure detection

**Verification:** Start two daemons, verify they discover each other via heartbeats, simulate failure by stopping one

## Milestone 5: OC Bridge (System Event Injection)

**Tasks:**
- [x] Config: list of local OC instances with profile/port
- [x] Function to inject system event: `openclaw [--profile X] system event --text "..."`
- [x] Route commands and alerts through bridge
- [x] Include hive metadata in injected text (from, corr, channel context)
- [x] Handle OC not running gracefully (log error, don't crash)
- [x] Unit tests with mocked subprocess

**Verification:** Send a command via MQTT, verify it arrives as system event text in OC

## Milestone 6: CLI (hive-cli)

**Tasks:**
- [x] `hive-cli send` — publish envelope to MQTT topic
- [x] `hive-cli reply` — publish response with correlation
- [x] `hive-cli status` — read meta/{node}/state topics, display cluster overview
- [x] `hive-cli roster` — read meta/{node}/roster topics, show capabilities
- [x] Config: MQTT broker, node identity, topic prefix (shared with daemon or standalone)
- [x] Unit tests for envelope construction and argument parsing

**Verification:** `hive-cli send --to all --ch command --text "hello"` publishes correctly; `hive-cli status` shows known nodes

## Milestone 7: Integration Test

**Tasks:**
- [ ] Start two daemons (different node identities) against same MQTT broker
- [ ] Send command from node A to node B via hive-cli
- [ ] Verify node B's daemon receives, dispatches handler (or escalates to OC bridge)
- [ ] Verify response flows back to node A
- [ ] Verify sync event triggers handler on receiving node
- [ ] Verify heartbeat discovery and failure alerting

**Verification:** End-to-end flow works with real MQTT broker (Mosquitto in Docker or local)

## Milestone 8: Example Handlers + Service Units

**Tasks:**
- [ ] `contrib/handlers/git-sync` — pull across configured workspaces
- [ ] `contrib/handlers/health-check` — check OC instance(s) + system metrics
- [ ] `systemd/hive-daemon.service` — example unit file
- [ ] `launchd/ai.hive.daemon.plist` — example plist

**Verification:** Handlers execute correctly when triggered via hive command

## Out of Scope (Phase 2+)

- OC skills (hive-master, hive-member) — needs real OC integration testing
- Sentinel monitoring handlers (check-email, check-calendar)
- Topic ACLs in Mosquitto
- Web dashboard / status UI
- Correlation timeout tracking
- Message deduplication
