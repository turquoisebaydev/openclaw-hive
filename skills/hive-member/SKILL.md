---
name: hive-member
description: Inter-gateway coordination via MQTT using hive-cli tool
---

# Hive Member — Inter-Gateway Coordination

You are part of a **hive** — a cluster of independent OpenClaw gateways coordinating via MQTT. A local `hive-daemon` handles the transport; you interact through the `hive-cli` tool.

## When This Skill Activates

You receive system events prefixed with `[hive:...]`. These are messages from other nodes in the cluster, injected by your local hive-daemon.

Example inbound:
```
[hive:turq->pg1 ch:command] Generate meural images for tomorrow's theme
```

## Architecture (What You Need to Know)

```
Other node's OC  →  hive-cli send  →  MQTT  →  your hive-daemon  →  system event  →  YOU
YOU  →  hive-cli reply/send  →  MQTT  →  their hive-daemon  →  system event  →  their OC
```

- You never touch MQTT directly — `hive-cli` handles all protocol details
- The hive-daemon runs alongside you on this box, handling heartbeats and deterministic tasks
- You only get woken up for things that need LLM judgment
- **Deterministic handlers** (git-sync, ping, health-check) run in the daemon without waking you

## hive-cli Commands

The CLI reads config from `~/.config/hive/hive.toml` by default. You can override this with `--config <path>`.

### Send a message (fire-and-forget)
```bash
hive-cli send \
  --to <node-id> \
  --ch <channel> \
  --text "your message" \
  [--action <handler-name>] \
  [--urgency now|later]
```

**Fire-and-forget is the default and preferred pattern.** Use this for:
- Tasks where you don't need the result back immediately
- Deterministic actions (git-sync, ping) where the daemon handles it
- Commands where the remote node will notify Hugh directly if needed

### Send and wait for response (synchronous)
```bash
hive-cli send \
  --to pg1 \
  --ch command \
  --action ping \
  --text "health check" \
  --wait 30
```
Blocks up to N seconds. Prints the response envelope JSON when a correlated reply arrives. Exits 1 on timeout.

**Use `--wait` ONLY when you need the answer in the same tool call** — e.g. checking disk space before deciding what to do next. For most tasks, fire-and-forget is better.

### Reply to a hive command
When you receive a hive command via system event, reply with:
```bash
hive-cli reply \
  --to-msg '<original-envelope-json>' \
  --text "your response" \
  [--session <session-key>]
```

The reply automatically sets `ch: response`, copies the correlation ID, and sets `replyTo`. **Only reply when the sender needs information back.** If you were told to "do X", just do it — don't reply unless results were requested.

The optional `--session` flag stores a local mapping so that when the remote node responds to *your* reply, their response gets routed back to the specified session instead of the default hive session. This enables bi-directional session-pinned conversations. The mapping inherits the original envelope's TTL (or defaults to 1 hour). **Session mappings are never sent over MQTT** — they're purely local IPC between CLI and daemon.

### Check cluster status
```bash
hive-cli status
```

### View node capabilities
```bash
hive-cli roster
```

## Channels

| Channel | Purpose | When you see it | Should you respond? |
|---------|---------|-----------------|---------------------|
| `command` | Tasks/requests | **Yes** — if it needs LLM judgment | Only if results were requested |
| `command` + `action` | Deterministic tasks (git-sync etc.) | **Rarely** — only on handler failure | Investigate the failure |
| `response` | Reply to something you sent | **Yes** | No — just consume it |
| `alert` | Urgent escalation | **Yes** (marked URGENT) | Triage + act or notify Hugh |
| `sync` + `action` | File/memory sync | **Rarely** — only on failure | Investigate the failure |
| `status` | Node state changes | **Sometimes** | Usually no |
| `heartbeat` | Aliveness pings (5s) | **Never** | Never — daemon handles this |

## When to Reply vs Fire-and-Forget

**Reply when:**
- The sender explicitly asked for information ("report disk usage", "what's the status of X")
- You're responding to a `--wait` synchronous request (the sender is blocking)
- A task failed and the sender needs to know

**Don't reply when:**
- You were told to do something ("generate images", "run sync") — just do it
- The command had `--action` (deterministic handler) — the daemon already sent the result
- It's a broadcast (`--to all`) with no specific question

## Action Field — Deterministic Dispatch

If a message has an `action` field (e.g. `"action": "git-sync"`), the daemon handles it locally with a handler script — **you never see it**. The daemon runs the script, publishes the result back as a response, and that's it. Zero tokens.

You only see action messages when:
- No handler exists for that action (daemon escalates to you)
- The handler failed (daemon escalates with error context)

## How to Handle Inbound Messages

### Command — do the work
```
[hive:turq->pg1 ch:command] Check what services are using the most memory
```
1. Do the work
2. Reply with results via `hive-cli reply` (only because info was requested)

### Alert — triage urgently
```
[hive:turq->pg1 ch:alert] URGENT Service mosquitto is down
```
1. Investigate immediately
2. Fix if possible
3. Notify Hugh if serious

### Failed handler — investigate
```
[hive:turq->pg1 ch:command] git-sync handler failed: merge conflict
```
1. Check the conflict
2. Resolve if safe, otherwise escalate

### Response — consume it
```
[hive:pg1->turq ch:response] {"ok": true, "synced": 2, "failed": 0}
```
Use the information. Don't reply to responses.

## What NOT to Do

- **Don't publish to MQTT directly** — always use `hive-cli`
- **Don't reply to heartbeats** — the daemon handles those
- **Don't reply unnecessarily** — if no info was requested, stay silent
- **Don't use `--wait` by default** — fire-and-forget is almost always right
- **Don't send secrets over hive** — use references ("pull from 1Password")
