import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";

import { normalizeConfig } from "../src/config.js";
import { configSchema } from "../src/config-schema.js";

test("manifest embeds the shared config schema", () => {
  const manifestPath = path.join(import.meta.dirname, "..", "openclaw.plugin.json");
  const manifest = JSON.parse(fs.readFileSync(manifestPath, "utf8"));
  assert.deepEqual(manifest.configSchema, configSchema);
});

test("normalizeConfig applies defaults and clamps integer fields", () => {
  const config = normalizeConfig({
    mqtt: { url: "mqtt://localhost", keepaliveSec: 999, qos: 5 },
    identity: { gatewayId: "turq", agentId: "main" },
    presence: { debounceMs: 10 },
    events: { summaryMaxLength: 5 },
  });

  assert.equal(config.topicPrefix, "turq/hive");
  assert.equal(config.mqtt.keepaliveSec, 300);
  assert.equal(config.mqtt.qos, 2);
  assert.equal(config.presence.debounceMs, 50);
  assert.equal(config.events.summaryMaxLength, 32);
});
