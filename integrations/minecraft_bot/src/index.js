// Elysia headless Minecraft body entrypoint.
//
// Configuration comes from explicit environment variables owned by the
// Elysium Python session lifecycle:
//   ELYSIUM_BOT_BRIDGE_URI              ws:// controller listener (required)
//   ELYSIUM_BOT_TOKEN                   bridge authentication token (required)
//   ELYSIUM_BOT_SERVER_HOST             Minecraft server host (required)
//   ELYSIUM_BOT_SERVER_PORT             Minecraft server port (required)
//   ELYSIUM_BOT_USERNAME                in-game account name (required)
//   ELYSIUM_BOT_MC_VERSION              protocol version, default 1.21.1
//   ELYSIUM_BOT_INSTANCE_ID             stable instance identity (required)
//   ELYSIUM_BOT_OBSERVATION_INTERVAL_MS snapshot cadence, default 1000
//   ELYSIUM_BOT_ENTITY_RADIUS_BLOCKS    sensor radius, default 32

import { BridgeBodyEndpoint } from "./protocol.js";
import { MineflayerBody } from "./body.js";

const log = (line) => {
  process.stderr.write(`[elysium-minecraft-bot] ${line}\n`);
};

function requiredEnv(name) {
  const value = process.env[name];
  if (!value || value.trim() === "") {
    log(`missing required environment variable: ${name}`);
    process.exit(2);
  }
  return value.trim();
}

function envInt(name, fallback) {
  const raw = process.env[name];
  if (!raw) {
    return fallback;
  }
  const value = Number.parseInt(raw, 10);
  if (!Number.isFinite(value) || value <= 0) {
    log(`invalid numeric environment variable: ${name}`);
    process.exit(2);
  }
  return value;
}

const bridgeUri = requiredEnv("ELYSIUM_BOT_BRIDGE_URI");
const token = requiredEnv("ELYSIUM_BOT_TOKEN");
const serverHost = requiredEnv("ELYSIUM_BOT_SERVER_HOST");
const serverPort = envInt("ELYSIUM_BOT_SERVER_PORT", 0);
const username = requiredEnv("ELYSIUM_BOT_USERNAME");
const instanceId = requiredEnv("ELYSIUM_BOT_INSTANCE_ID");
const minecraftVersion = process.env.ELYSIUM_BOT_MC_VERSION?.trim() || "1.21.1";
const observationIntervalMs = envInt("ELYSIUM_BOT_OBSERVATION_INTERVAL_MS", 1000);
const entityRadiusBlocks = envInt("ELYSIUM_BOT_ENTITY_RADIUS_BLOCKS", 32);

if (!bridgeUri.startsWith("ws://") && !bridgeUri.startsWith("wss://")) {
  log("ELYSIUM_BOT_BRIDGE_URI must be an explicit ws:// URI");
  process.exit(2);
}

const bodyConfig = {
  serverHost,
  serverPort,
  username,
  minecraftVersion,
  entityRadiusBlocks,
};

const body = new MineflayerBody(bodyConfig, log);

const operations = [
  "chat.send",
  "control.release_all",
  "interaction.attack",
  "interaction.use",
  "inventory.select_hotbar",
  "item.drop",
  "movement.input",
  "navigation.follow",
  "navigation.goto",
  "navigation.stop",
  "observation.wait",
  "player.respawn",
  "world.mine",
];

const endpoint = new BridgeBodyEndpoint(
  {
    bridgeUri,
    token,
    instanceId,
    bodyType: "mineflayer-bot",
    minecraftVersion,
    capabilities: operations,
  },
  {
    onAuthenticated: () => {
      log(`bridge authenticated as ${instanceId}`);
      endpoint.broadcastObservation(collectFacts());
    },
    onDisconnected: (reason) => log(`bridge disconnected: ${reason}`),
    onCommand: (operation, parameters) => body.execute(operation, parameters),
    onInterrupt: (intentId, reason) => body.interrupt(intentId, reason),
    onReleaseAll: (reason) => body.releaseAll(reason),
  },
  log,
);

function collectFacts() {
  return {
    ...body.collectFacts(),
    bridge_transport: endpoint.transportFacts(),
  };
}

let observationTimer = null;

function startObservationLoop() {
  if (observationTimer) {
    return;
  }
  observationTimer = setInterval(() => {
    try {
      endpoint.broadcastObservation(collectFacts());
    } catch (error) {
      log(`observation collection failed: ${error.message}`);
    }
  }, observationIntervalMs);
  observationTimer.unref?.();
}

body.onBotReady = () => {
  log(`joined ${body.endpointDescription()} as ${username}`);
  endpoint.broadcastObservation(collectFacts());
};

let shuttingDown = false;

async function shutdown(signal) {
  if (shuttingDown) {
    return;
  }
  shuttingDown = true;
  log(`received ${signal}; releasing controls and stopping`);
  if (observationTimer) {
    clearInterval(observationTimer);
    observationTimer = null;
  }
  try {
    body.releaseAll("process stopping");
  } catch (error) {
    log(`control release failed: ${error.message}`);
  }
  endpoint.stop();
  body.quit();
  setTimeout(() => process.exit(0), 250).unref();
}

process.on("SIGTERM", () => shutdown("SIGTERM"));
process.on("SIGINT", () => shutdown("SIGINT"));
process.on("uncaughtException", (error) => {
  log(`uncaught exception: ${error.stack ?? error}`);
  shutdown("uncaughtException");
});

log(
  `starting body ${instanceId} for ${serverHost}:${serverPort} `
  + `(minecraft ${minecraftVersion}, bridge ${bridgeUri})`,
);
body.join();
endpoint.start();
startObservationLoop();
