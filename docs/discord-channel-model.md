# Hive Discord Channel/Thread Model

**Status:** Active (2026-04-02)  
**Authors:** Hugh + Turq

## Overview

Hive uses a two-tier Discord structure to separate **long-running projects** from **short-term initiatives**:

1. **Hive channels** (`*-hive`) — parent channels that contain initiative threads
2. **Project channels** (`*-proj`) — top-level channels for persistent projects
3. **Initiative threads** (`*-init`) — temporary threads within hive channels that auto-archive

## Channel Naming Conventions

### Hive Channels (Parent Channels)
Format: `<alias>-hive`

These channels contain temporary initiative threads. Each gateway has its own hive channel:

| Channel | Gateway | Alias |
|---------|---------|-------|
| `#pg-hive` | juniorgw (turq-playground) | `pg` |
| `#turq-hive` | turqgw (Mac mini) | `turq` |
| `#hermes-hive` | Hermes | `hermes` |

### Project Channels (Top-Level)
Format: `<projectname>-<alias>-proj`

These are **top-level channels** (not nested) for long-running projects. Examples:
- `#openclaw-pg-proj` — OpenClaw development on juniorgw
- `#meural-turq-proj` — Meural work on turqgw
- `#hive-cli-hermes-proj` — Hive CLI work on Hermes

### Initiative Threads
Format: `<what>-init`

Threads within hive channels for short-term work. Examples:
- In `#pg-hive`: thread `qmd-migration-init`
- In `#turq-hive`: thread `deploy-openclaw-init`

## Alias Mapping

| Alias | Full Name | Gateway/Node |
|-------|-----------|--------------|
| `pg` | juniorgw | turq-playground |
| `turq` | turqgw | Mac mini (turq) |
| `hermes` | Hermes | Hermes node |

## Structure Examples

### Example 1: juniorgw Hive Channel
```
#pg-hive (channel)
├── thread: qmd-migration-init (short-term, auto-archives)
├── thread: bugfix-dashboard-init (short-term, auto-archives)
└── thread: deploy-meural-init (short-term, auto-archives)
```

### Example 2: Long-Running Project Channel
```
#openclaw-pg-proj (top-level channel, persists indefinitely)
├── thread: v2026.4.0-release (temporary discussion within project)
└── thread: performance-tuning (temporary discussion within project)
```

### Example 3: Full Guild Structure
```
#pg-hive           ← juniorgw initiatives
#turq-hive         ← turqgw initiatives  
#hermes-hive       ← Hermes initiatives

#openclaw-pg-proj  ← long-running project channel
#meural-pg-proj    ← long-running project channel
#hive-cli-turq-proj ← long-running project channel
#hermes-core-proj  ← long-running project channel
```

## Lifecycle Behavior

### Hive Channels
- **Created:** Once per gateway (manual setup)
- **Purpose:** Container for initiative threads
- **Lifecycle:** Permanent (never deleted)
- **Content:** Threads only (no direct messages in channel)

### Project Channels
- **Created:** When a long-running project starts
- **Purpose:** Persistent workspace for ongoing development
- **Lifecycle:** Indefinite (stays open until project is complete/abandoned)
- **Content:** Can have threads for temporary sub-discussions

### Initiative Threads
- **Created:** When a short-term task is started
- **Purpose:** Time-boxed work that naturally expires
- **Lifecycle:** Auto-archives when Discord's thread timeout triggers
- **Content:** Complete the work, let it archive, done

## Why This Model

### Problems It Solves

1. **Thread sprawl:** Without structure, all threads mix together in one channel
2. **Project visibility:** Long-running work gets lost in archived threads
3. **Cleanup burden:** Manual archiving is error-prone and forgotten
4. **Gateway confusion:** Hard to tell which node owns which work

### Benefits

1. **Natural expiration:** Initiative threads auto-archive without manual intervention
2. **Project permanence:** Important work stays visible in dedicated channels
3. **Clear ownership:** Channel names immediately show which gateway is responsible
4. **Scalable:** Add new projects as channels, new initiatives as threads

## Discord RPC Integration

The hive-daemon's Discord RPC layer supports this model through configurable suffixes:

### Config Example
```toml
[discord_master]
enabled = true
guild_id = "1476805337968279685"
bot_token = "<token>"

# Default search suffixes for hive-thread-list
default_parent_suffix = "-hive"
default_thread_suffix = "-proj"

[[discord_master.channels]]
name = "pg-hive"
mention_target = "user:1477047646769254643"  # juniorgw bot
mention_type = "user"

[[discord_master.channels]]
name = "turq-hive"
mention_target = "user:1482865686102671481"  # turqgw bot
mention_type = "user"

[[discord_master.channels]]
name = "hermes-hive"
mention_target = "user:..."  # Hermes bot
mention_type = "user"
```

### CLI Commands

List initiative threads in hive channels:
```bash
hive-cli hive-thread-list --to pg1
# Searches for threads ending in "-init" within "*-hive" channels
```

List project channels:
```bash
hive-cli hive-thread-list --to pg1 --thread-suffix "-proj"
# Searches for threads ending in "-proj" (within any channel)
```

Send to an initiative thread:
```bash
hive-cli hive-thread-send \
  --to pg1 \
  --thread-id <id> \
  --message "Update on qmd-migration-init"
```

## Migration Notes

### Existing Channels

Current channels to migrate:
- `#juniorgw-hive` → `#pg-hive`
- `#turqgw-hive` → `#turq-hive`
- `#hermes-hive` → stays `#hermes-hive`

Project threads in hive channels should become top-level project channels:
- `#juniorgw-hive > openclaw-pg-proj` (thread) → `#openclaw-pg-proj` (channel)
- `#turqgw-hive > hive-cli-turq-proj` (thread) → `#hive-cli-turq-proj` (channel)

### Migration Steps

1. Create new top-level project channels for existing project threads
2. Rename hive channels to use aliases (`juniorgw-hive` → `pg-hive`)
3. Move initiative threads to appropriate hive channels
4. Update Discord bot config with new channel mappings
5. Update `hive-thread-list` defaults if needed

## Related Docs

- [Discord Master RPC](./features/discord-master-rpc.md) — Technical RPC layer
- [Hive Protocol](./protocol.md) — Overall coordination protocol
- [Hive Member Skill](../../skills/shared/hive-member/SKILL.md) — Agent usage guide
