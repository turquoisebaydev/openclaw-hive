function readPath(value, path) {
  return path.split(".").reduce((current, segment) => {
    if (current == null || typeof current !== "object") {
      return undefined;
    }
    return current[segment];
  }, value);
}

function pickFirst(value, paths) {
  for (const path of paths) {
    const result = readPath(value, path);
    if (result !== undefined && result !== null && result !== "") {
      return result;
    }
  }
  return undefined;
}

function normalizeString(value, fallback = "") {
  const normalized = String(value ?? fallback).trim();
  return normalized || fallback;
}

function normalizeNumber(value) {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric : undefined;
}

export function extractDomain(event) {
  return normalizeString(pickFirst(event, ["domain", "type"]), "runtime").toLowerCase();
}

export function extractEventName(event) {
  const explicit = pickFirst(event, ["event", "name", "action"]);
  if (explicit) {
    return normalizeString(explicit).toLowerCase();
  }
  const type = normalizeString(event?.type);
  if (type.includes(":")) {
    return type.split(":").at(-1)?.toLowerCase() ?? "update";
  }
  return "update";
}

export function extractPhase(event) {
  return normalizeString(pickFirst(event, ["phase", "status"]), "").toLowerCase();
}

export function extractIdentity(event, fallbackIdentity) {
  return {
    gatewayId: normalizeString(
      pickFirst(event, ["gw", "gatewayId", "gateway", "node"]),
      fallbackIdentity.gatewayId,
    ),
    agentId: normalizeString(
      pickFirst(event, ["agent", "agentId", "meta.agentId"]),
      fallbackIdentity.agentId,
    ),
    sessionId: normalizeString(
      pickFirst(event, [
        "session",
        "sessionId",
        "sessionKey",
        "data.session",
        "data.sessionId",
        "data.sessionKey",
        "meta.session",
        "meta.sessionId",
        "meta.sessionKey",
      ]),
    ),
  };
}

export function extractActivityType(event, identity = undefined) {
  const explicit = normalizeString(
    pickFirst(event, [
      "activityType",
      "data.activityType",
      "task.activityType",
      "data.task.activityType",
    ]),
  ).toLowerCase();
  if (explicit) {
    return explicit;
  }

  const agentId = normalizeString(
    pickFirst(event, ["agent", "agentId", "meta.agentId"]),
    identity?.agentId,
  ).toLowerCase();
  const sessionId = normalizeString(
    pickFirst(event, [
      "session",
      "sessionId",
      "sessionKey",
      "data.session",
      "data.sessionId",
      "data.sessionKey",
      "meta.session",
      "meta.sessionId",
      "meta.sessionKey",
    ]),
    identity?.sessionId,
  ).toLowerCase();

  if (agentId.includes("codex") || sessionId.includes(":codex:") || sessionId.includes("codex")) {
    return "codex";
  }
  if (agentId.includes("claude") || sessionId.includes(":claude:") || sessionId.includes("claude")) {
    return "claude";
  }
  if (sessionId.includes(":acp:")) {
    return "acp";
  }
  if (agentId.includes("local-llm")) {
    return "local-llm";
  }
  if (agentId || sessionId) {
    return "gw";
  }
  return undefined;
}

export function extractTimestampMs(event, nowMs) {
  const raw = pickFirst(event, ["ts", "timestamp", "time", "updatedTs"]);
  const numeric = normalizeNumber(raw);
  if (numeric === undefined) {
    return nowMs;
  }
  return numeric < 10_000_000_000 ? numeric * 1000 : numeric;
}

export function extractRunId(event) {
  const runId = pickFirst(event, ["runId", "data.runId", "meta.runId"]);
  return runId ? normalizeString(runId) : undefined;
}

export function extractModel(event) {
  const model = pickFirst(event, ["model", "data.model", "data.modelId", "meta.model"]);
  return model ? normalizeString(model) : undefined;
}

export function extractTool(event) {
  const toolName = pickFirst(event, ["tool", "toolName", "data.tool", "data.toolName", "data.name"]);
  if (!toolName) {
    return undefined;
  }
  return {
    name: normalizeString(toolName),
    phase: normalizeString(pickFirst(event, ["phase", "status", "data.phase"]), "running"),
  };
}

export function extractTokenUsage(event) {
  const usage = pickFirst(event, ["usage", "data.usage", "data.tokens"]);
  const input = normalizeNumber(
    pickFirst(usage ?? {}, ["input", "inputTokens", "promptTokens", "prompt", "in"]),
  );
  const output = normalizeNumber(
    pickFirst(usage ?? {}, ["output", "outputTokens", "completionTokens", "completion", "out"]),
  );
  const total = normalizeNumber(pickFirst(usage ?? {}, ["total", "totalTokens"]));
  const cache = normalizeNumber(
    pickFirst(usage ?? {}, ["cache", "cacheTokens", "cachedTokens"]),
  );

  if ([input, output, total, cache].every((value) => value === undefined)) {
    return undefined;
  }

  return {
    input: input ?? 0,
    output: output ?? 0,
    total: total ?? (input ?? 0) + (output ?? 0),
    cache: cache ?? 0,
  };
}

export function extractContext(event) {
  const context = pickFirst(event, ["context", "data.context"]);
  const used = normalizeNumber(pickFirst(context ?? {}, ["used", "tokens", "current", "contextUsed"]));
  const max = normalizeNumber(pickFirst(context ?? {}, ["max", "window", "limit", "contextWindow"]));
  if (used === undefined && max === undefined) {
    return undefined;
  }
  return { used, max };
}

export function extractCompactionCount(event) {
  const count = normalizeNumber(
    pickFirst(event, ["compactionCount", "data.compactionCount", "data.compactions.count"]),
  );
  return count;
}

export function extractErrorSummary(event) {
  const raw = pickFirst(event, ["error", "data.error", "data.message", "message"]);
  if (!raw) {
    return undefined;
  }
  return normalizeString(typeof raw === "string" ? raw : raw.message ?? raw.code ?? "error");
}

export function extractQueue(event) {
  const queue = pickFirst(event, ["queue", "data.queue"]);
  const lane = pickFirst(queue ?? {}, ["lane", "name"]);
  const position = normalizeNumber(pickFirst(queue ?? {}, ["position", "depth", "size"]));
  if (!lane && position === undefined) {
    return undefined;
  }
  return {
    lane: lane ? normalizeString(lane) : "",
    position: position ?? null,
  };
}

export function extractChannel(event) {
  const channel = pickFirst(event, ["channel", "data.channel"]);
  if (!channel || typeof channel !== "object") {
    return undefined;
  }
  const provider = normalizeString(pickFirst(channel, ["provider", "name"]));
  const chatType = normalizeString(pickFirst(channel, ["chatType", "type"]));
  const chatId = normalizeString(pickFirst(channel, ["chatId", "id"]));
  if (!provider && !chatType && !chatId) {
    return undefined;
  }
  return { provider, chatType, chatId };
}

function truncateString(value, maxLength) {
  if (value.length <= maxLength) {
    return value;
  }
  return `${value.slice(0, Math.max(maxLength - 1, 1))}…`;
}

export function extractTaskSummary(event, maxLength = 160) {
  const task = pickFirst(event, ["task", "data.task"]);
  const summary = normalizeString(pickFirst(task ?? event, ["summary", "data.summary", "activity"]));
  const activity = normalizeString(pickFirst(task ?? event, ["activity", "data.activity"]));
  const cwd = normalizeString(pickFirst(task ?? event, ["cwd", "data.cwd"]));
  const cmdline = normalizeString(pickFirst(task ?? event, ["cmdline", "data.cmdline"]));
  const url = normalizeString(pickFirst(task ?? event, ["url", "data.url"]));

  if (!summary && !activity && !cwd && !cmdline && !url) {
    return undefined;
  }

  return {
    summary: truncateString(summary, maxLength),
    activity: truncateString(activity, maxLength),
    cwd: truncateString(cwd, maxLength),
    cmdline: truncateString(cmdline, maxLength),
    url: truncateString(url, Math.max(maxLength * 2, maxLength)),
  };
}
