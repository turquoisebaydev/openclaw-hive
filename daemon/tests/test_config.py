"""Tests for hive daemon configuration loading."""

from pathlib import Path

import pytest

from hive_daemon.config import (
    AnnouncementsConfig,
    DiscordAnnouncementConfig,
    HiveConfig,
    MqttConfig,
    OcInstance,
    PresenceConfig,
    PresenceDiscoveryConfig,
    ThreadingConfig,
    load_config,
)


MINIMAL_TOML = """\
[node]
id = "turq-18789"
"""

FULL_TOML = """\
[node]
id = "turq-18789"
topic_prefix = "custom/prefix"
handler_dir = "/etc/hive-daemon.d"
handler_timeout = 60

[mqtt]
host = "mqtt.local"
port = 8883
username = "hive"
password = "secret"
keepalive = 30

[[oc_instances]]
name = "turq-18789"
profile = "turq"
port = 18789

[[oc_instances]]
name = "mini1-18889"
port = 18889
openclaw_cmd = "/opt/openclaw-mini1/bin/openclaw"

[logging]
level = "DEBUG"
"""


ANNOUNCEMENTS_TOML = """\
[node]
id = "turq"

[announcements]
enabled = true

[announcements.discord]
enabled = true
channel = "ops-feed"
webhook_url = "https://discord.com/api/webhooks/123/abc"
publish_send = true
publish_receive = false
"""

ANNOUNCEMENTS_PARTIAL_TOML = """\
[node]
id = "turq"

[announcements]
enabled = true

[announcements.discord]
enabled = true
"""

OPENCLAW_ALIAS_TOML = """\
[node]
id = "turq"

[[oc_instances]]
name = "mini1"
openclaw = "/opt/mini1/bin/openclaw"
"""

THREADING_FULL_TOML = """\
[node]
id = "turq"

[announcements]
enabled = true

[announcements.discord]
enabled = true
webhook_url = "https://discord.com/api/webhooks/123/abc"

[announcements.discord.threading]
enabled = true
mode = "by_corr"
thread_name_prefix = "ops"
max_age_hours = 48
fallback_to_channel = false
"""

THREADING_PARTIAL_TOML = """\
[node]
id = "turq"

[announcements]
enabled = true

[announcements.discord]
enabled = true

[announcements.discord.threading]
enabled = true
"""

PRESENCE_FULL_TOML = """\
[node]
id = "turq"

[presence]
enabled = true
interval_sec = 15
ttl_sec = 120
retain = false
publish_task_details = false

[presence.discovery]
accept_remote = false
prune_stale = false
"""

PRESENCE_PARTIAL_TOML = """\
[node]
id = "turq"

[presence]
enabled = false
"""

PRESENCE_DISABLED_TOML = """\
[node]
id = "turq"

[presence]
enabled = false
"""


class TestLoadConfig:
    def test_minimal(self, tmp_path: Path):
        f = tmp_path / "hive.toml"
        f.write_text(MINIMAL_TOML)
        cfg = load_config(f)
        assert cfg.node_id == "turq-18789"
        assert cfg.topic_prefix == "turq/hive"
        assert cfg.handler_dir == "hive-daemon.d"
        assert cfg.handler_timeout == 30
        assert cfg.mqtt.host == "localhost"
        assert cfg.mqtt.port == 1883
        assert cfg.oc_instances == []
        assert cfg.log_level == "INFO"

    def test_full(self, tmp_path: Path):
        f = tmp_path / "hive.toml"
        f.write_text(FULL_TOML)
        cfg = load_config(f)
        assert cfg.node_id == "turq-18789"
        assert cfg.topic_prefix == "custom/prefix"
        assert cfg.handler_dir == "/etc/hive-daemon.d"
        assert cfg.handler_timeout == 60
        assert cfg.mqtt.host == "mqtt.local"
        assert cfg.mqtt.port == 8883
        assert cfg.mqtt.username == "hive"
        assert cfg.mqtt.password == "secret"
        assert cfg.mqtt.keepalive == 30
        assert cfg.log_level == "DEBUG"
        assert len(cfg.oc_instances) == 2
        assert cfg.oc_instances[0].name == "turq-18789"
        assert cfg.oc_instances[0].profile == "turq"
        assert cfg.oc_instances[1].name == "mini1-18889"
        assert cfg.oc_instances[1].profile is None
        assert cfg.oc_instances[1].openclaw_cmd == "/opt/openclaw-mini1/bin/openclaw"
        assert cfg.oc_instances[0].resolved_openclaw_cmd == "openclaw"
        assert cfg.oc_instances[1].resolved_openclaw_cmd == "/opt/openclaw-mini1/bin/openclaw"

    def test_missing_node_id(self, tmp_path: Path):
        f = tmp_path / "hive.toml"
        f.write_text("[node]\n")
        with pytest.raises(KeyError):
            load_config(f)

    def test_file_not_found(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            load_config(tmp_path / "nope.toml")

    def test_announcements_defaults_when_missing(self, tmp_path: Path):
        """Announcements section absent → all defaults (disabled)."""
        f = tmp_path / "hive.toml"
        f.write_text(MINIMAL_TOML)
        cfg = load_config(f)
        assert cfg.announcements.enabled is False
        assert cfg.announcements.discord.enabled is False
        assert cfg.announcements.discord.channel == "hive-announcements"
        assert cfg.announcements.discord.webhook_url is None
        assert cfg.announcements.discord.publish_send is True
        assert cfg.announcements.discord.publish_receive is True

    def test_announcements_full(self, tmp_path: Path):
        """Explicit announcements config is parsed correctly."""
        f = tmp_path / "hive.toml"
        f.write_text(ANNOUNCEMENTS_TOML)
        cfg = load_config(f)
        assert cfg.announcements.enabled is True
        assert cfg.announcements.discord.enabled is True
        assert cfg.announcements.discord.channel == "ops-feed"
        assert cfg.announcements.discord.webhook_url == "https://discord.com/api/webhooks/123/abc"
        assert cfg.announcements.discord.publish_send is True
        assert cfg.announcements.discord.publish_receive is False

    def test_announcements_partial_defaults(self, tmp_path: Path):
        """Partial announcements.discord → missing fields use defaults."""
        f = tmp_path / "hive.toml"
        f.write_text(ANNOUNCEMENTS_PARTIAL_TOML)
        cfg = load_config(f)
        assert cfg.announcements.enabled is True
        assert cfg.announcements.discord.enabled is True
        assert cfg.announcements.discord.channel == "hive-announcements"
        assert cfg.announcements.discord.webhook_url is None
        assert cfg.announcements.discord.publish_send is True
        assert cfg.announcements.discord.publish_receive is True

    def test_openclaw_alias_compatibility(self, tmp_path: Path):
        """The 'openclaw' alias field is accepted as openclaw_cmd."""
        f = tmp_path / "hive.toml"
        f.write_text(OPENCLAW_ALIAS_TOML)
        cfg = load_config(f)
        assert cfg.oc_instances[0].openclaw_cmd == "/opt/mini1/bin/openclaw"
        assert cfg.oc_instances[0].resolved_openclaw_cmd == "/opt/mini1/bin/openclaw"

    def test_threading_defaults_when_missing(self, tmp_path: Path):
        """No threading section → all defaults (disabled)."""
        f = tmp_path / "hive.toml"
        f.write_text(MINIMAL_TOML)
        cfg = load_config(f)
        t = cfg.announcements.discord.threading
        assert t.enabled is False
        assert t.mode == "by_corr"
        assert t.thread_name_prefix == "hive"
        assert t.max_age_hours == 168
        assert t.fallback_to_channel is True

    def test_threading_full(self, tmp_path: Path):
        """Explicit threading config is parsed correctly."""
        f = tmp_path / "hive.toml"
        f.write_text(THREADING_FULL_TOML)
        cfg = load_config(f)
        t = cfg.announcements.discord.threading
        assert t.enabled is True
        assert t.mode == "by_corr"
        assert t.thread_name_prefix == "ops"
        assert t.max_age_hours == 48
        assert t.fallback_to_channel is False

    def test_threading_partial_defaults(self, tmp_path: Path):
        """Partial threading section → missing fields use defaults."""
        f = tmp_path / "hive.toml"
        f.write_text(THREADING_PARTIAL_TOML)
        cfg = load_config(f)
        t = cfg.announcements.discord.threading
        assert t.enabled is True
        assert t.mode == "by_corr"
        assert t.thread_name_prefix == "hive"
        assert t.max_age_hours == 168
        assert t.fallback_to_channel is True


class TestOcInstanceOpenclawCmd:
    """Regression tests for per-instance openclaw_cmd resolution."""

    def test_default_fallback(self):
        inst = OcInstance(name="node1")
        assert inst.resolved_openclaw_cmd == "openclaw"

    def test_explicit_cmd(self):
        inst = OcInstance(name="node1", openclaw_cmd="/opt/oc/bin/openclaw")
        assert inst.resolved_openclaw_cmd == "/opt/oc/bin/openclaw"

    def test_empty_string_falls_back_to_default(self):
        inst = OcInstance(name="node1", openclaw_cmd="")
        assert inst.resolved_openclaw_cmd == "openclaw"

    def test_whitespace_only_falls_back_to_default(self):
        inst = OcInstance(name="node1", openclaw_cmd="   ")
        assert inst.resolved_openclaw_cmd == "openclaw"

    def test_none_falls_back_to_default(self):
        inst = OcInstance(name="node1", openclaw_cmd=None)
        assert inst.resolved_openclaw_cmd == "openclaw"

    def test_preserves_exact_path(self):
        path = "/Users/turquoise/opt/openclaw-mini1/current/bin/openclaw"
        inst = OcInstance(name="mini1", openclaw_cmd=path)
        assert inst.resolved_openclaw_cmd == path


class TestPresenceConfig:
    """Tests for presence config parsing."""

    def test_defaults_when_missing(self, tmp_path: Path):
        """No presence section → all defaults (enabled by default)."""
        f = tmp_path / "hive.toml"
        f.write_text(MINIMAL_TOML)
        cfg = load_config(f)
        p = cfg.presence
        assert p.enabled is True
        assert p.interval_sec == 30
        assert p.ttl_sec == 300
        assert p.retain is True
        assert p.publish_task_details is True
        assert p.discovery.accept_remote is True
        assert p.discovery.prune_stale is True

    def test_full(self, tmp_path: Path):
        """Explicit presence config is parsed correctly."""
        f = tmp_path / "hive.toml"
        f.write_text(PRESENCE_FULL_TOML)
        cfg = load_config(f)
        p = cfg.presence
        assert p.enabled is True
        assert p.interval_sec == 15
        assert p.ttl_sec == 120
        assert p.retain is False
        assert p.publish_task_details is False
        assert p.discovery.accept_remote is False
        assert p.discovery.prune_stale is False

    def test_partial_defaults(self, tmp_path: Path):
        """Partial presence section → missing fields use defaults."""
        f = tmp_path / "hive.toml"
        f.write_text(PRESENCE_PARTIAL_TOML)
        cfg = load_config(f)
        p = cfg.presence
        assert p.enabled is False
        assert p.interval_sec == 30
        assert p.ttl_sec == 300
        assert p.discovery.accept_remote is True

    def test_disabled(self, tmp_path: Path):
        """Presence can be explicitly disabled."""
        f = tmp_path / "hive.toml"
        f.write_text(PRESENCE_DISABLED_TOML)
        cfg = load_config(f)
        assert cfg.presence.enabled is False
