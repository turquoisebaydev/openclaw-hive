"""Configuration loading for hive daemon."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_OPENCLAW_CMD = "openclaw"


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
    # OpenClaw agent id to target for hive injections (e.g. "main" or "default").
    agent_id: str | None = None
    # Optional path/command override for this instance's OpenClaw CLI.
    # Examples: "openclaw", "/Users/turquoise/opt/openclaw-mini1/bin/openclaw"
    openclaw_cmd: str | None = None

    @property
    def resolved_openclaw_cmd(self) -> str:
        """Effective CLI command for this instance."""
        cmd = (self.openclaw_cmd or "").strip()
        return cmd or DEFAULT_OPENCLAW_CMD


@dataclass(frozen=True, slots=True)
class HeartbeatConfig:
    """Heartbeat timing settings."""

    interval: float = 5.0
    miss_threshold: int = 3


@dataclass(frozen=True, slots=True)
class ThreadingConfig:
    """Discord thread-grouping settings for correlated announcements."""

    enabled: bool = False
    mode: str = "by_corr"
    thread_name_prefix: str = "hive"
    max_age_hours: int = 168
    fallback_to_channel: bool = True


@dataclass(frozen=True, slots=True)
class DiscordAnnouncementConfig:
    """Discord-specific announcement settings."""

    enabled: bool = False
    channel: str = "hive-announcements"
    webhook_url: str | None = None
    publish_send: bool = True
    publish_receive: bool = True
    threading: ThreadingConfig = field(default_factory=ThreadingConfig)


@dataclass(frozen=True, slots=True)
class AnnouncementsConfig:
    """Top-level announcement settings."""

    enabled: bool = False
    discord: DiscordAnnouncementConfig = field(default_factory=DiscordAnnouncementConfig)


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
    announcements: AnnouncementsConfig = field(default_factory=AnnouncementsConfig)
    log_level: str = "INFO"

    @property
    def instance_names(self) -> set[str]:
        """Set of all managed OC instance names (hive addresses)."""
        return {inst.name for inst in self.oc_instances}

    def instance_by_name(self, name: str) -> OcInstance | None:
        """Look up an OC instance by its hive address name."""
        for inst in self.oc_instances:
            if inst.name == name:
                return inst
        return None


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
            agent_id=inst.get("agent_id") or inst.get("agent"),
            openclaw_cmd=inst.get("openclaw_cmd") or inst.get("openclaw"),
        ))

    hb_section = raw.get("heartbeat", {})
    heartbeat = HeartbeatConfig(
        interval=hb_section.get("interval", 5.0),
        miss_threshold=hb_section.get("miss_threshold", 3),
    )

    ann_section = raw.get("announcements", {})
    discord_section = ann_section.get("discord", {})
    threading_section = discord_section.get("threading", {})
    threading_cfg = ThreadingConfig(
        enabled=threading_section.get("enabled", False),
        mode=threading_section.get("mode", "by_corr"),
        thread_name_prefix=threading_section.get("thread_name_prefix", "hive"),
        max_age_hours=threading_section.get("max_age_hours", 168),
        fallback_to_channel=threading_section.get("fallback_to_channel", True),
    )
    discord_ann = DiscordAnnouncementConfig(
        enabled=discord_section.get("enabled", False),
        channel=discord_section.get("channel", "hive-announcements"),
        webhook_url=discord_section.get("webhook_url"),
        publish_send=discord_section.get("publish_send", True),
        publish_receive=discord_section.get("publish_receive", True),
        threading=threading_cfg,
    )
    announcements = AnnouncementsConfig(
        enabled=ann_section.get("enabled", False),
        discord=discord_ann,
    )

    return HiveConfig(
        node_id=node_id,
        topic_prefix=node_section.get("topic_prefix", "turq/hive"),
        handler_dir=node_section.get("handler_dir", "hive-daemon.d"),
        handler_timeout=node_section.get("handler_timeout", 30),
        mqtt=mqtt,
        oc_instances=oc_list,
        heartbeat=heartbeat,
        announcements=announcements,
        log_level=raw.get("logging", {}).get("level", "INFO"),
    )
