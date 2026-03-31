# Discord Master RPC (hive-daemon)

Single-node Discord API execution with MQTT proxy for non-token nodes.

## Why

- Keep one Discord bot token in one place.
- Avoid duplicate bot behavior across gateways.
- Let every hive node perform thread/history/send/mention operations deterministically.

## Config

```toml
[discord_master]
enabled = true
guild_id = "1476805337968279685"
bot_token = "<discord-bot-token>"      # only on the master node
proxy_to = "turq"                        # on non-master nodes, route discord.* there
api_base = "https://discord.com/api/v10"
request_timeout_sec = 10

[[discord_master.channels]]
name = "qmd"
mention_target = "user:1482865686102671481"
mention_type = "user"                    # auto|user|role
thread_suffix = "-proj"
```

### Master node

- `enabled=true`
- `guild_id` and `bot_token` set
- Executes `discord.*` actions directly via Discord REST API.

### Non-master node

- `enabled=true`
- no `bot_token`
- `proxy_to=<master-node-id>`
- Proxies `discord.*` envelopes over MQTT to master.

## Deterministic actions

All use `hive-cli send --ch command --action <name> --text '<json>'`.

1. `discord.thread.resolve`
- Input: `{ "thread_id": "..." }` OR `{ "thread_name": "QMD-proj" }`
- Output: `{ ok, thread: { id, name, parent_id, archived } }`

2. `discord.thread.history`
- Input: `{ "thread_id": "...", "limit": 20 }`
- Output: `{ ok, thread_id, count, messages[] }`

3. `discord.thread.send`
- Input: `{ "thread_id": "...", "content": "hello" }`
- Output: `{ ok, thread_id, message: { id, channel_id, content } }`

4. `discord.mention.resolve`
- Input: `{ "channel": "qmd" }` OR `{ "mention_target": "user:123" }` OR `{ "query": "Hermes" }`
- Output: `{ ok, mention, mention_type, ... }`

## Example calls

```bash
# Resolve thread by name
hive-cli send --to mini1 --ch command \
  --action discord.thread.resolve \
  --text '{"thread_name":"QMD-proj"}' \
  --wait 10

# Resolve mention from channel config, then send message
hive-cli send --to mini1 --ch command \
  --action discord.mention.resolve \
  --text '{"channel":"qmd"}' \
  --wait 10

hive-cli send --to mini1 --ch command \
  --action discord.thread.send \
  --text '{"thread_id":"1487915581649981451","content":"<@1482865686102671481> ping"}' \
  --wait 10
```

## Notes

- Responses are correlated via normal hive envelope `corr` behavior.
- If neither local token nor `proxy_to` is configured, daemon returns a deterministic failure response.


## High-level caller commands (complexity-free)

These wrappers are for humans/agents that just want "hive threads" and "send hive message".

### List hive project threads (defaults: `-hive` + `-proj`)

```bash
hive-cli hive-thread-list --to <node>
```

- Defaults come from daemon config (`discord_master.default_parent_suffix`, `discord_master.default_thread_suffix`).
- Output includes `mention_user_id` so callers can inspect who would be pinged.

### Send to a hive thread with automatic mention

```bash
hive-cli hive-thread-send \
  --to <node> \
  --thread-id <thread_id> \
  --message "your text"
```

- CLI auto-resolves thread context first.
- If a configured mention exists, CLI prefixes it automatically.
- Use `--no-auto-mention` to disable.
