import assert from "node:assert/strict";
import test from "node:test";

import { MineflayerBody } from "../src/body.js";

test("world.mine paths to and digs the exact selected block", async () => {
  const body = new MineflayerBody(
    {
      serverHost: "127.0.0.1",
      serverPort: 25565,
      username: "AyerElysia",
      minecraftVersion: "1.21.1",
      entityRadiusBlocks: 32,
    },
    () => {},
  );
  const targetPosition = { x: 4, y: 63, z: -2 };
  let selectedBlockType = null;
  let digCalls = 0;
  let pathGoal = null;
  body.bot = {
    entity: {
      position: { x: 0, y: 64, z: 0 },
      yaw: 0,
      pitch: 0,
    },
    version: "1.21.1",
    findBlocks: ({ matching }) => {
      selectedBlockType = matching;
      return [targetPosition];
    },
    pathfinder: {
      setGoal: (goal) => {
        pathGoal = goal;
      },
    },
    blockAt: (position) => {
      assert.equal(position, targetPosition);
      return { type: selectedBlockType };
    },
    canDigBlock: () => true,
    dig: async () => {
      digCalls += 1;
    },
  };

  const dispatch = body.worldMine({ block: "minecraft:stone" });

  assert.equal(dispatch.dispatch_accepted, true);
  assert.equal(dispatch.block, "minecraft:stone");
  assert.notEqual(pathGoal, null);
  assert.equal(body.activeMine.phase, "navigating");

  await body.completePendingMine(body.bot);

  assert.equal(digCalls, 1);
  assert.equal(body.activeMine, null);
  assert.deepEqual(body.lastActionOutcome, {
    operation: "world.mine",
    success: true,
    block: "minecraft:stone",
    target: targetPosition,
  });
});

test("quit releases a bot that has not spawned yet", () => {
  const body = new MineflayerBody(
    {
      serverHost: "127.0.0.1",
      serverPort: 25565,
      username: "AyerElysia",
      minecraftVersion: "1.21.1",
      entityRadiusBlocks: 32,
    },
    () => {},
  );
  let quitCalls = 0;
  body.connectingBot = {
    quit: () => {
      quitCalls += 1;
    },
  };
  body.joining = true;

  body.quit();

  assert.equal(quitCalls, 1);
  assert.equal(body.connectingBot, null);
  assert.equal(body.stopped, true);
});
