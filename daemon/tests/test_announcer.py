"""Tests for Discord announcement publish behavior."""

from unittest.mock import patch, MagicMock

import pytest

from hive_daemon.announcer import Announcer, format_send, format_recv
from hive_daemon.config import AnnouncementsConfig, DiscordAnnouncementConfig
from hive_daemon.envelope import Envelope


def _make_envelope(
    from_: str = "turq",
    to: str = "pg1",
    ch: str = "command",
    text: str = "check disk",
    action: str | None = None,
    corr: str | None = None,
) -> Envelope:
    return Envelope(
        v=1, id="msg-001", ts=1000000, from_=from_, to=to,
        ch=ch, urgency="now", text=text, action=action, corr=corr,
    )


# --- Format functions ---


class TestFormatSend:
    def test_basic(self):
        env = _make_envelope()
        text = format_send(env)
        assert text.startswith("HIVE SEND")
        assert "from=turq" in text
        assert "to=pg1" in text
        assert "ch=command" in text
        assert "id=msg-001" in text

    def test_with_action_and_corr(self):
        env = _make_envelope(action="git-sync", corr="abc123")
        text = format_send(env)
        assert "action=git-sync" in text
        assert "corr=abc123" in text

    def test_omits_action_when_none(self):
        env = _make_envelope(action=None)
        text = format_send(env)
        assert "action=" not in text

    def test_omits_corr_when_none(self):
        env = _make_envelope(corr=None)
        text = format_send(env)
        assert "corr=" not in text


class TestFormatRecv:
    def test_basic(self):
        env = _make_envelope(from_="pg1", to="turq")
        text = format_recv(env)
        assert text.startswith("HIVE RECV")
        assert "from=pg1" in text
        assert "to=turq" in text
        assert "ch=command" in text
        assert "id=msg-001" in text

    def test_with_action_and_corr(self):
        env = _make_envelope(action="health-check", corr="xyz789")
        text = format_recv(env)
        assert "action=health-check" in text
        assert "corr=xyz789" in text


# --- Announcer class ---


def _disabled_config() -> AnnouncementsConfig:
    return AnnouncementsConfig(enabled=False)


def _enabled_config(
    webhook_url: str = "https://discord.com/api/webhooks/test/hook",
    publish_send: bool = True,
    publish_receive: bool = True,
) -> AnnouncementsConfig:
    return AnnouncementsConfig(
        enabled=True,
        discord=DiscordAnnouncementConfig(
            enabled=True,
            channel="hive-announcements",
            webhook_url=webhook_url,
            publish_send=publish_send,
            publish_receive=publish_receive,
        ),
    )


class TestAnnouncerDisabled:
    async def test_send_noop_when_disabled(self):
        announcer = Announcer(_disabled_config())
        assert not announcer.send_enabled
        with patch.object(announcer, "_publish_discord") as mock:
            await announcer.announce_send(_make_envelope())
            mock.assert_not_called()

    async def test_recv_noop_when_disabled(self):
        announcer = Announcer(_disabled_config())
        assert not announcer.recv_enabled
        with patch.object(announcer, "_publish_discord") as mock:
            await announcer.announce_recv(_make_envelope())
            mock.assert_not_called()

    async def test_top_level_disabled_blocks_discord(self):
        """Top-level enabled=false blocks even if discord.enabled=true."""
        cfg = AnnouncementsConfig(
            enabled=False,
            discord=DiscordAnnouncementConfig(enabled=True),
        )
        announcer = Announcer(cfg)
        assert not announcer.send_enabled
        assert not announcer.recv_enabled


class TestAnnouncerEnabled:
    async def test_send_publishes_when_enabled(self):
        announcer = Announcer(_enabled_config())
        assert announcer.send_enabled
        with patch.object(announcer, "_publish_discord") as mock:
            await announcer.announce_send(_make_envelope())
            mock.assert_called_once()
            text = mock.call_args[0][0]
            assert "HIVE SEND" in text

    async def test_recv_publishes_when_enabled(self):
        announcer = Announcer(_enabled_config())
        assert announcer.recv_enabled
        with patch.object(announcer, "_publish_discord") as mock:
            await announcer.announce_recv(_make_envelope())
            mock.assert_called_once()
            text = mock.call_args[0][0]
            assert "HIVE RECV" in text

    async def test_send_disabled_recv_enabled(self):
        announcer = Announcer(_enabled_config(publish_send=False, publish_receive=True))
        assert not announcer.send_enabled
        assert announcer.recv_enabled

    async def test_recv_disabled_send_enabled(self):
        announcer = Announcer(_enabled_config(publish_send=True, publish_receive=False))
        assert announcer.send_enabled
        assert not announcer.recv_enabled


class TestAnnouncerFailureNonFatal:
    async def test_webhook_error_does_not_raise(self):
        """A failing webhook POST must not propagate exceptions."""
        announcer = Announcer(_enabled_config())
        with patch.object(
            Announcer, "_post_webhook", side_effect=Exception("connection refused")
        ):
            # Must NOT raise
            await announcer.announce_send(_make_envelope())

    async def test_webhook_error_logs_warning(self, caplog):
        """A failing webhook POST logs a warning."""
        announcer = Announcer(_enabled_config())
        with patch.object(
            Announcer, "_post_webhook", side_effect=Exception("timeout")
        ):
            import logging
            with caplog.at_level(logging.WARNING, logger="hive_daemon.announcer"):
                await announcer.announce_send(_make_envelope())
            assert any("discord announcement failed" in r.message for r in caplog.records)

    async def test_no_webhook_url_skips_silently(self):
        """If webhook_url is None, skip without error."""
        cfg = _enabled_config(webhook_url=None)
        # webhook_url=None but we set it on the config directly
        cfg = AnnouncementsConfig(
            enabled=True,
            discord=DiscordAnnouncementConfig(
                enabled=True,
                webhook_url=None,
                publish_send=True,
            ),
        )
        announcer = Announcer(cfg)
        with patch.object(Announcer, "_post_webhook") as mock:
            await announcer.announce_send(_make_envelope())
            mock.assert_not_called()
