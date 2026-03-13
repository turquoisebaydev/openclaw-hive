import test from "node:test";
import assert from "node:assert/strict";

import { normalizeConfig } from "../src/config.js";
import { buildPresencePayload, reduceSessionEvent } from "../src/reducer.js";
import { sanitizeEvent, sanitizePresencePayload } from "../src/sanitizer.js";

test("sanitizeEvent redacts secrets from errors and task details", () => {
  const config = normalizeConfig({
    mqtt: { url: "mqtt://localhost" },
    identity: { gatewayId: "turq", agentId: "main" },
  });

  const payload = sanitizeEvent({
    domain: "llm",
    event: "error",
    sessionKey: "sess-1",
    data: {
      error: "Bearer super-secret-token",
      task: {
        summary: "api_key=abc123 should never leak",
        cmdline: "run --token=xyz",
      },
    },
  }, config, config.identity, 1700000000000);

  assert.equal(payload.session, "sess-1");
  assert.equal(payload.activityType, "gw");
  assert.match(payload.error, /Bearer \[redacted\]/i);
  assert.match(payload.task.summary, /api_key=\[redacted\]/i);
});

test("sanitizeEvent emits deterministic activityType for claude acp sessions", () => {
  const config = normalizeConfig({
    mqtt: { url: "mqtt://localhost" },
    identity: { gatewayId: "turq", agentId: "main" },
  });

  const payload = sanitizeEvent({
    domain: "run",
    event: "started",
    sessionKey: "agent:claude:acp:run-1",
    agentId: "claude",
  }, config, config.identity, 1700000000000);

  assert.equal(payload.activityType, "claude");
});

test("presence payload omits task details when disabled and redacts errors", () => {
  const config = normalizeConfig({
    mqtt: { url: "mqtt://localhost" },
    identity: { gatewayId: "turq", agentId: "main" },
    presence: { publishTaskDetails: false },
  });

  const state = reduceSessionEvent(undefined, {
    domain: "run",
    event: "error",
    sessionKey: "sess-2",
    data: {
      error: "token=abcd1234",
      task: {
        summary: "debugging auth",
        cwd: "/tmp/work",
      },
    },
    ts: 1700000000,
  }, {
    identityFallback: config.identity,
    nowMs: 1700000000000,
    summaryMaxLength: config.events.summaryMaxLength,
  });

  const payload = sanitizePresencePayload(buildPresencePayload(state, config), config);

  assert.equal(payload.session, "sess-2");
  assert.equal("task" in payload, false);
  assert.match(payload.lastError, /token=\[redacted\]/i);
});
