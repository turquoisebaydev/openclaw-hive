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
  if (value === undefined || value === null || value === "") {
    return undefined;
  }
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
  ).toLowerCase();

  if (agentId.includes("codex") || sessionId.includes(":codex:") || sessionId.startsWith("codex:")) {
    return "codex";
  }
  if (agentId.includes("claude") || sessionId.includes(":claude:") || sessionId.startsWith("claude:")) {
    return "claude";
  }
  if (agentId.includes("acp") || sessionId.includes(":acp:") || sessionId.startsWith("acp:")) {
    return "acp";
  }
  if (
    agentId.includes("local") ||
    sessionId.includes(":local-llm:") ||
    sessionId.includes(":ollama:")
  ) {
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
  const model = pickFirst(event, [
    "model",
    "modelId",
    "data.model",
    "data.modelId",
    "meta.model",
    "data.modelName",
  ]);
  return model ? normalizeString(model) : undefined;
}

export function extractThinking(event) {
  const thinking = pickFirst(event, [
    "thinking",
    "thinkingLevel",
    "data.thinking",
    "data.thinkingLevel",
    "meta.thinking",
  ]);
  return thinking ? normalizeString(thinking).toLowerCase() : undefined;
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
  const usage = pickFirst(event, ["usage", "data.usage", "data.tokens"]) ?? event;
  const input = normalizeNumber(
    pickFirst(usage ?? {}, [
      "input",
      "inputTokens",
      "promptTokens",
      "prompt",
      "in",
      "data.inputTokens",
    ]),
  );
  const output = normalizeNumber(
    pickFirst(usage ?? {}, [
      "output",
      "outputTokens",
      "completionTokens",
      "completion",
      "out",
      "data.outputTokens",
    ]),
  );
  const total = normalizeNumber(pickFirst(usage ?? {}, ["total", "totalTokens", "data.totalTokens"]));
  const cache = normalizeNumber(
    pickFirst(usage ?? {}, ["cache", "cacheTokens", "cachedTokens", "data.cacheTokens"]),
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
  const used = normalizeNumber(
    pickFirst(context ?? event ?? {}, [
      "used",
      "tokens",
      "current",
      "contextUsed",
      "contextTokens",
      "context_tokens",
      "data.contextTokens",
      "data.context_tokens",
    ]),
  );
  const max = normalizeNumber(
    pickFirst(context ?? event ?? {}, [
      "max",
      "window",
      "limit",
      "contextWindow",
      "context_window",
      "data.contextWindow",
      "data.context_window",
    ]),
  );
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

function truncateTailString(value, maxLength) {
  if (value.length <= maxLength) {
    return value;
  }
  return `…${value.slice(-Math.max(maxLength - 1, 1))}`;
}

function normalizeTaskSummaryValue(value) {
  if (value === undefined || value === null) {
    return undefined;
  }
  const normalized = normalizeString(value);
  if (!normalized) {
    return undefined;
  }
  const lowered = normalized.toLowerCase();
  if (lowered === "undefined" || lowered === "null") {
    return undefined;
  }
  return normalized;
}

// Matches one or more leading KEY=VALUE assignments (e.g. "OPENCLAW_PROFILE=mini1 FOO=bar cmd")
const LEADING_ENV_VARS_RE = /^(?:[A-Z_][A-Z0-9_]*=[^\s]* +)+/;
const SHELL_WRAPPER_RE = /^(?:\/bin\/)?(?:bash|sh|zsh|dash|fish)\s+-[A-Za-z-]*c\s+([\s\S]+)$/i;

function stripLeadingEnvVars(value) {
  if (typeof value !== "string") {
    return value;
  }
  const stripped = value.replace(LEADING_ENV_VARS_RE, "").trim();
  return stripped || value;
}

function stripMatchingQuotes(value) {
  if (typeof value !== "string" || value.length < 2) {
    return value;
  }
  const quote = value[0];
  if ((quote !== "'" && quote !== '"') || value.at(-1) !== quote) {
    return value;
  }
  const inner = value.slice(1, -1).trim();
  return inner || value;
}

function unwrapShellWrapper(value) {
  if (typeof value !== "string") {
    return value;
  }
  const match = value.trim().match(SHELL_WRAPPER_RE);
  if (!match) {
    return value;
  }
  const inner = stripMatchingQuotes(match[1]?.trim());
  return inner || value;
}

function pickStructuredField(value, paths) {
  if (!value || typeof value !== "object") {
    return undefined;
  }
  return normalizeTaskSummaryValue(pickFirst(value, paths));
}

function summarizeStructuredField(value) {
  const raw = pickStructuredField(value, [
    "commandPreview",
    "displayCommand",
    "detail",
    "command",
    "cmd",
    "textPreview",
    "pathPreview",
    "preview",
    "summary",
    "text",
    "value",
    "title",
    "name",
  ]);
  // Strip leading env-var assignments from command-like fields so monitoring
  // shows "git status" rather than "OPENCLAW_PROFILE=mini1 … bash -lc git status"
  // when core hasn't generated a commandPreview.
  return raw ? unwrapShellWrapper(stripLeadingEnvVars(raw)) : undefined;
}

function extractStructuredDetail(value) {
  return (
    pickStructuredField(value, [
      "command",
      "cmd",
      "displayCommand",
      "detail",
      "commandPreview",
      "textPreview",
      "pathPreview",
      "preview",
      "summary",
      "text",
      "value",
      "title",
      "name",
    ]) ?? summarizeStructuredField(value)
  );
}

export function extractTaskSummary(event, maxLength = 160) {
  const domain = normalizeTaskSummaryValue(pickFirst(event, ["domain", "type"]))?.toLowerCase();
  const task = pickFirst(event, ["task", "data.task"]);
  const toolName = normalizeTaskSummaryValue(
    pickFirst(event, ["data.toolName", "toolName", "data.name", "name"]),
  );
  const toolMeta = normalizeTaskSummaryValue(pickFirst(event, ["data.meta", "meta"]));
  const argsSummary = pickFirst(event, ["data.argsSummary"]);
  const resultSummary = pickFirst(event, ["data.resultSummary"]);
  const derivedToolSummary =
    summarizeStructuredField(argsSummary) ??
    summarizeStructuredField(resultSummary) ??
    (toolName && toolMeta ? `${toolName}: ${toolMeta}` : toolMeta ?? toolName);
  const explicitSummary = normalizeTaskSummaryValue(
    pickFirst(task ?? event, ["summary", "title", "data.summary", "activity"]),
  );
  const summary =
    domain === "tool"
      ? derivedToolSummary ?? explicitSummary
      : domain === "llm"
        ? derivedToolSummary ?? explicitSummary
        : explicitSummary ?? derivedToolSummary;
  const activity =
    normalizeTaskSummaryValue(pickFirst(task ?? event, ["activity", "lastStatus", "data.activity"])) ??
    (toolName ? `tool:${toolName}` : undefined);
  const cwd = normalizeTaskSummaryValue(pickFirst(task ?? event, ["cwd", "data.cwd"]));
  const cmdline =
    normalizeTaskSummaryValue(pickFirst(task ?? event, ["cmdline", "data.cmdline"])) ??
    extractStructuredDetail(argsSummary);
  const url = normalizeTaskSummaryValue(pickFirst(task ?? event, ["url", "data.url"]));

  if (!summary && !activity && !cwd && !cmdline && !url) {
    return undefined;
  }

  return {
    summary: summary ? truncateTailString(summary, maxLength) : undefined,
    activity: activity ? truncateTailString(activity, maxLength) : undefined,
    cwd: cwd ? truncateTailString(cwd, maxLength) : undefined,
    cmdline: cmdline ? truncateTailString(cmdline, maxLength) : undefined,
    url: url ? truncateTailString(url, Math.max(maxLength * 2, maxLength)) : undefined,
  };
}
