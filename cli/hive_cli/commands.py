"""CLI commands: send, reply, status, roster."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import aiomqtt
import click

from hive_daemon.config import HiveConfig
from hive_daemon.envelope import (
    Envelope,
    create_envelope,
    create_reply,
    VALID_CHANNELS,
    VALID_URGENCIES,
)
try:
    from hive_daemon.presence import (
        PresenceCache,
        PresenceRecord,
        read_local_session_history,
        resolve_session_target,
    )
except ImportError:  # backward compat with nodes missing session-history helpers
    from hive_daemon.presence import (
        PresenceCache,
        PresenceRecord,
        resolve_session_target,
    )

    def read_local_session_history(*args, **kwargs):  # type: ignore[override]
        return []


def _get_config(ctx: click.Context) -> HiveConfig:
    return ctx.obj["config"]


def _format_age(ts: int) -> str:
    """Format a timestamp as human-readable age (e.g., '5s ago', '2m ago', '3h ago')."""
    age_s = time.time() - ts
    if age_s < 60:
        return f"{int(age_s)}s ago"
    elif age_s < 3600:
        return f"{int(age_s / 60)}m ago"
    elif age_s < 86400:
        return f"{int(age_s / 3600)}h ago"
    else:
        return f"{int(age_s / 86400)}d ago"


def _mqtt_client(cfg: HiveConfig) -> aiomqtt.Client:
    """Build an aiomqtt Client from config."""
    return aiomqtt.Client(
        hostname=cfg.mqtt.host,
        port=cfg.mqtt.port,
        username=cfg.mqtt.username,
        password=cfg.mqtt.password,
    )


async def _publish(cfg: HiveConfig, topic: str, payload: str) -> None:
    """Connect, publish one message, disconnect."""
    async with _mqtt_client(cfg) as client:
        await client.publish(topic, payload.encode())


async def _publish_and_wait(
    cfg: HiveConfig,
    topic: str,
    payload: str,
    corr: str,
    wait_timeout: float,
) -> Envelope | None:
    """Publish a message and block until a correlated response arrives.

    Subscribes to the sender's response topic, publishes the message,
    then waits up to ``wait_timeout`` seconds for a response whose
    ``corr`` field matches. Returns the response Envelope or None on timeout.
    """
    response_topic = f"{cfg.topic_prefix}/{cfg.node_id}/response"
    async with _mqtt_client(cfg) as client:
        await client.subscribe(response_topic)
        # Also subscribe to broadcast responses
        await client.subscribe(f"{cfg.topic_prefix}/all/response")
        # Publish after subscribing so we don't miss a fast reply
        await client.publish(topic, payload.encode())
        try:
            async with asyncio.timeout(wait_timeout):
                async for message in client.messages:
                    try:
                        data = json.loads(message.payload.decode())
                        env = Envelope.from_json(data)
                    except (json.JSONDecodeError, UnicodeDecodeError, Exception):
                        continue
                    if env.corr == corr:
                        return env
        except TimeoutError:
            return None
    return None


async def _read_presence(cfg: HiveConfig, timeout: float = 2.0) -> PresenceCache:
    """Read retained presence records from MQTT into a PresenceCache."""
    topic_filter = f"{cfg.topic_prefix}/presence/#"
    cache = PresenceCache()
    async with _mqtt_client(cfg) as client:
        await client.subscribe(topic_filter)
        try:
            async with asyncio.timeout(timeout):
                async for message in client.messages:
                    try:
                        data = json.loads(message.payload.decode())
                        record = PresenceRecord.from_dict(data)
                        cache.update(record)
                    except (json.JSONDecodeError, UnicodeDecodeError, ValueError, KeyError, TypeError):
                        continue
        except TimeoutError:
            pass
    return cache


def _parse_session_target(target: str) -> tuple[str, str | None, str]:
    """Parse a session target string into (gw, agent, session).

    Accepts:
        gw/agent/session  -> (gw, agent, session)
        gw/session        -> (gw, None, session)
    """
    parts = target.split("/", 2)
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]
    elif len(parts) == 2:
        return parts[0], None, parts[1]
    else:
        raise click.BadParameter(
            f"invalid session target format: {target!r}. "
            "Expected gw/agent/session or gw/session."
        )


def _sorted_presence_records(cache: PresenceCache) -> list[PresenceRecord]:
    records = cache.all_fresh()
    records.sort(key=lambda r: (r.gw, r.agent, r.session))
    return records


async def _read_retained(cfg: HiveConfig, topic_filter: str, timeout: float = 2.0) -> list[dict]:
    """Subscribe to a topic, collect retained messages, then return.

    We want the *latest retained value per topic*. When a publisher updates
    retained state frequently, we may see multiple messages per topic during
    the short collection window. We dedupe by topic and keep the most recent.
    """
    latest_by_topic: dict[str, dict] = {}
    async with _mqtt_client(cfg) as client:
        await client.subscribe(topic_filter)
        try:
            async with asyncio.timeout(timeout):
                async for message in client.messages:
                    try:
                        data = json.loads(message.payload.decode())
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue
                    latest_by_topic[str(message.topic)] = data
        except TimeoutError:
            pass
    return list(latest_by_topic.values())


@click.command()
@click.option("--to", "to_node", required=False, default=None, help="Target node id (or 'all').")
@click.option("--to-session", "to_session", default=None, help="Target session as gw/agent/session or gw/session.")
@click.option("--ch", "channel", required=True, type=click.Choice(sorted(VALID_CHANNELS)), help="Message channel.")
@click.option("--text", required=True, help="Message text.")
@click.option("--action", default=None, help="Action name for handler dispatch.")
@click.option("--urgency", default="now", type=click.Choice(sorted(VALID_URGENCIES)), help="Message urgency.")
@click.option("--ttl", default=None, type=int, help="Time-to-live in seconds.")
@click.option("--wait", "wait_timeout", default=None, type=float, help="Block up to N seconds for a correlated response.")
@click.option("--session", default=None, help="Session key to route the response back to (stored locally, not sent over MQTT).")
@click.pass_context
def send(ctx: click.Context, to_node: str | None, to_session: str | None, channel: str, text: str, action: str | None, urgency: str, ttl: int | None, wait_timeout: float | None, session: str | None) -> None:
    """Publish an envelope to MQTT.

    With --wait, blocks until a correlated response arrives (or timeout).
    Prints the response envelope JSON on success, exits 1 on timeout.

    Use --to-session to target a specific session (gw/agent/session or gw/session).
    The presence cache is consulted to resolve the target deterministically.
    """
    cfg = _get_config(ctx)

    # Validate: exactly one of --to or --to-session must be provided
    if to_node is None and to_session is None:
        raise click.UsageError("Either --to or --to-session is required.")
    if to_node is not None and to_session is not None:
        raise click.UsageError("Cannot use both --to and --to-session.")

    # Session-targeted send: resolve via presence cache
    if to_session is not None:
        gw, agent, sess = _parse_session_target(to_session)
        cache = asyncio.run(_read_presence(cfg))
        result = resolve_session_target(cache, gw=gw, agent=agent, session=sess)
        if not result.ok:
            click.echo(json.dumps(result.error, indent=2), err=True)
            ctx.exit(1)
            return
        # Resolved — route to the gateway node
        to_node = result.record.gw
        to_session = result.record.session

    # Use corr = envelope id so the responder's create_reply sets corr automatically
    env = create_envelope(
        from_=cfg.node_id,
        to=to_node,
        ch=channel,
        text=text,
        urgency=urgency,
        ttl=ttl,
        action=action,
        target_session=to_session,
    )
    topic = f"{cfg.topic_prefix}/{to_node}/{channel}"
    payload = json.dumps(env.to_json())

    # Store session mapping locally (never goes on MQTT)
    if session is not None:
        from hive_daemon.session_map import put as session_map_put
        map_ttl = ttl if ttl is not None else 3600
        session_map_put(env.id, session, ttl=map_ttl)
        click.echo(f"session map: {env.id} -> {session} (ttl={map_ttl}s)")

    if wait_timeout is not None:
        # Synchronous send-and-wait: block for correlated response
        corr = env.id  # create_reply uses original.corr or original.id
        response = asyncio.run(_publish_and_wait(cfg, topic, payload, corr, wait_timeout))
        if response is not None:
            click.echo(json.dumps(response.to_json(), indent=2))
        else:
            click.echo(f"timeout: no response for {env.id} after {wait_timeout}s", err=True)
            ctx.exit(1)
    else:
        # Fire-and-forget
        asyncio.run(_publish(cfg, topic, payload))
        click.echo(f"sent {env.id} -> {topic}")


@click.command()
@click.option("--to-msg", "to_msg", required=True, help="Original envelope as JSON string or path to JSON file.")
@click.option("--text", required=True, help="Reply text.")
@click.option("--session", default=None, help="Session key to route the response back to (stored locally, not sent over MQTT).")
@click.pass_context
def reply(ctx: click.Context, to_msg: str, text: str, session: str | None) -> None:
    """Publish a reply to a previous message."""
    cfg = _get_config(ctx)

    # Try to read as file first, then as JSON string
    msg_path = Path(to_msg)
    if msg_path.is_file():
        raw = msg_path.read_text()
    else:
        raw = to_msg

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise click.BadParameter(f"invalid JSON: {exc}") from exc

    original = Envelope.from_json(data)
    env = create_reply(original, from_=cfg.node_id, text=text)
    topic = f"{cfg.topic_prefix}/{env.to}/{env.ch}"
    payload = json.dumps(env.to_json())

    # Store session mapping locally (never goes on MQTT)
    if session is not None:
        from hive_daemon.session_map import put as session_map_put
        # Use original envelope's ttl if available, else 3600
        map_ttl = original.ttl if original.ttl is not None else 3600
        # Key by corr (what future responses will carry), not reply envelope id
        session_map_put(env.corr, session, ttl=map_ttl)
        click.echo(f"session map: {env.corr} -> {session} (ttl={map_ttl}s)")

    asyncio.run(_publish(cfg, topic, payload))
    click.echo(f"reply {env.id} -> {topic} (corr={env.corr})")


@click.command()
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@click.pass_context
def status(ctx: click.Context, as_json: bool) -> None:
    """Display cluster status from retained meta/state messages."""
    cfg = _get_config(ctx)
    topic_filter = f"{cfg.topic_prefix}/meta/+/state"
    results = asyncio.run(_read_retained(cfg, topic_filter))

    if not results:
        click.echo("no nodes reporting status")
        return

    # Stable ordering for humans.
    results.sort(key=lambda e: str(e.get("node_id") or ""))

    if as_json:
        click.echo(json.dumps(results, indent=2))
        return

    # Staleness threshold in seconds
    stale_threshold_s = 30

    click.echo(f"{'NODE':<20} {'STATUS':<10} {'GW':<4} {'CRON':<5} {'ERR_1H':<6} {'OC_CMD':<14} {'LAST SEEN'}")
    click.echo("-" * 88)

    for entry in results:
        node = entry.get("node_id", "?")
        state = entry.get("status", "?")
        last_seen_raw = entry.get("last_seen")

        # Apply staleness detection
        if isinstance(last_seen_raw, int):
            if time.time() - last_seen_raw > stale_threshold_s:
                state = "stale"
            last_seen = _format_age(last_seen_raw)
        else:
            last_seen = str(last_seen_raw) if last_seen_raw is not None else "?"

        oc = entry.get("oc") if isinstance(entry.get("oc"), dict) else {}
        gw = oc.get("gw") if isinstance(oc.get("gw"), dict) else {}
        cron = oc.get("cron") if isinstance(oc.get("cron"), dict) else {}
        cron_status = cron.get("status") if isinstance(cron.get("status"), dict) else {}
        errs = oc.get("errors") if isinstance(oc.get("errors"), dict) else {}
        counts = errs.get("counts") if isinstance(errs.get("counts"), dict) else {}

        gw_ok = gw.get("rpcOk")
        gw_cell = "ok" if gw_ok is True else ("no" if gw_ok is False else "?")

        cron_jobs = cron_status.get("jobs")
        cron_cell = str(cron_jobs) if isinstance(cron_jobs, int) else "?"

        err_total = 0
        for v in counts.values():
            if isinstance(v, int):
                err_total += v
        err_cell = str(err_total)

        cmd_cell = "?"
        cmd_raw = gw.get("openclawCmd")
        if isinstance(cmd_raw, str) and cmd_raw.strip():
            cmd_cell = Path(cmd_raw).name or cmd_raw
            if len(cmd_cell) > 14:
                cmd_cell = cmd_cell[:11] + "..."

        click.echo(f"{str(node):<20} {str(state):<10} {gw_cell:<4} {cron_cell:<5} {err_cell:<6} {cmd_cell:<14} {last_seen}")


@click.command(name="sessions")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@click.pass_context
def sessions(ctx: click.Context, as_json: bool) -> None:
    """Display retained live session presence, including recent message previews."""
    cfg = _get_config(ctx)
    cache = asyncio.run(_read_presence(cfg))
    records = _sorted_presence_records(cache)

    if not records:
        click.echo("no active sessions")
        return

    if as_json:
        click.echo(json.dumps([record.to_dict() for record in records], indent=2))
        return

    click.echo(f"{'GW':<8} {'AGENT':<8} {'TYPE':<10} {'MODEL':<22} {'STATUS':<8} SESSION")
    click.echo("-" * 96)
    for record in records:
        model = record.model if len(record.model) <= 22 else record.model[:19] + "..."
        session_type = record.session_type or "-"
        click.echo(
            f"{record.gw:<8} {record.agent:<8} {session_type:<10} "
            f"{model:<22} {record.status:<8} {record.session}"
        )
        if record.task.summary:
            click.echo(f"  summary: {record.task.summary}")
        for msg in record.recent_messages:
            click.echo(f"  {msg.role}: {msg.text}")


@click.command(name="session-history")
@click.argument("target")
@click.option("--limit", default=100, type=int, show_default=True, help="Maximum transcript messages to return.")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@click.pass_context
def session_history(ctx: click.Context, target: str, limit: int, as_json: bool) -> None:
    """Display transcript history for one locally managed session."""
    cfg = _get_config(ctx)
    gw, agent, sess = _parse_session_target(target)
    cache = asyncio.run(_read_presence(cfg))
    result = resolve_session_target(cache, gw=gw, agent=agent, session=sess)
    if not result.ok:
        click.echo(json.dumps(result.error, indent=2), err=True)
        ctx.exit(1)
        return

    history = read_local_session_history(
        cfg,
        gw=result.record.gw,
        session=result.record.session,
        limit=limit,
    )
    if not history:
        click.echo("session history not available locally", err=True)
        ctx.exit(1)
        return

    if as_json:
        click.echo(json.dumps(history, indent=2))
        return

    for item in history:
        ts = item.get("ts", "")
        if ts:
            click.echo(f"[{ts}] {item.get('role', '?')}: {item.get('text', '')}")
        else:
            click.echo(f"{item.get('role', '?')}: {item.get('text', '')}")


@click.command()
@click.pass_context
def roster(ctx: click.Context) -> None:
    """Display handler capabilities per node from retained meta/roster messages."""
    cfg = _get_config(ctx)
    topic_filter = f"{cfg.topic_prefix}/meta/+/roster"
    results = asyncio.run(_read_retained(cfg, topic_filter))

    if not results:
        click.echo("no roster data available")
        return

    for entry in results:
        node = entry.get("node_id", "?")
        handlers = entry.get("handlers", [])
        click.echo(f"{node}:")
        if handlers:
            for h in handlers:
                click.echo(f"  - {h}")
        else:
            click.echo("  (no handlers)")


async def _send_discord_action(
    cfg: HiveConfig,
    *,
    to_node: str,
    action: str,
    payload: dict,
    wait_timeout: float,
) -> Envelope | None:
    """Send a discord.* deterministic action and wait for correlated response."""
    env = create_envelope(
        from_=cfg.node_id,
        to=to_node,
        ch="command",
        text=json.dumps(payload),
        urgency="now",
        action=action,
    )
    topic = f"{cfg.topic_prefix}/{to_node}/command"
    return await _publish_and_wait(cfg, topic, json.dumps(env.to_json()), env.id, wait_timeout)


def _print_discord_response(ctx: click.Context, response: Envelope | None, wait_timeout: float) -> None:
    if response is None:
        click.echo(f"timeout: no response after {wait_timeout}s", err=True)
        ctx.exit(1)
        return

    text = response.text or ""
    if text.startswith("FAILED:"):
        click.echo(text, err=True)
        ctx.exit(1)
        return

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        click.echo(text)
        return

    click.echo(json.dumps(parsed, indent=2))


@click.group(name="discord")
def discord() -> None:
    """Deterministic Discord master RPC actions via hive-daemon."""


@discord.command(name="thread-send")
@click.option("--to", "to_node", required=True, help="Target node (local daemon may proxy to master).")
@click.option("--thread-id", required=True, help="Discord thread channel id.")
@click.option("--content", required=True, help="Message content.")
@click.option("--wait", "wait_timeout", default=20.0, show_default=True, type=float, help="Wait timeout (seconds).")
@click.pass_context
def discord_thread_send(ctx: click.Context, to_node: str, thread_id: str, content: str, wait_timeout: float) -> None:
    cfg = _get_config(ctx)
    response = asyncio.run(
        _send_discord_action(
            cfg,
            to_node=to_node,
            action="discord.thread.send",
            payload={"thread_id": thread_id, "content": content},
            wait_timeout=wait_timeout,
        )
    )
    _print_discord_response(ctx, response, wait_timeout)


@discord.command(name="thread-history")
@click.option("--to", "to_node", required=True, help="Target node (local daemon may proxy to master).")
@click.option("--thread-id", required=True, help="Discord thread channel id.")
@click.option("--limit", default=20, show_default=True, type=int, help="Max messages (1-100).")
@click.option("--wait", "wait_timeout", default=20.0, show_default=True, type=float, help="Wait timeout (seconds).")
@click.pass_context
def discord_thread_history(ctx: click.Context, to_node: str, thread_id: str, limit: int, wait_timeout: float) -> None:
    cfg = _get_config(ctx)
    response = asyncio.run(
        _send_discord_action(
            cfg,
            to_node=to_node,
            action="discord.thread.history",
            payload={"thread_id": thread_id, "limit": max(1, min(limit, 100))},
            wait_timeout=wait_timeout,
        )
    )
    _print_discord_response(ctx, response, wait_timeout)


@discord.command(name="thread-resolve")
@click.option("--to", "to_node", required=True, help="Target node (local daemon may proxy to master).")
@click.option("--thread-id", default=None, help="Discord thread channel id.")
@click.option("--thread-name", default=None, help="Thread name to resolve.")
@click.option("--wait", "wait_timeout", default=20.0, show_default=True, type=float, help="Wait timeout (seconds).")
@click.pass_context
def discord_thread_resolve(
    ctx: click.Context,
    to_node: str,
    thread_id: str | None,
    thread_name: str | None,
    wait_timeout: float,
) -> None:
    if not thread_id and not thread_name:
        raise click.UsageError("Provide --thread-id or --thread-name")

    cfg = _get_config(ctx)
    payload: dict[str, str] = {}
    if thread_id:
        payload["thread_id"] = thread_id
    if thread_name:
        payload["thread_name"] = thread_name

    response = asyncio.run(
        _send_discord_action(
            cfg,
            to_node=to_node,
            action="discord.thread.resolve",
            payload=payload,
            wait_timeout=wait_timeout,
        )
    )
    _print_discord_response(ctx, response, wait_timeout)


@discord.command(name="mention-resolve")
@click.option("--to", "to_node", required=True, help="Target node (local daemon may proxy to master).")
@click.option("--channel", default=None, help="Channel mapping name from discord_master.channels[].")
@click.option("--query", default=None, help="Name query for member/role search.")
@click.option("--mention-target", default=None, help="Explicit target (user:<id>, role:<id>, <@id>, <@&id>).")
@click.option("--mention-type", default="auto", type=click.Choice(["auto", "user", "role"]), show_default=True)
@click.option("--wait", "wait_timeout", default=20.0, show_default=True, type=float, help="Wait timeout (seconds).")
@click.pass_context
def discord_mention_resolve(
    ctx: click.Context,
    to_node: str,
    channel: str | None,
    query: str | None,
    mention_target: str | None,
    mention_type: str,
    wait_timeout: float,
) -> None:
    if not any([channel, query, mention_target]):
        raise click.UsageError("Provide at least one of --channel, --query, or --mention-target")

    cfg = _get_config(ctx)
    payload: dict[str, str] = {"mention_type": mention_type}
    if channel:
        payload["channel"] = channel
    if query:
        payload["query"] = query
    if mention_target:
        payload["mention_target"] = mention_target

    response = asyncio.run(
        _send_discord_action(
            cfg,
            to_node=to_node,
            action="discord.mention.resolve",
            payload=payload,
            wait_timeout=wait_timeout,
        )
    )
    _print_discord_response(ctx, response, wait_timeout)


def _discord_response_json(response: Envelope | None) -> dict | None:
    if response is None:
        return None
    try:
        return json.loads(response.text or "")
    except json.JSONDecodeError:
        return None


@click.command(name="hive-thread-list")
@click.option("--to", "to_node", default=None, help="Target node (defaults to local node id).")
@click.option("--parent-suffix", default=None, help="Parent channel suffix filter (default from daemon config, usually -hive).")
@click.option("--thread-suffix", default=None, help="Thread suffix filter (default from daemon config, usually -init).")
@click.option("--wait", "wait_timeout", default=20.0, show_default=True, type=float, help="Wait timeout (seconds).")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@click.pass_context
def hive_thread_list(
    ctx: click.Context,
    to_node: str | None,
    parent_suffix: str | None,
    thread_suffix: str | None,
    wait_timeout: float,
    as_json: bool,
) -> None:
    """List hive threads (default: -init under -hive channels) with mention user ids."""
    cfg = _get_config(ctx)
    target = to_node or cfg.node_id
    payload: dict[str, str] = {}
    if parent_suffix is not None:
        payload["parent_suffix"] = parent_suffix
    if thread_suffix is not None:
        payload["thread_suffix"] = thread_suffix

    response = asyncio.run(
        _send_discord_action(
            cfg,
            to_node=target,
            action="discord.thread.list",
            payload=payload,
            wait_timeout=wait_timeout,
        )
    )
    parsed = _discord_response_json(response)
    if response is None:
        click.echo(f"timeout: no response after {wait_timeout}s", err=True)
        ctx.exit(1)
        return
    if parsed is None:
        click.echo(response.text)
        return
    if parsed.get("ok") is not True:
        click.echo(json.dumps(parsed, indent=2), err=True)
        ctx.exit(1)
        return

    if as_json:
        click.echo(json.dumps(parsed, indent=2))
        return

    threads = parsed.get("threads", []) or []
    if not threads:
        click.echo("no matching hive threads")
        return

    click.echo(f"filters: parent_suffix={parsed.get('parent_suffix')} thread_suffix={parsed.get('thread_suffix')}")
    click.echo(f"{'NAME':<26} {'ID':<20} {'TYPE':<9} {'PARENT':<18} {'MENTION_USER_ID':<20}")
    click.echo("-" * 103)
    for t in threads:
        click.echo(
            f"{str(t.get('name',''))[:26]:<26} {str(t.get('id','')):<20} "
            f"{str(t.get('type','thread'))[:9]:<9} "
            f"{str(t.get('parent_name') or '-')[:18]:<18} {str(t.get('mention_user_id') or '-'): <20}"
        )


@click.command(name="hive-thread-send")
@click.option("--to", "to_node", default=None, help="Target node (defaults to local node id).")
@click.option("--thread-id", required=True, help="Discord thread channel id.")
@click.option("--message", required=True, help="Message body to send.")
@click.option("--no-auto-mention", is_flag=True, help="Disable automatic mention prefixing.")
@click.option("--wait", "wait_timeout", default=20.0, show_default=True, type=float, help="Wait timeout (seconds).")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON response.")
@click.pass_context
def hive_thread_send(
    ctx: click.Context,
    to_node: str | None,
    thread_id: str,
    message: str,
    no_auto_mention: bool,
    wait_timeout: float,
    as_json: bool,
) -> None:
    """Send to a hive thread with automatic mention resolution."""
    cfg = _get_config(ctx)
    target = to_node or cfg.node_id

    final_message = message.strip()
    if not final_message:
        raise click.UsageError("--message must be non-empty")

    if not no_auto_mention:
        resolve_response = asyncio.run(
            _send_discord_action(
                cfg,
                to_node=target,
                action="discord.thread.resolve",
                payload={"thread_id": thread_id},
                wait_timeout=wait_timeout,
            )
        )
        parsed_resolve = _discord_response_json(resolve_response)
        if parsed_resolve and parsed_resolve.get("ok") is True:
            mention = ((parsed_resolve.get("thread") or {}).get("mention") or "").strip()
            if mention and mention not in final_message:
                final_message = f"{mention} {final_message}"

    send_response = asyncio.run(
        _send_discord_action(
            cfg,
            to_node=target,
            action="discord.thread.send",
            payload={"thread_id": thread_id, "content": final_message},
            wait_timeout=wait_timeout,
        )
    )

    parsed_send = _discord_response_json(send_response)
    if send_response is None:
        click.echo(f"timeout: no response after {wait_timeout}s", err=True)
        ctx.exit(1)
        return
    if parsed_send is None:
        click.echo(send_response.text)
        return
    if parsed_send.get("ok") is not True:
        click.echo(json.dumps(parsed_send, indent=2), err=True)
        ctx.exit(1)
        return

    if as_json:
        click.echo(json.dumps(parsed_send, indent=2))
        return

    msg = parsed_send.get("message") or {}
    click.echo(f"sent {msg.get('id')} -> thread {parsed_send.get('thread_id')}")
    click.echo(msg.get("content") or "")
