"""Machine-to-machine heartbeat manager.

Publishes heartbeats at a configurable interval and tracks heartbeats
from peer nodes. Detects missed heartbeats and fires an alert callback
when a node goes silent.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import aiomqtt

from hive_daemon.config import HiveConfig, OcInstance
from hive_daemon.envelope import Envelope, create_envelope
from hive_daemon.probe import probe_instance

log = logging.getLogger(__name__)

# Type for the alert callback invoked when a peer misses heartbeats.
AlertCallback = Callable[[str, float], Awaitable[None]]


@dataclass
class PeerState:
    """Tracked state for a peer node."""

    node_id: str
    last_seen: float
    payload: dict | None = None


class HeartbeatManager:
    """Manages heartbeat publishing and peer tracking.

    Publishes heartbeat envelopes on ``{prefix}/all/heartbeat`` at a
    configurable interval. Tracks heartbeats from other nodes and detects
    when peers go silent (missed heartbeat threshold exceeded).

    Args:
        config: The hive daemon configuration.
        client: An active aiomqtt Client for publishing.
        alert_callback: Async callable invoked with ``(node_id, last_seen_ts)``
            when a peer misses too many heartbeats.
        interval: Heartbeat publish interval in seconds (default 5).
        miss_threshold: Number of missed intervals before alerting (default 3).
    """

    def __init__(
        self,
        config: HiveConfig,
        client: aiomqtt.Client,
        alert_callback: AlertCallback | None = None,
        interval: float = 5.0,
        miss_threshold: int = 3,
    ) -> None:
        self._config = config
        self._client = client
        self._alert_callback = alert_callback
        self._interval = interval
        self._miss_threshold = miss_threshold
        self._peers: dict[str, PeerState] = {}
        self._start_time = time.monotonic()
        self._publish_task: asyncio.Task | None = None
        self._check_task: asyncio.Task | None = None
        self._probe_task: asyncio.Task | None = None

        # Latest deterministic probe results per managed OC instance name.
        # Published via retained meta/<name>/state.
        self._probe: dict[str, dict] = {}
        self._probe_interval = 30.0

    @property
    def known_peers(self) -> dict[str, PeerState]:
        """Map of node_id -> PeerState for all tracked peers."""
        return dict(self._peers)

    def _all_known_instances(self) -> list[str]:
        """Expand known daemon peers into their managed OC instance names.

        Uses the ``oc_instances`` list from each peer's heartbeat payload
        to resolve individual gateway names. Our own managed instances are
        always included. Falls back to the daemon node_id when a peer's
        payload doesn't include instance info.
        """
        instances: set[str] = set()
        # Our own instances
        for inst in self._config.oc_instances:
            instances.add(inst.name)
        if not self._config.oc_instances:
            instances.add(self._config.node_id)
        # Remote peer instances (from heartbeat payloads)
        for peer in self._peers.values():
            if peer.payload and "oc_instances" in peer.payload:
                for oc in peer.payload["oc_instances"]:
                    if isinstance(oc, dict) and "name" in oc:
                        instances.add(oc["name"])
            else:
                # Fallback: use the daemon node_id
                instances.add(peer.node_id)
        return sorted(instances)

    def _build_heartbeat_payload(self) -> str:
        """Build the JSON text payload for a heartbeat message."""
        uptime_s = round(time.monotonic() - self._start_time, 1)
        try:
            load_1m = os.getloadavg()[0]
        except OSError:
            load_1m = 0.0

        oc_instances = []
        for inst in self._config.oc_instances:
            oc_instances.append({"name": inst.name, "status": "configured"})

        payload = {
            "node_id": self._config.node_id,
            "uptime_s": uptime_s,
            "load_1m": round(load_1m, 2),
            "oc_instances": oc_instances,
        }
        return json.dumps(payload)

    async def publish_heartbeat(self) -> None:
        """Publish a single heartbeat envelope."""
        text = self._build_heartbeat_payload()
        topic = f"{self._config.topic_prefix}/all/heartbeat"

        envelope = create_envelope(
            from_=self._config.node_id,
            to="all",
            ch="heartbeat",
            text=text,
            urgency="later",
        )

        await self._client.publish(topic, json.dumps(envelope.to_json()))
        log.debug("published heartbeat to %s", topic)

    async def publish_state(self) -> None:
        """Publish retained state for each managed OC instance.

        Each instance gets its own ``meta/{instance_name}/state`` entry so
        that ``hive-cli status`` sees every gateway as a first-class node.
        Falls back to publishing a single entry for ``node_id`` when no OC
        instances are configured.
        """
        uptime_s = round(time.monotonic() - self._start_time, 1)
        known_peers = self._all_known_instances()

        instance_names = [inst.name for inst in self._config.oc_instances]
        if not instance_names:
            # No OC instances — publish a single daemon-level state entry.
            instance_names = [self._config.node_id]

        for name in instance_names:
            state = {
                "node_id": name,
                "status": "online",
                "last_seen": int(time.time()),
                "uptime_s": uptime_s,
                "known_peers": known_peers,
                "daemon_node": self._config.node_id,
            }

            probe = self._probe.get(name)
            if isinstance(probe, dict) and probe:
                # Deterministic gateway probe snapshot (no LLM calls).
                state["oc"] = probe
            topic = f"{self._config.topic_prefix}/meta/{name}/state"
            await self._client.publish(topic, json.dumps(state), retain=True)
            log.debug("published instance state to %s", topic)

    def track_peer(self, envelope: Envelope) -> None:
        """Record a heartbeat from a peer node.

        Call this from the router's heartbeat channel handler.
        """
        node_id = envelope.from_
        try:
            payload = json.loads(envelope.text)
        except (json.JSONDecodeError, TypeError):
            payload = None

        now = time.monotonic()
        if node_id not in self._peers:
            log.info("new peer discovered via heartbeat: %s", node_id)

        self._peers[node_id] = PeerState(
            node_id=node_id,
            last_seen=now,
            payload=payload,
        )

    async def check_peers(self) -> list[str]:
        """Check for peers that have missed their heartbeat threshold.

        Returns a list of node_ids that were detected as missing.
        Fires the alert callback for each missing peer.
        """
        now = time.monotonic()
        deadline = now - (self._interval * self._miss_threshold)
        missing: list[str] = []

        for node_id, peer in list(self._peers.items()):
            if peer.last_seen < deadline:
                missing.append(node_id)
                log.warning(
                    "peer %s missed heartbeat (last seen %.1fs ago, threshold %.1fs)",
                    node_id,
                    now - peer.last_seen,
                    self._interval * self._miss_threshold,
                )
                if self._alert_callback:
                    await self._alert_callback(node_id, peer.last_seen)

        # Remove stale peers after alerting
        for node_id in missing:
            del self._peers[node_id]

        return missing

    async def _publish_loop(self) -> None:
        """Background loop: publish heartbeats at the configured interval."""
        while True:
            try:
                await self.publish_heartbeat()
                await self.publish_state()
            except aiomqtt.MqttError as exc:
                log.error("heartbeat publish failed: %s", exc)
            await asyncio.sleep(self._interval)

    async def _check_loop(self) -> None:
        """Background loop: check for missing peers at each interval."""
        while True:
            await asyncio.sleep(self._interval)
            try:
                await self.check_peers()
            except Exception:
                log.exception("error checking peers")

    async def _probe_loop(self) -> None:
        """Background loop: probe local OC instances (deterministic).

        Runs less frequently than heartbeats; results are cached and included
        in retained `meta/<instance>/state` messages.
        """
        while True:
            try:
                for inst in self._config.oc_instances:
                    res = await probe_instance(inst)
                    # Keep a stable, compact schema for CLI display.
                    self._probe[inst.name] = {
                        "ts": res.ts,
                        **(res.data or {}),
                    }
            except Exception:
                log.exception("gateway probe failed")
            await asyncio.sleep(self._probe_interval)

    def start(self) -> None:
        """Start the heartbeat publish and peer-check background tasks."""
        self._publish_task = asyncio.create_task(self._publish_loop())
        self._check_task = asyncio.create_task(self._check_loop())
        if self._config.oc_instances:
            self._probe_task = asyncio.create_task(self._probe_loop())
        log.info(
            "heartbeat manager started (interval=%.1fs, miss_threshold=%d)",
            self._interval,
            self._miss_threshold,
        )

    async def stop(self) -> None:
        """Stop the background tasks."""
        for task in (self._publish_task, self._check_task, self._probe_task):
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._publish_task = None
        self._check_task = None
        self._probe_task = None
        log.info("heartbeat manager stopped")
