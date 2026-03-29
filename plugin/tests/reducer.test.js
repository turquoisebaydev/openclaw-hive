import test from "node:test";
import assert from "node:assert/strict";

import { normalizeConfig } from "../src/config.js";
import {
  buildPresencePayload,
  mergeSessionSnapshot,
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

test("mergeSessionSnapshot seeds model, thinking, tokens, and context", () => {
  const state = mergeSessionSnapshot(undefined, {
    identity: { gatewayId: "turq", agentId: "main", sessionId: "sess-snapshot" },
    updatedMs: 1700000000000,
    status: "idle",
    state: "active",
    model: "claude-sonnet-4-6",
    thinking: "low",
    tokens: { input: 11, output: 7, total: 18, cache: 2 },
    context: { used: 1000000, max: 2000000 },
    activityType: "gw",
  });

  assert.equal(state.model, "claude-sonnet-4-6");
  assert.equal(state.thinking, "low");
  assert.equal(state.tokens.total, 18);
  assert.equal(state.context.used, 1000000);
  assert.equal(state.busy, false);
});

test("presence payload includes enriched live state and compatibility context fields", () => {
  const state = reduceSessionEvent(undefined, {
    domain: "run",
    event: "started",
    sessionKey: "sess-2",
    data: {
      model: "gpt-5.3-codex",
      thinking: "high",
      usage: { inputTokens: 12, outputTokens: 8, totalTokens: 20, cacheTokens: 1 },
      context: { used: 4500, max: 200000 },
      channel: { provider: "discord", chatType: "channel", chatId: "123" },
      task: { summary: "debugging bridge", activity: "patching reducer" },
    },
    ts: 1700000000,
  }, {
    identityFallback: config.identity,
    nowMs: 1700000000000,
    summaryMaxLength: config.events.summaryMaxLength,
  });

  const payload = buildPresencePayload(state, config);

  assert.equal(payload.model, "gpt-5.3-codex");
  assert.equal(payload.thinking, "high");
  assert.equal(payload.tokens.total, 20);
  assert.equal(payload.context.used, 4500);
  assert.equal(payload.context.tokens, 4500);
  assert.equal(payload.context.max, 200000);
  assert.equal(payload.context.window, 200000);
  assert.equal(payload.channel.provider, "discord");
  assert.equal(payload.task.summary, "debugging bridge");
});

test("presence flushes immediately on state changes", () => {
  const previous = {
    state: "active",
    status: "idle",
    busy: false,
    phase: "idle",
    lastError: "",
  };
  const next = {
    state: "active",
    status: "running",
    busy: true,
    phase: "running",
    lastError: "",
  };

  assert.equal(shouldFlushPresenceImmediately(previous, next), true);
});


test("presence flushes immediately on task changes", () => {
  const previous = {
    state: "active",
    status: "running",
    busy: true,
    phase: "running",
    lastError: "",
    task: { summary: "old task" },
    tool: { name: "", phase: "idle", state: "idle" },
  };
  const next = {
    state: "active",
    status: "running",
    busy: true,
    phase: "running",
    lastError: "",
    task: { summary: "new task" },
    tool: { name: "", phase: "idle", state: "idle" },
  };

  assert.equal(shouldFlushPresenceImmediately(previous, next), true);
});

test("reducer derives task summary from tool observability payloads", () => {
  const state = reduceSessionEvent(undefined, {
    domain: "tool",
    event: "call",
    phase: "start",
    sessionKey: "sess-tool",
    data: {
      toolName: "exec",
      meta: "pty",
      argsSummary: { commandPreview: "run pnpm build in dashboard workspace" },
    },
  }, {
    identityFallback: config.identity,
    nowMs: 1700000000000,
    summaryMaxLength: config.events.summaryMaxLength,
  });

  assert.equal(state.task.summary, "run pnpm build in dashboard workspace");
  assert.equal(state.task.activity, "tool:exec");
});

test("reducer strips leading env-var prefixes from raw command when commandPreview is absent", () => {
  // Simulates a core that doesn't produce commandPreview — the raw command may
  // carry env-var assignments that the plugin should strip for clean monitoring display.
  const state = reduceSessionEvent(undefined, {
    domain: "tool",
    event: "call",
    phase: "start",
    sessionKey: "sess-env-strip",
    data: {
      toolName: "exec",
      argsSummary: {
        kind: "object",
        command: 'OPENCLAW_PROFILE=mini1 OPENCLAW_STATE_DIR=/tmp/state bash -lc "git status --short"',
      },
    },
  }, {
    identityFallback: config.identity,
    nowMs: 1700000000000,
    summaryMaxLength: config.events.summaryMaxLength,
  });

  // Summary should not expose internal env vars
  assert.ok(!state.task.summary.startsWith("OPENCLAW_"), "should not start with env var");
  assert.ok(state.task.summary.includes("bash"), "should include the command after env vars");
});


test("llm events preserve latest live tool summary after a tool step", () => {
  const toolState = reduceSessionEvent(undefined, {
    domain: "tool",
    event: "call",
    phase: "start",
    sessionKey: "sess-live",
    data: {
      toolName: "exec",
      argsSummary: { commandPreview: "pwd" },
      task: { summary: "Use bash to run pwd", activity: "direct", cwd: "/tmp/work" },
    },
  }, {
    identityFallback: config.identity,
    nowMs: 1700000000000,
    summaryMaxLength: config.events.summaryMaxLength,
  });

  const llmState = reduceSessionEvent(toolState, {
    domain: "llm",
    event: "assistant",
    phase: "streaming",
    sessionKey: "sess-live",
    data: {
      task: { summary: "Use bash to run pwd", activity: "direct", cwd: "/tmp/work" },
    },
  }, {
    identityFallback: config.identity,
    nowMs: 1700000001000,
    summaryMaxLength: config.events.summaryMaxLength,
  });

  assert.equal(toolState.task.summary, "pwd");
  assert.equal(toolState.task.cmdline, "pwd");
  assert.equal(llmState.task.summary, "pwd");
  assert.equal(llmState.task.cmdline, "pwd");
});


test("reducer derives live tool summary from raw tool args when observability summaries are unavailable", () => {
  const state = reduceSessionEvent(undefined, {
    domain: "tool",
    event: "start",
    sessionKey: "sess-raw-tool",
    data: {
      toolName: "exec",
      argsSummary: { command: "pwd" },
    },
  }, {
    identityFallback: config.identity,
    nowMs: 1700000000000,
    summaryMaxLength: config.events.summaryMaxLength,
  });

  assert.equal(state.task.summary, "pwd");
  assert.equal(state.task.cmdline, "pwd");
});


test("tool updates keep the latest command detail across update/result/llm events", () => {
  const startState = reduceSessionEvent(undefined, {
    domain: "tool",
    event: "start",
    sessionKey: "sess-step",
    data: {
      toolName: "exec",
      argsSummary: { command: "bash -lc 'pwd'" },
      task: { summary: "Use bash to run pwd", activity: "direct", cwd: "/tmp/work" },
    },
  }, {
    identityFallback: config.identity,
    nowMs: 1700000000000,
    summaryMaxLength: config.events.summaryMaxLength,
  });

  const updateState = reduceSessionEvent(startState, {
    domain: "tool",
    event: "update",
    sessionKey: "sess-step",
    data: {
      toolName: "exec",
      resultSummary: { name: "exec" },
      task: { summary: "Use bash to run pwd", activity: "direct", cwd: "/tmp/work" },
    },
  }, {
    identityFallback: config.identity,
    nowMs: 1700000001000,
    summaryMaxLength: config.events.summaryMaxLength,
  });

  const resultState = reduceSessionEvent(updateState, {
    domain: "tool",
    event: "result",
    sessionKey: "sess-step",
    data: {
      toolName: "exec",
      meta: "pwd",
      task: { summary: "Use bash to run pwd", activity: "direct", cwd: "/tmp/work" },
    },
  }, {
    identityFallback: config.identity,
    nowMs: 1700000001500,
    summaryMaxLength: config.events.summaryMaxLength,
  });

  const llmState = reduceSessionEvent(resultState, {
    domain: "llm",
    event: "assistant",
    phase: "streaming",
    sessionKey: "sess-step",
    data: {
      task: { summary: "Use bash to run pwd", activity: "direct", cwd: "/tmp/work" },
    },
  }, {
    identityFallback: config.identity,
    nowMs: 1700000002000,
    summaryMaxLength: config.events.summaryMaxLength,
  });

  assert.equal(startState.task.summary, "bash -lc 'pwd'");
  assert.equal(startState.task.cmdline, "bash -lc 'pwd'");
  assert.equal(updateState.task.summary, "bash -lc 'pwd'");
  assert.equal(updateState.task.cmdline, "bash -lc 'pwd'");
  assert.equal(resultState.task.summary, "exec: pwd");
  assert.equal(resultState.task.cmdline, "bash -lc 'pwd'");
  assert.equal(llmState.task.summary, "exec: pwd");
  assert.equal(llmState.task.cmdline, "bash -lc 'pwd'");
});


test("run completion keeps the latest live command detail", () => {
  const toolState = reduceSessionEvent(undefined, {
    domain: "tool",
    event: "result",
    sessionKey: "sess-complete",
    data: {
      toolName: "exec",
      meta: "pwd",
      argsSummary: { command: "bash -lc 'pwd'" },
      task: { summary: "Use bash to run pwd", activity: "direct", cwd: "/tmp/work" },
    },
  }, {
    identityFallback: config.identity,
    nowMs: 1700000000000,
    summaryMaxLength: config.events.summaryMaxLength,
  });

  const completedState = reduceSessionEvent(toolState, {
    domain: "run",
    event: "completed",
    sessionKey: "sess-complete",
    data: {
      task: { summary: "Use bash to run pwd", activity: "direct", cwd: "/tmp/work" },
    },
  }, {
    identityFallback: config.identity,
    nowMs: 1700000001000,
    summaryMaxLength: config.events.summaryMaxLength,
  });

  assert.equal(completedState.task.summary, toolState.task.summary);
  assert.equal(completedState.task.cmdline, toolState.task.cmdline);
});
