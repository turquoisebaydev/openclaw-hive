function encodeSegment(value) {
  return encodeURIComponent(String(value ?? "").trim());
}

function normalizeString(value, fallback = "") {
  const normalized = String(value ?? fallback).trim();
  return normalized || fallback;
}

export function inferServerId(gatewayId) {
  const normalized = normalizeString(gatewayId).toLowerCase();
  if (!normalized) {
    return "";
  }
  if (normalized === "turq" || normalized === "mini1") {
    return "turq";
  }
  if (normalized === "turqette" || normalized.startsWith("turqette")) {
    return "turqette";
  }
  if (normalized === "pg1" || normalized.startsWith("pg")) {
    return "pg";
  }
  return normalized;
}

export function sessionKey(identity) {
  return `${identity.gatewayId}/${identity.agentId}/${identity.sessionId}`;
}

export function presenceTopic(prefix, identity) {
  return `${prefix}/presence/${encodeSegment(inferServerId(identity.gatewayId))}/gw/${encodeSegment(identity.gatewayId)}/${encodeSegment(identity.sessionId)}`;
}

export function eventTopic(prefix, identity) {
  return `${prefix}/events/${encodeSegment(inferServerId(identity.gatewayId))}/gw/${encodeSegment(identity.gatewayId)}/${encodeSegment(identity.sessionId)}`;
}
