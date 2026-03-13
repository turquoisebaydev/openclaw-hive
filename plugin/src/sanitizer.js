import {
  extractActivityType,
  extractChannel,
  extractContext,
  extractDomain,
  extractErrorSummary,
  extractEventName,
  extractIdentity,
  extractModel,
  extractPhase,
  extractQueue,
  extractRunId,
  extractTaskSummary,
  extractTimestampMs,
  extractTokenUsage,
  extractTool,
} from "./extract.js";
import { EVENT_VERSION } from "./constants.js";

const SECRET_PATTERNS = [
  /(bearer\s+)[a-z0-9._-]+/gi,
  /(api[_-]?key\s*[:=]\s*)[^\s]+/gi,
  /(token\s*[:=]\s*)[^\s]+/gi,
  /sk-[a-z0-9]+/gi,
];

function truncate(value, maxLength) {
  if (!value || value.length <= maxLength) {
    return value;
  }
  return `${value.slice(0, Math.max(maxLength - 1, 1))}…`;
}

function redactString(value) {
  let redacted = value;
  for (const pattern of SECRET_PATTERNS) {
    redacted = redacted.replace(pattern, "$1[redacted]");
  }
  return redacted;
}

export function sanitizeEvent(event, config, identityFallback, nowMs) {
  const identity = extractIdentity(event, identityFallback);
  if (!identity.sessionId) {
    return undefined;
  }

  const maxLength = config.events.summaryMaxLength;
  const payload = {
    v: EVENT_VERSION,
    kind: "runtime_event",
    gw: identity.gatewayId,
    agent: identity.agentId,
    session: identity.sessionId,
    domain: extractDomain(event),
    event: extractEventName(event),
    phase: extractPhase(event) || undefined,
    activityType: extractActivityType(event, identity) || "gw",
    updatedTs: Math.floor(extractTimestampMs(event, nowMs) / 1000),
  };

  const runId = extractRunId(event);
  if (runId) {
    payload.runId = runId;
  }

  const model = extractModel(event);
  if (model) {
    payload.model = truncate(model, maxLength);
  }

  const tool = extractTool(event);
  if (tool) {
    payload.tool = {
      name: truncate(tool.name, maxLength),
      phase: truncate(tool.phase, maxLength),
    };
  }

  const usage = extractTokenUsage(event);
  if (usage) {
    payload.tokens = usage;
  }

  const context = extractContext(event);
  if (context) {
    payload.context = {
      used: context.used ?? "unknown",
      max: context.max ?? "unknown",
    };
  }

  const queue = extractQueue(event);
  if (queue && config.events.includeQueue) {
    payload.queue = queue;
  }

  const channel = extractChannel(event);
  if (channel) {
    payload.channel = channel;
  }

  const task = extractTaskSummary(event, maxLength);
  if (task) {
    payload.task = config.events.redact
      ? Object.fromEntries(
          Object.entries(task).map(([key, value]) => [
            key,
            truncate(
              redactString(String(value)),
              key === "url" ? maxLength * 2 : maxLength,
            ),
          ]),
        )
      : task;
  }

  const error = extractErrorSummary(event);
  if (error) {
    const summary = config.events.redact ? redactString(error) : error;
    payload.error = truncate(summary, maxLength);
  }

  return payload;
}

export function sanitizePresencePayload(payload, config) {
  if (!config.events.redact) {
    return payload;
  }

  if (!payload.lastError && !payload.task) {
    return payload;
  }

  const next = { ...payload };
  if (next.lastError) {
    next.lastError = truncate(redactString(next.lastError), config.events.summaryMaxLength);
  }
  if (next.task) {
    next.task = Object.fromEntries(
      Object.entries(next.task).map(([key, value]) => [
        key,
        truncate(redactString(String(value)), key === "url" ? config.events.summaryMaxLength * 2 : config.events.summaryMaxLength),
      ]),
    );
  }
  return next;
}
