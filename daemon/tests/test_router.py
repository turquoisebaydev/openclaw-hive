"""Tests for message routing by channel."""

import pytest

from hive_daemon.envelope import Envelope
from hive_daemon.router import Router


def _make_envelope(ch: str = "command") -> Envelope:
    return Envelope(
        v=1, id="test-1", ts=1000000, from_="node-a", to="node-b",
        ch=ch, urgency="now", text="hello",
    )


class TestRouter:
    async def test_route_to_registered_handler(self):
        router = Router()
        received = []

        async def handler(env: Envelope) -> None:
            received.append(env)

        router.register("command", handler)
        env = _make_envelope("command")
        await router.route(env)
        assert received == [env]

    async def test_unregistered_channel_does_not_raise(self):
        router = Router()
        env = _make_envelope("heartbeat")
        # Should log warning but not raise
        await router.route(env)

    async def test_multiple_channels(self):
        router = Router()
        command_msgs: list[Envelope] = []
        sync_msgs: list[Envelope] = []

        async def cmd_handler(env: Envelope) -> None:
            command_msgs.append(env)

        async def sync_handler(env: Envelope) -> None:
            sync_msgs.append(env)

        router.register("command", cmd_handler)
        router.register("sync", sync_handler)

        await router.route(_make_envelope("command"))
        await router.route(_make_envelope("sync"))
        await router.route(_make_envelope("command"))

        assert len(command_msgs) == 2
        assert len(sync_msgs) == 1

    async def test_handler_replacement(self):
        router = Router()
        first_calls: list[Envelope] = []
        second_calls: list[Envelope] = []

        async def first(env: Envelope) -> None:
            first_calls.append(env)

        async def second(env: Envelope) -> None:
            second_calls.append(env)

        router.register("alert", first)
        router.register("alert", second)

        await router.route(_make_envelope("alert"))
        assert len(first_calls) == 0
        assert len(second_calls) == 1
