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

## hive-cli Commands

The CLI lives at `cli/.venv/bin/hive-cli` in the openclaw-hive directory. Always pass `--config <path-to-hive.toml>`.

### Send a message (fire-and-forget)
```bash
hive-cli --config /path/to/hive.toml send \
  --to <node-id> \
  --ch <channel> \
  --text "your message" \
  [--action <handler-name>] \
  [--urgency now|later] \
  [--ttl <seconds>]
```

### Send and wait for response (synchronous)
```bash
hive-cli --config /path/to/hive.toml send \
  --to pg1 \
  --ch command \
  --text "check disk usage" \
  --wait 60
```
Blocks up to 60 seconds. Prints the full response envelope JSON when the correlated reply arrives. Exits 1 on timeout. **Use this when you need the answer back in the same tool call.**

### Reply to a message
When you receive a hive command and need to respond, use reply with the original envelope:
```bash
hive-cli --config /path/to/hive.toml reply \
  --to-msg '<original-envelope-json>' \
  --text "your response"
```

The reply automatically:
- Sets `ch` to `response`
- Copies the `corr` (correlation ID) from the original
- Sets `replyTo` to the original message's `id`

### Check cluster status
```bash
hive-cli --config /path/to/hive.toml status
```

### View node capabilities
```bash
hive-cli --config /path/to/hive.toml roster
```

## Message Envelope

Every hive message is wrapped in an envelope with these fields:

| Field | Type | Required | Purpose |
|-------|------|----------|---------|
| `v` | int | yes | Schema version (currently `1`) |
| `id` | string | yes | Unique message UUID |
| `ts` | int | yes | Unix epoch seconds when sent |
| `from` | string | yes | Sender node ID (e.g. `turq`, `pg1`) |
| `to` | string | yes | Target node ID, or `all` for broadcast |
| `ch` | string | yes | Logical channel (see below) |
| `urgency` | string | yes | `now` (wake agent immediately) or `later` (can wait) |
| `text` | string | yes | Payload — natural language or structured content |
| `corr` | string | no | Correlation ID linking request ↔ response |
| `replyTo` | string | no | The `id` of the message being replied to |
| `ttl` | int | no | Seconds until message expires |
| `action` | string | no | Handler name for deterministic dispatch (e.g. `git-sync`) |

## Channels

| Channel | Purpose | You'll see it? | How to respond |
|---------|---------|----------------|----------------|
| `command` | "Do something" — tasks, requests, queries | **Yes** | Do the work, then `hive-cli reply` |
| `response` | Reply to a command (carries `corr` + `replyTo`) | **Yes** (if you sent the original) | Usually just consume it |
| `alert` | Urgent escalation ("disk full", "job failed 3x") | **Yes** (always, marked URGENT) | Triage and act or notify Hugh |
| `sync` | File/memory sync notifications | **Rarely** (only on failure) | The daemon handles these |
| `status` | Node state changes ("going down", "back online") | **Sometimes** | Acknowledge if needed |
| `heartbeat` | Node aliveness pings (5s interval) | **Never** | Daemon handles this entirely |

## Correlation IDs — Request/Response Tracking

When a command expects a response, it includes a `corr` (correlation ID). Your reply must carry the same `corr` so the sender can match it.

**Flow:**
1. Sender sends: `{ "id": "msg-123", "corr": "task-abc", "ch": "command", "text": "..." }`
2. You receive it as a system event with the metadata
3. You do the work
4. You reply: `hive-cli reply --to-msg '<envelope>' --text "done"` → auto-sets `corr: "task-abc"`, `replyTo: "msg-123"`

If there's no `corr` in the original, `reply` uses the original `id` as the correlation ID.

**You don't need to generate correlation IDs yourself** — `hive-cli` handles it.

## Urgency

- **`now`**: You were woken up specifically for this. Handle it promptly.
- **`later`**: Informational, can wait. You might see these batched.

Alerts are always treated as `now` regardless of the field value.

## Action Field — Deterministic Dispatch

If a message has an `action` field (e.g. `"action": "git-sync"`), the daemon tries to handle it locally with a handler script first — **before** it reaches you. You only see action messages when:
- No handler exists for that action (daemon escalates to you)
- The handler failed (daemon escalates with error context)

When this happens, do your best to handle it or explain why you can't.

## How to Respond to Hive Messages

### Command received — do the work
```
[hive:turq->pg1 ch:command] Check disk usage on this host and report back
```
1. Do the work (run commands, check things)
2. Reply with results via `hive-cli reply`

### Alert received — triage urgently
```
[hive:turq->pg1 ch:alert] URGENT Service mosquitto is down on pg1
```
1. Investigate immediately
2. Fix if possible
3. Reply with status + notify Hugh if serious

### Failed sync — investigate
```
[hive:turq->pg1 ch:sync] git-sync handler failed: merge conflict in MEMORY.md
```
1. Check the conflict
2. Resolve if safe, otherwise escalate to Hugh

### Response received — consume it
```
[hive:pg1->turq ch:response] Done. 6 images uploaded to Meural.
```
This is a reply to something you sent earlier. Use the info as needed.

## What NOT to Do

- **Don't publish to MQTT directly** — always use `hive-cli`
- **Don't reply to heartbeats** — the daemon handles those
- **Don't generate your own envelope IDs** — `hive-cli` does this
- **Don't ignore correlation IDs** — if the original has a `corr`, your reply must carry it (use `hive-cli reply`, it's automatic)
- **Don't send secrets over hive** — use references ("pull from 1Password", not the actual key)
