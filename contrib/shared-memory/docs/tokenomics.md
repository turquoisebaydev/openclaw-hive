# Tokenomics — Cost Analysis for Multi-Gateway OpenClaw Clusters

## The Boot Tax

Every LLM turn loads context files ("boot files") before the model can respond. This is the **boot tax** — a fixed input token cost paid on every single turn, whether it's a complex task or a simple "nothing to report."

### Per-Turn Cost Breakdown

| File | Approx tokens | Purpose |
|------|---------------|---------|
| AGENTS.md | ~4–5k | Workspace rules, conventions, workflow |
| TOOLS.md | ~5–6k | Tool configurations, local specifics |
| MEMORY.md | ~2–3k | Curated long-term memory (main sessions only) |
| SOUL.md | ~500 | Personality, tone, identity |
| USER.md | ~300 | User preferences, contact info |
| HEARTBEAT.md | ~100 | Heartbeat checklist |
| Node file | ~500 | Node identity, role, capabilities |
| System prompt + metadata | ~2–3k | OC system prompt, inbound context |
| **Total boot tax** | **~15–18k** | **Per turn, per instance** |

This tax is justified for interactive sessions — the LLM needs this context to be useful. But for background operations where 90%+ of invocations find nothing to do, it's pure waste.

## Heartbeat Cost Model

OC heartbeats wake the LLM at a fixed interval to check a task list (HEARTBEAT.md). Each heartbeat pays the full boot tax.

### Cost by Frequency

| Interval | Beats/day | Input tokens/day | At $3/M input |
|----------|-----------|-------------------|----------------|
| 15 min | ~96 | ~1.7M | ~$5.10 |
| 30 min | ~48 | ~860k | ~$2.58 |
| 1 hour | ~24 | ~430k | ~$1.29 |
| 4 hours | ~6 | ~108k | ~$0.32 |

### Multi-Gateway Multiplier

Each independent gateway runs its own heartbeat loop. Token burn scales linearly:

| Gateways | 30m interval | 4h interval |
|----------|-------------|-------------|
| 1 | ~860k/day | ~108k/day |
| 2 | ~1.7M/day | ~216k/day |
| 3 | ~2.6M/day | ~324k/day |
| 5 | ~4.3M/day | ~540k/day |

### The Waste Problem

In practice, most heartbeats return `HEARTBEAT_OK` — the LLM loaded 18k tokens of context, checked the checklist, found nothing, and replied with 2 tokens. Typical useful-to-wasted ratio:

| Scenario | Useful beats/day | Wasted beats/day | Waste % |
|----------|-----------------|-------------------|---------|
| Active day (lots happening) | 8–12 | 36–40 | ~75% |
| Quiet day (nothing urgent) | 2–4 | 44–46 | ~92% |
| Night hours (sleeping) | 0 | ~16 | 100% |

**Conservative estimate: 85–95% of heartbeat tokens are wasted across a typical cluster.**

## The Sentinel Alternative

Replace LLM heartbeats with zero-cost daemon scripts that check deterministic conditions:

| Check | Traditional (LLM) | Sentinel (daemon) |
|-------|-------------------|-------------------|
| New email? | 18k tokens, 30m interval | 0 tokens, 5m interval |
| Calendar event soon? | 18k tokens, 30m interval | 0 tokens, 15m interval |
| Service down? | 18k tokens, 30m interval | 0 tokens, 1m interval |
| Git repo dirty? | 18k tokens, 30m interval | 0 tokens, 30m interval |
| Disk space low? | 18k tokens, 30m interval | 0 tokens, 10m interval |

The sentinel daemon runs these checks on tight intervals (faster than heartbeats ever could) at zero LLM cost. When a check finds something, it wakes the LLM with **specific context** about what happened — the LLM doesn't waste tokens discovering the problem.

### Daily Token Comparison (3 gateways)

| Approach | Monitoring tokens/day | Monitoring frequency |
|----------|----------------------|---------------------|
| Heartbeat every 30m | ~2.6M | 30 min, blind |
| Heartbeat every 4h | ~324k | 4 hours, blind |
| Sentinel only | ~0 on quiet days | 1–5 min, targeted |
| Sentinel + 4h safety | ~324k ceiling | Best of both |

## Reducing Boot Tax

Beyond heartbeat frequency, the boot tax itself can be reduced:

### 1. Keep files lean

Every line in a boot file costs tokens on every turn across every session. Be ruthless about what earns its place:

- **AGENTS.md:** Rules and conventions only. No tutorials, no history.
- **TOOLS.md:** Local specifics only. Don't duplicate skill documentation.
- **MEMORY.md:** Curated wisdom only. Not a journal — that's what daily files are for.
- **Node files:** Role + capabilities + key facts. Server runbooks go in `server-docs/`.

### 2. Regular curation

Schedule memory maintenance (evening cron or hive handler):
- Distill daily files → MEMORY.md
- Prune stale entries from all boot files
- Track file sizes and alert if they exceed targets

### 3. Size targets

| File | Target | Alert threshold |
|------|--------|-----------------|
| AGENTS.md | < 15KB | > 20KB |
| TOOLS.md | < 25KB | > 30KB |
| MEMORY.md | < 10KB | > 15KB |
| Node files | < 2KB each | > 5KB |
| HEARTBEAT.md | < 500B | > 2KB |

### 4. Context-aware loading

Not all files need to load in all contexts:
- **MEMORY.md:** Main sessions only (not group chats, not shared contexts)
- **HEARTBEAT.md:** Only on heartbeat turns (if OC supports conditional loading)
- **TOOLS.md:** Could be split into always-load (essential) and on-demand (reference)

## Recommendations

### Immediate (no hive needed)

1. **Reduce heartbeat to 4h** — saves ~75% of heartbeat tokens immediately
2. **Clear HEARTBEAT.md** of deterministic checks — they'll move to sentinel handlers
3. **Audit boot file sizes** — prune anything that doesn't earn its per-turn cost
4. **Disable heartbeats on worker nodes** — they only need inbound events

### With hive (Phase 1+)

5. **Build sentinel handlers** — start with email + calendar, most impactful
6. **Replace sync timers** — event-driven git sync via hive, zero polling waste
7. **Consider disabling heartbeats entirely** — sentinel + cron + hive covers all proactive behavior
8. **Monitor actual LLM wake-ups** — track how many times the LLM is woken per day and why

### Long-term

9. **Per-handler cost tracking** — which sentinel checks trigger the most LLM wake-ups?
10. **Adaptive monitoring** — increase check frequency during active hours, reduce at night
11. **Cross-cluster dedup** — if 3 gateways all check email, only one needs to
