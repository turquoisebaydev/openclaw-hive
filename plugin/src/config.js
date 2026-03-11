import { DEFAULT_CONFIG } from "./constants.js";

function clampInteger(value, fallback, minimum, maximum) {
  const numeric = Number.parseInt(String(value ?? ""), 10);
  if (!Number.isFinite(numeric)) {
    return fallback;
  }
  return Math.min(Math.max(numeric, minimum), maximum);
}

function normalizeString(value, fallback = "") {
  const normalized = String(value ?? fallback).trim();
  return normalized || fallback;
}

function normalizeOptionalString(value) {
  const normalized = String(value ?? "").trim();
  return normalized || undefined;
}

export function buildClientId(config) {
  const prefix = normalizeString(config.mqtt.clientIdPrefix, DEFAULT_CONFIG.mqtt.clientIdPrefix);
  const gatewayId = normalizeString(config.identity.gatewayId, "gw");
  const agentId = normalizeString(config.identity.agentId, "agent");
  return `${prefix}-${gatewayId}-${agentId}`.slice(0, 120);
}

export function normalizeConfig(rawConfig = {}) {
  const rawMqtt = rawConfig.mqtt ?? {};
  const rawIdentity = rawConfig.identity ?? {};
  const rawPresence = rawConfig.presence ?? {};
  const rawEvents = rawConfig.events ?? {};

  return {
    topicPrefix: normalizeString(rawConfig.topicPrefix, DEFAULT_CONFIG.topicPrefix),
    mqtt: {
      url: normalizeString(rawMqtt.url),
      username: normalizeOptionalString(rawMqtt.username),
      password: normalizeOptionalString(rawMqtt.password),
      clientIdPrefix: normalizeString(rawMqtt.clientIdPrefix, DEFAULT_CONFIG.mqtt.clientIdPrefix),
      keepaliveSec: clampInteger(
        rawMqtt.keepaliveSec,
        DEFAULT_CONFIG.mqtt.keepaliveSec,
        5,
        300,
      ),
      connectTimeoutMs: clampInteger(
        rawMqtt.connectTimeoutMs,
        DEFAULT_CONFIG.mqtt.connectTimeoutMs,
        1000,
        60000,
      ),
      qos: clampInteger(rawMqtt.qos, DEFAULT_CONFIG.mqtt.qos, 0, 2),
      clean: rawMqtt.clean ?? DEFAULT_CONFIG.mqtt.clean,
    },
    identity: {
      gatewayId: normalizeString(rawIdentity.gatewayId),
      agentId: normalizeString(rawIdentity.agentId, DEFAULT_CONFIG.identity.agentId),
    },
    presence: {
      enabled: rawPresence.enabled ?? DEFAULT_CONFIG.presence.enabled,
      retain: rawPresence.retain ?? DEFAULT_CONFIG.presence.retain,
      ttlSec: clampInteger(rawPresence.ttlSec, DEFAULT_CONFIG.presence.ttlSec, 30, 3600),
      debounceMs: clampInteger(
        rawPresence.debounceMs,
        DEFAULT_CONFIG.presence.debounceMs,
        50,
        5000,
      ),
      maxDelayMs: clampInteger(
        rawPresence.maxDelayMs,
        DEFAULT_CONFIG.presence.maxDelayMs,
        100,
        10000,
      ),
      publishTaskDetails:
        rawPresence.publishTaskDetails ?? DEFAULT_CONFIG.presence.publishTaskDetails,
    },
    events: {
      enabled: rawEvents.enabled ?? DEFAULT_CONFIG.events.enabled,
      redact: rawEvents.redact ?? DEFAULT_CONFIG.events.redact,
      includeQueue: rawEvents.includeQueue ?? DEFAULT_CONFIG.events.includeQueue,
      summaryMaxLength: clampInteger(
        rawEvents.summaryMaxLength,
        DEFAULT_CONFIG.events.summaryMaxLength,
        32,
        1000,
      ),
    },
  };
}
