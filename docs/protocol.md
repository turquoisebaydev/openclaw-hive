# Hive Protocol — Inter-Gateway Coordination for OpenClaw Clusters

**Status:** Design (2026-02-15)
**Authors:** Hugh + Turq

## Overview

Hive is a non-deterministic coordination layer for independent OpenClaw gateway instances. Rather than building deterministic cluster protocols (leader election, shared state stores), hive uses MQTT as a message bus with a thin local daemon on each box that bridges between the hive and local OC instances.

Each gateway remains fully independent. Coordination happens through natural-language messages interpreted by LLM agents when needed, and handled deterministically by the hive service when not.

## Core Principles

1. **Independent gateways, shared bus** — no single point of failure, no leader
2. **Token gatekeeper** — the hive service decides what needs LLM vs what doesn't
3. **OC stays decoupled** — receives system events, replies via a hive tool/script. OC doesn't know MQTT exists for hive purposes
4. **LLM as interpreter, not router** — deterministic routing by the hive service, LLM only for judgment calls
5. **Scales by subscription** — add a node, subscribe to topics, immediately part of the hive

## Architecture

```
  MQTT (hive topics)
       ↕
  [hive-service daemon]          ← thin local daemon, one per box
       ↕                    ↕
  system event (in)    hive-reply tool (out)
       ↕                    ↕
  [OpenClaw instance(s)]
```

- **Inbound to OC:** hive service → `openclaw system event --text "..."` into local OC instance(s)
- **Outbound from OC:** OC calls `hive-reply` tool/script → hive service → MQTT
- **Local actions:** hive service handles deterministic tasks (git sync, health, file ops) without involving OC at all

## Message Envelope

```json
{
  "v": 1,
  "id": "uuid",
  "ts": 1771143000,
  "from": "turq-18789",
  "to": "pg1-18890",
  "ch": "command",
  "urgency": "now",
  "corr": null,
  "replyTo": null,
  "ttl": 3600,
  "text": "Generate meural images for 2026-02-16"
}
```

### Field Reference

| Field | Type | Required | Purpose |
|-------|------|----------|---------|
| `v` | int | yes | Schema version (currently `1`) |
| `id` | string | yes | Unique message ID (UUID) |
| `ts` | int | yes | Unix epoch seconds when sent |
| `from` | string | yes | Sender node identity (`<name>-<port>`) |
| `to` | string | yes | Target: specific node (`pg1-18890`) or `all` |
| `ch` | string | yes | Logical channel (see below) |
| `urgency` | string | yes | `now` (wake agent) or `later` (queue for heartbeat) |
| `corr` | string | no | Correlation ID for request/reply flows |
| `replyTo` | string | no | Message `id` this is responding to |
| `ttl` | int | no | Seconds until message expires (0 = no expiry) |
| `text` | string | yes | Payload — natural language or structured content |

## Logical Channels (`ch`)

| Channel | Purpose | Typical urgency | LLM needed? |
|---------|---------|-----------------|-------------|
| `command` | Do something ("run job", "restart service") | `now` or `later` | Usually yes |
| `response` | Reply to a command (carries `replyTo` + `corr`) | `now` | Rarely |
| `sync` | File/memory sync notifications ("MEMORY.md updated") | `later` | No (handle locally) |
| `heartbeat` | Node aliveness ("pg1 alive, load 0.3, 4 jobs ok") | `later` | Never |
| `status` | Node state changes ("going down", "back online") | `now` or `later` | Sometimes |
| `alert` | Escalation ("disk 90%", "job failed 3x") | `now` (always) | Yes |

## MQTT Topic Structure

```
turq/hive/{to}/{ch}
```

Examples:
- `turq/hive/pg1-18890/command` — command addressed to pg1
- `turq/hive/all/sync` — cluster-wide sync notification
- `turq/hive/all/heartbeat` — cluster-wide heartbeat
- `turq/hive/turq-18789/response` — response addressed to turq

### Routing Matrix

| | Point-to-point | Cluster-wide |
|---|---|---|
| **Read now** | `turq/hive/<node>/command` | `turq/hive/all/alert` |
| **Read later** | `turq/hive/<node>/sync` | `turq/hive/all/sync` |

### Subscription Pattern

Each hive service subscribes to:
- `turq/hive/<mynode>/+` — messages addressed to this node
- `turq/hive/all/+` — cluster-wide broadcasts

## Hive Service Routing Rules

The hive service on each box receives all messages and routes by channel:

| Channel | Action |
|---------|--------|
| `heartbeat` | Handle locally. Never touch OC. Respond with own heartbeat. |
| `sync` | Handle locally (git pull, file copy). Escalate to OC only on failure. |
| `command` | Check `urgency`. Inject system event into appropriate local OC instance. |
| `response` | Match `corr`/`replyTo`. Deliver to OC if it's waiting, otherwise log. |
| `status` | Log locally. If `urgency=now`, inject system event. |
| `alert` | Always wake OC immediately (system event), regardless of `urgency` field. |

### What Gets Injected into OC

Only `command`, `alert`, and failed `sync`/`status` — everything else stays in the hive service.

System event text includes metadata naturally:
> "Hive command [corr:abc123] from turq-18789: Generate meural images. Response expected."

### What OC Sends Back

OC calls a `hive-reply` tool or script:
```bash
hive-reply --id abc123 --text "Done. 6 images uploaded to Meural."
```

The hive service publishes the response on MQTT with the correlation ID to `turq/hive/<original-sender>/response`.

## Hive Heartbeat (Machine-to-Machine)

Separate from OC heartbeats. This is the hive service checking on other hive services.

- **Interval:** 5 seconds (configurable)
- **No LLM involvement** — pure hive service daemon
- **Payload:** node identity, uptime, load, local OC instance status, job counts
- **Failure detection:** 3 missed heartbeats → publish `alert` to cluster
- **Topic:** `turq/hive/all/heartbeat` (retained per-node for latest state)

## Sync Channel — Event-Driven File Propagation

Replaces (or supplements) the current 5-minute git sync timer:

1. Writer node commits + pushes to git
2. Writer's hive service publishes: `ch: sync`, `text: "MEMORY.md updated, commit abc1234"`
3. Other nodes' hive services receive → `git pull` immediately
4. No LLM tokens burned

Retained messages on sync topics so offline nodes catch up on reconnect.

## Correlation / Request-Reply Pattern

For commands that expect a response:

1. Sender generates `corr: "abc123"` and sets `replyTo: null`
2. Receiver processes, then sends response with `corr: "abc123"` and `replyTo: "<original-msg-id>"`
3. Sender's hive service matches by `corr` and delivers to the waiting context

OC can choose not to respond — the correlation is optional guidance, not a contract.

## Trust & Security

- Hive topics are internal-only (LAN MQTT, no internet exposure)
- Sender identity (`from`) is trusted within the cluster
- Public MQTT traffic (e.g., /turq avatar chat) uses completely separate topics
- Hive service never processes messages from unknown senders
- No secrets in hive messages — use references ("sync from git", not file contents)

## Tooling

### `hive-cli` — Stateless command-line tool

Called by OC agents (via skills) or humans. Does one thing and exits.

```bash
# Send a command to a specific node
hive-cli send --to pg1-18890 --ch command --text "generate meural images" --expect-reply

# Reply to a correlated request
hive-cli reply --corr abc123 --text "done, 6 images uploaded"

# Cluster status
hive-cli status

# Show known nodes and roles
hive-cli roster
```

- Bundled with the OC skills (hive-master / hive-member)
- SKILL.md tells OC to call this
- Talks to MQTT directly or via hive-daemon's local socket
- Generates envelope (id, ts, from, correlation IDs) automatically

### `hive-daemon` — Always-running sidecar

One per physical box. Manages all MQTT subscriptions and routing. **The core daemon is generic and open-sourceable — all custom logic lives in pluggable handler scripts.**

**Responsibilities:**
- Subscribe to `turq/hive/<mynode>/+` and `turq/hive/all/+`
- Route inbound: pluggable handler vs `openclaw system event` into local OC instance(s)
- Machine-to-machine heartbeats (no LLM)
- Dispatch deterministic actions to `hive-daemon.d/` handler scripts
- Correlation tracking (outstanding requests, timeouts) via MQTT meta channel
- Expose local status for `hive-cli status` to read
- Multi-instance aware (turq box has turq-18789 + mini1-18889)
- systemd (Linux) / launchd (macOS) managed

**The boundary:**
- OC only ever touches `hive-cli`
- `hive-daemon` only ever touches OC via `openclaw system event`
- They share MQTT as transport
- Daemon may also expose a local Unix socket for `hive-cli` (faster than MQTT roundtrip for status queries)

### `hive-daemon.d/` — Pluggable action handlers

The daemon does **not** hardcode any actions. All deterministic action handling is delegated to executable scripts in a `hive-daemon.d/` directory.

```
hive-daemon.d/
  git-sync            ← handles action: "git-sync"
  health-check        ← handles action: "health-check"
  restart-oc          ← handles action: "restart-oc"
  deploy              ← handles action: "deploy"
```

**Handler contract:**
- Filename = action name (`"action": "git-sync"` dispatches to `hive-daemon.d/git-sync`)
- Must be executable (`chmod +x`)
- Receives the full message envelope as JSON on **stdin**
- Exit code: `0` = success, non-zero = failure
- **stdout**: JSON result object (published to meta channel by the daemon)
- **stderr**: error detail (included in escalation alert if handler fails)
- Handlers can be any language — bash, Python, Node, compiled binary

**Dispatch flow:**
1. Message arrives with `"action": "git-sync"`
2. Daemon looks for `hive-daemon.d/git-sync`
3. Found → pipe envelope to handler → collect result → publish to `turq/hive/meta/<node>/<action>`
4. Not found → escalate to OC via system event: "Received action 'git-sync' but no handler installed"
5. Handler exits non-zero → publish error to meta + escalate as alert if configured

**Example handler — `git-sync`:**
```bash
#!/usr/bin/env bash
# hive-daemon.d/git-sync — pull latest changes across local workspaces
set -euo pipefail

INPUT=$(cat)
COMMIT=$(echo "$INPUT" | jq -r '.commit // empty')

RESULTS=()
for ws in /home/turq/clawd-pg1 /home/turq/clawd; do
  cd "$ws"
  git pull --ff-only origin main 2>&1
  CURRENT=$(git rev-parse --short HEAD)
  RESULTS+=("{\"path\":\"$ws\",\"commit\":\"$CURRENT\",\"status\":\"ok\"}")
done

echo "{\"workspaces\":[$(IFS=,; echo "${RESULTS[*]}")]}"
```

**Example handler result (stdout):**
```json
{
  "workspaces": [
    {"path": "/home/turq/clawd-pg1", "commit": "3acaca5", "status": "ok"},
    {"path": "/home/turq/clawd", "commit": "3acaca5", "status": "ok"}
  ]
}
```

**Unknown actions** (no matching handler script):
- Daemon escalates to OC via system event with full message context
- OC can decide whether to handle it, ignore it, or ask the user
- This means new action types work immediately — even before a handler exists, the LLM can improvise

## OC Skills

Two skills provide the LLM-side intelligence for hive participation:

### `hive-member` (on every node)

- How to interpret inbound hive system events
- How to respond using `hive-cli reply`
- Correlation ID handling
- Channel semantics (what command/sync/alert mean)
- "I received a command, here's how I participate"

### `hive-master` (on coordinator node — turq)

- How to delegate work using `hive-cli send`
- Cluster roster — which nodes exist, their roles, capabilities, channels
- Correlation tracking — what's outstanding, what timed out
- Batch orchestration ("send to all, collect responses")
- Escalation when members don't respond
- "I'm orchestrating work across the cluster"

**Skill loading:**
- turq: both `hive-master` and `hive-member` available
- pg1, mini1: `hive-member` only
- Skills only loaded when a hive event arrives — zero token cost on normal turns
- Hive-daemon includes a hint in the system event text: "Use the hive skill for protocol details"

## Meta Channel — Distributed State on MQTT

Retained messages on meta topics form the cluster's distributed state store. No database required.

### Topic Structure

```
turq/hive/meta/{node}/state         ← node health/status (retained, updated by daemon)
turq/hive/meta/{node}/roster        ← node identity/capabilities (retained, published at startup)
turq/hive/meta/{node}/{action}      ← latest result of deterministic action (retained)
turq/hive/meta/corr/{corr-id}       ← correlation tracking (retained, cleared on completion)
```

### Node State (`meta/{node}/state`)

Published by each daemon continuously (heartbeat interval). Retained.

```json
{
  "node": "pg1-18890",
  "status": "online",
  "since": 1771143000,
  "load": 0.3,
  "oc_instances": ["pg1-18890"],
  "channels": ["telegram", "mqtt"],
  "jobs_active": 2,
  "last_heartbeat": 1771143500
}
```

### Roster (`meta/{node}/roster`)

Published once at daemon startup. Retained. Updated on config change.

```json
{
  "node": "pg1-18890",
  "role": "member",
  "capabilities": ["image-gen", "heavy-compute"],
  "models": ["sonnet-4.5", "g3flash"],
  "handlers": ["git-sync", "health-check", "restart-oc"]
}
```

The `handlers` field is auto-discovered from `hive-daemon.d/` contents — tells the cluster what actions this node can handle.

### Action Results (`meta/{node}/{action}`)

Published by daemon after each handler execution. Retained (latest result only).

```json
{
  "node": "pg1-18890",
  "action": "git-sync",
  "trigger_id": "sync-001",
  "status": "ok",
  "ts": 1771143500,
  "result": {
    "workspaces": [
      {"path": "/home/turq/clawd-pg1", "commit": "3acaca5", "status": "ok"},
      {"path": "/home/turq/clawd", "commit": "3acaca5", "status": "ok"}
    ]
  }
}
```

On failure, `status: "error"` with an `error` field. Daemon decides whether to escalate to alert based on action config.

### Correlation Tracking (`meta/corr/{corr-id}`)

Published by sender on request, updated by receiver during processing.

```json
{
  "corr": "abc123",
  "from": "turq-18789",
  "to": "pg1-18890",
  "ch": "command",
  "text": "generate meural images",
  "sent_at": 1771143000,
  "status": "pending",
  "ttl": 3600
}
```

Lifecycle: `pending` → `in_progress` → `completed` or `error` → sender clears the retained message.

### What `hive-cli` reads from meta

- `hive-cli status` → reads `turq/hive/meta/+/state` — cluster overview
- `hive-cli roster` → reads `turq/hive/meta/+/roster` — capabilities and handlers
- `hive-cli pending` → reads `turq/hive/meta/corr/+` — outstanding requests
- `hive-cli sync-status` → reads `turq/hive/meta/+/git-sync` — who's in sync

## Deterministic Action Patterns

### Escalation Rules

Every handler result is evaluated by the daemon:
- Exit 0 + valid JSON → publish to meta, done
- Exit non-zero → publish error to meta + publish `alert` to sender (or `all`)
- Handler not found → escalate to local OC via system event (LLM improvises)
- Handler timeout → kill, publish error, escalate

### Sync Flow (end-to-end example)

1. turq commits + pushes MEMORY.md to git
2. turq's daemon (or OC via `hive-cli`): publishes `ch: sync, action: git-sync, commit: 3acaca5`
3. Each receiving daemon: matches `action: git-sync` → runs `hive-daemon.d/git-sync`
4. Handler: `git pull` across local workspaces → outputs JSON result
5. Daemon: publishes result to `turq/hive/meta/<node>/git-sync` (retained)
6. On failure: daemon publishes `alert` back to `turq/hive/<sender>/alert`
7. Master can check: `hive-cli sync-status` → reads all `meta/+/git-sync` topics

Replaces polling-based git sync timers with event-driven propagation + visibility.

## Tokenomics — Why Hive Exists

### The Problem: Heartbeat Tax

Every OC heartbeat costs tokens even when nothing happens:

1. OC injects the heartbeat prompt into the main session
2. The LLM wakes — loading **all boot files** as context (~15–18k input tokens)
3. It reads HEARTBEAT.md, checks if anything needs doing
4. Usually nothing does → replies `HEARTBEAT_OK` (swallowed silently)
5. You still paid the full boot tax for a no-op

**The numbers at 30-minute intervals:**

| Metric | Per instance | 3 gateways |
|--------|-------------|------------|
| Heartbeats/day | ~48 | ~144 |
| Input tokens/day (minimum) | ~860k | ~2.5M |
| Useful heartbeats (typical) | 2–4 | 6–12 |
| Wasted heartbeats | ~44 | ~132 |

Over 90% of heartbeat tokens are spent saying "nothing happening." And the more gateways you add, the worse it scales — linearly.

### Per-Turn Boot Tax Breakdown

Every LLM turn (not just heartbeats) pays to load context:

| File | Approx tokens |
|------|---------------|
| AGENTS.md | ~4–5k |
| TOOLS.md | ~5–6k |
| SOUL.md | ~500 |
| USER.md | ~300 |
| HEARTBEAT.md | ~100 |
| MEMORY.md | ~2–3k |
| Node file | ~500 |
| System prompt + metadata | ~2–3k |
| **Total boot tax** | **~15–18k input tokens/turn** |

This is unavoidable for interactive sessions (you need the context to be useful). But for background monitoring where 90%+ of checks find nothing? It's pure waste.

### The Sentinel Pattern

Instead of waking an expensive LLM every 30 minutes to check if anything happened, use a **zero-cost daemon** that monitors continuously and only wakes the LLM when something actually needs a brain.

```
BEFORE (heartbeat):
  Every 30 min → wake LLM (18k tokens) → "anything happening?" → "no" → waste

AFTER (sentinel):
  Every 1–5 min → daemon script (0 tokens) → "anything happening?" → "no" → free
  ...
  Every 1–5 min → daemon script (0 tokens) → "urgent email!" → wake LLM with context
```

**Sentinel monitors are hive action handlers** — executable scripts in `hive-daemon.d/`:

```
hive-daemon.d/
  check-email          ← Gmail API poll (every 5 min)
  check-calendar       ← Google Calendar API (every 15 min)
  check-git-status     ← dirty working tree? (every 30 min)
  check-services       ← Uptime Kuma / systemd (every 1 min)
```

Each handler runs on a configurable schedule, costs zero LLM tokens, and can check **much more frequently** than heartbeats ever could.

When a handler finds something noteworthy, it exits with a result that the daemon routes to the appropriate OC instance via `alert` or `command` channel — with **specific context** about what happened:

> "Hive alert from sentinel: Hugh has an urgent email from [sender] about [subject]. Triage and notify."

The LLM wakes up knowing exactly what to do, instead of having to discover it.

### Token Savings

| Pattern | Daily input tokens (3 gateways) | Monitoring frequency |
|---------|--------------------------------|---------------------|
| Heartbeat every 30m | ~2.5M | 30 min (slow) |
| Heartbeat every 4h | ~400k | 4 hours (very slow) |
| Sentinel (daemon checks) | ~0 on quiet days | 1–5 min (fast) |
| Sentinel + 4h safety heartbeat | ~400k ceiling | Best of both |

The sentinel pattern provides **faster monitoring at near-zero token cost**. On a quiet day with no alerts, the LLM never wakes for monitoring at all. On a busy day, it only wakes for things that actually need judgment.

### Recommendations

1. **Drop heartbeat frequency to 4h** as an immediate win (6 beats/day vs 48)
2. **Clear HEARTBEAT.md** of deterministic checks (email, calendar) — those become sentinel handlers
3. **Keep heartbeats as a safety net** — the 4h beat catches anything the sentinel doesn't cover yet
4. **Build sentinel handlers incrementally** — start with email + calendar, add more as patterns emerge
5. **Disable heartbeats entirely on worker nodes** (pg1, mini1) once sentinel is running — they only need to wake on inbound commands/alerts
6. **Long-term: heartbeats become optional** — sentinel + cron + hive commands cover all proactive behavior

### What Still Needs LLM Heartbeats?

Even with a sentinel, some checks genuinely need LLM judgment:

- **Memory maintenance** — reviewing daily files, curating MEMORY.md (but this runs via cron, not heartbeat)
- **Proactive outreach** — "it's been 8 hours, should I check in?" (judgment call)
- **Novel situations** — anything the sentinel doesn't have a handler for yet

The 4h safety heartbeat covers these edge cases until the sentinel is mature enough to handle or escalate them.

## Open Source Structure

The hive project is designed to be generic and open-sourceable. Custom/site-specific logic is separated into pluggable handlers and a contrib section.

### Core (open source)

```
hive/
  hive-daemon           ← main daemon process
  hive-cli              ← command-line tool
  hive-daemon.d/        ← empty by default (user populates)
  skills/
    hive-master/
      SKILL.md          ← OC skill for coordinator nodes
    hive-member/
      SKILL.md          ← OC skill for all nodes
  docs/
    protocol.md         ← this document
    handler-contract.md ← how to write handlers
  systemd/
    hive-daemon.service ← example unit file
  launchd/
    ai.hive.daemon.plist ← example plist
```

### Contrib (example handlers, community-provided)

```
contrib/
  handlers/
    git-sync            ← git pull across workspaces
    health-check        ← check OC instances + system health
    restart-oc          ← restart local OC gateway/node
    bootfile-sync       ← OpenClaw boot file propagation pattern
  examples/
    syncthing-sync      ← alternative sync via Syncthing
    k8s-deploy          ← Kubernetes deployment handler
    docker-restart      ← Docker Compose service restart
```

Users install handlers by copying (or symlinking) into their `hive-daemon.d/` directory. The daemon auto-discovers on startup and publishes available handlers in its roster.

## Relationship to Existing Components

| Component | Role | Changes needed |
|-----------|------|----------------|
| MQTT channel (OC) | Public chat (/turq avatar) | None — stays as-is, separate topics |
| `hive-daemon` | Always-on coordination sidecar | New — Python, one per box |
| `hive-cli` | Stateless CLI for OC/human use | New — Python, bundled with skills |
| `system event` (OC CLI) | Inbound injection to OC | None — already works |
| OC skills | Agent instructions for hive participation | New — `hive-master` + `hive-member` |
| Git sync timers | File propagation | Could be replaced by hive sync events |
| OC heartbeats | Agent periodic checks | Unchanged — separate from hive heartbeats |
| Mosquitto | Message broker | Already running — just new topics |

## Implementation Plan

### Phase 1: Proof of concept
- [ ] `hive-daemon`: subscribe to topics, log messages, handle heartbeat, inject system events
- [ ] `hive-cli`: send, reply, status commands
- [ ] `hive-member` skill: SKILL.md + bundled hive-cli reference
- [ ] Test: turq sends command to pg1, pg1 agent processes, replies back via hive-cli
- [ ] Test: sync event triggers immediate git pull (replaces 5-min timer)

### Phase 2: Production
- [ ] `hive-master` skill with cluster roster and delegation logic
- [ ] systemd/launchd service units for hive-daemon on each box
- [ ] Multi-instance support (turq box has turq + mini1)
- [ ] Heartbeat failure detection + alert escalation
- [ ] Correlation timeout handling (no response after N seconds)

### Phase 3: Polish
- [ ] Hive dashboard / status view (via `hive-cli status` or web)
- [ ] Topic ACLs in Mosquitto (per-node publish/subscribe restrictions)
- [ ] Replace git sync timers with hive sync events entirely

## Resolved Questions

1. ~~Should hive-reply be a script, an OC skill, or both?~~ → `hive-cli` is the tool, skills tell OC how to use it.
2. ~~Should hive-daemon have a local state store?~~ → No. MQTT retained messages on meta topics are the state store.
3. ~~Should hive-cli be Python or Node?~~ → Doesn't matter. Handlers in `hive-daemon.d/` can be any language. CLI and daemon language chosen for best fit.
4. ~~How does this interact with OC's cron?~~ → hive-master can trigger remote work via `hive-cli send --action <handler>`. For LLM-needed tasks, the system event approach works. Cron stays local to each gateway.

## Open Questions

1. Do we need message deduplication / idempotency keys?
2. Local Unix socket vs MQTT-only for `hive-cli` ↔ `hive-daemon` communication?
3. Should the daemon support handler-level config (e.g., `hive-daemon.d/git-sync.conf` for workspace paths)?
4. Auth between hive members — is LAN isolation sufficient or do we want message signing?
5. How should the daemon handle multiple local OC instances (round-robin, route by content, or broadcast system events to all)?

---

*"The hive is the nervous system, not the memory. It doesn't need its own journal."*
