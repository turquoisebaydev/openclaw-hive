# CLAUDE.md — openclaw-hive

## Project Overview

Hive is an inter-gateway coordination layer for OpenClaw clusters. It connects independent OpenClaw gateway instances via MQTT, enabling cross-gateway commands, event-driven file sync, sentinel monitoring, and machine-to-machine heartbeats — without burning LLM tokens on deterministic operations.

**Read `docs/protocol.md` for the full design document before making architectural decisions.**

## Architecture

```
MQTT (turq/hive/* topics)
     ↕
[hive-daemon]          ← always-running sidecar, one per box (daemon/)
     ↕           ↕
system event   hive-cli (cli/)
     ↕           ↕
[OpenClaw instance(s)]
```

Three components:
- **`daemon/`** — Long-running MQTT subscriber. Routes messages, dispatches handlers, manages heartbeats. No LLM.
- **`cli/`** — Stateless CLI. Send commands, check status, reply to requests. Called by OC agents or humans.
- **`skills/`** — OpenClaw skill files (SKILL.md). Teach LLM agents how to use hive-cli.

Plus pluggable action handlers in `contrib/handlers/` (any language, executed by daemon).

## Language & Style

- **Python 3.11+** for daemon and CLI
- Use `asyncio` for the daemon (MQTT client is async)
- **`aiomqtt`** for MQTT (async wrapper around paho-mqtt)
- Type hints on all functions
- Docstrings on public functions/classes
- Use `dataclasses` or `pydantic` for message envelope models
- **No classes where a function will do** — keep it simple
- Prefer standard library over dependencies where reasonable

## Project Structure

```
daemon/
  hive_daemon/
    __init__.py
    main.py           ← entry point, MQTT subscribe loop
    config.py          ← configuration loading
    router.py          ← message routing by channel
    dispatcher.py      ← handler script dispatch
    heartbeat.py       ← machine-to-machine heartbeat
    envelope.py        ← message envelope model
    oc_bridge.py       ← inject agent messages into local OC
    session_map.py     ← corr→session file-based store (CLI↔daemon IPC)
  tests/
    conftest.py
    test_router.py
    test_dispatcher.py
    test_heartbeat.py
    test_envelope.py
cli/
  hive_cli/
    __init__.py
    main.py           ← entry point (click or argparse)
    commands.py        ← send, reply, status, roster
    envelope.py        ← shared with daemon (or import from daemon)
  tests/
    test_commands.py
skills/
  hive-master/
    SKILL.md
  hive-member/
    SKILL.md
contrib/
  handlers/            ← example hive-daemon.d/ scripts
  shared-memory/       ← git-based cluster memory pattern
```

## Key Concepts

### Message Envelope
Every hive message uses this JSON envelope (see `docs/protocol.md` for details):
```json
{"v": 1, "id": "uuid", "ts": 1234, "from": "turq-18789", "to": "pg1-18890", "ch": "command", "urgency": "now", "text": "..."}
```
Optional fields: `corr`, `replyTo`, `ttl`, `action`

### Channels (`ch` field)
- `command` — do something (usually needs LLM)
- `response` — reply to a command
- `sync` — file sync notification (deterministic)
- `heartbeat` — node aliveness (never LLM)
- `status` — node state changes
- `alert` — escalation (always wake LLM)

### MQTT Topics
```
turq/hive/{to}/{ch}          ← routed messages
turq/hive/meta/{node}/...    ← retained state (status, roster, action results)
```

### Handler Contract (hive-daemon.d/ scripts)
- Filename = action name
- Must be executable (chmod +x)
- Receives full envelope JSON on stdin
- Exit 0 = success, non-zero = failure
- stdout: JSON result object
- stderr: error detail for escalation

### OC Bridge
- Inbound to OC: `openclaw agent --agent main --session-id <id> --message "..."` (CLI command, triggers real LLM turn)
- The daemon decides what goes to OC (commands, alerts, failed syncs) vs what stays local (heartbeats, successful syncs)
- Response routing: when a response arrives with a correlated session mapping (via `session_map.py`), the bridge injects into the originating session instead of the default daily hive session

### Session Map (Response Routing)
- File-based corr→session store at `~/.local/share/hive/session-map.json`
- CLI writes mappings (`hive-cli send --session <key>` and `hive-cli reply --session <key>`), daemon reads on response delivery
- Both `send` and `reply` support `--session` — enables bi-directional session-pinned conversations where both sides route responses to specific sessions
- **Important for reply chains:** when using `hive-cli reply --session`, store the mapping using the correlation id (`corr`) that the next response will carry (and preserve compatibility for existing id-based behavior)
- Mappings have TTL (defaults to `--ttl` value or 1 hour), auto-pruned on read/write
- **Never sent over MQTT** — purely local IPC between CLI and daemon on same host
- Falls back to default hive session if mapping expired, missing, or session gone

## Build & Run

```bash
# Install dependencies
cd daemon && pip install -e ".[dev]"
cd cli && pip install -e ".[dev]"

# Run tests
cd daemon && pytest tests/ -v
cd cli && pytest tests/ -v

# Run daemon (dev mode)
cd daemon && python -m hive_daemon.main --config hive.toml

# CLI usage
hive-cli send --to pg1-18890 --ch command --text "do something"
hive-cli send --to pg1 --ch command --text "check disk" --session "my-session-key"
hive-cli status
hive-cli roster
```

## Testing

- **pytest** for all tests
- Use `pytest-asyncio` for async daemon tests
- Mock MQTT broker with `asyncio` test fixtures (don't require a running Mosquitto)
- Test handler dispatch with subprocess mocking
- Test envelope serialization/deserialization thoroughly
- **Run tests after every significant change**
- Prefer running single test files, not the whole suite, during development

## Design Constraints

- Daemon must work without OC installed (standalone use case)
- CLI must work without daemon running (direct MQTT publish)
- No LLM calls from daemon or CLI — ever. They are deterministic tools.
- Handlers can be any language — the contract is stdin/stdout/exit code
- MQTT topic prefix (`turq/hive/`) should be configurable
- Multi-instance aware: one daemon per box, but a box may run multiple OC instances
- Config via TOML file (not environment variables for complex config)

## Git Workflow

- Commit early and often at task checkpoints
- Each commit should pass tests
- Use conventional commit messages: `feat:`, `fix:`, `test:`, `docs:`
- Don't reference AI/Claude in commit messages
