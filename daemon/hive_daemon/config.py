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
class PresenceDiscoveryConfig:
    """Settings for remote presence discovery."""

    accept_remote: bool = True
    prune_stale: bool = True


@dataclass(frozen=True, slots=True)
class PresenceConfig:
    """Session presence registry settings."""

    enabled: bool = True
    interval_sec: int = 30
    ttl_sec: int = 300
    retain: bool = True
    publish_task_details: bool = True
    discovery: PresenceDiscoveryConfig = field(default_factory=PresenceDiscoveryConfig)


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
    channel_id: str | None = None
    bot_token: str | None = None
    webhook_url: str | None = None
    publish_send: bool = True
    publish_receive: bool = True
    threading: ThreadingConfig = field(default_factory=ThreadingConfig)


@dataclass(frozen=True, slots=True)
class AuditConfig:
    """Local audit log settings for announcements."""

    enabled: bool = True
    path: str = "~/.local/state/hive/hive-announcements.log"


@dataclass(frozen=True, slots=True)
class AnnouncementsConfig:
    """Top-level announcement settings."""

    enabled: bool = False
    discord: DiscordAnnouncementConfig = field(default_factory=DiscordAnnouncementConfig)
    audit: AuditConfig = field(default_factory=AuditConfig)


@dataclass(frozen=True, slots=True)
class DiscordMasterChannelConfig:
    """A configured Discord channel managed by the Discord master daemon."""

    name: str
    gateway: str | None = None
    mention_target: str | None = None
    mention_type: str = "auto"
    thread_suffix: str = "-init"


@dataclass(frozen=True, slots=True)
class DiscordMasterAliasConfig:
    """Discord-wide mention alias mapping (e.g. pg -> user:123)."""

    name: str
    mention_target: str
    mention_type: str = "auto"


@dataclass(frozen=True, slots=True)
class ThreadGovernorCleanupConfig:
    """Cleanup policy for thread governor durable state."""

    interval_minutes: int = 60
    idle_expiry_days: int = 7
    archived_expiry_days: int = 2


@dataclass(frozen=True, slots=True)
class ThreadGovernorConfig:
    """Thread governor policy for watched Discord parents."""

    owner_id: str
    watched_parents: list[str]
    default_limit: int = 12
    auto_unlock_minutes: int = 10
    notice_template: str = (
        "Paused for {minutes}m: thread exceeded {limit} messages without Hugh. "
        "Hugh can use /unlock to resume sooner."
    )
    cleanup: ThreadGovernorCleanupConfig = field(default_factory=ThreadGovernorCleanupConfig)


@dataclass(frozen=True, slots=True)
class DiscordMasterConfig:
    """Discord bot API settings for the Discord master daemon."""

    enabled: bool = False
    guild_id: str | None = None
    bot_token: str | None = None
    proxy_to: str | None = None
    api_base: str = "https://discord.com/api/v10"
    request_timeout_sec: int = 10
    default_parent_suffix: str = "-hive"
    default_thread_suffix: str = "-init"
    default_hive_channel_id: str | None = None
    channels: list[DiscordMasterChannelConfig] = field(default_factory=list)
    aliases: list[DiscordMasterAliasConfig] = field(default_factory=list)
    thread_governor: ThreadGovernorConfig | None = None


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
    presence: PresenceConfig = field(default_factory=PresenceConfig)
    announcements: AnnouncementsConfig = field(default_factory=AnnouncementsConfig)
    discord_master: DiscordMasterConfig = field(default_factory=DiscordMasterConfig)
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

    pres_section = raw.get("presence", {})
    pres_disc_section = pres_section.get("discovery", {})
    presence_discovery = PresenceDiscoveryConfig(
        accept_remote=pres_disc_section.get("accept_remote", True),
        prune_stale=pres_disc_section.get("prune_stale", True),
    )
    presence = PresenceConfig(
        enabled=pres_section.get("enabled", True),
        interval_sec=pres_section.get("interval_sec", 30),
        ttl_sec=pres_section.get("ttl_sec", 300),
        retain=pres_section.get("retain", True),
        publish_task_details=pres_section.get("publish_task_details", True),
        discovery=presence_discovery,
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
        channel_id=discord_section.get("channel_id"),
        bot_token=discord_section.get("bot_token"),
        webhook_url=discord_section.get("webhook_url"),
        publish_send=discord_section.get("publish_send", True),
        publish_receive=discord_section.get("publish_receive", True),
        threading=threading_cfg,
    )
    audit_section = ann_section.get("audit", {})
    audit_cfg = AuditConfig(
        enabled=audit_section.get("enabled", True),
        path=audit_section.get("path", "~/.local/state/hive/hive-announcements.log"),
    )
    announcements = AnnouncementsConfig(
        enabled=ann_section.get("enabled", False),
        discord=discord_ann,
        audit=audit_cfg,
    )

    discord_master_section = raw.get("discord_master", {})
    discord_master_channels: list[DiscordMasterChannelConfig] = []
    for channel in discord_master_section.get("channels", []):
        name = channel.get("name") or channel.get("channel")
        if not name:
            raise KeyError("discord_master.channels[].name is required")
        discord_master_channels.append(
            DiscordMasterChannelConfig(
                name=name,
                gateway=channel.get("gateway") or channel.get("gw"),
                mention_target=channel.get("mention_target") or channel.get("mention"),
                mention_type=channel.get("mention_type") or channel.get("mention_kind", "auto"),
                thread_suffix=channel.get("thread_suffix") or channel.get("thread_name_suffix", "-init"),
            )
        )

    discord_master_aliases: list[DiscordMasterAliasConfig] = []
    aliases_section = discord_master_section.get("aliases", {})
    if isinstance(aliases_section, dict):
        for alias, alias_value in aliases_section.items():
            mention_target = None
            mention_type = "auto"
            if isinstance(alias_value, str):
                mention_target = alias_value
            elif isinstance(alias_value, dict):
                mention_target = alias_value.get("mention_target") or alias_value.get("target") or alias_value.get("mention")
                mention_type = alias_value.get("mention_type") or alias_value.get("mention_kind", "auto")
            if not mention_target:
                raise KeyError(f"discord_master.aliases.{alias} requires mention_target/target")
            discord_master_aliases.append(
                DiscordMasterAliasConfig(
                    name=str(alias),
                    mention_target=str(mention_target),
                    mention_type=str(mention_type),
                )
            )
    elif isinstance(aliases_section, list):
        for alias in aliases_section:
            if not isinstance(alias, dict):
                continue
            name = alias.get("name") or alias.get("alias")
            mention_target = alias.get("mention_target") or alias.get("target") or alias.get("mention")
            if not name or not mention_target:
                raise KeyError("discord_master.aliases[] requires name and mention_target/target")
            discord_master_aliases.append(
                DiscordMasterAliasConfig(
                    name=str(name),
                    mention_target=str(mention_target),
                    mention_type=str(alias.get("mention_type") or alias.get("mention_kind", "auto")),
                )
            )

    thread_governor = _load_thread_governor_config(discord_master_section.get("thread_governor"))

    discord_master = DiscordMasterConfig(
        enabled=discord_master_section.get("enabled", False),
        guild_id=discord_master_section.get("guild_id"),
        bot_token=discord_master_section.get("bot_token"),
        proxy_to=discord_master_section.get("proxy_to") or discord_master_section.get("proxy_node"),
        api_base=discord_master_section.get("api_base", "https://discord.com/api/v10"),
        request_timeout_sec=discord_master_section.get("request_timeout_sec", 10),
        default_parent_suffix=discord_master_section.get("default_parent_suffix", "-hive"),
        default_thread_suffix=discord_master_section.get("default_thread_suffix", "-init"),
        default_hive_channel_id=(
            discord_master_section.get("default_hive_channel_id")
            or discord_master_section.get("default_hive_forum_channel_id")
            or discord_master_section.get("default_hive_parent_id")
        ),
        channels=discord_master_channels,
        aliases=discord_master_aliases,
        thread_governor=thread_governor,
    )

    return HiveConfig(
        node_id=node_id,
        topic_prefix=node_section.get("topic_prefix", "turq/hive"),
        handler_dir=node_section.get("handler_dir", "hive-daemon.d"),
        handler_timeout=node_section.get("handler_timeout", 30),
        mqtt=mqtt,
        oc_instances=oc_list,
        heartbeat=heartbeat,
        presence=presence,
        announcements=announcements,
        discord_master=discord_master,
        log_level=raw.get("logging", {}).get("level", "INFO"),
    )


def _load_thread_governor_config(raw: object) -> ThreadGovernorConfig | None:
    """Parse an optional thread governor config block."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("discord_master.thread_governor must be a table")

    owner_id = str(raw.get("owner_id") or "").strip()
    if not owner_id:
        raise KeyError("discord_master.thread_governor.owner_id is required")

    watched_raw = raw.get("watched_parents")
    if watched_raw is None:
        raise KeyError("discord_master.thread_governor.watched_parents is required")
    if not isinstance(watched_raw, list):
        raise ValueError("discord_master.thread_governor.watched_parents must be a list")
    watched_parents = [str(value).strip() for value in watched_raw if str(value).strip()]
    if not watched_parents:
        raise ValueError("discord_master.thread_governor.watched_parents must not be empty")

    default_limit = int(raw.get("default_limit", 12))
    if default_limit < 1:
        raise ValueError("discord_master.thread_governor.default_limit must be >= 1")

    auto_unlock_minutes = int(raw.get("auto_unlock_minutes", 10))
    if auto_unlock_minutes < 1:
        raise ValueError("discord_master.thread_governor.auto_unlock_minutes must be >= 1")

    notice_template = str(
        raw.get(
            "notice_template",
            "Paused for {minutes}m: thread exceeded {limit} messages without Hugh. "
            "Hugh can use /unlock to resume sooner.",
        )
    ).strip()
    if not notice_template:
        raise ValueError("discord_master.thread_governor.notice_template must not be empty")

    cleanup_raw = raw.get("cleanup", {})
    if not isinstance(cleanup_raw, dict):
        raise ValueError("discord_master.thread_governor.cleanup must be a table")
    cleanup = ThreadGovernorCleanupConfig(
        interval_minutes=int(cleanup_raw.get("interval_minutes", 60)),
        idle_expiry_days=int(cleanup_raw.get("idle_expiry_days", 7)),
        archived_expiry_days=int(cleanup_raw.get("archived_expiry_days", 2)),
    )
    if cleanup.interval_minutes < 1:
        raise ValueError("discord_master.thread_governor.cleanup.interval_minutes must be >= 1")
    if cleanup.idle_expiry_days < 1:
        raise ValueError("discord_master.thread_governor.cleanup.idle_expiry_days must be >= 1")
    if cleanup.archived_expiry_days < 1:
        raise ValueError("discord_master.thread_governor.cleanup.archived_expiry_days must be >= 1")

    return ThreadGovernorConfig(
        owner_id=owner_id,
        watched_parents=watched_parents,
        default_limit=default_limit,
        auto_unlock_minutes=auto_unlock_minutes,
        notice_template=notice_template,
        cleanup=cleanup,
    )
