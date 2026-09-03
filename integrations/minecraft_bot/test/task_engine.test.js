import assert from "node:assert/strict";
import test from "node:test";

import { MinecraftTaskEngine } from "../src/task_engine.js";

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((ok, fail) => {
    resolve = ok;
    reject = fail;
  });
  return { promise, resolve, reject };
}

async function waitFor(predicate) {
  for (let index = 0; index < 100; index += 1) {
    if (predicate()) return;
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
  throw new Error("condition did not become true");
}

test("task engine accepts quickly, owns one body, and emits a terminal event", async () => {
  const completion = deferred();
  const events = [];
  let runCount = 0;
  const body = {
    runHighLevelTask: async (_kind, args, context) => {
      runCount += 1;
      context.progress("working", { target: args.target });
      return completion.promise;
    },
    cancelHighLevelTask: async () => {},
  };
  const engine = new MinecraftTaskEngine(body, (kind, payload) => {
    events.push({ kind, payload });
  });

  const accepted = await engine.start({
    task_id: "task-one",
    kind: "go_to_position",
    arguments: { target: "village" },
  });
  await waitFor(() => runCount === 1);
  assert.equal(accepted.task_accepted, true);
  assert.equal(engine.snapshot().active.task_id, "task-one");
  await assert.rejects(
    engine.start({
      task_id: "task-two",
      kind: "go_to_position",
      arguments: {},
    }),
    /body gate is occupied/,
  );

  completion.resolve({ reached: true });
  await waitFor(() => engine.snapshot().active === null);
  assert.equal(engine.status({ task_id: "task-one" }).status, "completed");
  assert.equal(events.at(-1).kind, "minecraft.task.completed");
  assert.deepEqual(events.at(-1).payload.result, { reached: true });
});

test("task replay is idempotent and replacement cancels before reacquiring", async () => {
  const runs = [];
  const cancelled = [];
  const body = {
    runHighLevelTask: async (_kind, args, context) => {
      runs.push(args.label);
      await new Promise((resolve) => {
        context.signal.addEventListener("abort", resolve, { once: true });
      });
      throw new Error("aborted");
    },
    cancelHighLevelTask: async (taskId) => cancelled.push(taskId),
  };
  const engine = new MinecraftTaskEngine(body);

  await engine.start({
    task_id: "task-old",
    kind: "follow_player",
    arguments: { label: "old", player: "AyerElysia" },
  });
  await waitFor(() => runs.length === 1);
  const replay = await engine.start({
    task_id: "task-old",
    kind: "follow_player",
    arguments: { player: "AyerElysia", label: "old" },
  });
  assert.equal(replay.replayed, true);
  assert.equal(runs.length, 1);
  await assert.rejects(
    engine.start({
      task_id: "task-old",
      kind: "follow_player",
      arguments: { label: "changed" },
    }),
    /another task payload/,
  );

  await engine.start({
    task_id: "task-new",
    kind: "follow_player",
    arguments: { label: "new", player: "AyerElysia" },
    replace_current: true,
  });
  await waitFor(() => runs.length === 2);
  assert.deepEqual(cancelled, ["task-old"]);
  assert.equal(engine.status({ task_id: "task-old" }).status, "cancelled");
  assert.equal(engine.snapshot().active.task_id, "task-new");
  await engine.stop("test complete");
});

test("task deadline releases the body gate and reports an explicit failure", async () => {
  const cancelled = [];
  const events = [];
  const body = {
    runHighLevelTask: async (_kind, _args, context) => {
      await new Promise((resolve) => {
        context.signal.addEventListener("abort", resolve, { once: true });
      });
      throw new Error("aborted");
    },
    cancelHighLevelTask: async (taskId, reason) => {
      cancelled.push({ taskId, reason });
    },
  };
  const engine = new MinecraftTaskEngine(
    body,
    (kind, payload) => events.push({ kind, payload }),
    { defaultTaskTimeoutMs: 25 },
  );

  await engine.start({
    task_id: "task-timeout",
    kind: "follow_player",
    arguments: { player: "AyerElysia" },
  });
  await waitFor(() => engine.snapshot().active === null);

  const terminal = engine.status({ task_id: "task-timeout" });
  assert.equal(terminal.status, "failed");
  assert.equal(terminal.phase, "timed_out");
  assert.equal(terminal.max_duration_seconds, 0.025);
  assert.match(terminal.error, /technical task deadline exceeded/);
  assert.equal(cancelled.length, 1);
  assert.equal(cancelled[0].taskId, "task-timeout");
  assert.equal(events.at(-1).kind, "minecraft.task.failed");
  assert.equal(events.at(-1).payload.timeout_seconds, 0.025);
});

test("task duration is part of replay identity and wire bounds are enforced", async () => {
  const completion = deferred();
  const body = {
    runHighLevelTask: async () => completion.promise,
    cancelHighLevelTask: async () => {},
  };
  const engine = new MinecraftTaskEngine(body);

  await assert.rejects(
    engine.start({
      task_id: 42,
      kind: "go_to_position",
      arguments: { x: 0, y: 64, z: 0 },
    }),
    /task_id must be 1-160 safe identifier characters/,
  );
  await assert.rejects(
    engine.start({
      task_id: "wrong-replace-type",
      kind: "go_to_position",
      arguments: { x: 0, y: 64, z: 0 },
      replace_current: "false",
    }),
    /replace_current must be boolean/,
  );
  await assert.rejects(
    engine.start({
      task_id: "too-short",
      kind: "go_to_position",
      arguments: { x: 0, y: 64, z: 0 },
      max_duration_seconds: 1,
    }),
    /max_duration_seconds must be 5..600/,
  );
  await assert.rejects(
    engine.start({
      task_id: "wrong-type",
      kind: "go_to_position",
      arguments: { x: 0, y: 64, z: 0 },
      max_duration_seconds: "30",
    }),
    /max_duration_seconds must be 5..600/,
  );
  await engine.start({
    task_id: "duration-bound",
    kind: "go_to_position",
    arguments: { x: 0, y: 64, z: 0 },
    max_duration_seconds: 30,
  });
  await assert.rejects(
    engine.start({
      task_id: "duration-bound",
      kind: "go_to_position",
      arguments: { x: 0, y: 64, z: 0 },
      max_duration_seconds: 31,
    }),
    /another task payload/,
  );
  completion.resolve({ reached: true });
  await waitFor(() => engine.snapshot().active === null);
});

test("accepted-event failure never starts work or leaks the body gate", async () => {
  let runCount = 0;
  const body = {
    runHighLevelTask: async () => {
      runCount += 1;
      return {};
    },
    cancelHighLevelTask: async () => {},
  };
  const engine = new MinecraftTaskEngine(body, () => {
    throw new Error("event journal unavailable");
  });

  await assert.rejects(
    engine.start({
      task_id: "event-failed",
      kind: "go_to_position",
      arguments: { x: 0, y: 64, z: 0 },
    }),
    /event journal unavailable/,
  );
  assert.equal(runCount, 0);
  assert.equal(engine.snapshot().active, null);
  assert.throws(
    () => engine.status({ task_id: "event-failed" }),
    /unknown task_id/,
  );
});
