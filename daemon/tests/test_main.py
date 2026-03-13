"""Tests for the daemon main module — message handling and topic parsing."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from hive_daemon.config import HiveConfig, MqttConfig, OcInstance, PresenceConfig
from hive_daemon.envelope import Envelope
from hive_daemon.main import (
    _build_topics,
    _extract_topic_target,
    _handle_message,
    _parse_topic_channel,
    setup_router,
)
from hive_daemon.presence import PresenceCache, PresenceRecord
from hive_daemon.router import Router


def _config(**overrides) -> HiveConfig:
    defaults = dict(node_id="turq-18789", topic_prefix="turq/hive")
    defaults.update(overrides)
    return HiveConfig(**defaults)


def _mqtt_msg(topic: str, payload: dict | str | bytes) -> MagicMock:
    """Create a mock aiomqtt.Message."""
    msg = MagicMock()
    msg.topic = MagicMock()
    msg.topic.__str__ = MagicMock(return_value=topic)
    if isinstance(payload, dict):
        msg.payload = json.dumps(payload).encode()
    elif isinstance(payload, str):
        msg.payload = payload.encode()
    else:
        msg.payload = payload
    return msg


VALID_PAYLOAD = {
    "v": 1,
    "id": "msg-1",
    "ts": 1000000,
    "from": "pg1-18890",
    "to": "turq-18789",
    "ch": "command",
    "urgency": "now",
    "text": "do something",
}


class TestBuildTopics:
    def test_default_prefix(self):
        cfg = _config()
        topics = _build_topics(cfg)
        assert "turq/hive/turq-18789/+" in topics
        assert "turq/hive/all/+" in topics
        assert "turq/hive/+/command" in topics

    def test_custom_prefix(self):
        cfg = _config(topic_prefix="my/prefix")
        topics = _build_topics(cfg)
        assert "my/prefix/turq-18789/+" in topics
        assert "my/prefix/all/+" in topics
        assert "my/prefix/+/command" in topics

    def test_multi_instance_subscriptions(self):
        """Each OC instance gets its own subscription topic."""
        cfg = _config(
            node_id="turq",
            oc_instances=[
                OcInstance(name="turq", port=18789),
                OcInstance(name="mini1", profile="mini1", port=18889),
            ],
        )
        topics = _build_topics(cfg)
        assert "turq/hive/turq/+" in topics
        assert "turq/hive/mini1/+" in topics
        assert "turq/hive/all/+" in topics
        assert "turq/hive/+/command" in topics

    def test_no_duplicate_when_node_id_matches_instance(self):
        """When node_id == an instance name, no duplicate subscriptions."""
        cfg = _config(
            node_id="turq",
            oc_instances=[OcInstance(name="turq")],
        )
        topics = _build_topics(cfg)
        # Count occurrences of the turq subscription
        turq_subs = [t for t in topics if t == "turq/hive/turq/+"]
        assert len(turq_subs) == 1


class TestExtractTopicTarget:
    def test_normal_target(self):
        cfg = _config()
        assert _extract_topic_target("turq/hive/mini1/command", cfg) == "mini1"

    def test_all_target(self):
        cfg = _config()
        assert _extract_topic_target("turq/hive/all/heartbeat", cfg) == "all"

    def test_short_topic(self):
        cfg = _config()
        assert _extract_topic_target("turq/hive", cfg) == ""


class TestParseTopicChannel:
    def test_normal_topic(self):
        cfg = _config()
        assert _parse_topic_channel("turq/hive/turq-18789/command", cfg) == "command"

    def test_all_topic(self):
        cfg = _config()
        assert _parse_topic_channel("turq/hive/all/heartbeat", cfg) == "heartbeat"

    def test_wrong_prefix(self):
        cfg = _config()
        assert _parse_topic_channel("other/prefix/node/command", cfg) is None

    def test_too_few_parts(self):
        cfg = _config()
        assert _parse_topic_channel("turq/hive/command", cfg) is None

    def test_too_many_parts(self):
        cfg = _config()
        assert _parse_topic_channel("turq/hive/node/command/extra", cfg) is None


class TestHandleMessage:
    async def test_valid_message_routes(self):
        cfg = _config()
        router = Router()
        received = []

        async def handler(env: Envelope, target: str) -> None:
            received.append(env)

        router.register("command", handler)

        msg = _mqtt_msg("turq/hive/turq-18789/command", VALID_PAYLOAD)
        await _handle_message(msg, cfg, router)
        assert len(received) == 1
        assert received[0].id == "msg-1"

    async def test_invalid_json_is_dropped(self):
        cfg = _config()
        router = Router()
        msg = _mqtt_msg("turq/hive/turq-18789/command", b"not json")
        # Should not raise
        await _handle_message(msg, cfg, router)

    async def test_invalid_envelope_is_dropped(self):
        cfg = _config()
        router = Router()
        bad_payload = {"v": 1, "id": "x"}  # missing fields
        msg = _mqtt_msg("turq/hive/turq-18789/command", bad_payload)
        await _handle_message(msg, cfg, router)

    async def test_own_command_to_self_allowed(self):
        """Self-originated commands to local node are processed (for multi-instance setups)."""
        cfg = _config()
        router = Router()
        received = []

        async def handler(env: Envelope, target: str) -> None:
            received.append(env)

        router.register("command", handler)

        own_payload = {**VALID_PAYLOAD, "from": "turq-18789"}
        msg = _mqtt_msg("turq/hive/turq-18789/command", own_payload)
        await _handle_message(msg, cfg, router)
        assert len(received) == 1

    async def test_own_instance_command_to_local_allowed(self):
        """Commands from managed instances to local nodes are processed."""
        cfg = _config(
            node_id="turq",
            oc_instances=[
                OcInstance(name="turq"),
                OcInstance(name="mini1"),
            ],
        )
        router = Router()
        received = []

        async def handler(env: Envelope, target: str) -> None:
            received.append(env)

        router.register("command", handler)

        # Message from mini1 (managed instance) to turq — should be processed
        payload = {**VALID_PAYLOAD, "from": "mini1", "to": "turq"}
        msg = _mqtt_msg("turq/hive/turq/command", payload)
        await _handle_message(msg, cfg, router)
        assert len(received) == 1

    async def test_own_non_command_messages_ignored(self):
        """Self-originated non-command messages (heartbeat, response) are ignored to prevent loops."""
        cfg = _config()
        router = Router()
        received = []

        async def handler(env: Envelope, target: str) -> None:
            received.append(env)

        router.register("heartbeat", handler)

        own_payload = {**VALID_PAYLOAD, "from": "turq-18789", "ch": "heartbeat"}
        msg = _mqtt_msg("turq/hive/turq-18789/heartbeat", own_payload)
        await _handle_message(msg, cfg, router)
        assert len(received) == 0

    async def test_own_instance_broadcast_allowed(self):
        """Self-messages on broadcast topic are still processed."""
        cfg = _config(
            node_id="turq",
            oc_instances=[OcInstance(name="mini1")],
        )
        router = Router()
        received = []

        async def handler(env: Envelope, target: str) -> None:
            received.append(env)

        router.register("command", handler)

        payload = {**VALID_PAYLOAD, "from": "mini1", "to": "all"}
        msg = _mqtt_msg("turq/hive/all/command", payload)
        await _handle_message(msg, cfg, router)
        assert len(received) == 1


    async def test_own_instance_non_command_announced_as_recv(self):
        """Non-command from a managed sibling instance should still be announced as recv."""
        cfg = _config(
            node_id="turq",
            oc_instances=[OcInstance(name="mini1")],
        )
        router = Router()
        received = []

        async def handler(env: Envelope, target: str) -> None:
            received.append(env)

        router.register("response", handler)

        announcer = MagicMock()
        announcer.announce_recv = AsyncMock()
        announcer.announce_send = AsyncMock()

        payload = {**VALID_PAYLOAD, "from": "mini1", "to": "turq", "ch": "response"}
        msg = _mqtt_msg("turq/hive/turq/response", payload)
        await _handle_message(msg, cfg, router, announcer=announcer)

        announcer.announce_recv.assert_awaited_once()
        assert len(received) == 0

    async def test_own_node_non_command_not_announced(self):
        """Non-command from the daemon node_id itself should remain unannounced."""
        cfg = _config(node_id="turq")
        router = Router()

        announcer = MagicMock()
        announcer.announce_recv = AsyncMock()
        announcer.announce_send = AsyncMock()

        payload = {**VALID_PAYLOAD, "from": "turq", "to": "turq", "ch": "response"}
        msg = _mqtt_msg("turq/hive/turq/response", payload)
        await _handle_message(msg, cfg, router, announcer=announcer)

        announcer.announce_recv.assert_not_awaited()

    async def test_target_passed_to_router(self):
        """The topic target is passed through to the router handler."""
        cfg = _config()
        router = Router()
        targets = []

        async def handler(env: Envelope, target: str) -> None:
            targets.append(target)

        router.register("command", handler)

        msg = _mqtt_msg("turq/hive/turq-18789/command", VALID_PAYLOAD)
        await _handle_message(msg, cfg, router)
        assert targets == ["turq-18789"]


class TestPresenceHandling:
    async def test_presence_message_updates_cache(self):
        """Inbound presence messages from remote nodes update the local cache."""
        cfg = _config(node_id="turq-18789")
        router = Router()
        cache = PresenceCache()

        presence_payload = {
            "v": 1,
            "kind": "session_presence",
            "gw": "pg1",
            "agent": "main",
            "session": "sess-1",
            "state": "active",
            "status": "idle",
            "updatedTs": 1000,
            "ttlSec": 300,
        }
        msg = _mqtt_msg("turq/hive/presence/pg/gw/pg1/sess-1", presence_payload)
        await _handle_message(msg, cfg, router, presence_cache=cache)

        assert len(cache) == 1
        record = cache.get("pg1/main/sess-1")
        assert record is not None
        assert record.gw == "pg1"

    async def test_own_presence_message_ignored(self):
        """Presence messages from own node are not cached."""
        cfg = _config(
            node_id="turq",
            oc_instances=[OcInstance(name="turq")],
        )
        router = Router()
        cache = PresenceCache()

        presence_payload = {
            "v": 1,
            "kind": "session_presence",
            "gw": "turq",
            "agent": "main",
            "session": "sess-1",
            "state": "active",
            "status": "idle",
            "updatedTs": 1000,
            "ttlSec": 300,
        }
        msg = _mqtt_msg("turq/hive/presence/turq/gw/turq/sess-1", presence_payload)
        await _handle_message(msg, cfg, router, presence_cache=cache)

        assert len(cache) == 0

    async def test_invalid_presence_message_ignored(self):
        """Malformed presence messages are silently dropped."""
        cfg = _config()
        router = Router()
        cache = PresenceCache()

        msg = _mqtt_msg("turq/hive/presence/pg/gw/pg1/sess-1", {"kind": "wrong"})
        await _handle_message(msg, cfg, router, presence_cache=cache)
        assert len(cache) == 0

    async def test_presence_not_handled_when_cache_none(self):
        """When presence_cache is None, presence topic messages are not processed."""
        cfg = _config()
        router = Router()

        presence_payload = {
            "v": 1,
            "kind": "session_presence",
            "gw": "pg1",
            "agent": "main",
            "session": "sess-1",
            "state": "active",
            "status": "idle",
            "updatedTs": 1000,
            "ttlSec": 300,
        }
        msg = _mqtt_msg("turq/hive/presence/pg/gw/pg1/sess-1", presence_payload)
        # Should not raise — just ignored
        await _handle_message(msg, cfg, router, presence_cache=None)

    async def test_presence_topic_in_build_topics(self):
        """Presence topic subscription is included when enabled."""
        cfg = _config()
        topics = _build_topics(cfg)
        assert "turq/hive/presence/#" in topics

    async def test_presence_topic_not_in_build_topics_when_disabled(self):
        """Presence topic subscription is excluded when disabled."""
        cfg = _config()
        # Override presence to disabled
        cfg = HiveConfig(
            node_id="turq-18789",
            topic_prefix="turq/hive",
            presence=PresenceConfig(enabled=False),
        )
        topics = _build_topics(cfg)
        assert "turq/hive/presence/#" not in topics


class TestSetupRouter:
    async def test_all_channels_have_handlers(self):
        cfg = _config()
        router = setup_router(cfg)
        for ch in ("command", "response", "sync", "heartbeat", "status", "alert"):
            env = Envelope(
                v=1, id="t", ts=1000000, from_="other", to="me",
                ch=ch, urgency="now", text="test",
            )
            # Should not raise — handler exists
            await router.route(env)
