"""Tests for deterministic discord master actions."""

import json

import pytest

from hive_daemon.config import DiscordMasterChannelConfig, DiscordMasterConfig
from hive_daemon.discord_master import DiscordMasterError, DiscordMasterService
from hive_daemon.envelope import create_envelope


def _service(**kwargs) -> DiscordMasterService:
    channels = kwargs.pop("channels", [DiscordMasterChannelConfig(name="qmd", mention_target="user:123")])
    cfg = DiscordMasterConfig(
        enabled=True,
        guild_id="1476805337968279685",
        bot_token="tok",
        channels=channels,
        **kwargs,
    )
    return DiscordMasterService(cfg)


def test_thread_send_action(monkeypatch):
    svc = _service()

    def fake_request(method, path, data=None):
        assert method == "POST"
        assert path == "/channels/abc/messages"
        assert data == {"content": "hello"}
        return {"id": "m1", "channel_id": "abc", "content": "hello"}

    monkeypatch.setattr(DiscordMasterService, "_request_json", lambda self, method, path, data=None: fake_request(method, path, data))
    env = create_envelope(
        from_="mini1",
        to="turq",
        ch="command",
        action="discord.thread.send",
        text=json.dumps({"thread_id": "abc", "content": "hello"}),
    )

    out = svc.execute(env)
    assert out["ok"] is True
    assert out["message"]["id"] == "m1"


def test_thread_history_action(monkeypatch):
    svc = _service()

    def fake_request(method, path, data=None):
        assert method == "GET"
        assert path.startswith("/channels/thr/messages?")
        return [{"id": "1", "timestamp": "t", "content": "x", "author": {"id": "u1", "username": "h"}}]

    monkeypatch.setattr(DiscordMasterService, "_request_json", lambda self, method, path, data=None: fake_request(method, path, data))
    env = create_envelope(
        from_="mini1",
        to="turq",
        ch="command",
        action="discord.thread.history",
        text=json.dumps({"thread_id": "thr", "limit": 10}),
    )

    out = svc.execute(env)
    assert out["ok"] is True
    assert out["count"] == 1
    assert out["messages"][0]["author"]["id"] == "u1"


def test_mention_resolve_from_channel_config():
    svc = _service()
    env = create_envelope(
        from_="mini1",
        to="turq",
        ch="command",
        action="discord.mention.resolve",
        text=json.dumps({"channel": "qmd"}),
    )

    out = svc.execute(env)
    assert out["ok"] is True
    assert out["mention"] == "<@123>"


def test_mention_resolve_falls_back_to_role_search(monkeypatch):
    svc = _service(channels=[])

    calls = []

    def fake_request(method, path, data=None):
        calls.append(path)
        if "/members/search" in path:
            return []
        if path.endswith("/roles"):
            return [{"id": "999", "name": "Hermes"}]
        raise AssertionError(path)

    monkeypatch.setattr(DiscordMasterService, "_request_json", lambda self, method, path, data=None: fake_request(method, path, data))
    env = create_envelope(
        from_="mini1",
        to="turq",
        ch="command",
        action="discord.mention.resolve",
        text=json.dumps({"query": "Hermes"}),
    )

    out = svc.execute(env)
    assert out["ok"] is True
    assert out["mention"] == "<@&999>"
    assert any("/members/search" in c for c in calls)


def test_unavailable_service_raises():
    svc = DiscordMasterService(DiscordMasterConfig(enabled=False))
    env = create_envelope(
        from_="mini1",
        to="turq",
        ch="command",
        action="discord.thread.send",
        text="{}",
    )
    with pytest.raises(DiscordMasterError):
        svc.execute(env)

def test_thread_list_filters_and_includes_mention(monkeypatch):
    svc = _service()

    def fake_request(method, path, data=None):
        if path.endswith('/channels'):
            return [
                {"id": "c1", "name": "qmd-hive"},
                {"id": "c2", "name": "random"},
            ]
        if path.endswith('/threads/active'):
            return {
                "threads": [
                    {"id": "t1", "name": "QMD-proj", "parent_id": "c1", "thread_metadata": {}},
                    {"id": "t2", "name": "other", "parent_id": "c1", "thread_metadata": {}},
                ]
            }
        raise AssertionError(path)

    monkeypatch.setattr(DiscordMasterService, "_request_json", lambda self, method, path, data=None: fake_request(method, path, data))
    env = create_envelope(
        from_="mini1",
        to="turq",
        ch="command",
        action="discord.thread.list",
        text="{}",
    )

    out = svc.execute(env)
    assert out["ok"] is True
    assert out["count"] == 1
    assert out["threads"][0]["name"] == "QMD-proj"
    assert out["threads"][0]["mention_user_id"] == "123"

def test_thread_list_mention_falls_back_to_thread_base(monkeypatch):
    svc = _service()

    def fake_request(method, path, data=None):
        if path.endswith('/channels'):
            return [{"id": "c1", "name": "hermes-hive"}]
        if path.endswith('/threads/active'):
            return {"threads": [{"id": "t1", "name": "QMD-proj", "parent_id": "c1", "thread_metadata": {}}]}
        raise AssertionError(path)

    monkeypatch.setattr(DiscordMasterService, "_request_json", lambda self, method, path, data=None: fake_request(method, path, data))
    env = create_envelope(
        from_="mini1",
        to="turq",
        ch="command",
        action="discord.thread.list",
        text="{}",
    )

    out = svc.execute(env)
    assert out["ok"] is True
    assert out["threads"][0]["mention_user_id"] == "123"
