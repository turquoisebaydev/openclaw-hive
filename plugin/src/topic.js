function encodeSegment(value) {
  return encodeURIComponent(String(value ?? "").trim());
}

export function sessionKey(identity) {
  return `${identity.gatewayId}/${identity.agentId}/${identity.sessionId}`;
}

export function presenceTopic(prefix, identity) {
  return `${prefix}/presence/${encodeSegment(identity.gatewayId)}/${encodeSegment(identity.agentId)}/${encodeSegment(identity.sessionId)}`;
}

export function eventTopic(prefix, identity) {
  return `${prefix}/events/${encodeSegment(identity.gatewayId)}/${encodeSegment(identity.agentId)}/${encodeSegment(identity.sessionId)}`;
}
