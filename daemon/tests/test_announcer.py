"""Tests for Discord announcement publish behavior."""

from unittest.mock import patch, MagicMock

import pytest

from hive_daemon.announcer import Announcer, format_send, format_recv, _format_thread_parent
from hive_daemon.config import AnnouncementsConfig, DiscordAnnouncementConfig, ThreadingConfig
from hive_daemon.envelope import Envelope
from hive_daemon.thread_map import ThreadMap


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
        text = format_send(env, gw="turq")
        assert text.startswith("HIVE SEND")
        assert "from=turq" in text
        assert "to=pg1" in text
        assert "type=command" in text
        assert "urgency=now" in text
        assert "ts=1000000" in text
        assert "text_len=10" in text
        assert "text='check disk'" in text
        assert "gw=turq" in text
        assert "gw=turq" in text
        assert "urgency=now" in text
        assert "ts=1000000" in text
        assert "text_len=10" in text
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
        text = format_recv(env, gw="turq")
        assert text.startswith("HIVE RECV")
        assert "from=pg1" in text
        assert "to=turq" in text
        assert "type=command" in text
        assert "text='check disk'" in text
        assert "id=msg-001" in text

    def test_with_action_and_corr(self):
        env = _make_envelope(action="health-check", corr="xyz789")
        text = format_recv(env)
        assert "action=health-check" in text
        assert "corr=xyz789" in text


class TestFormatCommonOptionalFields:
    def test_includes_reply_to_and_ttl_when_present(self):
        env = Envelope(
            v=1,
            id="msg-002",
            ts=1000001,
            from_="pg1",
            to="turq",
            ch="response",
            urgency="later",
            text="ok",
            corr="corr-1",
            reply_to="msg-001",
            ttl=30,
        )
        text = format_recv(env)
        assert "type=response" in text
        assert "reply_to=msg-001" in text
        assert "ttl=30" in text
        assert "urgency=later" in text


class TestTextPreview:
    def test_truncates_long_text(self):
        env = _make_envelope(text="x" * 200)
        text = format_send(env)
        assert "text_len=200" in text
        assert "text='" in text
        assert "…'" in text


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


class TestAnnouncerNodeId:
    async def test_includes_local_gateway_node_id_in_published_text(self):
        announcer = Announcer(_enabled_config(), node_id="turq")
        with patch.object(announcer, "_publish_discord") as mock:
            await announcer.announce_send(_make_envelope())
            mock.assert_called_once()
            text = mock.call_args[0][0]
            assert "gw=turq" in text


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


class TestWebhookRequestHeaders:
    def test_post_webhook_sets_user_agent(self):
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.__enter__.return_value = mock_resp
            mock_resp.__exit__.return_value = False
            mock_urlopen.return_value = mock_resp

            Announcer._post_webhook("https://discord.com/api/webhooks/test/hook", "hello")

            req = mock_urlopen.call_args[0][0]
            assert req.get_header("User-agent") is not None
            assert "openclaw-hive-daemon" in req.get_header("User-agent")
            assert req.get_header("Content-type") == "application/json"


class TestAnnouncerFilters:
    async def test_send_heartbeat_is_filtered(self):
        announcer = Announcer(_enabled_config())
        env = _make_envelope(ch="heartbeat")
        with patch.object(announcer, "_publish_discord") as mock:
            await announcer.announce_send(env)
            mock.assert_not_called()

    async def test_recv_heartbeat_is_filtered(self):
        announcer = Announcer(_enabled_config())
        env = _make_envelope(ch="heartbeat")
        with patch.object(announcer, "_publish_discord") as mock:
            await announcer.announce_recv(env)
            mock.assert_not_called()


# --- Threading support ---


def _threading_config(
    threading_enabled: bool = True,
    fallback_to_channel: bool = True,
    webhook_url: str = "https://discord.com/api/webhooks/test/hook",
) -> AnnouncementsConfig:
    return AnnouncementsConfig(
        enabled=True,
        discord=DiscordAnnouncementConfig(
            enabled=True,
            channel="hive-announcements",
            webhook_url=webhook_url,
            publish_send=True,
            publish_receive=True,
            threading=ThreadingConfig(
                enabled=threading_enabled,
                fallback_to_channel=fallback_to_channel,
            ),
        ),
    )


class TestFormatThreadParent:
    def test_basic(self):
        env = _make_envelope(corr="abc123")
        text = _format_thread_parent(env)
        assert "HIVE FLOW" in text
        assert "corr=abc123" in text
        assert "from=turq" in text
        assert "to=pg1" in text

    def test_fallback_to_id_when_no_corr(self):
        env = _make_envelope(corr=None)
        text = _format_thread_parent(env)
        assert "corr=msg-001" in text

    def test_includes_action(self):
        env = _make_envelope(action="ping", corr="c1")
        text = _format_thread_parent(env)
        assert "action=ping" in text


class TestAnnouncerThreadingDisabled:
    """When threading config exists but is disabled, uses webhook path."""

    async def test_threading_disabled_uses_webhook(self):
        cfg = _threading_config(threading_enabled=False)
        announcer = Announcer(cfg)
        assert not announcer.threading_enabled
        with patch.object(announcer, "_publish_discord") as mock:
            await announcer.announce_send(_make_envelope())
            mock.assert_called_once()

    async def test_threading_enabled_but_no_bot_token_uses_webhook(self):
        cfg = _threading_config(threading_enabled=True)
        announcer = Announcer(cfg, bot_token=None, channel_id="123")
        assert not announcer.threading_enabled
        with patch.object(announcer, "_publish_discord") as mock:
            await announcer.announce_send(_make_envelope())
            mock.assert_called_once()

    async def test_threading_enabled_but_no_channel_id_uses_webhook(self):
        cfg = _threading_config(threading_enabled=True)
        announcer = Announcer(cfg, bot_token="tok", channel_id=None)
        assert not announcer.threading_enabled
        with patch.object(announcer, "_publish_discord") as mock:
            await announcer.announce_send(_make_envelope())
            mock.assert_called_once()


class TestAnnouncerThreadingEnabled:
    """When threading is fully enabled, uses bot API to create/reuse threads."""

    def _make_announcer(self, tmp_path, fallback=True):
        cfg = _threading_config(threading_enabled=True, fallback_to_channel=fallback)
        tm = ThreadMap(path=tmp_path / "threads.json")
        return Announcer(
            cfg,
            bot_token="test-bot-token",
            channel_id="chan-123",
            thread_map=tm,
        ), tm

    async def test_creates_thread_on_first_event(self, tmp_path):
        announcer, tm = self._make_announcer(tmp_path)
        env = _make_envelope(corr="corr-1")

        with patch.object(announcer, "_post_bot_message", return_value="msg-parent") as mock_post, \
             patch.object(announcer, "_create_thread", return_value="thread-100") as mock_thread:
            await announcer.announce_send(env)

            # First call: parent message in channel
            assert mock_post.call_count == 2
            parent_call = mock_post.call_args_list[0]
            assert parent_call[0][0] == "chan-123"
            assert "HIVE FLOW" in parent_call[0][1]

            # Thread created from parent
            mock_thread.assert_called_once()
            assert mock_thread.call_args[0][1] == "msg-parent"

            # Second call: event in thread
            event_call = mock_post.call_args_list[1]
            assert event_call[0][0] == "thread-100"
            assert "HIVE SEND" in event_call[0][1]

        # Thread stored in map
        entry = tm.get("corr-1")
        assert entry is not None
        assert entry.thread_id == "thread-100"

    async def test_reuses_existing_thread(self, tmp_path):
        announcer, tm = self._make_announcer(tmp_path)
        tm.put("corr-1", thread_id="thread-existing", parent_message_id="p-1")
        env = _make_envelope(corr="corr-1")

        with patch.object(announcer, "_post_bot_message") as mock_post, \
             patch.object(announcer, "_create_thread") as mock_thread:
            await announcer.announce_send(env)

            # Should post into existing thread, no new thread created
            mock_post.assert_called_once()
            assert mock_post.call_args[0][0] == "thread-existing"
            mock_thread.assert_not_called()

    async def test_remaps_on_stale_thread(self, tmp_path):
        """If existing thread fails, removes and creates a new one."""
        announcer, tm = self._make_announcer(tmp_path)
        tm.put("corr-1", thread_id="thread-dead", parent_message_id="p-1")
        env = _make_envelope(corr="corr-1")

        call_count = 0

        def side_effect(channel_id, text):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # First call is to the dead thread — fail
                raise Exception("thread archived")
            return "new-msg-id"

        with patch.object(announcer, "_post_bot_message", side_effect=side_effect) as mock_post, \
             patch.object(announcer, "_create_thread", return_value="thread-new") as mock_thread:
            await announcer.announce_send(env)

            # Should have tried the dead thread, then created a new one
            mock_thread.assert_called_once()

        entry = tm.get("corr-1")
        assert entry is not None
        assert entry.thread_id == "thread-new"

    async def test_fallback_to_channel_on_failure(self, tmp_path):
        """When threading fails entirely, falls back to webhook post."""
        announcer, tm = self._make_announcer(tmp_path, fallback=True)
        env = _make_envelope(corr="corr-1")

        with patch.object(announcer, "_post_bot_message", side_effect=Exception("API down")), \
             patch.object(announcer, "_publish_discord") as mock_webhook:
            await announcer.announce_send(env)
            mock_webhook.assert_called_once()
            assert "HIVE SEND" in mock_webhook.call_args[0][0]

    async def test_no_fallback_when_disabled(self, tmp_path):
        """When fallback_to_channel=false, don't attempt webhook on failure."""
        announcer, tm = self._make_announcer(tmp_path, fallback=False)
        env = _make_envelope(corr="corr-1")

        with patch.object(announcer, "_post_bot_message", side_effect=Exception("API down")), \
             patch.object(announcer, "_publish_discord") as mock_webhook:
            await announcer.announce_send(env)
            mock_webhook.assert_not_called()

    async def test_threading_failure_is_non_fatal(self, tmp_path):
        """Even a total threading failure must not raise."""
        announcer, tm = self._make_announcer(tmp_path, fallback=False)
        env = _make_envelope(corr="corr-1")

        with patch.object(announcer, "_post_bot_message", side_effect=Exception("boom")):
            # Must NOT raise
            await announcer.announce_send(env)

    async def test_heartbeat_still_filtered_with_threading(self, tmp_path):
        announcer, tm = self._make_announcer(tmp_path)
        env = _make_envelope(ch="heartbeat", corr="hb-1")

        with patch.object(announcer, "_post_bot_message") as mock_post:
            await announcer.announce_send(env)
            mock_post.assert_not_called()

    async def test_uses_id_as_thread_key_when_no_corr(self, tmp_path):
        announcer, tm = self._make_announcer(tmp_path)
        env = _make_envelope(corr=None)  # id="msg-001"

        with patch.object(announcer, "_post_bot_message", return_value="p-id") as mock_post, \
             patch.object(announcer, "_create_thread", return_value="t-id"):
            await announcer.announce_send(env)

        entry = tm.get("msg-001")
        assert entry is not None
        assert entry.thread_id == "t-id"
