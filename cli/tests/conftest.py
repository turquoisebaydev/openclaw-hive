"""Shared test fixtures for hive CLI tests."""

import pytest

from hive_daemon.config import HiveConfig, MqttConfig


@pytest.fixture
def hive_config() -> HiveConfig:
    """Minimal HiveConfig for testing."""
    return HiveConfig(
        node_id="test-node-1",
        topic_prefix="turq/hive",
        mqtt=MqttConfig(host="localhost", port=1883),
    )
