# Feature Spec — Discord Announcements + Per-Gateway OpenClaw Command

## Status
Draft (ready for implementation)

## Why
For hive operations and debugging, we want optional visibility of hive traffic in Discord (`#hive-announcements`) without changing core routing behavior. We also want confidence that per-gateway OpenClaw command paths remain deterministic and correct.

## Scope
1. **Optional Discord announcements** for hive messages:
   - outbound publish events (send)
   - inbound receive events (receive)
2. **Verification + guardrails** for per-instance (`[[oc_instances]]`) OpenClaw command resolution.

Out of scope (for this round):
- Rich formatting/UI components in Discord
- Historical replay/backfill of old hive messages
- Message batching/throttling policies beyond simple toggles

---

## Proposed Config
Add a top-level `announcements` block in `hive.toml`:

```toml
[announcements]
enabled = false

[announcements.discord]
enabled = false
# either channel id or stable alias/name resolved by sender integration
channel = "hive-announcements"
# optional provider override if needed by integration
provider = "discord"

# direction filters
publish_send = true
publish_receive = true

# optional filters (phase 1 can ignore if not implemented)
# include_channels = ["command", "response", "alert"]
# exclude_channels = ["heartbeat"]
```

### Defaults
- `announcements.enabled = false`
- `announcements.discord.enabled = false`
- `publish_send = true`, `publish_receive = true`
- If disabled/missing, behavior is unchanged from today.

---

## Runtime Behavior

### On send
When hive publishes an envelope, optionally emit a Discord announcement.

Suggested line format (compact):

`HIVE SEND | from=turq to=pg1 ch=command action=git-sync corr=abc123 id=...`

### On receive
When hive receives an envelope (before/after dispatch), optionally emit:

`HIVE RECV | from=pg1 to=turq ch=response corr=abc123 id=...`

### Failure policy
- Discord publish failure must be **non-fatal**.
- Core hive send/receive/dispatch continues.
- Log warning with reason and channel target.

### Security / privacy
- Do not include secrets/tokens in announcement text.
- Truncate long `text` payloads (or omit by default).

---

## Per-Gateway OpenClaw Command (existing + verification)

Current behavior already supports per-instance command override:

```toml
[[oc_instances]]
name = "mini1"
profile = "mini1"
openclaw_cmd = "/opt/openclaw-mini1/bin/openclaw"
```

### Resolution rule
For each oc instance:
1. `oc_instances[i].openclaw_cmd` if non-empty
2. fallback to `openclaw`

Used in:
- OC bridge subprocess commands
- probe/status commands
- handler env var injection (`HIVE_OPENCLAW_CMD`)

### This round requires
- Confirm this still works end-to-end after announcement feature changes.
- Preserve existing compatibility alias `openclaw` field if present.

---

## Environment Context (current)
- Hive dev discussion channel: Discord `#hive-dev` (where implementation is coordinated)
- Announcement target: Discord `#hive-announcements` (configurable value)
- Primary repo: `/Users/turquoise/Projects/openclaw-hive`
- Relevant config example file: `/Users/turquoise/Projects/openclaw-hive/hive.toml`

---

## Acceptance Criteria
1. With announcements disabled, no Discord publishes occur.
2. With announcements enabled:
   - send announcements emitted when `publish_send=true`
   - receive announcements emitted when `publish_receive=true`
3. Discord publish errors do not break hive message flow.
4. Per-instance `openclaw_cmd` resolution remains correct in:
   - OC bridge execution
   - probe/status checks
   - deterministic handler env (`HIVE_OPENCLAW_CMD`)
5. Tests added for config parsing + announcement behavior + command resolution regression.

---

## Test Plan
- Unit tests:
  - config parse defaults + explicit announcement settings
  - send path announcement on/off
  - receive path announcement on/off
  - publish error => warning, no crash
  - `openclaw_cmd` resolution unchanged (regression)
- Optional integration smoke:
  - local broker + fake Discord sink/logger transport

---

## Rollout
1. Ship disabled by default.
2. Enable on one node with `publish_send=true`, `publish_receive=false`.
3. Validate signal/noise.
4. Expand to receive announcements if useful.
