import assert from "node:assert/strict";
import test from "node:test";

import { MineflayerBody } from "../src/body.js";

function bodyWithWorld() {
  const body = new MineflayerBody(
    {
      serverHost: "127.0.0.1",
      serverPort: 25565,
      username: "Elysia",
      minecraftVersion: "1.21.1",
      entityRadiusBlocks: 32,
    },
    () => {},
  );
  body.bot = {
    entity: {
      position: { x: 1, y: 64, z: 2 },
      yaw: 0,
      pitch: 0,
    },
    version: "1.21.1",
  };
  return body;
}

function taskContext(onProgress = () => {}) {
  const controller = new AbortController();
  return {
    controller,
    value: {
      taskId: "body-contract-test",
      generation: 1,
      signal: controller.signal,
      progress: onProgress,
      ownsBody: () => true,
    },
  };
}

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

test("high-level navigation reaches a visible player and an exact coordinate", async () => {
  const body = bodyWithWorld();
  const goals = [];
  body.bot.players = {
    AyerElysia: {
      entity: { position: { x: 12.7, y: 65.2, z: -4.1 } },
    },
  };
  body.bot.pathfinder = {
    goto: async (goal) => goals.push(goal),
    setGoal: () => {},
  };

  const player = await body.runHighLevelTask(
    "go_to_player",
    { player: "AyerElysia", distance: 2 },
    taskContext().value,
  );
  const position = await body.runHighLevelTask(
    "go_to_position",
    { x: 20, y: 70, z: -8, distance: 1 },
    taskContext().value,
  );

  assert.equal(player.player, "AyerElysia");
  assert.equal(player.distance, 2);
  assert.deepEqual(position.target, { x: 20, y: 70, z: -8 });
  assert.equal(goals.length, 2);
});

test("high-level follow is continuous and obeys an explicit abort", async () => {
  const body = bodyWithWorld();
  let followGoal = null;
  body.bot.players = {
    AyerElysia: {
      entity: { position: { x: 3, y: 64, z: 3 } },
    },
  };
  body.bot.pathfinder = {
    setGoal: (goal) => {
      followGoal = goal;
    },
  };
  const built = taskContext((phase) => {
    assert.equal(phase, "following");
    built.controller.abort("test finished");
  });

  await assert.rejects(
    body.runHighLevelTask(
      "follow_player",
      { player: "AyerElysia", distance: 3 },
      built.value,
    ),
    /no longer owns the body gate/,
  );
  assert.notEqual(followGoal, null);
});

test("high-level gather uses CollectBlock with exact bounded targets", async () => {
  const body = bodyWithWorld();
  const positions = [
    { x: 4, y: 64, z: 4 },
    { x: 5, y: 64, z: 4 },
  ];
  let selectedType = null;
  let collected = null;
  body.bot.findBlocks = ({ matching, count }) => {
    selectedType = matching;
    assert.equal(count, 2);
    return positions;
  };
  body.bot.blockAt = (position) => ({ position, type: selectedType });
  body.bot.collectBlock = {
    collect: async (targets) => {
      collected = targets;
    },
  };

  const result = await body.runHighLevelTask(
    "gather_block",
    { block: "minecraft:oak_log", count: 2, max_distance: 24 },
    taskContext().value,
  );

  assert.equal(result.block, "minecraft:oak_log");
  assert.equal(result.collected, 2);
  assert.equal(collected.length, 2);
  assert.ok(collected.every((target) => target.type === selectedType));
});

test("high-level craft, place, and eat call the typed Mineflayer APIs", async () => {
  const body = bodyWithWorld();
  const calls = [];
  const recipe = { result: { count: 4 } };
  const inventoryItems = [
    { name: "oak_planks" },
    { name: "apple" },
  ];
  body.bot.pathfinder = {
    goto: async () => calls.push("goto"),
  };
  body.bot.recipesFor = () => [recipe];
  body.bot.craft = async (selected, count, table) => {
    assert.equal(selected, recipe);
    assert.equal(count, 1);
    assert.equal(table, null);
    calls.push("craft");
  };
  body.bot.inventory = { items: () => inventoryItems };
  body.bot.equip = async (item, destination) => {
    calls.push("equip:" + item.name + ":" + destination);
  };
  body.bot.blockAt = () => ({ name: "stone" });
  body.bot.placeBlock = async (_reference, face) => {
    calls.push("place:" + face.x + "," + face.y + "," + face.z);
  };
  body.bot.consume = async () => calls.push("consume");
  body.bot.food = 17;

  const crafted = await body.runHighLevelTask(
    "craft_item",
    { item: "minecraft:stick", count: 4 },
    taskContext().value,
  );
  const placed = await body.runHighLevelTask(
    "place_block",
    {
      item: "minecraft:oak_planks",
      reference_x: 1,
      reference_y: 63,
      reference_z: 2,
      face_x: 0,
      face_y: 1,
      face_z: 0,
    },
    taskContext().value,
  );
  const eaten = await body.runHighLevelTask(
    "eat_item",
    { item: "minecraft:apple" },
    taskContext().value,
  );

  assert.equal(crafted.item, "minecraft:stick");
  assert.deepEqual(placed.placed_at, { x: 1, y: 64, z: 2 });
  assert.deepEqual(eaten, { item: "minecraft:apple", food: 17 });
  assert.deepEqual(calls, [
    "craft",
    "goto",
    "equip:oak_planks:hand",
    "place:0,1,0",
    "equip:apple:hand",
    "consume",
  ]);
});
