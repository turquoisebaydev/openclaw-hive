"""Entry point for hive daemon — MQTT subscribe loop."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
from pathlib import Path

import aiomqtt

from hive_daemon.config import HiveConfig, load_config
from hive_daemon.envelope import Envelope, EnvelopeError
from hive_daemon.router import Router

log = logging.getLogger("hive_daemon")


def _build_topics(config: HiveConfig) -> list[str]:
    """Build the MQTT subscription topics for this node."""
    prefix = config.topic_prefix
    return [
        f"{prefix}/{config.node_id}/+",  # messages addressed to this node
        f"{prefix}/all/+",               # cluster-wide broadcasts
    ]


def _parse_topic_channel(topic: str, config: HiveConfig) -> str | None:
    """Extract the channel from an MQTT topic string.

    Expected format: {prefix}/{target}/{channel}
    Returns None if the topic doesn't match the expected format.
    """
    prefix = config.topic_prefix
    parts = topic.split("/")
    prefix_parts = prefix.split("/")
    # Topic must be prefix/target/channel
    expected_len = len(prefix_parts) + 2
    if len(parts) != expected_len:
        return None
    # Verify prefix matches
    if parts[:len(prefix_parts)] != prefix_parts:
        return None
    return parts[-1]


async def _handle_message(
    msg: aiomqtt.Message,
    config: HiveConfig,
    router: Router,
) -> None:
    """Parse an MQTT message into an Envelope and route it."""
    topic = str(msg.topic)
    try:
        payload = json.loads(msg.payload)
    except (json.JSONDecodeError, TypeError) as exc:
        log.error("invalid JSON on topic %s: %s", topic, exc)
        return

    try:
        envelope = Envelope.from_json(payload)
    except EnvelopeError as exc:
        log.error("invalid envelope on topic %s: %s", topic, exc)
        return

    # Ignore messages from ourselves
    if envelope.from_ == config.node_id:
        log.debug("ignoring own message %s", envelope.id)
        return

    await router.route(envelope)


def setup_router(config: HiveConfig) -> Router:
    """Create a Router with default channel handlers.

    Channel handlers for command, sync, heartbeat, etc. are registered here.
    Placeholder handlers log the message — real implementations will be added
    in later milestones (heartbeat, oc_bridge, dispatcher).
    """
    router = Router()

    async def _log_handler(envelope: Envelope) -> None:
        log.info("received %s message %s from %s: %s",
                 envelope.ch, envelope.id, envelope.from_, envelope.text[:80])

    for ch in ("command", "response", "sync", "heartbeat", "status", "alert"):
        router.register(ch, _log_handler)

    return router


async def run_daemon(config: HiveConfig) -> None:
    """Main daemon loop: connect to MQTT and process messages."""
    topics = _build_topics(config)
    log.info("starting hive daemon as %s", config.node_id)
    log.info("subscribing to: %s", topics)

    shutdown = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown.set)

    router = setup_router(config)

    while not shutdown.is_set():
        try:
            async with aiomqtt.Client(
                hostname=config.mqtt.host,
                port=config.mqtt.port,
                username=config.mqtt.username,
                password=config.mqtt.password,
                keepalive=config.mqtt.keepalive,
            ) as client:
                for topic in topics:
                    await client.subscribe(topic)
                    log.info("subscribed to %s", topic)

                async for msg in client.messages:
                    if shutdown.is_set():
                        break
                    await _handle_message(msg, config, router)

        except aiomqtt.MqttError as exc:
            if shutdown.is_set():
                break
            log.error("MQTT connection error: %s — reconnecting in 5s", exc)
            await asyncio.sleep(5)

    log.info("hive daemon shutting down")


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Hive coordination daemon")
    parser.add_argument("--config", type=Path, default=Path("hive.toml"), help="config file path")
    args = parser.parse_args()

    config = load_config(args.config)

    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    asyncio.run(run_daemon(config))


if __name__ == "__main__":
    main()
