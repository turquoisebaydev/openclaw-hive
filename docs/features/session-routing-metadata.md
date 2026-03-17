# Feature Spec — SRM (Session Routing Metadata)

## Status
Proposal

## Short Name
`SRM`

## Why
Current Hive presence is good enough to answer "which sessions exist right now?" but not
"which live session should receive a command for `#dashboard-dev`, and how should it be
routed?"

Today the retained presence payload already exposes deterministic session identity and some
channel metadata:
- `gw`
- `agent`
- `session`
- `channel.provider`
- `channel.chatType`
- `channel.chatId`

That is enough only when the consumer already knows the exact channel id. It is not enough
for robust autonomous routing from human-facing context like:
- Discord guild name
- Discord channel name
- whether the session accepts commands
- whether the live worker is a main session, ACP subagent, cron, or observer
- which session should control a delegated ACP subagent
- which active ACP child sessions were spawned by a human-facing controller session
- whether a running ACP child should receive a direct `message` or `steer` command

## Problem Statement
The dashboard worker on `pg1` needs to identify the correct gateway session attached to the
Discord `dashboard-dev` context, then send instructions to the right controlling session or
subagent. Current MQTT presence and Hive CLI data do not provide a deterministic contract for
that resolution.

Current gaps:
- no `channel.name`
- no `channel.guildName`
- no stable human-readable session label
- no explicit command-routing target
- no explicit parent/child session linkage
- no deterministic spawned-by / controller lineage for ACP or subagent sessions
- no way to tell whether a child session accepts direct `message`, `steer`, `cancel`, or `close`
- no session-addressable Hive send primitive

## Goals
1. Let deterministic consumers resolve a live session from human channel context.
2. Let consumers tell whether a session is command-capable.
3. Let consumers distinguish main sessions from ACP/subagent workers.
4. Let Hive route directly to a resolved session without node-only inference.
5. Let a controller session discover active steerable ACP/subagent children deterministically.
6. Keep all resolution and routing metadata deterministic.

## Non-Goals
- No model-based resolution.
- No fuzzy matching on channel names.
- No transport redesign beyond adding deterministic session targeting.

## Proposal

### 1. Extend retained presence with SRM fields

Add these fields to `session_presence` payloads:

```json
{
  "v": 3,
  "kind": "session_presence",
  "gw": "pg1",
  "agent": "codex",
  "session": "agent:codex:acp:7f5c...",
  "channel": {
    "provider": "discord",
    "chatType": "channel",
    "chatId": "147...",
    "guildId": "123...",
    "guildName": "Turquoise Bay AI",
    "name": "dashboard-dev",
    "label": "discord:Turquoise Bay AI:#dashboard-dev"
  },
  "runtime": {
    "kind": "acp",
    "mode": "persistent",
    "parentSessionKey": "pg1/main/agent:main:discord:channel:147...",
    "spawnedBySessionKey": "pg1/main/agent:main:discord:channel:147...",
    "spawnDepth": 1,
    "controlScope": "none"
  },
  "routing": {
    "gatewayId": "pg1",
    "sessionKey": "pg1/codex/agent:codex:acp:7f5c...",
    "replyVia": "openclaw_session",
    "acceptsCommands": true,
    "supportedCommands": ["message", "steer", "cancel", "close"],
    "directMessageSessionKey": "pg1/codex/agent:codex:acp:7f5c...",
    "steerSessionKey": "pg1/codex/agent:codex:acp:7f5c..."
  },
  "delegation": {
    "preferredControllerSessionKey": "pg1/main/agent:main:discord:channel:147...",
    "preferredReplySessionKey": "pg1/main/agent:main:discord:channel:147..."
  }
}
```

### 2. Define SRM field semantics

- `channel.guildId`: stable provider-side guild/server id
- `channel.guildName`: human-visible guild/server name
- `channel.name`: human-visible channel/thread name
- `channel.label`: stable display label for operators and consumers
- `runtime.kind`: one of `main`, `subagent`, `acp`, `cron`, `mqtt`, `discord`
- `runtime.mode`: worker runtime mode when relevant (for example ACP `persistent` or `oneshot`)
- `runtime.parentSessionKey`: controlling parent session when this session is delegated
- `runtime.spawnedBySessionKey`: canonical controller/session-store lineage when the session was
  spawned by another session
- `runtime.spawnDepth`: deterministic child depth when available
- `runtime.controlScope`: whether the child is allowed to control descendants (`children` or
  `none`)
- `routing.gatewayId`: physical gateway that owns this live session
- `routing.sessionKey`: canonical deterministic target key
- `routing.replyVia`: required transport mode for replies/commands
- `routing.acceptsCommands`: whether this session is an allowed command target
- `routing.supportedCommands`: deterministic list of supported direct commands such as
  `message`, `steer`, `cancel`, `close`
- `routing.directMessageSessionKey`: session target for raw message injection
- `routing.steerSessionKey`: session target for ACP steer semantics when different from message
  injection
- `delegation.preferredControllerSessionKey`: session to target when work should go through a
  controller instead of the current worker directly
- `delegation.preferredReplySessionKey`: session whose bound human surface should be used for
  operator-visible follow-up/replies

### 2a. ACP steering contract

SRM must model two distinct but deterministic operations for live ACP children:

1. `message`
   Send a normal inter-session message into the child session.
2. `steer`
   Send ACP steer/control text to the running child runtime.

For ACP sessions, `routing.supportedCommands` must declare whether either or both are legal.
If the child is directly steerable, `routing.steerSessionKey` must be present.
If the child should not receive direct commands, `routing.acceptsCommands=false` and
`delegation.preferredControllerSessionKey` must point at the controller session instead.

This lets a consumer attached to a human-facing thread do both of these deterministically:
- continue talking to the controller/main session
- target a running Codex/Claude ACP child directly when the worker is steerable

### 3. Add deterministic session routing to Hive

Add one of these deterministic control-plane capabilities:

#### Preferred
`hive-cli send --to-session <sessionKey> ...`

#### Optional resolver helpers
- `hive-cli resolve session --provider discord --guild-name "Turquoise Bay AI" --channel-name "dashboard-dev"`
- `hive-cli resolve session --provider discord --chat-id <channelId>`

The resolver must return deterministic outcomes only:
- exact match
- ambiguous
- missing
- stale

Direct send primitives should support explicit command intent:
- `hive-cli send --to-session <sessionKey> --mode message ...`
- `hive-cli send --to-session <sessionKey> --mode steer ...`

If `--mode steer` is used against a session without `routing.steerSessionKey` or without
`steer` in `routing.supportedCommands`, Hive must fail deterministically.

### 4. Optional retained index for channel lookup

If consumers should not scan all presence topics, add an index topic:

`<prefix>/index/discord/<guildId>/<channelId>`

Example payload:

```json
{
  "v": 1,
  "kind": "channel_session_index",
  "provider": "discord",
  "guildId": "123...",
  "guildName": "Turquoise Bay AI",
  "channelId": "147...",
  "channelName": "dashboard-dev",
  "sessionKey": "pg1/main/agent:main:discord:channel:147...",
  "gw": "pg1",
  "acceptsCommands": true,
  "updatedTs": 1773500000
}
```

This is optional. SRM still works without it if consumers can scan retained presence.

## Deterministic Resolution Contract

Given a channel context like `discord / Turquoise Bay AI / dashboard-dev`, a deterministic
consumer should be able to:

1. resolve the exact live session
2. determine whether it accepts commands
3. determine whether it is a main session or delegated worker
4. determine the controller session if a subagent should not be targeted directly
5. discover active ACP/subagent children whose `runtime.parentSessionKey` or
   `runtime.spawnedBySessionKey` points at that controller session
6. decide whether to send `message` or `steer` to a child without guessing
7. send a command through Hive without guessing

Failure outcomes must stay deterministic:
- `SESSION_NOT_FOUND`
- `SESSION_STALE`
- `TARGET_AMBIGUOUS`
- `COMMANDS_NOT_ACCEPTED`
- `PARENT_SESSION_REQUIRED`
- `COMMAND_MODE_UNSUPPORTED`

## Compatibility

SRM should be additive:
- keep existing topic paths
- keep existing `v:2` consumers working
- add new fields without removing current ones
- only promote to `v:3` when field presence is reliable enough for consumers to depend on

Consumers should treat missing SRM fields as "identity only, routing not guaranteed."

## Minimum Useful Slice

If full SRM is too large for one pass, the minimum slice that solves the dashboard worker use
case is:

1. `channel.guildName`
2. `channel.name`
3. `routing.sessionKey`
4. `routing.acceptsCommands`
5. `runtime.kind`
6. `runtime.parentSessionKey`
7. `runtime.spawnedBySessionKey`
8. `routing.supportedCommands`
9. `routing.steerSessionKey`
10. `delegation.preferredControllerSessionKey`
11. `hive-cli send --to-session`

That is enough to resolve `dashboard-dev` deterministically, find its live steerable children,
and route work to the right controller or worker session.

## Acceptance Criteria
1. A consumer can resolve `discord guild + channel name` to one live session deterministically.
2. A consumer can tell whether the session accepts commands.
3. A consumer can determine whether to target the session or its controller.
4. A consumer can discover active ACP children belonging to that controller session.
5. A consumer can tell whether a child supports direct `message`, direct `steer`, or controller-only routing.
6. `hive-cli` can send directly to a resolved `sessionKey`.
7. Missing, ambiguous, or unsupported command-mode routing returns deterministic error codes.

## Recommended Next Step
Implement the minimum useful slice first, then decide whether the retained channel index is
worth the extra maintenance cost.
