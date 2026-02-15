# Shared Memory Architecture

Detailed design for git-based cluster memory in OpenClaw multi-gateway deployments.

See [../README.md](../README.md) for the overview and quick start.

## Design Principles

### 1. Single Writer, Many Readers

The most important rule: **one node writes shared files, all others read.** This eliminates merge conflicts, race conditions, and divergence without needing distributed locks or consensus protocols.

The writer is typically the "cluster manager" — the node that coordinates work across the cluster. Other nodes get updates via git pull (event-driven or timer-based).

### 2. Scoped Write Responsibility

Not all files need single-writer protection. The rule is based on scope:

- **Node-local files** (daily logs) — written by the node that owns them, no coordination needed
- **Cluster-shared files** (boot files, MEMORY.md) — single writer only
- **Project files** (code, docs) — standard git workflow (branches, PRs, or direct push)

### 3. Protection by Default

Non-writer nodes should be physically unable to modify shared files:

- **File permissions:** `chmod 444` on all boot files
- **Git hooks:** Pre-commit hook blocks boot file changes without explicit writer role
- **Operational discipline:** AGENTS.md instructs non-writer nodes not to edit shared files

Defence in depth — any one layer failing doesn't compromise the model.

### 4. Lean Context, Always

Every byte in boot files costs tokens on every turn. The memory architecture actively fights bloat:

- **No duplication across tiers** — if it's in TOOLS.md, it's not in MEMORY.md
- **Regular curation** — distill daily files → MEMORY.md, then prune
- **Separate reference from context** — server docs in `server-docs/`, not in AGENTS.md
- **Size targets** — MEMORY.md < 10KB, TOOLS.md < 25KB, AGENTS.md < 15KB

## Node Identity Files

Each gateway instance gets a node file loaded at bootstrap:

```
node-<hostname>-<port>.md
```

### Why hostname + port?

Multiple gateways can run on the same physical machine. Hostname alone isn't unique. The port identifies the specific OC instance.

### Bootstrap Mechanism

A small patch to OC's agent scope files auto-loads the node file:

```javascript
// Injected by patch-node-md-bootstrap.sh
const nodeFile = `node-${os.hostname()}-${process.env.OPENCLAW_GATEWAY_PORT}.md`;
```

The file is added to the bootstrap file list alongside AGENTS.md, SOUL.md, etc. Zero per-session overhead (it's loaded once with the other boot files).

### Contents

```markdown
# Node: pg1 (turq-playground) — port 18890

## Role
- Heavy compute worker
- Telegram + MQTT channels
- Image generation, cron jobs

## Server
- Hostname: turq-playground
- IP: <mqtt-broker-ip>
- OS: Ubuntu Linux

## Capabilities
- GPU: none (CPU only)
- Models: sonnet-4.5, g3flash
- Channels: telegram, mqtt
```

Keep it focused — role, capabilities, key facts. Server runbooks go in `server-docs/`.

## Sync Patterns

### Pattern A: Event-Driven (hive)

Best for: clusters running hive-daemon.

```
Writer commits → Writer's daemon publishes sync event →
  All daemons receive → Run git-sync handler → Instant propagation
```

Latency: seconds. Cost: zero LLM tokens.

### Pattern B: Timer-Based (no hive)

Best for: simple setups, or as a fallback alongside hive.

```
Every N minutes → sync script runs →
  git stash → git pull → git stash pop → done
```

Latency: up to N minutes. Cost: zero LLM tokens.

The stash-pull-pop pattern handles dirty working trees gracefully. If the node has uncommitted local changes (e.g., daily file being written), they're preserved.

### Pattern C: Hybrid

Run both. Hive sync for immediate propagation, timer as a safety net that catches anything hive missed (network blip, daemon restart, etc.).

## Memory Maintenance Automation

### Evening Recap (Recommended)

A nightly cron job that reviews the day's events and curates long-term memory:

1. Read today's + yesterday's daily files from all nodes
2. Identify significant events, lessons, decisions
3. Update MEMORY.md with distilled insights
4. Update node files if roles/capabilities changed
5. Update TOOLS.md if tool configurations changed
6. Prune outdated entries from MEMORY.md
7. Commit + push changes
8. Trigger hive sync event (if available)

**Model recommendation:** Use a capable model with high reasoning for curation. This is judgment work, not mechanical.

**Frequency:** Daily is sufficient. More frequent curation wastes tokens reviewing the same content.

### State Tracking

```json
// memory/memory-maintenance-state.json
{
  "lastRun": "2026-02-15T21:00:00Z",
  "lastDailyFileProcessed": "2026-02-15",
  "memoryMdSizeBytes": 8200,
  "toolsMdSizeBytes": 21000,
  "nodesUpdated": ["turq-18789", "pg1-18890"]
}
```

Prevents re-processing the same daily files and tracks file sizes for bloat detection.

## Migration Guide

### From single-gateway to multi-gateway

1. **Designate a writer node** — typically your primary/coordinator gateway
2. **Set file permissions** — `chmod 444` on boot files on all non-writer nodes
3. **Add git hook** — pre-commit hook on writer node requiring `TURQ_BOOTFILE_ROLE=writer`
4. **Set up sync** — timer-based initially, hive-driven once hive is running
5. **Create node files** — one per gateway instance, tracked in git
6. **Patch OC bootstrap** — apply `patch-node-md-bootstrap.sh` to auto-load node files
7. **Update AGENTS.md** — add write routing rules so all instances know the model

### From heartbeat-based monitoring to sentinel

1. **Reduce heartbeat frequency** — 30m → 4h (immediate token savings)
2. **Clear HEARTBEAT.md** — remove deterministic checks (email, calendar)
3. **Build sentinel handlers** — `hive-daemon.d/check-email`, `check-calendar`, etc.
4. **Disable heartbeats on worker nodes** — they only need inbound commands/alerts
5. **Long-term:** heartbeats become optional safety net, sentinel handles proactive monitoring
