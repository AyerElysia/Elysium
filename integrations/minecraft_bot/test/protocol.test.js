import assert from "node:assert/strict";
import { createHmac } from "node:crypto";
import test from "node:test";
import { once } from "node:events";

import { WebSocketServer } from "ws";

import {
  BRIDGE_PROTOCOL,
  BRIDGE_VERSION,
  BridgeBodyEndpoint,
} from "../src/protocol.js";

const TEST_TOKEN = "test-token-never-used-outside-this-process";

function withTimeout(promise, label) {
  let timer;
  return Promise.race([
    promise,
    new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error(`${label} timed out`)), 3000);
    }),
  ]).finally(() => clearTimeout(timer));
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject, settled: false };
}

test("bridge authenticates, observes, and durably replays a command receipt", async () => {
  const server = new WebSocketServer({ host: "127.0.0.1", port: 0 });
  await once(server, "listening");
  const address = server.address();
  assert.equal(typeof address, "object");

  let commandCalls = 0;
  const endpoint = new BridgeBodyEndpoint(
    {
      bridgeUri: `ws://127.0.0.1:${address.port}/elysium`,
      token: TEST_TOKEN,
      instanceId: "bot_protocol_test",
      bodyType: "mineflayer-bot",
      minecraftVersion: "1.21.1",
      capabilities: ["observation.wait"],
    },
    {
      onAuthenticated: () => endpoint.broadcastObservation({ world_loaded: true }),
      onCommand: async () => {
        commandCalls += 1;
        return { dispatched: true };
      },
    },
    () => {},
  );

  const command = {
    command_id: "command_protocol_test",
    intent_id: "intent_protocol_test",
    operation: "observation.wait",
    parameters: {},
  };
  const messages = [];
  const authenticated = deferred();
  const firstTerminal = deferred();
  const replayTerminal = deferred();

  server.on("connection", (socket) => {
    socket.on("message", (raw) => {
      const message = JSON.parse(raw.toString());
      messages.push(message);
      if (message.type === "hello") {
        assert.equal(message.protocol, BRIDGE_PROTOCOL);
        assert.equal(message.bridge_version, BRIDGE_VERSION);
        const digest = createHmac("sha256", TEST_TOKEN)
          .update(message.nonce)
          .digest("hex");
        socket.send(JSON.stringify({
          type: "authenticate",
          protocol: BRIDGE_PROTOCOL,
          digest,
        }));
      } else if (message.type === "authentication" && message.accepted) {
        authenticated.resolve(socket);
      } else if (message.type === "receipt" && message.receipt.completed) {
        if (commandCalls === 1 && !firstTerminal.settled) {
          firstTerminal.settled = true;
          firstTerminal.resolve(message.receipt);
        } else {
          replayTerminal.resolve(message.receipt);
        }
      }
    });
  });

  endpoint.start();
  const socket = await withTimeout(authenticated.promise, "authentication");
  socket.send(JSON.stringify({ type: "command", command }));
  const terminal = await withTimeout(firstTerminal.promise, "first terminal receipt");
  assert.equal(commandCalls, 1);
  assert.equal(terminal.accepted, true);
  assert.equal(terminal.completed, true);
  assert.deepEqual(terminal.facts, { dispatched: true });

  socket.send(JSON.stringify({ type: "command", command }));
  const replay = await withTimeout(replayTerminal.promise, "replayed terminal receipt");
  assert.equal(commandCalls, 1);
  assert.deepEqual(replay, terminal);
  assert.equal(
    messages.some(
      (message) => message.type === "observation"
        && message.observation.facts.world_loaded === true,
    ),
    true,
  );

  endpoint.stop();
  await new Promise((resolve) => server.close(resolve));
});
