"""Tests for deterministic discord master actions."""

import json

import pytest

from hive_daemon.config import DiscordMasterAliasConfig, DiscordMasterChannelConfig, DiscordMasterConfig
from hive_daemon.discord_master import DiscordMasterError, DiscordMasterService
from hive_daemon.envelope import create_envelope


def _service(**kwargs) -> DiscordMasterService:
    channels = kwargs.pop("channels", [DiscordMasterChannelConfig(name="qmd", mention_target="user:123")])
    aliases = kwargs.pop("aliases", [])
    cfg = DiscordMasterConfig(
        enabled=True,
        guild_id="1476805337968279685",
        bot_token="tok",
        channels=channels,
        aliases=aliases,
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
                {"id": "c1", "name": "qmd-hive", "type": 0},
                {"id": "c2", "name": "random", "type": 0},
            ]
        if path.endswith('/threads/active'):
            return {
                "threads": [
                    {"id": "t1", "name": "deploy-dashboard-init", "parent_id": "c1", "thread_metadata": {}},
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
    threads = [t for t in out["threads"] if t["type"] == "thread"]
    assert len(threads) == 1
    assert threads[0]["name"] == "deploy-dashboard-init"
    assert threads[0]["mention_user_id"] == "123"

def test_thread_list_mention_falls_back_to_thread_base(monkeypatch):
    svc = _service()

    def fake_request(method, path, data=None):
        if path.endswith('/channels'):
            return [{"id": "c1", "name": "hermes-hive", "type": 0}]
        if path.endswith('/threads/active'):
            return {"threads": [{"id": "t1", "name": "qmd-init", "parent_id": "c1", "thread_metadata": {}}]}
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
    threads = [t for t in out["threads"] if t["type"] == "thread"]
    assert threads[0]["mention_user_id"] == "123"

def test_thread_list_mention_from_parent_hive_convention(monkeypatch):
    svc = _service(channels=[])

    def fake_request(method, path, data=None):
        if path.endswith('/channels'):
            return [{"id": "c1", "name": "hermes-hive", "type": 0}]
        if path.endswith('/threads/active'):
            return {"threads": [{"id": "t1", "name": "mission-control-init", "parent_id": "c1", "thread_metadata": {}}]}
        if '/members/search' in path:
            return [{"user": {"id": "1482865686102671481", "username": "Hermes"}}]
        if path.endswith('/roles'):
            return []
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
    threads = [t for t in out["threads"] if t["type"] == "thread"]
    t = threads[0]
    assert t["mention"] == "<@1482865686102671481>"
    assert t["mention_user_id"] == "1482865686102671481"


def test_thread_list_includes_proj_channels(monkeypatch):
    """Top-level *-proj channels appear as type=channel when thread_suffix matches."""
    svc = _service()

    def fake_request(method, path, data=None):
        if path.endswith('/channels'):
            return [
                {"id": "c1", "name": "pg-hive", "type": 0},
                {"id": "c2", "name": "openclaw-pg-proj", "type": 0},
                {"id": "c3", "name": "random", "type": 0},
                {"id": "c4", "name": "voice-chat", "type": 2},  # voice, not text
            ]
        if path.endswith('/threads/active'):
            return {"threads": []}
        raise AssertionError(path)

    monkeypatch.setattr(DiscordMasterService, "_request_json", lambda self, method, path, data=None: fake_request(method, path, data))
    env = create_envelope(
        from_="mini1",
        to="turq",
        ch="command",
        action="discord.thread.list",
        text=json.dumps({"thread_suffix": "-proj"}),
    )

    out = svc.execute(env)
    assert out["ok"] is True
    assert out["count"] == 1
    assert out["threads"][0]["name"] == "openclaw-pg-proj"
    assert out["threads"][0]["type"] == "channel"
    assert out["threads"][0]["parent_id"] is None


def test_thread_list_default_excludes_proj_channels(monkeypatch):
    """Default -init suffix should not return -proj top-level channels."""
    svc = _service()

    def fake_request(method, path, data=None):
        if path.endswith('/channels'):
            return [
                {"id": "c1", "name": "pg-hive", "type": 0},
                {"id": "c2", "name": "openclaw-pg-proj", "type": 0},
            ]
        if path.endswith('/threads/active'):
            return {"threads": [
                {"id": "t1", "name": "deploy-init", "parent_id": "c1", "thread_metadata": {}},
            ]}
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
    # Only the -init thread, not the -proj channel
    assert out["count"] == 1
    assert out["threads"][0]["name"] == "deploy-init"
    assert out["threads"][0]["type"] == "thread"


def test_thread_list_include_channels_false(monkeypatch):
    """include_channels=false suppresses top-level channel scanning."""
    svc = _service()

    def fake_request(method, path, data=None):
        if path.endswith('/channels'):
            return [{"id": "c1", "name": "openclaw-pg-proj", "type": 0}]
        if path.endswith('/threads/active'):
            return {"threads": []}
        raise AssertionError(path)

    monkeypatch.setattr(DiscordMasterService, "_request_json", lambda self, method, path, data=None: fake_request(method, path, data))
    env = create_envelope(
        from_="mini1",
        to="turq",
        ch="command",
        action="discord.thread.list",
        text=json.dumps({"thread_suffix": "-proj", "include_channels": False}),
    )

    out = svc.execute(env)
    assert out["ok"] is True
    assert out["count"] == 0


def test_mention_resolve_from_discord_wide_alias():
    svc = _service(
        channels=[],
        aliases=[DiscordMasterAliasConfig(name="pg", mention_target="user:1477047646769254643", mention_type="user")],
    )
    env = create_envelope(
        from_="mini1",
        to="turq",
        ch="command",
        action="discord.mention.resolve",
        text=json.dumps({"channel": "hive-pg-proj"}),
    )

    out = svc.execute(env)
    assert out["ok"] is True
    assert out["source"] == "alias:pg"
    assert out["mention"] == "<@1477047646769254643>"


def test_thread_list_mention_uses_discord_wide_alias(monkeypatch):
    svc = _service(
        channels=[],
        aliases=[DiscordMasterAliasConfig(name="pg", mention_target="user:1477047646769254643", mention_type="user")],
    )

    def fake_request(method, path, data=None):
        if path.endswith('/channels'):
            return [{"id": "c2", "name": "hive-pg-proj", "type": 0}]
        if path.endswith('/threads/active'):
            return {"threads": []}
        raise AssertionError(path)

    monkeypatch.setattr(DiscordMasterService, "_request_json", lambda self, method, path, data=None: fake_request(method, path, data))
    env = create_envelope(
        from_="mini1",
        to="turq",
        ch="command",
        action="discord.thread.list",
        text=json.dumps({"thread_suffix": "-proj"}),
    )

    out = svc.execute(env)
    assert out["ok"] is True
    assert out["threads"][0]["mention"] == "<@1477047646769254643>"
    assert out["threads"][0]["mention_source"] == "alias:pg"


def test_thread_rename_action(monkeypatch):
    svc = _service()

    def fake_request(method, path, data=None):
        assert method == "PATCH"
        assert path == "/channels/abc"
        assert data == {"name": "qmd-migration-init"}
        return {"id": "abc", "name": "qmd-migration-init", "parent_id": "p1"}

    monkeypatch.setattr(DiscordMasterService, "_request_json", lambda self, method, path, data=None: fake_request(method, path, data))
    env = create_envelope(
        from_="mini1",
        to="turq",
        ch="command",
        action="discord.thread.rename",
        text=json.dumps({"thread_id": "abc", "new_name": "qmd-migration-init"}),
    )

    out = svc.execute(env)
    assert out["ok"] is True
    assert out["thread"]["id"] == "abc"
    assert out["thread"]["name"] == "qmd-migration-init"


def test_thread_create_action_text_parent(monkeypatch):
    svc = _service(default_hive_channel_id="1490207637101613076")

    calls = []

    def fake_request(method, path, data=None):
        calls.append((method, path, data))
        if method == "GET" and path == "/channels/1490207637101613076":
            return {"id": "1490207637101613076", "type": 0}
        if method == "POST" and path == "/channels/1490207637101613076/messages":
            assert "<@123>" in (data or {}).get("content", "")
            return {"id": "m123"}
        if method == "POST" and path == "/channels/1490207637101613076/messages/m123/threads":
            assert data == {"name": "qmd-migration-init"}
            return {"id": "t123", "name": "qmd-migration-init", "parent_id": "1490207637101613076"}
        raise AssertionError((method, path, data))

    monkeypatch.setattr(DiscordMasterService, "_request_json", lambda self, method, path, data=None: fake_request(method, path, data))
    env = create_envelope(
        from_="mini1",
        to="turq",
        ch="command",
        action="discord.thread.create",
        text=json.dumps({"name": "qmd-migration-init", "content": "kickoff", "mention_targets": ["user:123"]}),
    )

    out = svc.execute(env)
    assert out["ok"] is True
    assert out["thread"]["id"] == "t123"
    assert out["thread"]["name"] == "qmd-migration-init"
