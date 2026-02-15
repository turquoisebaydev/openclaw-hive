"""Entry point for hive daemon — MQTT subscribe loop."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import aiomqtt

from hive_daemon.config import HiveConfig, load_config
from hive_daemon.dispatcher import Dispatcher
from hive_daemon.envelope import Envelope, EnvelopeError
from hive_daemon.heartbeat import HeartbeatManager
from hive_daemon.oc_bridge import OcBridge
from hive_daemon.router import Router

log = logging.getLogger("hive_daemon")


@dataclass
class PendingCommand:
    """A command we observed being sent, awaiting a correlated response."""

    corr: str
    to: str
    text: str
    ts: float  # monotonic time for expiry


class CorrelationStore:
    """Tracks outbound commands so inbound responses can be enriched.

    The daemon passively observes all messages on MQTT — when it sees a
    command sent FROM this node, it stores the corr/text. When a response
    arrives matching that corr, it provides the original context.

    Entries expire after ``ttl`` seconds (default 1 hour).
    """

    def __init__(self, ttl: float = 3600.0) -> None:
        self._pending: dict[str, PendingCommand] = {}
        self._ttl = ttl

    def track(self, envelope: Envelope) -> None:
        """Record an outbound command for correlation tracking."""
        # Use envelope.corr if set, otherwise use envelope.id
        # (create_reply uses original.corr or original.id as corr)
        corr = envelope.corr or envelope.id
        self._pending[corr] = PendingCommand(
            corr=corr,
            to=envelope.to,
            text=envelope.text[:500],  # truncate for sanity
            ts=time.monotonic(),
        )
        log.debug("tracking outbound command corr=%s to=%s", corr, envelope.to)
        self._prune()

    def match(self, envelope: Envelope) -> PendingCommand | None:
        """Look up the original command for a correlated response.

        Returns the PendingCommand and removes it from the store,
        or None if no match / expired.
        """
        if not envelope.corr:
            return None
        pending = self._pending.pop(envelope.corr, None)
        if pending is None:
            return None
        if time.monotonic() - pending.ts > self._ttl:
            log.debug("corr=%s expired, discarding", envelope.corr)
            return None
        return pending

    def _prune(self) -> None:
        """Remove expired entries."""
        now = time.monotonic()
        expired = [k for k, v in self._pending.items() if now - v.ts > self._ttl]
        for k in expired:
            del self._pending[k]


def _build_topics(config: HiveConfig) -> list[str]:
    """Build the MQTT subscription topics for this node."""
    prefix = config.topic_prefix
    return [
        f"{prefix}/{config.node_id}/+",  # messages addressed to this node
        f"{prefix}/all/+",               # cluster-wide broadcasts
        f"{prefix}/+/command",           # all outbound commands (for correlation tracking)
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
    corr_store: CorrelationStore | None = None,
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

    # Track outbound commands from this node for correlation.
    # Allow self-messages on broadcast topic ("all") so that --to all
    # commands are processed by every node including the sender.
    topic = str(msg.topic)
    prefix_parts = config.topic_prefix.split("/")
    topic_parts = topic.split("/")
    target = topic_parts[len(prefix_parts)] if len(topic_parts) > len(prefix_parts) else ""

    if envelope.from_ == config.node_id:
        if corr_store is not None and envelope.ch == "command":
            corr_store.track(envelope)
        if target != "all":
            log.debug("ignoring own message %s", envelope.id)
            return

    await router.route(envelope)


def setup_router(
    config: HiveConfig,
    *,
    heartbeat_mgr: HeartbeatManager | None = None,
    oc_bridge: OcBridge | None = None,
    dispatcher: Dispatcher | None = None,
    corr_store: CorrelationStore | None = None,
    mqtt_client: aiomqtt.Client | None = None,
) -> Router:
    """Create a Router with channel handlers.

    Wires real handlers for command, alert, sync, and heartbeat channels.
    Falls back to logging for channels without a dedicated handler.
    """
    router = Router()

    async def _log_handler(envelope: Envelope) -> None:
        log.info("received %s message %s from %s: %s",
                 envelope.ch, envelope.id, envelope.from_, envelope.text[:80])

    async def _publish_dispatch_response(envelope: Envelope, result: "DispatchResult") -> None:
        """Publish a handler's dispatch result back as a response envelope."""
        if mqtt_client is None:
            return
        from hive_daemon.envelope import create_reply
        text = result.stdout.strip() if result.success else f"FAILED (exit {result.exit_code}): {result.stderr.strip()}"
        reply = create_reply(envelope, from_=config.node_id, text=text)
        topic = f"{config.topic_prefix}/{envelope.from_}/response"
        payload = json.dumps(reply.to_json())
        await mqtt_client.publish(topic, payload)
        log.info("published dispatch response %s -> %s", reply.id, topic)

    # --- command channel -> dispatcher (if handler exists) then OC bridge ---
    if dispatcher is not None or oc_bridge is not None:
        async def _command_handler(envelope: Envelope) -> None:
            if dispatcher is not None and envelope.action and dispatcher.has_handler(envelope.action):
                log.info("dispatching command action %r from %s", envelope.action, envelope.id)
                result = await dispatcher.dispatch(envelope)
                await _publish_dispatch_response(envelope, result)
            elif oc_bridge is not None:
                log.info("routing command %s to OC bridge", envelope.id)
                await oc_bridge.inject_envelope(envelope)
            else:
                log.info("command message %s: no dispatcher or OC bridge", envelope.id)
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
                result = await dispatcher.dispatch(envelope)
                await _publish_dispatch_response(envelope, result)
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

    # --- response channel -> enrich with original context, inject to OC ---
    if oc_bridge is not None:
        async def _response_handler(envelope: Envelope) -> None:
            original = corr_store.match(envelope) if corr_store else None
            if original is not None:
                prefix = f're: "{original.text[:200]}"'
                log.info(
                    "routing response %s to OC bridge (corr=%s, enriched with original)",
                    envelope.id, envelope.corr,
                )
                await oc_bridge.inject_envelope(envelope, prefix=prefix)
            else:
                log.info("routing response %s to OC bridge (no original context)", envelope.id)
                await oc_bridge.inject_envelope(envelope)
        router.register("response", _response_handler)
    else:
        router.register("response", _log_handler)

    # --- status -> log only (inject to OC if urgency=now) ---
    if oc_bridge is not None:
        async def _status_handler(envelope: Envelope) -> None:
            if envelope.urgency == "now":
                log.info("routing urgent status %s to OC bridge", envelope.id)
                await oc_bridge.inject_envelope(envelope)
            else:
                log.info("status from %s: %s", envelope.from_, envelope.text[:80])
        router.register("status", _status_handler)
    else:
        router.register("status", _log_handler)

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

    # Correlation store for enriching responses with original command context
    corr_store = CorrelationStore()

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
                    corr_store=corr_store,
                    mqtt_client=client,
                )

                heartbeat_mgr.start()

                try:
                    for topic in topics:
                        await client.subscribe(topic)
                        log.info("subscribed to %s", topic)

                    async for msg in client.messages:
                        if shutdown.is_set():
                            break
                        await _handle_message(msg, config, router, corr_store)
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
