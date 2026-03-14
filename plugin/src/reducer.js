import {
  extractActivityType,
  extractChannel,
  extractCompactionCount,
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
  extractThinking,
  extractTimestampMs,
  extractTokenUsage,
  extractTool,
} from "./extract.js";
import { PRESENCE_VERSION } from "./constants.js";
import { sessionKey } from "./topic.js";

function createSessionState(identity, updatedMs) {
  return {
    key: sessionKey(identity),
    identity,
    state: "active",
    status: "idle",
    busy: false,
    phase: "idle",
    activityType: "gw",
    runId: undefined,
    model: "unknown",
    thinking: "unknown",
    lastError: "",
    updatedMs,
    tokens: {
      input: 0,
      output: 0,
      total: 0,
      cache: 0,
    },
    context: {
      used: undefined,
      max: undefined,
    },
    compactions: {
      count: 0,
      last: 0,
    },
    tool: {
      name: "",
      phase: "idle",
      state: "idle",
    },
    queue: {
      lane: "",
      position: null,
    },
    channel: undefined,
    task: undefined,
  };
}

function isOneOf(value, candidates) {
  return candidates.includes(value);
}

function applyLifecycle(next, domain, eventName, phase, hasError) {
  if (domain === "session") {
    if (isOneOf(eventName, ["closed", "ended", "stopped"])) {
      next.state = "closed";
      next.status = "idle";
      next.busy = false;
      next.phase = "idle";
      return;
    }
    if (isOneOf(eventName, ["stuck"])) {
      next.status = "stuck";
      next.busy = true;
      next.phase = "stuck";
      return;
    }
    if (hasError || isOneOf(eventName, ["error", "failed"])) {
      next.status = "error";
      next.busy = false;
      next.phase = "error";
      return;
    }
    next.state = "active";
    next.status = next.busy ? next.status : "idle";
    next.phase = next.busy ? next.phase : "idle";
    return;
  }

  if (domain === "run") {
    if (isOneOf(eventName, ["queued", "enqueue"])) {
      next.status = "queued";
      next.busy = true;
      next.phase = "queued";
      return;
    }
    if (isOneOf(eventName, ["started", "running", "resumed"])) {
      next.status = "running";
      next.busy = true;
      next.phase = "running";
      return;
    }
    if (isOneOf(eventName, ["completed", "finished", "succeeded", "done", "cancelled"])) {
      next.status = "idle";
      next.busy = false;
      next.phase = "idle";
      next.tool = { name: "", phase: "idle", state: "idle" };
      return;
    }
    if (hasError || isOneOf(eventName, ["error", "failed"])) {
      next.status = "error";
      next.busy = false;
      next.phase = "error";
    }
    return;
  }

  if (domain === "llm") {
    if (hasError || isOneOf(eventName, ["error", "failed"])) {
      next.status = "error";
      next.busy = false;
      next.phase = "error";
      return;
    }
    if (isOneOf(eventName, ["start", "started", "begin"])) {
      next.status = "llm";
      next.busy = true;
      next.phase = "llm";
      return;
    }
    if (isOneOf(eventName, ["end", "ended", "completed", "finished", "succeeded"])) {
      next.status = "running";
      next.busy = true;
      next.phase = "running";
      return;
    }
    next.status = "llm";
    next.busy = true;
    next.phase = phase || "llm";
    return;
  }

  if (domain === "tool") {
    if (hasError || isOneOf(eventName, ["error", "failed"])) {
      next.status = "error";
      next.busy = false;
      next.phase = "error";
      next.tool.state = "error";
      return;
    }
    if (isOneOf(eventName, ["end", "ended", "completed", "finished", "succeeded", "result"])) {
      next.status = "running";
      next.busy = true;
      next.phase = "running";
      next.tool.state = "idle";
      next.tool.phase = "done";
      return;
    }
    next.status = "tool";
    next.busy = true;
    next.phase = "tool";
    next.tool.state = "active";
    next.tool.phase = phase || eventName || "running";
    return;
  }

  if (domain === "queue") {
    if (isOneOf(eventName, ["enqueue", "queued"])) {
      next.status = "queued";
      next.busy = true;
      next.phase = "queued";
      return;
    }
    if (isOneOf(eventName, ["dequeue", "drain", "released"])) {
      next.status = "running";
      next.busy = true;
      next.phase = "running";
      return;
    }
  }
}

function mergeTask(previousTask, nextTask, domain) {
  if (!previousTask) {
    return nextTask;
  }
  const merged = {
    ...previousTask,
    ...nextTask,
  };
  if (domain === "llm" && previousTask.cmdline && !nextTask.cmdline && !nextTask.url) {
    merged.summary = previousTask.summary;
    merged.cmdline = previousTask.cmdline;
    if (previousTask.url && !nextTask.url) {
      merged.url = previousTask.url;
    }
  }
  return merged;
}

function normalizeSnapshotStatus(status) {
  const normalized = String(status ?? "idle").trim().toLowerCase() || "idle";
  if (["executing", "active", "busy"].includes(normalized)) {
    return "running";
  }
  if (["waiting"].includes(normalized)) {
    return "queued";
  }
  return normalized;
}

function applySnapshotLifecycle(next, status, state) {
  const normalizedStatus = normalizeSnapshotStatus(status);
  const normalizedState = String(state ?? "active").trim().toLowerCase() || "active";

  next.state = normalizedState;
  next.status = normalizedStatus;

  if (normalizedState === "closed") {
    next.busy = false;
    next.phase = "idle";
    return;
  }

  if (normalizedStatus === "error") {
    next.busy = false;
    next.phase = "error";
    return;
  }

  if (normalizedStatus === "stuck") {
    next.busy = true;
    next.phase = "stuck";
    return;
  }

  if (normalizedStatus === "queued") {
    next.busy = true;
    next.phase = "queued";
    return;
  }

  if (["llm", "tool", "running"].includes(normalizedStatus)) {
    next.busy = true;
    next.phase = normalizedStatus === "running" ? "running" : normalizedStatus;
    return;
  }

  next.busy = false;
  next.phase = "idle";
}

export function reduceSessionEvent(previous, event, { identityFallback, nowMs, summaryMaxLength }) {
  const identity = extractIdentity(event, identityFallback);
  if (!identity.sessionId) {
    return undefined;
  }

  const updatedMs = extractTimestampMs(event, nowMs);
  const next = {
    ...(previous ?? createSessionState(identity, updatedMs)),
    identity,
    key: sessionKey(identity),
    updatedMs,
  };

  const domain = extractDomain(event);
  const eventName = extractEventName(event);
  const phase = extractPhase(event);
  const errorSummary = extractErrorSummary(event);
  const activityType = extractActivityType(event, identity);
  const runId = extractRunId(event);
  const model = extractModel(event);
  const thinking = extractThinking(event);
  const tool = extractTool(event);
  const usage = extractTokenUsage(event);
  const context = extractContext(event);
  const compactionCount = extractCompactionCount(event);
  const queue = extractQueue(event);
  const channel = extractChannel(event);
  const task = extractTaskSummary(event, summaryMaxLength);

  if (runId) {
    next.runId = runId;
  }
  if (activityType) {
    next.activityType = activityType;
  }
  if (model) {
    next.model = model;
  }
  if (thinking) {
    next.thinking = thinking;
  }
  if (tool) {
    next.tool = {
      ...next.tool,
      ...tool,
    };
  }
  if (usage && (domain === "llm" || domain === "run")) {
    next.tokens = {
      input: next.tokens.input + usage.input,
      output: next.tokens.output + usage.output,
      total: next.tokens.total + usage.total,
      cache: next.tokens.cache + usage.cache,
    };
  }
  if (context) {
    next.context = {
      used: context.used ?? next.context.used,
      max: context.max ?? next.context.max,
    };
  }
  if (compactionCount !== undefined) {
    next.compactions = {
      count: next.compactions.count + compactionCount,
      last: compactionCount,
    };
  }
  if (queue) {
    next.queue = queue;
  }
  if (channel) {
    next.channel = channel;
  }
  if (task) {
    next.task = mergeTask(previous?.task, task, domain);
  }
  if (errorSummary) {
    next.lastError = errorSummary;
  }

  applyLifecycle(next, domain, eventName, phase, Boolean(errorSummary));
  return next;
}

export function mergeSessionSnapshot(previous, snapshot) {
  if (!snapshot?.identity?.sessionId) {
    return previous;
  }

  const next = {
    ...(previous ?? createSessionState(snapshot.identity, snapshot.updatedMs ?? Date.now())),
    identity: snapshot.identity,
    key: sessionKey(snapshot.identity),
    updatedMs: snapshot.updatedMs ?? previous?.updatedMs ?? Date.now(),
  };

  if (snapshot.activityType) {
    next.activityType = snapshot.activityType;
  }
  if (snapshot.runId) {
    next.runId = snapshot.runId;
  }
  if (snapshot.model) {
    next.model = snapshot.model;
  }
  if (snapshot.thinking) {
    next.thinking = snapshot.thinking;
  }
  if (snapshot.tokens) {
    next.tokens = {
      input: snapshot.tokens.input ?? next.tokens.input,
      output: snapshot.tokens.output ?? next.tokens.output,
      total: snapshot.tokens.total ?? next.tokens.total,
      cache: snapshot.tokens.cache ?? next.tokens.cache,
    };
  }
  if (snapshot.context) {
    next.context = {
      used: snapshot.context.used ?? next.context.used,
      max: snapshot.context.max ?? next.context.max,
    };
  }
  if (snapshot.channel) {
    next.channel = snapshot.channel;
  }
  if (snapshot.task) {
    next.task = snapshot.task;
  }
  if (snapshot.lastError) {
    next.lastError = snapshot.lastError;
  }

  applySnapshotLifecycle(next, snapshot.status, snapshot.state);
  return next;
}

export function buildPresencePayload(sessionState, config) {
  const payload = {
    v: PRESENCE_VERSION,
    kind: "session_presence",
    gw: sessionState.identity.gatewayId,
    agent: sessionState.identity.agentId,
    session: sessionState.identity.sessionId,
    state: sessionState.state,
    status: sessionState.status,
    busy: sessionState.busy,
    phase: sessionState.phase,
    activityType: sessionState.activityType,
    model: sessionState.model,
    thinking: sessionState.thinking,
    tokens: {
      input: sessionState.tokens.input,
      output: sessionState.tokens.output,
      total: sessionState.tokens.total,
      cache: sessionState.tokens.cache,
    },
    context: {
      used: sessionState.context.used ?? "unknown",
      max: sessionState.context.max ?? "unknown",
      tokens: sessionState.context.used ?? "unknown",
      window: sessionState.context.max ?? "unknown",
    },
    compactions: {
      count: sessionState.compactions.count,
      last: sessionState.compactions.last,
    },
    updatedTs: Math.floor(sessionState.updatedMs / 1000),
    ttlSec: config.presence.ttlSec,
  };

  if (sessionState.runId) {
    payload.runId = sessionState.runId;
  }
  if (sessionState.tool.name) {
    payload.tool = {
      name: sessionState.tool.name,
      phase: sessionState.tool.phase,
      state: sessionState.tool.state,
    };
  }
  if (sessionState.queue.lane || sessionState.queue.position !== null) {
    payload.queue = {
      lane: sessionState.queue.lane,
      position: sessionState.queue.position,
    };
  }
  if (sessionState.lastError) {
    payload.lastError = sessionState.lastError;
  }
  if (sessionState.channel) {
    payload.channel = sessionState.channel;
  }
  if (config.presence.publishTaskDetails && sessionState.task) {
    payload.task = Object.fromEntries(
      Object.entries(sessionState.task).filter(([, value]) => Boolean(value)),
    );
  }

  return payload;
}

function taskChanged(previous, next) {
  const prevTask = previous?.task ?? {};
  const nextTask = next?.task ?? {};
  return (
    prevTask.summary !== nextTask.summary ||
    prevTask.activity !== nextTask.activity ||
    prevTask.cwd !== nextTask.cwd ||
    prevTask.cmdline !== nextTask.cmdline ||
    prevTask.url !== nextTask.url
  );
}

function toolChanged(previous, next) {
  const prevTool = previous?.tool ?? {};
  const nextTool = next?.tool ?? {};
  return (
    prevTool.name !== nextTool.name ||
    prevTool.phase !== nextTool.phase ||
    prevTool.state !== nextTool.state
  );
}

export function shouldFlushPresenceImmediately(previous, next) {
  if (!previous) {
    return true;
  }

  return (
    previous.state !== next.state ||
    previous.status !== next.status ||
    previous.busy !== next.busy ||
    previous.phase !== next.phase ||
    previous.lastError !== next.lastError ||
    taskChanged(previous, next) ||
    toolChanged(previous, next)
  );
}
