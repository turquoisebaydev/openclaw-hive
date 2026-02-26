"""Tests for hive daemon configuration loading."""

from pathlib import Path

import pytest

from hive_daemon.config import HiveConfig, MqttConfig, OcInstance, load_config


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
