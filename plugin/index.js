import { PLUGIN_DESCRIPTION, PLUGIN_ID, PLUGIN_NAME } from "./src/constants.js";
import { normalizeConfig } from "./src/config.js";
import { configSchema } from "./src/config-schema.js";
import { createBridgeService } from "./src/listener.js";
import { createMqttPublisher } from "./src/publisher.js";

export default {
  id: PLUGIN_ID,
  name: PLUGIN_NAME,
  description: PLUGIN_DESCRIPTION,
  configSchema,
  register(api) {
    const config = normalizeConfig(api.pluginConfig ?? {});
    const publisher = createMqttPublisher({ config, logger: api.logger });
    const service = createBridgeService({ api, config, logger: api.logger, publisher });

    api.registerService({
      id: `${PLUGIN_ID}.bridge`,
      start: () => service.start(),
      stop: () => service.stop(),
    });
  },
};
