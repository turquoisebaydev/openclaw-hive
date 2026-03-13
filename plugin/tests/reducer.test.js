import test from "node:test";
import assert from "node:assert/strict";

import { normalizeConfig } from "../src/config.js";
import {
  buildPresencePayload,
  reduceSessionEvent,
  shouldFlushPresenceImmediately,
} from "../src/reducer.js";

const config = normalizeConfig({
  mqtt: { url: "mqtt://localhost" },
  identity: { gatewayId: "turq", agentId: "main" },
});

test("reducer accumulates llm metrics and tool state", () => {
  const llmStart = reduceSessionEvent(undefined, {
    domain: "llm",
    event: "start",
    sessionKey: "sess-1",
    runId: "run-1",
    data: { model: "claude-sonnet-4" },
    ts: 1700000000,
  }, {
    identityFallback: config.identity,
    nowMs: 1700000000000,
    summaryMaxLength: config.events.summaryMaxLength,
  });

  const llmEnd = reduceSessionEvent(llmStart, {
    domain: "llm",
    event: "end",
    sessionKey: "sess-1",
    runId: "run-1",
    data: {
      usage: { inputTokens: 100, outputTokens: 50, totalTokens: 150, cacheTokens: 20 },
      context: { used: 12000, max: 200000 },
      compactionCount: 1,
    },
    ts: 1700000001,
  }, {
    identityFallback: config.identity,
    nowMs: 1700000001000,
    summaryMaxLength: config.events.summaryMaxLength,
  });

  const toolState = reduceSessionEvent(llmEnd, {
    domain: "tool",
    event: "start",
    sessionKey: "sess-1",
    data: { toolName: "bash" },
    ts: 1700000002,
  }, {
    identityFallback: config.identity,
    nowMs: 1700000002000,
    summaryMaxLength: config.events.summaryMaxLength,
  });

  assert.equal(toolState.model, "claude-sonnet-4");
  assert.equal(toolState.tokens.total, 150);
  assert.equal(toolState.tokens.cache, 20);
  assert.equal(toolState.context.used, 12000);
  assert.equal(toolState.compactions.count, 1);
  assert.equal(toolState.status, "tool");
  assert.equal(toolState.tool.name, "bash");
});

test("presence payload includes enriched live state", () => {
  const state = reduceSessionEvent(undefined, {
    domain: "run",
    event: "started",
    sessionKey: "sess-2",
    data: {
      task: {
        summary: "Debugging mqtt auth",
        cmdline: "npm test",
      },
    },
    ts: 1700000000,
  }, {
    identityFallback: config.identity,
    nowMs: 1700000000000,
    summaryMaxLength: config.events.summaryMaxLength,
  });

  const payload = buildPresencePayload(state, config);
  assert.equal(payload.v, 2);
  assert.equal(payload.kind, "session_presence");
  assert.equal(payload.gw, "turq");
  assert.equal(payload.agent, "main");
  assert.equal(payload.session, "sess-2");
  assert.equal(payload.status, "running");
  assert.equal(payload.busy, true);
  assert.equal(payload.activityType, "gw");
  assert.equal(payload.task.summary, "Debugging mqtt auth");
  assert.equal(payload.ttlSec, 300);
});

test("activityType is inferred deterministically from codex acp session identity", () => {
  const state = reduceSessionEvent(undefined, {
    domain: "run",
    event: "started",
    sessionKey: "agent:codex:acp:abc123",
    agentId: "codex",
    ts: 1700000000,
  }, {
    identityFallback: config.identity,
    nowMs: 1700000000000,
    summaryMaxLength: config.events.summaryMaxLength,
  });

  const payload = buildPresencePayload(state, config);
  assert.equal(payload.activityType, "codex");
});

test("presence coalescer flush predicate triggers on lifecycle changes", () => {
  const queued = {
    state: "active",
    status: "queued",
    busy: true,
    phase: "queued",
    lastError: "",
  };
  const running = {
    state: "active",
    status: "running",
    busy: true,
    phase: "running",
    lastError: "",
  };

  assert.equal(shouldFlushPresenceImmediately(undefined, queued), true);
  assert.equal(shouldFlushPresenceImmediately(queued, running), true);
  assert.equal(shouldFlushPresenceImmediately(running, { ...running }), false);
});
