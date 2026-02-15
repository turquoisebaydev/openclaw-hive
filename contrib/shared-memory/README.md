# Shared Memory — Git-Based Cluster Memory for OpenClaw

A pattern for managing persistent memory across multiple OpenClaw gateway instances using git as the synchronisation layer and hive as the event trigger.

## The Problem

When you run multiple OpenClaw gateways, each has its own workspace with memory files. Without coordination:

- **Memory diverges** — each instance accumulates different daily notes, lessons, decisions
- **Boot files drift** — AGENTS.md, TOOLS.md, SOUL.md get edited on one instance and not propagated
- **Conflicting writes** — two instances edit MEMORY.md simultaneously, one wins silently
- **Orphaned files** — files created on one instance never reach others
- **Bloat** — without curation, memory files grow indefinitely (~20KB+ is common)

## Architecture: 6-Tier Memory Model

### Tier 1: Node Short-Term (daily files)

```
memory/YYYY-MM-DD.md
```

- **Scope:** Single node, single day
- **Writer:** The node that experienced the events
- **Sync:** Node-local source record, pushed to git for archival
- **Lifecycle:** Written throughout the day, archived after curation

Raw session logs — what happened, decisions made, errors hit, things learned. Every node writes its own daily file.

### Tier 2: Node Long-Term (node identity files)

```
node-<hostname>-<port>.md
```

- **Scope:** Single node, persistent
- **Writer:** Cluster manager (single-writer)
- **Sync:** Tracked in git, pulled by all nodes
- **Contents:** Node role, server facts, capabilities, operational notes

Each node's identity file is auto-loaded at bootstrap via an OC patch. It tells the LLM "you are this node, here is your role, here are your capabilities."

### Tier 3: Cluster Short-Term Shared (boot files)

```
AGENTS.md, SOUL.md, USER.md, TOOLS.md, HEARTBEAT.md, IDENTITY.md
```

- **Scope:** All nodes, persistent but actively maintained
- **Writer:** Cluster manager only (single-writer, enforced by git hook + file permissions)
- **Sync:** Git push from writer → git pull on all others (via hive sync event or timer)
- **Protection:** `chmod 444` on non-writer nodes; git hook requires `TURQ_BOOTFILE_ROLE=writer`

These files define shared identity, personality, workspace rules, and tool configuration. Single-writer prevents conflicting edits.

### Tier 4: Cluster Long-Term Shared

```
MEMORY.md
```

- **Scope:** All nodes, curated long-term memory
- **Writer:** Cluster manager only (single-writer)
- **Sync:** Same as boot files
- **Maintenance:** Periodic curation — distill daily files into lasting lessons, prune stale entries

The "wisdom" layer. Not raw logs, but curated decisions, rules, lessons, and project status. Only loaded in main sessions (not group chats or shared contexts) for privacy.

### Tier 5: Cluster Reference

```
server-docs/, docs/, projects/
```

- **Scope:** All nodes, reference material
- **Writer:** Any node via git (standard PR/commit flow)
- **Sync:** Git
- **Not loaded at boot** — referenced on demand

Server runbooks, project documentation, design docs. Available to all nodes but not injected into every session.

### Tier 6: Cluster Distant (Semantic Search)

```
QMD / vector embeddings
```

- **Scope:** All sessions across all nodes
- **Access:** Semantic search on demand
- **Sync:** Periodic embedding sync (hourly cron)
- **Contents:** Past session transcripts, indexed memory files

"That thing we discussed last week" — searchable without loading everything into context.

## Write Routing Rules

| What | Where | Who writes |
|------|-------|------------|
| Session events, raw logs | `memory/YYYY-MM-DD.md` | Any node (local only) |
| Curated lessons, rules, decisions | `MEMORY.md` | Cluster manager only |
| Node identity, role, server facts | `node-<host>-<port>.md` | Cluster manager only |
| Tool setup, device specifics | `TOOLS.md` | Cluster manager only |
| Workspace rules, safety | `AGENTS.md` | Cluster manager only |
| Project code, configs | Project repo / `projects/` | Any node via git |
| Server runbooks | `server-docs/` or `docs/` | Any node via git |

**Key rule:** Don't duplicate between tiers. If it's in TOOLS.md, don't repeat in MEMORY.md. If it's in a node file, don't repeat in AGENTS.md.

## Sync Mechanisms

### Event-Driven (with hive)

1. Writer commits + pushes to git
2. Writer's hive-daemon publishes sync event: `ch: sync, action: git-sync, commit: abc1234`
3. Other nodes' daemons receive → run `hive-daemon.d/git-sync` handler immediately
4. Zero LLM tokens burned, near-instant propagation

### Timer-Based (without hive, or as fallback)

Periodic sync scripts running on each non-writer node:

```bash
# Every 5 minutes: stash local changes, pull, pop stash
cd /path/to/workspace
git stash --include-untracked -q 2>/dev/null || true
git pull --ff-only origin main -q 2>/dev/null
git stash pop -q 2>/dev/null || true
```

Managed by systemd timers (Linux) or LaunchAgents (macOS).

### Protection: Single-Writer Enforcement

On non-writer nodes, boot files are read-only:
```bash
chmod 444 AGENTS.md SOUL.md USER.md TOOLS.md HEARTBEAT.md IDENTITY.md MEMORY.md
```

On the writer node, a git hook enforces role:
```bash
# .git/hooks/pre-commit — blocks boot file commits without writer role
if [ "$TURQ_BOOTFILE_ROLE" != "writer" ]; then
  echo "Boot-file commit blocked. Set TURQ_BOOTFILE_ROLE=writer"
  exit 1
fi
```

## Memory Maintenance

Periodic curation prevents unbounded growth:

1. **Review recent daily files** — scan `memory/YYYY-MM-DD.md` for the past few days
2. **Distill to MEMORY.md** — extract lasting lessons, decisions, rules
3. **Prune stale entries** — remove outdated info from MEMORY.md
4. **Archive old daily files** — after curation, daily files become archival only
5. **Track maintenance state** — `memory/memory-maintenance-state.json` records what's been curated

This can run as a cron job (recommended: evening, using a capable model with high reasoning) or as a hive action handler.

## Tokenomics Impact

Boot files are loaded every LLM turn. Their size directly impacts cost:

| File | Approx tokens | Loaded when |
|------|---------------|-------------|
| AGENTS.md | ~4-5k | Every turn |
| TOOLS.md | ~5-6k | Every turn |
| MEMORY.md | ~2-3k | Main sessions only |
| Node file | ~500 | Every turn |
| Total boot tax | ~15-18k/turn | — |

**Recommendations:**
- Keep boot files lean — every extra line costs tokens on every turn
- MEMORY.md target: under 10KB (~2-3k tokens)
- TOOLS.md: local specifics only, not duplicating skill docs
- Node files: role + capabilities + key facts, not full server runbooks

## Example: 3-Gateway Cluster

```
turq (Mac mini, port 18789)     ← cluster manager, MEMORY.md writer
├── WhatsApp channel
├── Writes: MEMORY.md, AGENTS.md, TOOLS.md, node files
└── Role: orchestrator, not heavy-lifter

mini1 (Mac mini, port 18889)    ← lightweight secondary
├── No channels (utility gateway)
├── Reads: all boot files (read-only)
└── Role: overflow, experiments

pg1 (Linux, port 18890)         ← worker node
├── Telegram + MQTT channels
├── Reads: all boot files (read-only)
├── Writes: daily files, project code
└── Role: heavy compute, image gen, cron jobs
```

All three share the same git repo for boot files. turq is the single writer. Sync happens via hive events (or 5-minute timer fallback).
