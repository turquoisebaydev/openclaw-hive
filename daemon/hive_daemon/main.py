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
from hive_daemon.dispatcher import Dispatcher
from hive_daemon.envelope import Envelope, EnvelopeError
from hive_daemon.heartbeat import HeartbeatManager
from hive_daemon.oc_bridge import OcBridge
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


def setup_router(
    config: HiveConfig,
    *,
    heartbeat_mgr: HeartbeatManager | None = None,
    oc_bridge: OcBridge | None = None,
    dispatcher: Dispatcher | None = None,
) -> Router:
    """Create a Router with channel handlers.

    Wires real handlers for command, alert, sync, and heartbeat channels.
    Falls back to logging for channels without a dedicated handler.
    """
    router = Router()

    async def _log_handler(envelope: Envelope) -> None:
        log.info("received %s message %s from %s: %s",
                 envelope.ch, envelope.id, envelope.from_, envelope.text[:80])

    # --- command channel -> OC bridge ---
    if oc_bridge is not None:
        async def _command_handler(envelope: Envelope) -> None:
            log.info("routing command %s to OC bridge", envelope.id)
            await oc_bridge.inject_envelope(envelope)
        router.register("command", _command_handler)
    else:
        router.register("command", _log_handler)

    # --- alert channel -> OC bridge with URGENT prefix ---
    if oc_bridge is not None:
        async def _alert_handler(envelope: Envelope) -> None:
            log.info("routing alert %s to OC bridge (URGENT)", envelope.id)
            await oc_bridge.inject_envelope(envelope, prefix="URGENT")
        router.register("alert", _alert_handler)
    else:
        router.register("alert", _log_handler)

    # --- sync channel -> dispatcher (if handler exists) or OC bridge ---
    if dispatcher is not None or oc_bridge is not None:
        async def _sync_handler(envelope: Envelope) -> None:
            if dispatcher is not None and envelope.action and dispatcher.has_handler(envelope.action):
                log.info("dispatching sync action %r from %s", envelope.action, envelope.id)
                await dispatcher.dispatch(envelope)
            elif oc_bridge is not None:
                log.info("no handler for sync %s, falling back to OC bridge", envelope.id)
                await oc_bridge.inject_envelope(envelope)
            else:
                log.info("sync message %s: no dispatcher or OC bridge", envelope.id)
        router.register("sync", _sync_handler)
    else:
        router.register("sync", _log_handler)

    # --- heartbeat channel -> heartbeat manager ---
    if heartbeat_mgr is not None:
        async def _heartbeat_handler(envelope: Envelope) -> None:
            heartbeat_mgr.track_peer(envelope)
        router.register("heartbeat", _heartbeat_handler)
    else:
        router.register("heartbeat", _log_handler)

    # --- status and response -> log only ---
    router.register("status", _log_handler)
    router.register("response", _log_handler)

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

    # Set up dispatcher
    dispatcher = Dispatcher(config.handler_dir, timeout=config.handler_timeout)
    dispatcher.discover()

    # Set up OC bridge
    oc_bridge = OcBridge(config.oc_instances) if config.oc_instances else None

    while not shutdown.is_set():
        try:
            async with aiomqtt.Client(
                hostname=config.mqtt.host,
                port=config.mqtt.port,
                username=config.mqtt.username,
                password=config.mqtt.password,
                keepalive=config.mqtt.keepalive,
            ) as client:
                # Set up heartbeat manager (needs client for publishing)
                async def _heartbeat_alert(node_id: str, last_seen: float) -> None:
                    log.warning("peer %s missed heartbeat, alerting", node_id)
                    if oc_bridge is not None:
                        await oc_bridge.inject_event(
                            f"[hive:heartbeat] peer {node_id} missed heartbeat — may be offline"
                        )

                heartbeat_mgr = HeartbeatManager(
                    config,
                    client,
                    alert_callback=_heartbeat_alert,
                    interval=config.heartbeat.interval,
                    miss_threshold=config.heartbeat.miss_threshold,
                )

                router = setup_router(
                    config,
                    heartbeat_mgr=heartbeat_mgr,
                    oc_bridge=oc_bridge,
                    dispatcher=dispatcher,
                )

                heartbeat_mgr.start()

                try:
                    for topic in topics:
                        await client.subscribe(topic)
                        log.info("subscribed to %s", topic)

                    async for msg in client.messages:
                        if shutdown.is_set():
                            break
                        await _handle_message(msg, config, router)
                finally:
                    await heartbeat_mgr.stop()

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
