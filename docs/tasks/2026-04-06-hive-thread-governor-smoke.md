# Hive Thread Governor Smoke Plan

## Purpose

Manual MVP verification for the Discord thread governor in `hive-daemon`.

## Preconditions

1. `discord_master.enabled = true`
2. `discord_master.guild_id` and `discord_master.bot_token` are set
3. `discord_master.thread_governor.owner_id` is Hugh's Discord user ID
4. `discord_master.thread_governor.watched_parents` contains the target parent channel ID
5. Bot has:
   - `ViewChannel`
   - `ReadMessageHistory`
   - `SendMessages`
   - `SendMessagesInThreads`
   - `ManageThreads`
   - `UseApplicationCommands`

## Recommended test config

Use a short budget and cooldown for smoke:

```toml
[discord_master.thread_governor]
owner_id = "737554625577746492"
watched_parents = ["1490207637101613076"]
default_limit = 2
auto_unlock_minutes = 1

[discord_master.thread_governor.cleanup]
interval_minutes = 5
idle_expiry_days = 7
archived_expiry_days = 2
```

## Bring-up

1. Start the daemon with the governor-enabled config.
2. Confirm startup logs show:
   - effective governor config
   - `thread governor ready`
   - overdue/pending recovery counts

## Lifecycle smoke

1. Create a fresh thread under a watched parent.
2. Send one non-Hugh human message.
   Expected:
   - state row created
   - discover log emitted
   - `/status` shows `count: 1/2`
3. Send a Hugh message in the same thread.
   Expected:
   - count resets to `0`
   - reset log emitted
4. Run `/budget set 2`.
   Expected:
   - ephemeral confirmation
   - override survives daemon restart
5. Send two non-Hugh human messages after Hugh's reset.
   Expected:
   - thread locks on the second message
   - one notice is posted
   - lock log emitted with `unlock_at`
6. Try another non-Hugh message while locked.
   Expected:
   - Discord blocks the send
   - no duplicate notice
7. Run `/status`.
   Expected:
   - `state: locked`
   - `count: 2/2`
   - short auto-unlock ETA
8. Run `/unlock`.
   Expected:
   - thread unlocks immediately
   - count resets to `0`
   - unlock log emitted
9. Run `/pause`.
   Expected:
   - thread locks immediately
   - `/status` shows locked with fresh ETA
10. Wait for cooldown expiry without sending `/unlock`.
    Expected:
    - thread unlocks silently
    - count resets to `0`
    - unlock log emitted

## Recovery smoke

1. Trigger a lock.
2. Restart `hive-daemon` during the cooldown.
3. If `unlock_at` is already in the past at startup:
   Expected:
   - thread unlocks immediately
   - recovery log emitted
4. If `unlock_at` is still in the future at startup:
   Expected:
   - thread stays locked
   - later auto-unlocks normally

## Cleanup smoke

1. Leave one governed thread idle beyond `idle_expiry_days`, or temporarily lower the value in test config.
2. Archive another governed thread and leave it archived beyond `archived_expiry_days`, or temporarily lower the value in test config.
3. Remove access to a third thread by deleting it.
4. Wait for the cleanup interval.
   Expected:
   - only SQLite state rows are deleted
   - cleanup log emitted for each deletion reason
   - no Discord thread mutations happen during cleanup
