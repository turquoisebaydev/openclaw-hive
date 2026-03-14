import {
  buildPresencePayload,
  mergeSessionSnapshot,
  reduceSessionEvent,
  shouldFlushPresenceImmediately,
} from "./reducer.js";
import { extractIdentity } from "./extract.js";
import { sanitizeEvent, sanitizePresencePayload } from "./sanitizer.js";
import { readRuntimeSessionSnapshots, resolveSnapshotRefreshMs } from "./snapshot.js";
import { sessionKey } from "./topic.js";

function noop() {}

function subscribeEmitter(source, eventNames, handler) {
  if (!source || typeof source.on !== "function") {
    return undefined;
  }

  for (const eventName of eventNames) {
    source.on(eventName, handler);
  }

  return () => {
    for (const eventName of eventNames) {
      source.off?.(eventName, handler);
      source.removeListener?.(eventName, handler);
    }
  };
}

function subscribeObservabilityEvents(source, handler) {
  if (!source || typeof source.onObservabilityEvent !== "function") {
    return undefined;
  }

  return source.onObservabilityEvent(handler);
}

function subscribeObservable(source, handler) {
  if (!source || typeof source.subscribe !== "function") {
    return undefined;
  }

  const result = source.subscribe(handler);
  if (typeof result === "function") {
    return result;
  }
  if (result && typeof result.unsubscribe === "function") {
    return () => result.unsubscribe();
  }
  return undefined;
}

function normalizeAgentEvent(event) {
  if (!event || typeof event !== "object") {
    return undefined;
  }

  const data = event.data && typeof event.data === "object" ? event.data : {};
  const phase = typeof data.phase === "string" ? data.phase.toLowerCase() : "";
  const base = {
    runId: typeof event.runId === "string" ? event.runId : undefined,
    sessionKey: typeof event.sessionKey === "string" ? event.sessionKey : undefined,
    ts: typeof event.ts === "number" ? event.ts : undefined,
  };

  if (event.stream === "lifecycle") {
    return {
      ...base,
      domain: "run",
      event: phase === "start" ? "started" : phase === "end" ? "completed" : phase || "update",
      data,
    };
  }

  if (event.stream === "tool") {
    return {
      ...base,
      domain: "tool",
      event: phase || "update",
      data: {
        ...data,
        toolName: typeof data.name === "string" ? data.name : data.toolName,
      },
    };
  }

  if (event.stream === "thinking" || event.stream === "assistant") {
    return {
      ...base,
      domain: "llm",
      event: event.stream,
      phase: phase || "streaming",
      data,
    };
  }

  if (event.stream === "error") {
    return {
      ...base,
      domain: "run",
      event: "error",
      data,
    };
  }

  return undefined;
}

function subscribeAgentEvents(source, handler) {
  if (!source || typeof source.onAgentEvent !== "function") {
    return undefined;
  }

  return source.onAgentEvent((event) => {
    const normalized = normalizeAgentEvent(event);
    if (normalized) {
      handler(normalized);
    }
  });
}

export function subscribeToRuntimeEvents(api, handler) {
  const candidates = [
    subscribeAgentEvents(api.runtime?.events, handler),
    subscribeObservabilityEvents(api.runtime?.observability, handler),
    subscribeObservabilityEvents(api.observability, handler),
    subscribeObservable(api.runtime?.observability, handler),
    subscribeObservable(api.runtime?.events, handler),
    subscribeEmitter(api.runtime?.observability, ["event", "observability", "runtime_event"], handler),
    subscribeEmitter(api.runtime?.events, ["event", "runtime_event", "observability"], handler),
    subscribeEmitter(api.events, ["runtime_event", "observability"], handler),
  ].filter(Boolean);

  if (candidates.length === 0) {
    return undefined;
  }

  return () => {
    for (const unsubscribe of candidates) {
      unsubscribe();
    }
  };
}

export function createPresenceCoalescer({ publishPresence, debounceMs, maxDelayMs, schedule, cancel, now }) {
  const pending = new Map();

  async function flush(key) {
    const entry = pending.get(key);
    if (!entry) {
      return;
    }
    pending.delete(key);
    if (entry.timer) {
      cancel(entry.timer);
    }
    await publishPresence(entry.identity, entry.payload);
  }

  function queue(identity, payload, immediate = false) {
    const key = `${identity.gatewayId}/${identity.agentId}/${identity.sessionId}`;
    const queuedAt = now();
    const existing = pending.get(key);
    const firstQueuedAt = existing?.firstQueuedAt ?? queuedAt;
    const state = {
      identity,
      payload,
      firstQueuedAt,
      timer: existing?.timer,
    };
    pending.set(key, state);

    if (immediate || queuedAt - firstQueuedAt >= maxDelayMs) {
      return flush(key);
    }

    if (state.timer) {
      cancel(state.timer);
    }
    state.timer = schedule(() => {
      void flush(key);
    }, debounceMs);
    pending.set(key, state);
    return Promise.resolve();
  }

  return {
    queue,
    async flushAll() {
      await Promise.all([...pending.keys()].map((key) => flush(key)));
    },
    stop() {
      for (const entry of pending.values()) {
        if (entry.timer) {
          cancel(entry.timer);
        }
      }
      pending.clear();
    },
  };
}

export function createBridgeService({
  api,
  config,
  logger,
  publisher,
  now = () => Date.now(),
  readSnapshots = (params) => readRuntimeSessionSnapshots(params),
}) {
  const log = logger ?? console;
  const sessions = new Map();
  const outputsEnabled = config.events.enabled || config.presence.enabled;
  const snapshotRefreshMs = config.presence.enabled ? resolveSnapshotRefreshMs(config) : 0;
  const coalescer = createPresenceCoalescer({
    publishPresence: (identity, payload) => publisher.publishPresence(identity, payload),
    debounceMs: config.presence.debounceMs,
    maxDelayMs: config.presence.maxDelayMs,
    schedule: (fn, delayMs) => setTimeout(fn, delayMs),
    cancel: (timer) => clearTimeout(timer),
    now,
  });

  let unsubscribe = noop;
  let started = false;
  let connected = false;
  let snapshotTimer = undefined;

  async function publishSessionState(previous, sessionState, immediate = false) {
    const presencePayload = sanitizePresencePayload(buildPresencePayload(sessionState, config), config);
    await coalescer.queue(
      sessionState.identity,
      presencePayload,
      immediate || shouldFlushPresenceImmediately(previous, sessionState),
    );
  }

  async function handleEvent(event) {
    const identity = extractIdentity(event, config.identity);
    if (!identity.sessionId) {
      return;
    }

    const previous = sessions.get(sessionKey(identity));
    const sessionState = reduceSessionEvent(previous, event, {
      identityFallback: config.identity,
      nowMs: now(),
      summaryMaxLength: config.events.summaryMaxLength,
    });
    if (!sessionState) {
      return;
    }

    sessions.set(sessionState.key, sessionState);

    if (config.events.enabled) {
      const eventPayload = sanitizeEvent(event, config, config.identity, now());
      if (eventPayload) {
        await publisher.publishEvent(sessionState.identity, eventPayload);
      }
    }

    if (config.presence.enabled) {
      await publishSessionState(previous, sessionState);
    }
  }

  async function refreshSnapshots() {
    if (!config.presence.enabled) {
      return;
    }

    const snapshotNow = now();
    const snapshots = await readSnapshots({
      identityFallback: config.identity,
      nowMs: snapshotNow,
      logger: log,
    });
    const seenKeys = new Set();

    for (const snapshot of snapshots) {
      const key = sessionKey(snapshot.identity);
      seenKeys.add(key);
      const previous = sessions.get(key);
      const sessionState = mergeSessionSnapshot(previous, snapshot);
      if (!sessionState) {
        continue;
      }
      sessions.set(key, sessionState);
      await publishSessionState(previous, sessionState, true);
    }

    for (const [key, value] of sessions.entries()) {
      if (value.identity.gatewayId === config.identity.gatewayId && !seenKeys.has(key)) {
        sessions.delete(key);
      }
    }
  }

  return {
    async start() {
      if (started) {
        return;
      }

      if (!outputsEnabled) {
        started = true;
        log.info?.("hive-bridge: bridge outputs disabled");
        return;
      }

      const stop = subscribeToRuntimeEvents(api, (event) => {
        void handleEvent(event).catch((error) => {
          log.error?.("hive-bridge: failed to process runtime event", error);
        });
      });

      const snapshotEnabled =
        config.presence.enabled &&
        typeof readSnapshots === "function" &&
        snapshotRefreshMs > 0;
      if (!stop && !snapshotEnabled) {
        log.warn?.("hive-bridge: no runtime observability or snapshot source found");
        unsubscribe = noop;
        started = true;
        return;
      }

      unsubscribe = stop ?? noop;
      await publisher.connect();
      connected = true;

      if (snapshotEnabled) {
        await refreshSnapshots().catch((error) => {
          log.warn?.("hive-bridge: initial snapshot refresh failed", error);
        });
        if (snapshotRefreshMs > 0) {
          snapshotTimer = setInterval(() => {
            void refreshSnapshots().catch((error) => {
              log.warn?.("hive-bridge: snapshot refresh failed", error);
            });
          }, snapshotRefreshMs);
        }
      }

      started = true;
      log.info?.("hive-bridge: bridge service started");
    },
    async stop() {
      if (!started) {
        return;
      }
      unsubscribe();
      if (snapshotTimer) {
        clearInterval(snapshotTimer);
        snapshotTimer = undefined;
      }
      await coalescer.flushAll();
      coalescer.stop();
      if (connected) {
        await publisher.disconnect();
        connected = false;
      }
      started = false;
      log.info?.("hive-bridge: bridge service stopped");
    },
  };
}
