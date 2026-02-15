"""Configuration loading for hive daemon."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True, slots=True)
class MqttConfig:
    """MQTT broker connection settings."""

    host: str = "localhost"
    port: int = 1883
    username: str | None = None
    password: str | None = None
    keepalive: int = 60


@dataclass(frozen=True, slots=True)
class OcInstance:
    """A local OpenClaw instance."""

    name: str
    profile: str | None = None
    port: int | None = None


@dataclass(frozen=True, slots=True)
class HeartbeatConfig:
    """Heartbeat timing settings."""

    interval: float = 5.0
    miss_threshold: int = 3


@dataclass(frozen=True, slots=True)
class HiveConfig:
    """Top-level hive daemon configuration."""

    node_id: str
    topic_prefix: str = "turq/hive"
    handler_dir: str = "hive-daemon.d"
    handler_timeout: int = 30
    mqtt: MqttConfig = field(default_factory=MqttConfig)
    oc_instances: list[OcInstance] = field(default_factory=list)
    heartbeat: HeartbeatConfig = field(default_factory=HeartbeatConfig)
    log_level: str = "INFO"


def load_config(path: Path) -> HiveConfig:
    """Load configuration from a TOML file.

    Raises FileNotFoundError if the file doesn't exist.
    Raises KeyError if required fields are missing.
    """
    with open(path, "rb") as f:
        raw = tomllib.load(f)

    node_section = raw.get("node", {})
    node_id = node_section["id"]  # required — let KeyError propagate

    mqtt_section = raw.get("mqtt", {})
    mqtt = MqttConfig(
        host=mqtt_section.get("host", "localhost"),
        port=mqtt_section.get("port", 1883),
        username=mqtt_section.get("username"),
        password=mqtt_section.get("password"),
        keepalive=mqtt_section.get("keepalive", 60),
    )

    oc_list = []
    for inst in raw.get("oc_instances", []):
        oc_list.append(OcInstance(
            name=inst["name"],
            profile=inst.get("profile"),
            port=inst.get("port"),
        ))

    hb_section = raw.get("heartbeat", {})
    heartbeat = HeartbeatConfig(
        interval=hb_section.get("interval", 5.0),
        miss_threshold=hb_section.get("miss_threshold", 3),
    )

    return HiveConfig(
        node_id=node_id,
        topic_prefix=node_section.get("topic_prefix", "turq/hive"),
        handler_dir=node_section.get("handler_dir", "hive-daemon.d"),
        handler_timeout=node_section.get("handler_timeout", 30),
        mqtt=mqtt,
        oc_instances=oc_list,
        heartbeat=heartbeat,
        log_level=raw.get("logging", {}).get("level", "INFO"),
    )
