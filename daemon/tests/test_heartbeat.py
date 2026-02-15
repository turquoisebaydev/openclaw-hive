"""Tests for machine-to-machine heartbeat manager."""

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hive_daemon.config import HeartbeatConfig, HiveConfig, MqttConfig, OcInstance
from hive_daemon.envelope import Envelope
from hive_daemon.heartbeat import HeartbeatManager, PeerState


def _make_config(
    node_id: str = "test-node",
    oc_instances: list[OcInstance] | None = None,
) -> HiveConfig:
    return HiveConfig(
        node_id=node_id,
        topic_prefix="turq/hive",
        mqtt=MqttConfig(),
        oc_instances=oc_instances or [],
        heartbeat=HeartbeatConfig(interval=5.0, miss_threshold=3),
    )


def _make_heartbeat_envelope(from_: str = "peer-1") -> Envelope:
    payload = json.dumps({
        "node_id": from_,
        "uptime_s": 120.0,
        "load_1m": 0.5,
        "oc_instances": [{"name": "main", "status": "configured"}],
    })
    return Envelope(
        v=1, id="hb-1", ts=1000000, from_=from_, to="all",
        ch="heartbeat", urgency="later", text=payload,
    )


def _make_mock_client() -> AsyncMock:
    """Create a mock aiomqtt.Client."""
    client = AsyncMock()
    client.publish = AsyncMock()
    return client


class TestHeartbeatManagerInit:
    def test_defaults(self):
        config = _make_config()
        client = _make_mock_client()
        mgr = HeartbeatManager(config, client)
        assert mgr.known_peers == {}
        assert mgr._interval == 5.0
        assert mgr._miss_threshold == 3

    def test_custom_interval(self):
        config = _make_config()
        client = _make_mock_client()
        mgr = HeartbeatManager(config, client, interval=10.0, miss_threshold=5)
        assert mgr._interval == 10.0
        assert mgr._miss_threshold == 5


class TestTrackPeer:
    def test_track_new_peer(self):
        config = _make_config()
        client = _make_mock_client()
        mgr = HeartbeatManager(config, client)

        env = _make_heartbeat_envelope("peer-1")
        mgr.track_peer(env)

        peers = mgr.known_peers
        assert "peer-1" in peers
        assert peers["peer-1"].node_id == "peer-1"
        assert peers["peer-1"].payload is not None
        assert peers["peer-1"].payload["uptime_s"] == 120.0

    def test_track_updates_existing_peer(self):
        config = _make_config()
        client = _make_mock_client()
        mgr = HeartbeatManager(config, client)

        env1 = _make_heartbeat_envelope("peer-1")
        mgr.track_peer(env1)
        first_seen = mgr.known_peers["peer-1"].last_seen

        # Small delay to ensure monotonic clock advances
        env2 = _make_heartbeat_envelope("peer-1")
        mgr.track_peer(env2)
        second_seen = mgr.known_peers["peer-1"].last_seen

        assert second_seen >= first_seen

    def test_track_multiple_peers(self):
        config = _make_config()
        client = _make_mock_client()
        mgr = HeartbeatManager(config, client)

        mgr.track_peer(_make_heartbeat_envelope("peer-1"))
        mgr.track_peer(_make_heartbeat_envelope("peer-2"))
        mgr.track_peer(_make_heartbeat_envelope("peer-3"))

        assert len(mgr.known_peers) == 3
        assert set(mgr.known_peers.keys()) == {"peer-1", "peer-2", "peer-3"}

    def test_track_peer_with_invalid_json_text(self):
        config = _make_config()
        client = _make_mock_client()
        mgr = HeartbeatManager(config, client)

        env = Envelope(
            v=1, id="hb-2", ts=1000000, from_="peer-bad", to="all",
            ch="heartbeat", urgency="later", text="not json",
        )
        mgr.track_peer(env)

        peers = mgr.known_peers
        assert "peer-bad" in peers
        assert peers["peer-bad"].payload is None


class TestCheckPeers:
    async def test_no_peers_returns_empty(self):
        config = _make_config()
        client = _make_mock_client()
        mgr = HeartbeatManager(config, client, interval=1.0, miss_threshold=2)

        missing = await mgr.check_peers()
        assert missing == []

    async def test_fresh_peer_not_missing(self):
        config = _make_config()
        client = _make_mock_client()
        mgr = HeartbeatManager(config, client, interval=1.0, miss_threshold=2)

        mgr.track_peer(_make_heartbeat_envelope("peer-1"))
        missing = await mgr.check_peers()
        assert missing == []

    async def test_stale_peer_detected(self):
        config = _make_config()
        client = _make_mock_client()
        mgr = HeartbeatManager(config, client, interval=1.0, miss_threshold=2)

        mgr.track_peer(_make_heartbeat_envelope("peer-stale"))

        # Manually backdate the last_seen to trigger miss detection
        mgr._peers["peer-stale"].last_seen = time.monotonic() - 10.0

        missing = await mgr.check_peers()
        assert "peer-stale" in missing

    async def test_stale_peer_removed_after_alert(self):
        config = _make_config()
        client = _make_mock_client()
        mgr = HeartbeatManager(config, client, interval=1.0, miss_threshold=2)

        mgr.track_peer(_make_heartbeat_envelope("peer-gone"))
        mgr._peers["peer-gone"].last_seen = time.monotonic() - 10.0

        await mgr.check_peers()
        assert "peer-gone" not in mgr.known_peers

    async def test_alert_callback_fired(self):
        config = _make_config()
        client = _make_mock_client()
        alert_cb = AsyncMock()
        mgr = HeartbeatManager(config, client, alert_callback=alert_cb, interval=1.0, miss_threshold=2)

        mgr.track_peer(_make_heartbeat_envelope("peer-alert"))
        mgr._peers["peer-alert"].last_seen = time.monotonic() - 10.0

        await mgr.check_peers()
        alert_cb.assert_awaited_once()
        args = alert_cb.call_args[0]
        assert args[0] == "peer-alert"

    async def test_mixed_fresh_and_stale(self):
        config = _make_config()
        client = _make_mock_client()
        mgr = HeartbeatManager(config, client, interval=1.0, miss_threshold=2)

        mgr.track_peer(_make_heartbeat_envelope("fresh"))
        mgr.track_peer(_make_heartbeat_envelope("stale"))
        mgr._peers["stale"].last_seen = time.monotonic() - 10.0

        missing = await mgr.check_peers()
        assert missing == ["stale"]
        assert "fresh" in mgr.known_peers
        assert "stale" not in mgr.known_peers


class TestPublishHeartbeat:
    async def test_publishes_to_correct_topic(self):
        config = _make_config(node_id="my-node")
        client = _make_mock_client()
        mgr = HeartbeatManager(config, client)

        await mgr.publish_heartbeat()

        client.publish.assert_awaited_once()
        call_args = client.publish.call_args
        assert call_args[0][0] == "turq/hive/all/heartbeat"

    async def test_heartbeat_payload_structure(self):
        config = _make_config(
            node_id="my-node",
            oc_instances=[OcInstance(name="main", profile="default")],
        )
        client = _make_mock_client()
        mgr = HeartbeatManager(config, client)

        await mgr.publish_heartbeat()

        published_data = client.publish.call_args[0][1]
        envelope_json = json.loads(published_data)
        assert envelope_json["from"] == "my-node"
        assert envelope_json["to"] == "all"
        assert envelope_json["ch"] == "heartbeat"

        text_payload = json.loads(envelope_json["text"])
        assert text_payload["node_id"] == "my-node"
        assert "uptime_s" in text_payload
        assert "load_1m" in text_payload
        assert len(text_payload["oc_instances"]) == 1
        assert text_payload["oc_instances"][0]["name"] == "main"


class TestPublishState:
    async def test_publishes_retained_state(self):
        config = _make_config(node_id="state-node")
        client = _make_mock_client()
        mgr = HeartbeatManager(config, client)

        await mgr.publish_state()

        client.publish.assert_awaited_once()
        call_args = client.publish.call_args
        assert call_args[0][0] == "turq/hive/meta/state-node/state"
        assert call_args[1]["retain"] is True

        state = json.loads(call_args[0][1])
        assert state["node_id"] == "state-node"
        assert state["status"] == "online"

    async def test_state_includes_known_peers(self):
        config = _make_config(node_id="state-node")
        client = _make_mock_client()
        mgr = HeartbeatManager(config, client)

        mgr.track_peer(_make_heartbeat_envelope("peer-a"))
        mgr.track_peer(_make_heartbeat_envelope("peer-b"))

        await mgr.publish_state()

        state = json.loads(client.publish.call_args[0][1])
        assert set(state["known_peers"]) == {"peer-a", "peer-b"}


class TestStartStop:
    async def test_start_creates_tasks(self):
        config = _make_config()
        client = _make_mock_client()
        mgr = HeartbeatManager(config, client, interval=100)

        mgr.start()
        assert mgr._publish_task is not None
        assert mgr._check_task is not None

        await mgr.stop()
        assert mgr._publish_task is None
        assert mgr._check_task is None

    async def test_stop_is_idempotent(self):
        config = _make_config()
        client = _make_mock_client()
        mgr = HeartbeatManager(config, client, interval=100)

        # Stop without start should not raise
        await mgr.stop()


class TestBuildHeartbeatPayload:
    def test_payload_is_valid_json(self):
        config = _make_config(node_id="payload-test")
        client = _make_mock_client()
        mgr = HeartbeatManager(config, client)

        payload_str = mgr._build_heartbeat_payload()
        payload = json.loads(payload_str)
        assert payload["node_id"] == "payload-test"
        assert isinstance(payload["uptime_s"], float)
        assert isinstance(payload["load_1m"], float)
        assert isinstance(payload["oc_instances"], list)

    def test_payload_with_oc_instances(self):
        config = _make_config(
            oc_instances=[
                OcInstance(name="main"),
                OcInstance(name="secondary", profile="pg1"),
            ],
        )
        client = _make_mock_client()
        mgr = HeartbeatManager(config, client)

        payload = json.loads(mgr._build_heartbeat_payload())
        assert len(payload["oc_instances"]) == 2
        assert payload["oc_instances"][0]["name"] == "main"
        assert payload["oc_instances"][1]["name"] == "secondary"
