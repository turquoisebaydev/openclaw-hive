import test from "node:test";
import assert from "node:assert/strict";
import { EventEmitter } from "node:events";

import { normalizeConfig } from "../src/config.js";
import { createBridgeService, createPresenceCoalescer } from "../src/listener.js";

test("presence coalescer debounces burst updates and flushes latest payload", async () => {
  const published = [];
  const timers = [];
  let nowMs = 1000;

  const coalescer = createPresenceCoalescer({
    publishPresence: async (identity, payload) => published.push({ identity, payload }),
    debounceMs: 50,
    maxDelayMs: 200,
    now: () => nowMs,
    schedule: (fn, delay) => {
      const timer = { fn, delay, cancelled: false };
      timers.push(timer);
      return timer;
    },
    cancel: (timer) => {
      timer.cancelled = true;
    },
  });

  const identity = { gatewayId: "turq", agentId: "main", sessionId: "sess-1" };
  await coalescer.queue(identity, { status: "running" }, false);
  nowMs += 10;
  await coalescer.queue(identity, { status: "llm" }, false);

  assert.equal(published.length, 0);
  assert.equal(timers.length, 2);

  await timers.at(-1).fn();
  assert.equal(published.length, 1);
  assert.equal(published[0].payload.status, "llm");
});

test("bridge service subscribes to runtime events and publishes event + presence", async () => {
  const runtime = new EventEmitter();
  const publishedEvents = [];
  const publishedPresence = [];
  const config = normalizeConfig({
    mqtt: { url: "mqtt://localhost" },
    identity: { gatewayId: "turq", agentId: "main" },
    presence: { debounceMs: 1, maxDelayMs: 1 },
  });

  const publisher = {
    connect: async () => {},
    publishEvent: async (identity, payload) => publishedEvents.push({ identity, payload }),
    publishPresence: async (identity, payload) => publishedPresence.push({ identity, payload }),
    disconnect: async () => {},
  };
  const service = createBridgeService({
    api: { runtime: { events: runtime }, logger: console },
    config,
    logger: console,
    publisher,
    now: () => 1700000000000,
  });

  await service.start();
  runtime.emit("event", {
    domain: "llm",
    event: "end",
    sessionKey: "sess-1",
    runId: "run-1",
    data: {
      model: "claude-sonnet-4",
      usage: { inputTokens: 120, outputTokens: 80, totalTokens: 200 },
      context: { used: 4000, max: 200000 },
      error: "Bearer abcdef should be redacted",
    },
  });

  await new Promise((resolve) => setTimeout(resolve, 5));
  await service.stop();

  assert.equal(publishedEvents.length, 1);
  assert.equal(publishedEvents[0].identity.sessionId, "sess-1");
  assert.equal(publishedEvents[0].payload.domain, "llm");
  assert.match(publishedEvents[0].payload.error, /Bearer \[redacted\]/i);
  assert.equal(publishedPresence.length, 1);
  assert.equal(publishedPresence[0].payload.tokens.total, 200);
  assert.equal(publishedPresence[0].payload.context.max, 200000);
});

test("bridge service reuses prior session state across multiple events", async () => {
  const runtime = new EventEmitter();
  const publishedPresence = [];
  const config = normalizeConfig({
    mqtt: { url: "mqtt://localhost" },
    identity: { gatewayId: "turq", agentId: "main" },
    presence: { debounceMs: 1, maxDelayMs: 1 },
  });

  const publisher = {
    connect: async () => {},
    publishEvent: async () => {},
    publishPresence: async (_identity, payload) => publishedPresence.push(payload),
    disconnect: async () => {},
  };
  const service = createBridgeService({
    api: { runtime: { events: runtime }, logger: console },
    config,
    logger: console,
    publisher,
    now: () => 1700000000000,
  });

  await service.start();
  runtime.emit("event", {
    domain: "llm",
    event: "end",
    sessionKey: "sess-2",
    data: {
      usage: { inputTokens: 100, outputTokens: 25, totalTokens: 125 },
    },
  });
  runtime.emit("event", {
    domain: "tool",
    event: "start",
    sessionKey: "sess-2",
    data: { toolName: "shell" },
  });

  await new Promise((resolve) => setTimeout(resolve, 5));
  await service.stop();

  assert.equal(publishedPresence.at(-1).tokens.total, 125);
  assert.equal(publishedPresence.at(-1).tool.name, "shell");
});

test("bridge service skips MQTT connect when no runtime source exists", async () => {
  const config = normalizeConfig({
    mqtt: { url: "mqtt://localhost" },
    identity: { gatewayId: "turq", agentId: "main" },
  });

  const calls = { connect: 0, disconnect: 0 };
  const service = createBridgeService({
    api: { logger: console },
    config,
    logger: console,
    publisher: {
      connect: async () => {
        calls.connect += 1;
      },
      publishEvent: async () => {},
      publishPresence: async () => {},
      disconnect: async () => {
        calls.disconnect += 1;
      },
    },
  });

  await service.start();
  await service.stop();

  assert.deepEqual(calls, { connect: 0, disconnect: 0 });
});

test("bridge service skips connect and subscription when outputs are disabled", async () => {
  const runtime = new EventEmitter();
  const config = normalizeConfig({
    mqtt: { url: "mqtt://localhost" },
    identity: { gatewayId: "turq", agentId: "main" },
    presence: { enabled: false },
    events: { enabled: false },
  });

  const calls = { connect: 0, publishEvent: 0, publishPresence: 0, disconnect: 0 };
  const service = createBridgeService({
    api: { runtime: { events: runtime }, logger: console },
    config,
    logger: console,
    publisher: {
      connect: async () => {
        calls.connect += 1;
      },
      publishEvent: async () => {
        calls.publishEvent += 1;
      },
      publishPresence: async () => {
        calls.publishPresence += 1;
      },
      disconnect: async () => {
        calls.disconnect += 1;
      },
    },
  });

  await service.start();
  runtime.emit("event", {
    domain: "llm",
    event: "start",
    sessionKey: "sess-disabled",
  });
  await new Promise((resolve) => setTimeout(resolve, 5));
  await service.stop();

  assert.deepEqual(calls, { connect: 0, publishEvent: 0, publishPresence: 0, disconnect: 0 });
});


test("bridge service subscribes to runtime onAgentEvent hooks", async () => {
  const publishedEvents = [];
  const publishedPresence = [];
  const config = normalizeConfig({
    mqtt: { url: "mqtt://localhost" },
    identity: { gatewayId: "mini1", agentId: "main" },
    presence: { debounceMs: 1, maxDelayMs: 1 },
  });

  let onAgentEventHandler;
  const service = createBridgeService({
    api: {
      runtime: {
        events: {
          onAgentEvent(handler) {
            onAgentEventHandler = handler;
            return () => {
              onAgentEventHandler = undefined;
            };
          },
        },
      },
      logger: console,
    },
    config,
    logger: console,
    publisher: {
      connect: async () => {},
      publishEvent: async (identity, payload) => publishedEvents.push({ identity, payload }),
      publishPresence: async (identity, payload) => publishedPresence.push({ identity, payload }),
      disconnect: async () => {},
    },
    now: () => 1700000000000,
  });

  await service.start();
  onAgentEventHandler({
    runId: "run-agent-1",
    stream: "tool",
    sessionKey: "agent:main:main",
    ts: 1700000000000,
    data: {
      phase: "start",
      name: "exec",
    },
  });

  await new Promise((resolve) => setTimeout(resolve, 5));
  await service.stop();

  assert.equal(publishedEvents.length, 1);
  assert.equal(publishedEvents[0].identity.sessionId, "agent:main:main");
  assert.equal(publishedEvents[0].payload.domain, "tool");
  assert.equal(publishedEvents[0].payload.tool.name, "exec");
  assert.equal(publishedPresence.at(-1).payload.tool.name, "exec");
});
