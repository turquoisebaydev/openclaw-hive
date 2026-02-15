"""Tests for OC bridge (OpenClaw system event injection)."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hive_daemon.config import OcInstance
from hive_daemon.envelope import Envelope
from hive_daemon.oc_bridge import OcBridge


def _make_envelope(
    from_: str = "node-a",
    to: str = "node-b",
    ch: str = "command",
    text: str = "do something",
) -> Envelope:
    return Envelope(
        v=1, id="test-1", ts=1000000, from_=from_, to=to,
        ch=ch, urgency="now", text=text,
    )


def _make_instances() -> list[OcInstance]:
    return [
        OcInstance(name="main", profile="default", port=3000),
        OcInstance(name="secondary", profile="pg1", port=3001),
    ]


class TestFormatEventText:
    def test_basic_format(self):
        env = _make_envelope(from_="node-a", to="node-b", ch="command", text="hello world")
        text = OcBridge.format_event_text(env)
        assert text == "[hive:node-a->node-b ch:command] hello world"

    def test_with_prefix(self):
        env = _make_envelope(text="urgent stuff")
        text = OcBridge.format_event_text(env, prefix="URGENT")
        assert text == "[hive:node-a->node-b ch:command] URGENT urgent stuff"

    def test_alert_channel(self):
        env = _make_envelope(ch="alert", text="disk full")
        text = OcBridge.format_event_text(env)
        assert "[hive:node-a->node-b ch:alert]" in text
        assert "disk full" in text


class TestBuildCommand:
    def test_command_without_profile(self):
        bridge = OcBridge([])
        inst = OcInstance(name="main")
        cmd = bridge._build_command(inst, "hello")
        assert cmd == ["openclaw", "system", "event", "--text", "hello"]

    def test_command_with_profile(self):
        bridge = OcBridge([])
        inst = OcInstance(name="main", profile="pg1")
        cmd = bridge._build_command(inst, "hello")
        assert cmd == ["openclaw", "--profile", "pg1", "system", "event", "--text", "hello"]


class TestInjectEvent:
    async def test_inject_to_all_instances(self):
        instances = _make_instances()
        bridge = OcBridge(instances)

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"ok", b""))
        mock_proc.returncode = 0
        mock_proc.wait = AsyncMock()

        with patch("hive_daemon.oc_bridge.asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            await bridge.inject_event("test event")

            # Should be called once per instance
            assert mock_exec.await_count == 2

    async def test_inject_to_specific_instance(self):
        instances = _make_instances()
        bridge = OcBridge(instances)

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"ok", b""))
        mock_proc.returncode = 0
        mock_proc.wait = AsyncMock()

        with patch("hive_daemon.oc_bridge.asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            await bridge.inject_event("test event", instance_name="main")

            assert mock_exec.await_count == 1
            # Verify correct profile was used
            call_args = mock_exec.call_args[0]
            assert "--profile" in call_args
            assert "default" in call_args

    async def test_inject_to_nonexistent_instance(self):
        instances = _make_instances()
        bridge = OcBridge(instances)

        with patch("hive_daemon.oc_bridge.asyncio.create_subprocess_exec") as mock_exec:
            await bridge.inject_event("test", instance_name="nonexistent")
            mock_exec.assert_not_awaited()

    async def test_inject_with_no_instances_configured(self):
        bridge = OcBridge([])

        with patch("hive_daemon.oc_bridge.asyncio.create_subprocess_exec") as mock_exec:
            await bridge.inject_event("test")
            mock_exec.assert_not_awaited()


class TestInjectToInstance:
    async def test_successful_injection(self):
        bridge = OcBridge([])
        inst = OcInstance(name="main", profile="default")

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"injected\n", b""))
        mock_proc.returncode = 0
        mock_proc.wait = AsyncMock()

        with patch("hive_daemon.oc_bridge.asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            await bridge._inject_to_instance(inst, "hello")

            mock_exec.assert_awaited_once()
            call_args = mock_exec.call_args[0]
            assert call_args[0] == "openclaw"
            assert "--profile" in call_args
            assert "default" in call_args

    async def test_failed_injection_nonzero_exit(self):
        bridge = OcBridge([])
        inst = OcInstance(name="main")

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b"connection refused\n"))
        mock_proc.returncode = 1
        mock_proc.wait = AsyncMock()

        with patch("hive_daemon.oc_bridge.asyncio.create_subprocess_exec", return_value=mock_proc):
            # Should not raise
            await bridge._inject_to_instance(inst, "hello")

    async def test_timeout_kills_process(self):
        bridge = OcBridge([], timeout=1)
        inst = OcInstance(name="main")

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError)
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock()

        with patch("hive_daemon.oc_bridge.asyncio.create_subprocess_exec", return_value=mock_proc):
            with patch("hive_daemon.oc_bridge.asyncio.wait_for", side_effect=asyncio.TimeoutError):
                await bridge._inject_to_instance(inst, "hello")

                mock_proc.kill.assert_called_once()

    async def test_openclaw_not_found(self):
        bridge = OcBridge([])
        inst = OcInstance(name="main")

        with patch(
            "hive_daemon.oc_bridge.asyncio.create_subprocess_exec",
            side_effect=FileNotFoundError("No such file: openclaw"),
        ):
            # Should not raise — logs error instead
            await bridge._inject_to_instance(inst, "hello")

    async def test_oserror_handled(self):
        bridge = OcBridge([])
        inst = OcInstance(name="main")

        with patch(
            "hive_daemon.oc_bridge.asyncio.create_subprocess_exec",
            side_effect=OSError("permission denied"),
        ):
            # Should not raise
            await bridge._inject_to_instance(inst, "hello")


class TestInjectEnvelope:
    async def test_inject_envelope_formats_text(self):
        instances = [OcInstance(name="main")]
        bridge = OcBridge(instances)

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"ok", b""))
        mock_proc.returncode = 0
        mock_proc.wait = AsyncMock()

        with patch("hive_daemon.oc_bridge.asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            env = _make_envelope(from_="node-a", to="node-b", ch="command", text="do it")
            await bridge.inject_envelope(env)

            call_args = mock_exec.call_args[0]
            text_arg = call_args[call_args.index("--text") + 1]
            assert "[hive:node-a->node-b ch:command]" in text_arg
            assert "do it" in text_arg

    async def test_inject_envelope_with_urgent_prefix(self):
        instances = [OcInstance(name="main")]
        bridge = OcBridge(instances)

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"ok", b""))
        mock_proc.returncode = 0
        mock_proc.wait = AsyncMock()

        with patch("hive_daemon.oc_bridge.asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            env = _make_envelope(ch="alert", text="disk full")
            await bridge.inject_envelope(env, prefix="URGENT")

            call_args = mock_exec.call_args[0]
            text_arg = call_args[call_args.index("--text") + 1]
            assert "URGENT" in text_arg
            assert "disk full" in text_arg

    async def test_inject_envelope_to_specific_instance(self):
        instances = _make_instances()
        bridge = OcBridge(instances)

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"ok", b""))
        mock_proc.returncode = 0
        mock_proc.wait = AsyncMock()

        with patch("hive_daemon.oc_bridge.asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            env = _make_envelope()
            await bridge.inject_envelope(env, instance_name="secondary")

            assert mock_exec.await_count == 1
            call_args = mock_exec.call_args[0]
            assert "pg1" in call_args
