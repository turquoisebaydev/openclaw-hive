"""Tests for Discord thread governor client helpers."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from hive_daemon.config import DiscordMasterConfig, HiveConfig, ThreadGovernorConfig
from hive_daemon.discord_thread_governor_bot import (
    DiscordThreadGovernorClient,
    _format_last_owner,
    _format_unlock_eta,
    create_thread_governor_client,
)


def _governor_config() -> ThreadGovernorConfig:
    return ThreadGovernorConfig(
        owner_id="737554625577746492",
        watched_parents=["1490207637101613076"],
    )


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 4, 6, 12, 0, tzinfo=tz or timezone.utc)


def test_format_unlock_eta(monkeypatch):
    import hive_daemon.discord_thread_governor_bot as bot_mod

    monkeypatch.setattr(bot_mod, "datetime", FrozenDateTime)
    assert _format_unlock_eta(datetime(2026, 4, 6, 12, 5, tzinfo=timezone.utc)) == "in 5m"


def test_format_last_owner(monkeypatch):
    import hive_daemon.discord_thread_governor_bot as bot_mod

    monkeypatch.setattr(bot_mod, "datetime", FrozenDateTime)
    assert _format_last_owner(datetime(2026, 4, 6, 10, 0, tzinfo=timezone.utc)) == "2h ago"


def test_create_client_requires_governor_config():
    with pytest.raises(RuntimeError, match="thread governor config is required"):
        create_thread_governor_client(
            HiveConfig(
                node_id="turq",
                discord_master=DiscordMasterConfig(
                    enabled=True,
                    guild_id="1476805337968279685",
                    bot_token="tok",
                ),
            )
        )


def test_is_authorized_allows_owner():
    client = object.__new__(DiscordThreadGovernorClient)
    client._governor_config = _governor_config()
    interaction = SimpleNamespace(
        user=SimpleNamespace(
            id=737554625577746492,
            guild_permissions=SimpleNamespace(administrator=False, manage_threads=False),
        )
    )

    assert client._is_authorized(interaction) is True


def test_is_authorized_allows_thread_admin():
    client = object.__new__(DiscordThreadGovernorClient)
    client._governor_config = _governor_config()
    interaction = SimpleNamespace(
        user=SimpleNamespace(
            id=1,
            guild_permissions=SimpleNamespace(administrator=False, manage_threads=True),
        )
    )

    assert client._is_authorized(interaction) is True
