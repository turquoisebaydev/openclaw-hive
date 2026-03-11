import { buildClientId } from "./config.js";
import { eventTopic, presenceTopic } from "./topic.js";

function createLogger(logger) {
  return logger ?? console;
}

function onceConnected(client) {
  if (client.connected) {
    return Promise.resolve();
  }

  return new Promise((resolve, reject) => {
    const onConnect = () => {
      cleanup();
      resolve();
    };
    const onError = (error) => {
      cleanup();
      reject(error);
    };
    const cleanup = () => {
      client.off?.("connect", onConnect);
      client.off?.("error", onError);
      client.removeListener?.("connect", onConnect);
      client.removeListener?.("error", onError);
    };

    client.on?.("connect", onConnect);
    client.on?.("error", onError);
  });
}

function publish(client, topic, payload, options) {
  return new Promise((resolve, reject) => {
    client.publish(topic, JSON.stringify(payload), options, (error) => {
      if (error) {
        reject(error);
        return;
      }
      resolve();
    });
  });
}

async function defaultCreateClient(config) {
  const mqtt = await import("mqtt");
  return mqtt.connect(config.mqtt.url, {
    username: config.mqtt.username,
    password: config.mqtt.password,
    clientId: buildClientId(config),
    keepalive: config.mqtt.keepaliveSec,
    connectTimeout: config.mqtt.connectTimeoutMs,
    clean: config.mqtt.clean,
    reconnectPeriod: 1000,
  });
}

export function createMqttPublisher({ config, logger, createClient = defaultCreateClient }) {
  const log = createLogger(logger);
  let client;

  async function ensureConnected() {
    if (client?.connected) {
      return client;
    }
    if (!client) {
      client = await createClient(config, log);
    }
    await onceConnected(client);
    return client;
  }

  return {
    async connect() {
      await ensureConnected();
    },
    async publishPresence(identity, payload) {
      const mqttClient = await ensureConnected();
      await publish(mqttClient, presenceTopic(config.topicPrefix, identity), payload, {
        qos: config.mqtt.qos,
        retain: config.presence.retain,
        messageExpiryInterval: config.presence.ttlSec,
      });
    },
    async publishEvent(identity, payload) {
      const mqttClient = await ensureConnected();
      await publish(mqttClient, eventTopic(config.topicPrefix, identity), payload, {
        qos: config.mqtt.qos,
        retain: false,
      });
    },
    async disconnect() {
      if (!client) {
        return;
      }
      await new Promise((resolve) => {
        client.end(false, {}, () => resolve());
      });
      client = undefined;
      log.info?.("hive-bridge: mqtt disconnected");
    },
  };
}
