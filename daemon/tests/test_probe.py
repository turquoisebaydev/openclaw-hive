"""Tests for deterministic OpenClaw probes."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from hive_daemon.config import OcInstance
from hive_daemon.probe import _run_openclaw_json, probe_instance


class TestRunOpenclawJson:
    async def test_uses_configured_openclaw_command(self):
        proc = AsyncMock()
        proc.communicate = AsyncMock(return_value=(b'{"ok": true}', b""))
        proc.returncode = 0

        with patch("hive_daemon.probe.asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
            ok, data, err = await _run_openclaw_json(
                openclaw_cmd="/opt/mini1/openclaw",
                profile="mini1",
                args=["status", "--json"],
                timeout_s=1.0,
            )

        assert ok is True
        assert data == {"ok": True}
        assert err == ""
        call_args = mock_exec.call_args.args
        assert call_args[0] == "/opt/mini1/openclaw"
        assert "--profile" in call_args
        assert "mini1" in call_args


class TestProbeInstance:
    async def test_probe_uses_instance_specific_openclaw_command(self):
        inst = OcInstance(
            name="mini1",
            profile="mini1",
            port=18889,
            openclaw_cmd="/opt/mini1/openclaw",
        )

        run_mock = AsyncMock(side_effect=[
            (True, {"enabled": True, "jobs": 3, "nextWakeAtMs": 123}, ""),
            (True, {"jobs": []}, ""),
            (True, {"providers": [], "updatedAt": 123}, ""),
        ])

        with patch("hive_daemon.probe._run_openclaw_json", run_mock):
            with patch("hive_daemon.probe._summarize_sessions", return_value={"count": 0}):
                with patch("hive_daemon.probe._scan_recent_session_errors", return_value={"counts": {}}):
                    result = await probe_instance(inst)

        assert result.ok is True
        assert result.data["gw"]["openclawCmd"] == "/opt/mini1/openclaw"

        assert run_mock.await_count == 3
        for call in run_mock.await_args_list:
            assert call.kwargs["openclaw_cmd"] == "/opt/mini1/openclaw"
