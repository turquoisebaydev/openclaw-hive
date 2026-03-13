import { execFile as execFileCallback } from "node:child_process";
import { promisify } from "node:util";

import { extractActivityType } from "./extract.js";

const execFile = promisify(execFileCallback);

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

function parseChannelFromSessionKey(sessionKey) {
  const parts = normalizeString(sessionKey).split(":");
  if (parts.length >= 5 && parts[0] === "agent") {
    return {
      provider: parts[2] ?? "",
      chatType: parts[3] ?? "",
      chatId: parts.slice(4).join(":"),
    };
  }
  return undefined;
}

function extractSessionList(raw) {
  if (Array.isArray(raw)) {
    return raw;
  }
  if (raw && typeof raw === "object" && Array.isArray(raw.sessions)) {
    return raw.sessions;
  }
  return [];
}

function parseJsonDocument(stdout) {
  const text = String(stdout ?? "").trim();
  if (!text) {
    return [];
  }

  const candidates = [text];
  const arrayIndex = text.indexOf("[");
  if (arrayIndex >= 0) {
    candidates.push(text.slice(arrayIndex));
  }
  const objectIndex = text.indexOf("{");
  if (objectIndex >= 0) {
    candidates.push(text.slice(objectIndex));
  }

  for (const candidate of [...new Set(candidates)]) {
    try {
      return JSON.parse(candidate);
    } catch {
      // Try the next slice.
    }
  }

  throw new Error("could not parse sessions JSON output");
}

export function resolveOpenClawCommand() {
  const nodeExec = normalizeString(process.execPath);
  const entry = normalizeString(process.argv?.[1]);
  if (nodeExec && entry) {
    return [nodeExec, entry, "sessions", "--all-agents", "--json"];
  }
  return ["openclaw", "sessions", "--all-agents", "--json"];
}

export function resolveSnapshotRefreshMs(config) {
  const refreshSec = normalizeNumber(config?.presence?.snapshotRefreshSec) ?? 0;
  if (refreshSec <= 0) {
    return 0;
  }
  const ttlSec = Math.max(normalizeNumber(config?.presence?.ttlSec) ?? 300, 30);
  return Math.min(refreshSec * 1000, Math.max(1000, Math.floor((ttlSec * 1000) / 2)));
}

export function normalizeSessionSnapshot(rawSession, identityFallback, nowMs = Date.now()) {
  const sessionId = normalizeString(
    rawSession?.key ?? rawSession?.sessionKey ?? rawSession?.sessionId ?? rawSession?.id,
  );
  if (!sessionId) {
    return undefined;
  }

  const identity = {
    gatewayId: normalizeString(rawSession?.gw ?? rawSession?.gatewayId, identityFallback.gatewayId),
    agentId: normalizeString(rawSession?.agent ?? rawSession?.agentId, identityFallback.agentId),
    sessionId,
  };

  const updatedMs =
    normalizeNumber(rawSession?.updatedAt) ??
    normalizeNumber(rawSession?.updatedTs) ??
    normalizeNumber(rawSession?.lastUpdatedAt) ??
    nowMs;

  const input = normalizeNumber(rawSession?.inputTokens);
  const output = normalizeNumber(rawSession?.outputTokens);
  const total = normalizeNumber(rawSession?.totalTokens);
  const cache = normalizeNumber(rawSession?.cacheTokens);
  const contextUsed =
    normalizeNumber(rawSession?.contextTokens) ?? normalizeNumber(rawSession?.context_tokens);
  const contextMax =
    normalizeNumber(rawSession?.contextWindow) ?? normalizeNumber(rawSession?.context_window);

  const channel =
    (rawSession?.channel && typeof rawSession.channel === "object" && {
      provider: normalizeString(rawSession.channel.provider),
      chatType: normalizeString(rawSession.channel.chatType),
      chatId: normalizeString(rawSession.channel.chatId),
    }) || parseChannelFromSessionKey(sessionId);

  const task = {
    summary: normalizeString(rawSession?.title ?? rawSession?.summary),
    activity: normalizeString(rawSession?.activity ?? rawSession?.lastStatus ?? rawSession?.kind),
    cwd: normalizeString(rawSession?.cwd),
    cmdline: normalizeString(rawSession?.cmdline),
    url: normalizeString(rawSession?.url),
  };

  const activityType =
    extractActivityType(
      {
        activityType: rawSession?.activityType,
        agentId: identity.agentId,
        sessionKey: sessionId,
      },
      identity,
    ) ?? "gw";

  return {
    identity,
    updatedMs,
    state: normalizeString(rawSession?.state, "active").toLowerCase(),
    status: normalizeString(rawSession?.status, "idle").toLowerCase(),
    model: normalizeString(rawSession?.model, "unknown"),
    thinking: normalizeString(rawSession?.thinkingLevel ?? rawSession?.thinking, "unknown").toLowerCase(),
    runId: normalizeString(rawSession?.runId) || undefined,
    activityType,
    tokens: {
      input: input ?? 0,
      output: output ?? 0,
      total: total ?? ((input ?? 0) + (output ?? 0)),
      cache: cache ?? 0,
    },
    context: {
      used: contextUsed,
      max: contextMax,
    },
    channel:
      channel && (channel.provider || channel.chatType || channel.chatId)
        ? channel
        : undefined,
    task: Object.values(task).some(Boolean) ? task : undefined,
  };
}

export async function readRuntimeSessionSnapshots({
  command = resolveOpenClawCommand(),
  env = process.env,
  identityFallback,
  nowMs = Date.now(),
  exec = execFile,
  logger,
}) {
  const [file, ...args] = command;
  const { stdout, stderr } = await exec(file, args, {
    env,
    timeout: 10000,
    maxBuffer: 2 * 1024 * 1024,
  });

  if (stderr?.trim()) {
    logger?.debug?.("hive-bridge: sessions snapshot stderr", stderr.trim());
  }

  const parsed = parseJsonDocument(stdout);
  return extractSessionList(parsed)
    .map((session) => normalizeSessionSnapshot(session, identityFallback, nowMs))
    .filter(Boolean);
}
