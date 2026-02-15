"""Tests for the daemon main module — message handling and topic parsing."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from hive_daemon.config import HiveConfig, MqttConfig
from hive_daemon.envelope import Envelope
from hive_daemon.main import _build_topics, _handle_message, _parse_topic_channel, setup_router
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
        assert topics == ["turq/hive/turq-18789/+", "turq/hive/all/+"]

    def test_custom_prefix(self):
        cfg = _config(topic_prefix="my/prefix")
        topics = _build_topics(cfg)
        assert topics == ["my/prefix/turq-18789/+", "my/prefix/all/+"]


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

        async def handler(env: Envelope) -> None:
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

        async def handler(env: Envelope) -> None:
            received.append(env)

        router.register("command", handler)

        own_payload = {**VALID_PAYLOAD, "from": "turq-18789"}
        msg = _mqtt_msg("turq/hive/turq-18789/command", own_payload)
        await _handle_message(msg, cfg, router)
        assert len(received) == 0


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
