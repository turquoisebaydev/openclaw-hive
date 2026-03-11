import {
  buildPresencePayload,
  reduceSessionEvent,
  shouldFlushPresenceImmediately,
} from "./reducer.js";
import { extractIdentity } from "./extract.js";
import { sanitizeEvent, sanitizePresencePayload } from "./sanitizer.js";
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

export function subscribeToRuntimeEvents(api, handler) {
  const candidates = [
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

export function createBridgeService({ api, config, logger, publisher, now = () => Date.now() }) {
  const log = logger ?? console;
  const sessions = new Map();
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
      const presencePayload = sanitizePresencePayload(
        buildPresencePayload(sessionState, config),
        config,
      );
      await coalescer.queue(
        sessionState.identity,
        presencePayload,
        shouldFlushPresenceImmediately(previous, sessionState),
      );
    }
  }

  return {
    async start() {
      if (started) {
        return;
      }
      await publisher.connect();
      const stop = subscribeToRuntimeEvents(api, (event) => {
        void handleEvent(event).catch((error) => {
          log.error?.("hive-bridge: failed to process runtime event", error);
        });
      });
      if (!stop) {
        log.warn?.("hive-bridge: no runtime observability source found");
        unsubscribe = noop;
      } else {
        unsubscribe = stop;
      }
      started = true;
      log.info?.("hive-bridge: bridge service started");
    },
    async stop() {
      if (!started) {
        return;
      }
      unsubscribe();
      await coalescer.flushAll();
      coalescer.stop();
      await publisher.disconnect();
      started = false;
      log.info?.("hive-bridge: bridge service stopped");
    },
  };
}
