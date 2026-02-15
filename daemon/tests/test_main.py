"""Tests for the daemon main module — message handling and topic parsing."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from hive_daemon.config import HiveConfig, MqttConfig, OcInstance
from hive_daemon.envelope import Envelope
from hive_daemon.main import (
    _build_topics,
    _extract_topic_target,
    _handle_message,
    _parse_topic_channel,
    setup_router,
)
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

    async def test_own_messages_ignored(self):
        cfg = _config()
        router = Router()
        received = []

        async def handler(env: Envelope, target: str) -> None:
            received.append(env)

        router.register("command", handler)

        own_payload = {**VALID_PAYLOAD, "from": "turq-18789"}
        msg = _mqtt_msg("turq/hive/turq-18789/command", own_payload)
        await _handle_message(msg, cfg, router)
        assert len(received) == 0

    async def test_own_instance_messages_ignored(self):
        """Messages from any managed instance name are treated as self."""
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

        # Message from mini1 (managed instance) to turq — should be ignored
        payload = {**VALID_PAYLOAD, "from": "mini1", "to": "turq"}
        msg = _mqtt_msg("turq/hive/turq/command", payload)
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
