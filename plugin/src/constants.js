export const PLUGIN_ID = "hive-bridge";
export const PLUGIN_NAME = "Hive Bridge";
export const PLUGIN_DESCRIPTION =
  "Publishes OpenClaw runtime presence snapshots and sanitized event telemetry to Hive MQTT.";

export const PRESENCE_VERSION = 2;
export const EVENT_VERSION = 1;
export const DEFAULT_TOPIC_PREFIX = "turq/hive";

export const DEFAULT_CONFIG = Object.freeze({
  topicPrefix: DEFAULT_TOPIC_PREFIX,
  mqtt: Object.freeze({
    clientIdPrefix: "hive-bridge",
    keepaliveSec: 30,
    connectTimeoutMs: 5000,
    qos: 1,
    clean: true,
  }),
  identity: Object.freeze({
    gatewayId: "",
    agentId: "main",
  }),
  presence: Object.freeze({
    enabled: true,
    retain: true,
    ttlSec: 300,
    debounceMs: 750,
    maxDelayMs: 5000,
    publishTaskDetails: true,
  }),
  events: Object.freeze({
    enabled: true,
    redact: true,
    includeQueue: true,
    summaryMaxLength: 160,
  }),
});
