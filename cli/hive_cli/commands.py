"""CLI commands: send, reply, status, roster."""

from __future__ import annotations

import asyncio
import json
import sys
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


def _get_config(ctx: click.Context) -> HiveConfig:
    return ctx.obj["config"]


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


async def _read_retained(cfg: HiveConfig, topic_filter: str, timeout: float = 2.0) -> list[dict]:
    """Subscribe to a topic, collect retained messages, then return.

    Uses a short timeout to wait for retained messages to arrive.
    """
    results: list[dict] = []
    async with _mqtt_client(cfg) as client:
        await client.subscribe(topic_filter)
        try:
            async with asyncio.timeout(timeout):
                async for message in client.messages:
                    try:
                        data = json.loads(message.payload.decode())
                        results.append(data)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue
        except TimeoutError:
            pass
    return results


@click.command()
@click.option("--to", "to_node", required=True, help="Target node id (or 'all').")
@click.option("--ch", "channel", required=True, type=click.Choice(sorted(VALID_CHANNELS)), help="Message channel.")
@click.option("--text", required=True, help="Message text.")
@click.option("--action", default=None, help="Action name for handler dispatch.")
@click.option("--urgency", default="now", type=click.Choice(sorted(VALID_URGENCIES)), help="Message urgency.")
@click.option("--ttl", default=None, type=int, help="Time-to-live in seconds.")
@click.option("--wait", "wait_timeout", default=None, type=float, help="Block up to N seconds for a correlated response.")
@click.pass_context
def send(ctx: click.Context, to_node: str, channel: str, text: str, action: str | None, urgency: str, ttl: int | None, wait_timeout: float | None) -> None:
    """Publish an envelope to MQTT.

    With --wait, blocks until a correlated response arrives (or timeout).
    Prints the response envelope JSON on success, exits 1 on timeout.
    """
    cfg = _get_config(ctx)
    # Use corr = envelope id so the responder's create_reply sets corr automatically
    env = create_envelope(
        from_=cfg.node_id,
        to=to_node,
        ch=channel,
        text=text,
        urgency=urgency,
        ttl=ttl,
        action=action,
    )
    topic = f"{cfg.topic_prefix}/{to_node}/{channel}"
    payload = json.dumps(env.to_json())

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
@click.pass_context
def reply(ctx: click.Context, to_msg: str, text: str) -> None:
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
    asyncio.run(_publish(cfg, topic, payload))
    click.echo(f"reply {env.id} -> {topic} (corr={env.corr})")


@click.command()
@click.pass_context
def status(ctx: click.Context) -> None:
    """Display cluster status from retained meta/state messages."""
    cfg = _get_config(ctx)
    topic_filter = f"{cfg.topic_prefix}/meta/+/state"
    results = asyncio.run(_read_retained(cfg, topic_filter))

    if not results:
        click.echo("no nodes reporting status")
        return

    click.echo(f"{'NODE':<20} {'STATUS':<12} {'LAST SEEN'}")
    click.echo("-" * 50)
    for entry in results:
        node = entry.get("node_id", "?")
        state = entry.get("status", "?")
        last_seen = entry.get("last_seen", "?")
        click.echo(f"{node:<20} {state:<12} {last_seen}")


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
