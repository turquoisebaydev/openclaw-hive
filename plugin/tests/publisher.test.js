import test from "node:test";
import assert from "node:assert/strict";
import { EventEmitter } from "node:events";

import { normalizeConfig } from "../src/config.js";
import { createMqttPublisher } from "../src/publisher.js";

function createFakeClient(publishes) {
  const emitter = new EventEmitter();
  emitter.connected = true;
  emitter.publish = (topic, body, options, callback) => {
    publishes.push({ topic, body: JSON.parse(body), options });
    callback();
  };
  emitter.end = (_force, _opts, callback) => callback();
  return emitter;
}

test("publisher maps presence and event topics with correct MQTT flags", async () => {
  const publishes = [];
  const config = normalizeConfig({
    mqtt: { url: "mqtt://localhost", qos: 1 },
    identity: { gatewayId: "turq", agentId: "main" },
  });
  const publisher = createMqttPublisher({
    config,
    createClient: async () => createFakeClient(publishes),
  });

  await publisher.publishPresence(
    { gatewayId: "turq", agentId: "main", sessionId: "sess/1" },
    { kind: "session_presence" },
  );
  await publisher.publishEvent(
    { gatewayId: "turq", agentId: "main", sessionId: "sess/1" },
    { kind: "runtime_event" },
  );

  assert.equal(publishes[0].topic, "turq/hive/presence/turq/main/sess%2F1");
  assert.equal(publishes[0].options.retain, true);
  assert.equal(publishes[0].options.messageExpiryInterval, 300);
  assert.equal(publishes[1].topic, "turq/hive/events/turq/main/sess%2F1");
  assert.equal(publishes[1].options.retain, false);

  await publisher.disconnect();
});
