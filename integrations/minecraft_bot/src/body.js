// Mineflayer-backed game body facts and operations.
//
// This module mirrors the NeoForge bridge's StateCollector / ControlExecutor
// semantics closely enough for the shared planner guidance: observations are
// factual snapshots, operations are validated typed dispatches. It never
// decides what Elysia should want; receipts prove dispatch, not intent success.

import mineflayer from "mineflayer";
import collectBlockPkg from "mineflayer-collectblock";
import minecraftData from "minecraft-data";
import pathfinderPkg from "mineflayer-pathfinder";
import vec3Pkg from "vec3";
import { MINECRAFT_TASK_KINDS, MinecraftTaskEngine } from "./task_engine.js";
const { pathfinder, Movements, goals } = pathfinderPkg;
const { plugin: collectBlock } = collectBlockPkg;
const { Vec3 } = vec3Pkg;

const WORLD_COORDINATE_LIMIT = 30_000_000;
const PLAYER_NAME_PATTERN = /^[A-Za-z0-9_]{1,16}$/;
const RESOURCE_ID_PATTERN = /^(?:[a-z0-9_.-]+:)?[a-z0-9_./-]+$/;
const HELD_CONTROL_NAMES = [
  "forward", "back", "left", "right", "jump", "sneak", "sprint",
];
const PULSE_CONTROL_NAMES = [
  ...HELD_CONTROL_NAMES, "attack", "use", "drop",
];
const PULSE_RELEASE_MS = 60;
const MAX_RECENT_CHAT_EVENTS = 16;
const MAX_CHAT_MESSAGE_LENGTH = 256;
const MAX_VISIBLE_PLAYERS = 64;
const MAX_NEARBY_ENTITIES = 128;
const JOIN_RETRY_BACKOFF_SECONDS = [2, 5, 10, 30, 60];

function round(value, digits = 3) {
  const factor = 10 ** digits;
  return Math.round(Number(value) * factor) / factor;
}

/** Validate one required non-empty string parameter. */
function requiredString(parameters, name) {
  const value = parameters?.[name];
  if (typeof value !== "string" || value.trim() === "") {
    throw new Error(`Missing non-empty string parameter: ${name}`);
  }
  return value;
}

/** Validate one required integer parameter inside an inclusive range. */
function boundedInt(parameters, name, minimum, maximum) {
  const value = parameters?.[name];
  if (typeof value !== "number" || !Number.isFinite(value) || !Number.isInteger(value)) {
    throw new Error(`Missing numeric parameter: ${name}`);
  }
  if (value < minimum || value > maximum) {
    throw new Error(`${name} must be between ${minimum} and ${maximum}`);
  }
  return value;
}

/** Read one optional finite number inside an inclusive range. */
function optionalBoundedFloat(parameters, name, minimum, maximum) {
  const value = parameters?.[name];
  if (value === undefined || value === null) {
    return 0;
  }
  if (typeof value !== "number" || !Number.isFinite(value)) {
    throw new Error(`${name} must be numeric`);
  }
  if (value < minimum || value > maximum) {
    throw new Error(`${name} must be finite and between ${minimum} and ${maximum}`);
  }
  return value;
}

function optionalBoundedInt(parameters, name, minimum, maximum, fallback) {
  if (parameters?.[name] === undefined || parameters?.[name] === null) {
    return fallback;
  }
  return boundedInt(parameters, name, minimum, maximum);
}

function resourceName(parameters, name) {
  let value = requiredString(parameters, name).toLowerCase();
  if (!RESOURCE_ID_PATTERN.test(value)) {
    throw new Error(`${name} must be a Minecraft resource identifier`);
  }
  if (!value.includes(":")) value = `minecraft:${value}`;
  return value;
}

function shortResourceName(value) {
  return String(value).split(":", 2).at(-1);
}

function assertTaskActive(context) {
  if (context.signal.aborted || !context.ownsBody()) {
    throw new Error("high-level task no longer owns the body gate");
  }
}

async function taskDelay(context, milliseconds) {
  assertTaskActive(context);
  await new Promise((resolve) => {
    const timer = setTimeout(resolve, milliseconds);
    context.signal.addEventListener("abort", () => {
      clearTimeout(timer);
      resolve();
    }, { once: true });
  });
  assertTaskActive(context);
}

/** Headless Minecraft body joining one configured server endpoint. */
export class MineflayerBody {
  constructor(config, log = console.error) {
    this.config = config;
    this.log = log;
    this.bot = null;
    this.connectingBot = null;
    this.joining = false;
    this.stopped = false;
    this.joinFailures = 0;
    this.lastJoinError = "";
    this.retryTimer = null;
    this.recentChat = [];
    this.activeMine = null;
    this.lastActionOutcome = null;
    this.lastHealth = null;
    this.onEvent = null;
    this.taskEngine = new MinecraftTaskEngine(
      this,
      (kind, payload) => this.emitEvent(kind, payload),
    );
  }

  /** Join the configured server; bounded backoff keeps failures diagnosable. */
  join() {
    if (this.stopped || this.joining || this.bot) {
      return;
    }
    this.joining = true;
    this.lastJoinError = "";
    let bot;
    try {
      bot = mineflayer.createBot({
        host: this.config.serverHost,
        port: this.config.serverPort,
        username: this.config.username,
        version: this.config.minecraftVersion,
        auth: "offline",
        hideErrors: false,
      });
    } catch (error) {
      this.joining = false;
      this.scheduleRetry(String(error.message ?? error));
      return;
    }
    this.connectingBot = bot;
    bot.loadPlugin(pathfinder);
    bot.loadPlugin(collectBlock);
    const onSpawn = () => {
      if (this.stopped) {
        bot.quit("body stopped while connecting");
        return;
      }
      this.joining = false;
      this.joinFailures = 0;
      this.connectingBot = null;
      this.bot = bot;
      this.lastHealth = Number(bot.health ?? 0);
      try {
        bot.pathfinder.setMovements(new Movements(bot));
      } catch (error) {
        this.log(`pathfinder movement setup failed: ${error.message}`);
      }
      this.recordChat("system", "", `body joined ${this.endpointDescription()}`);
      this.emitEvent("minecraft.body.spawned", {
        username: String(bot.username ?? this.config.username),
        endpoint: this.endpointDescription(),
      });
      this.onBotReady?.();
    };
    const onEnd = (reason) => {
      if (this.bot === bot || this.connectingBot === bot) {
        this.bot = null;
        this.connectingBot = null;
        this.joining = false;
        this.recordChat("system", "", `body left the world: ${reason}`);
        this.emitEvent("minecraft.body.disconnected", {
          reason: String(reason || "connection ended").slice(0, 512),
        });
        void this.taskEngine.stop("Minecraft body disconnected").catch((error) => {
          this.log(`task cleanup after disconnect failed: ${error.message}`);
        });
        this.scheduleRetry(String(reason || "connection ended"));
      }
    };
    const onKicked = (reason) => {
      this.lastJoinError = `kicked: ${JSON.stringify(reason)}`;
      this.log(this.lastJoinError);
    };
    const onError = (error) => {
      this.lastJoinError = String(error.message ?? error);
      this.log(`minecraft connection error: ${this.lastJoinError}`);
    };
    bot.once("spawn", onSpawn);
    bot.on("end", onEnd);
    bot.on("kicked", onKicked);
    bot.on("error", onError);
    bot.on("goal_reached", () => {
      void this.completePendingMine(bot);
    });
    bot.on("chat", (username, message) => {
      this.recordChat("chat", username, message);
    });
    bot.on("whisper", (username, message) => {
      this.recordChat("whisper", username, message);
    });
    bot.on("messagestr", (message, position, json) => {
      if (position !== "chat") {
        this.recordChat("system", String(json?.translate ?? ""), message);
      }
    });
    bot.on("playerJoined", (player) => {
      this.recordChat("join", player.username, `${player.username} joined the game`);
    });
    bot.on("playerLeft", (player) => {
      this.recordChat("leave", player.username, `${player.username} left the game`);
    });
    bot.on("health", () => {
      const health = Number(bot.health ?? 0);
      const food = Number(bot.food ?? 0);
      if (this.lastHealth !== health) {
        this.lastHealth = health;
        this.emitEvent("minecraft.player.health_changed", { health, food });
      }
    });
    bot.on("death", () => {
      this.emitEvent("minecraft.player.died", {
        position: this.positionFacts(),
      });
    });
  }

  /** Leave the world and stop retrying; safe to call more than once. */
  quit() {
    this.stopped = true;
    void this.taskEngine.stop("body stopping").catch((error) => {
      this.log(`task cleanup while stopping failed: ${error.message}`);
    });
    if (this.retryTimer) {
      clearTimeout(this.retryTimer);
      this.retryTimer = null;
    }
    const bot = this.bot ?? this.connectingBot;
    this.bot = null;
    this.connectingBot = null;
    if (bot) {
      try {
        bot.quit("body stopping");
      } catch {
        // Already disconnected; ownership is released either way.
      }
    }
  }

  inWorld() {
    return this.bot !== null && this.bot.entity !== null;
  }

  endpointDescription() {
    return `${this.config.serverHost}:${this.config.serverPort}`;
  }

  scheduleRetry(reason) {
    if (this.stopped || this.retryTimer) {
      return;
    }
    this.lastJoinError = this.lastJoinError || reason;
    const attempt = Math.min(this.joinFailures, JOIN_RETRY_BACKOFF_SECONDS.length - 1);
    this.joinFailures += 1;
    const delaySeconds = JOIN_RETRY_BACKOFF_SECONDS[attempt];
    this.retryTimer = setTimeout(() => {
      this.retryTimer = null;
      this.join();
    }, delaySeconds * 1000);
  }

  recordChat(kind, username, message) {
    const entry = {
      kind,
      username: String(username ?? "").slice(0, 64),
      message: String(message ?? "").slice(0, 512),
      at: new Date().toISOString(),
    };
    this.recentChat.push(entry);
    if (this.recentChat.length > MAX_RECENT_CHAT_EVENTS) {
      this.recentChat.splice(0, this.recentChat.length - MAX_RECENT_CHAT_EVENTS);
    }
    const ownName = String(this.bot?.username ?? this.config.username ?? "");
    if ((kind === "chat" || kind === "whisper") && entry.username === ownName) {
      return;
    }
    const eventKinds = {
      chat: "minecraft.chat.received",
      whisper: "minecraft.whisper.received",
      system: "minecraft.system.received",
      join: "minecraft.player.joined",
      leave: "minecraft.player.left",
    };
    const eventKind = eventKinds[kind];
    if (eventKind) {
      this.emitEvent(eventKind, {
        username: entry.username,
        message: entry.message,
      }, entry.at);
    }
  }

  emitEvent(kind, payload, occurredAt = new Date().toISOString()) {
    if (typeof this.onEvent === "function") {
      this.onEvent(kind, payload, occurredAt);
    }
  }

  /** Collect one factual snapshot; mirrors the NeoForge StateCollector shape. */
  collectFacts() {
    const facts = {
      client_paused: false,
      window_active: false,
      fps: 0,
    };
    if (!this.inWorld()) {
      facts.world_loaded = false;
      facts.screen = {
        class: "headless_connecting",
        title: this.lastJoinError
          ? `connecting (${this.lastJoinError})`
          : "connecting",
      };
      return facts;
    }
    const bot = this.bot;
    facts.world_loaded = true;
    facts.world = {
      mode: "multiplayer",
      server_address: this.endpointDescription(),
    };
    facts.dimension = String(bot.game.dimension ?? "unknown");
    facts.game_time = Number(bot.time?.time ?? 0);
    facts.day_time = Number(bot.time?.timeOfDay ?? 0);
    facts.raining = Boolean(bot.isRaining);
    facts.thundering = Boolean(bot.thunderState?.isThundering ?? false);

    const entity = bot.entity;
    const player = {
      uuid: String(bot.player?.uuid ?? ""),
      name: String(bot.username ?? this.config.username),
      x: round(entity.position.x),
      y: round(entity.position.y),
      z: round(entity.position.z),
      yaw: round(bot.entity.yaw * 180 / Math.PI, 2),
      pitch: round(bot.entity.pitch * 180 / Math.PI, 2),
      health: Number(bot.health ?? 0),
      max_health: 20,
      alive: Number(bot.health ?? 0) > 0,
      dead_or_dying: Number(bot.health ?? 0) <= 0,
      absorption: 0,
      food: Number(bot.food ?? 0),
      saturation: Number(bot.foodSaturation ?? 0),
      air: Number(bot.oxygenLevel ?? 300),
      experience_level: Number(bot.experience?.level ?? 0),
      experience_progress: Number(bot.experience?.progress ?? 0),
      on_ground: Boolean(entity.onGround),
      sprinting: Boolean(bot.getControlState("sprint")),
      crouching: Boolean(bot.getControlState("sneak")),
      swimming: false,
      fall_flying: false,
      selected_hotbar_slot: Number(bot.quickBarSlot ?? 0),
      effects: Object.entries(bot.activeEffects ?? {}).map(([effect, detail]) => ({
        effect: String(effect),
        amplifier: Number(detail?.amplifier ?? 0),
        duration_ticks: Number(detail?.duration ?? 0),
        ambient: Boolean(detail?.ambient ?? false),
      })),
    };
    facts.player = player;

    try {
      const block = bot.blockAt(entity.position);
      if (block?.biome) {
        facts.biome = String(block.biome.name ?? "");
      }
    } catch {
      // Biome is informational; absence must not break the snapshot.
    }

    facts.inventory = bot.inventory.slots.map((slot, index) => stackFacts(index, slot));
    facts.players = Object.values(bot.players)
      .filter((other) => other && other.username !== bot.username)
      .map((other) => ({
        uuid: String(other.uuid ?? ""),
        name: String(other.username ?? ""),
        x: other.entity ? round(other.entity.position.x) : null,
        y: other.entity ? round(other.entity.position.y) : null,
        z: other.entity ? round(other.entity.position.z) : null,
        health: other.entity?.health ?? null,
      }))
      .slice(0, MAX_VISIBLE_PLAYERS);
    facts.entities = this.entityFacts();
    facts.crosshair = this.crosshairFacts();
    facts.controls = this.controlFacts();
    facts.baritone = { available: false, pathing: Boolean(bot.pathfinder.isMoving()) };
    facts.chat = [...this.recentChat];
    facts.bot_tasks = {
      high_level: this.taskEngine.snapshot(),
      active_mine: this.activeMine
        ? {
            block: this.activeMine.block,
            target: { ...this.activeMine.target },
            phase: this.activeMine.phase,
          }
        : null,
      last_action_outcome: this.lastActionOutcome,
    };
    return facts;
  }

  entityFacts() {
    const bot = this.bot;
    const radius = this.config.entityRadiusBlocks;
    const results = [];
    for (const entity of Object.values(bot.entities)) {
      if (!entity || entity === bot.entity) {
        continue;
      }
      const distance = entity.position.distanceTo(bot.entity.position);
      if (distance > radius) {
        continue;
      }
      results.push({
        id: Number(entity.id),
        uuid: String(entity.uuid ?? ""),
        type: entityTypeName(entity),
        name: String(entity.name ?? entity.displayName ?? ""),
        x: round(entity.position.x),
        y: round(entity.position.y),
        z: round(entity.position.z),
        distance: round(distance),
        alive: Boolean(entity.isValid),
      });
    }
    return results.slice(0, MAX_NEARBY_ENTITIES);
  }

  crosshairFacts() {
    const bot = this.bot;
    try {
      const block = bot.blockAtCursor?.(4.5);
      if (block) {
        return {
          kind: "BLOCK",
          x: round(block.position.x + 0.5),
          y: round(block.position.y + 0.5),
          z: round(block.position.z + 0.5),
          block: String(block.name ?? ""),
          block_x: Number(block.position.x),
          block_y: Number(block.position.y),
          block_z: Number(block.position.z),
        };
      }
    } catch {
      // Raycast failure is reported as unavailable, never invented.
    }
    return { kind: "unavailable" };
  }

  controlFacts() {
    const bot = this.bot;
    const result = {};
    for (const name of [...HELD_CONTROL_NAMES, "attack", "use"]) {
      try {
        result[name] = Boolean(bot.getControlState(name));
      } catch {
        // attack/use are pulse actions (bot.attack / activateBlock) and
        // expose no held control state in mineflayer.
        result[name] = false;
      }
    }
    return result;
  }

  /** Execute one validated operation and return dispatch facts. */
  async execute(operation, parameters) {
    const bodyGateExempt = new Set([
      "chat.send",
      "control.release_all",
      "task.cancel",
      "task.status",
    ]);
    if (this.taskEngine.ownsBody() && !bodyGateExempt.has(operation)) {
      if (operation !== "task.start") {
        throw new Error("body gate is occupied by an active high-level task");
      }
    }
    switch (operation) {
      case "task.start":
        return this.taskEngine.start(parameters);
      case "task.cancel":
        return this.taskEngine.cancel(parameters);
      case "task.status":
        return this.taskEngine.status(parameters);
      case "movement.input":
        return this.movementInput(parameters);
      case "navigation.goto":
        return this.navigationGoto(parameters);
      case "navigation.follow":
        return this.navigationFollow(parameters);
      case "navigation.stop":
        return this.navigationStop();
      case "world.mine":
        return this.worldMine(parameters);
      case "interaction.attack":
        return this.pulseAttack();
      case "interaction.use":
        return this.pulseUse();
      case "inventory.select_hotbar":
        return this.selectHotbar(parameters);
      case "item.drop":
        return this.pulseDrop();
      case "observation.wait":
        return this.observationWait();
      case "chat.send":
        return this.sendChat(parameters);
      case "player.respawn":
        return this.respawn();
      case "control.release_all":
        return this.releaseAll("command");
      default:
        throw new Error(`Unsupported operation: ${operation}`);
    }
  }

  requireWorld() {
    if (!this.inWorld()) {
      throw new Error("No Minecraft world is loaded");
    }
  }

  positionFacts() {
    const entity = this.bot.entity;
    return {
      x: round(entity.position.x),
      y: round(entity.position.y),
      z: round(entity.position.z),
      yaw: round(entity.yaw * 180 / Math.PI, 2),
      pitch: round(entity.pitch * 180 / Math.PI, 2),
    };
  }

  /** Apply a complete held-control snapshot and optional one-shot pulses. */
  movementInput(parameters) {
    this.requireWorld();
    const bot = this.bot;
    const holds = {};
    if (parameters.holds !== undefined) {
      if (typeof parameters.holds !== "object" || parameters.holds === null) {
        throw new Error("holds must be an object");
      }
      for (const [name, value] of Object.entries(parameters.holds)) {
        if (!HELD_CONTROL_NAMES.includes(name)) {
          throw new Error(`Unknown held control: ${name}`);
        }
        if (typeof value !== "boolean") {
          throw new Error(`Held control must be boolean: ${name}`);
        }
        holds[name] = value;
      }
    }
    for (const name of HELD_CONTROL_NAMES) {
      bot.setControlState(name, Boolean(holds[name]));
    }
    if (parameters.pulses !== undefined) {
      if (!Array.isArray(parameters.pulses)) {
        throw new Error("pulses must be an array");
      }
      for (const name of parameters.pulses) {
        if (typeof name !== "string" || !PULSE_CONTROL_NAMES.includes(name)) {
          throw new Error(`Unknown control pulse: ${name}`);
        }
        this.pulseControl(name);
      }
    }
    if (parameters.look_delta !== undefined) {
      if (typeof parameters.look_delta !== "object" || parameters.look_delta === null) {
        throw new Error("look_delta must be an object");
      }
      const deltaYaw = optionalBoundedFloat(parameters.look_delta, "yaw", -180, 180);
      const deltaPitch = optionalBoundedFloat(parameters.look_delta, "pitch", -90, 90);
      const yaw = bot.entity.yaw + deltaYaw * Math.PI / 180;
      const pitch = Math.max(
        -Math.PI / 2,
        Math.min(Math.PI / 2, bot.entity.pitch + deltaPitch * Math.PI / 180),
      );
      bot.look(yaw, pitch, true);
    }
    if (parameters.hotbar_slot !== undefined) {
      const slot = boundedInt(parameters, "hotbar_slot", 0, 8);
      bot.setQuickBarSlot(slot);
    }
    const facts = this.positionFacts();
    facts.holds = holds;
    return facts;
  }

  pulseControl(name) {
    const bot = this.bot;
    if (name === "attack") {
      const target = this.nearestAttackableEntity();
      if (target) {
        bot.attack(target);
      }
      return;
    }
    if (name === "use") {
      const block = bot.blockAtCursor?.(4.5);
      if (block) {
        bot.activateBlock(block).catch((error) => {
          this.log(`use activation failed: ${error.message}`);
        });
      }
      return;
    }
    if (name === "drop") {
      this.dropSelectedItem();
      return;
    }
    bot.setControlState(name, true);
    setTimeout(() => {
      if (this.bot === bot) {
        bot.setControlState(name, false);
      }
    }, PULSE_RELEASE_MS);
  }

  nearestAttackableEntity() {
    const bot = this.bot;
    return bot.nearestEntity((entity) =>
      entity && entity !== bot.entity && entity.isValid
      && entity.position.distanceTo(bot.entity.position) <= 4.5);
  }

  dropSelectedItem() {
    const bot = this.bot;
    const slotIndex = (bot.inventory.hotbarStart ?? 36) + Number(bot.quickBarSlot ?? 0);
    const item = bot.inventory.slots[slotIndex];
    if (!item) {
      throw new Error("Selected hotbar slot is empty");
    }
    bot.tossStack(item).catch((error) => {
      this.log(`item drop failed: ${error.message}`);
    });
  }

  /** Navigate to one exact block coordinate through the pathfinder. */
  navigationGoto(parameters) {
    this.requireWorld();
    this.cancelPendingMine("replaced by navigation.goto");
    const x = boundedInt(parameters, "x", -WORLD_COORDINATE_LIMIT, WORLD_COORDINATE_LIMIT);
    const y = boundedInt(parameters, "y", -2048, 2048);
    const z = boundedInt(parameters, "z", -WORLD_COORDINATE_LIMIT, WORLD_COORDINATE_LIMIT);
    this.bot.pathfinder.setGoal(new goals.GoalBlock(x, y, z));
    const facts = this.positionFacts();
    facts.executor = "pathfinder";
    facts.dispatch_accepted = true;
    facts.target = { x, y, z };
    return facts;
  }

  /** Follow one validated account name currently visible in the world. */
  navigationFollow(parameters) {
    this.requireWorld();
    this.cancelPendingMine("replaced by navigation.follow");
    const player = requiredString(parameters, "player");
    if (!PLAYER_NAME_PATTERN.test(player)) {
      throw new Error("player must match the Minecraft account-name contract");
    }
    const target = this.bot.players[player]?.entity;
    if (!target) {
      throw new Error(`player is not visible from this body: ${player}`);
    }
    this.bot.pathfinder.setGoal(new goals.GoalFollow(target, 2), true);
    const facts = this.positionFacts();
    facts.executor = "pathfinder";
    facts.dispatch_accepted = true;
    facts.player = player;
    return facts;
  }

  /** Stop pathfinder navigation while retaining ordinary body control. */
  navigationStop() {
    this.requireWorld();
    this.cancelPendingMine("navigation stopped");
    this.bot.pathfinder.setGoal(null);
    const facts = this.positionFacts();
    facts.navigation_stopped = true;
    return facts;
  }

  /** Locate and mine one validated block identifier; outcomes need later facts. */
  worldMine(parameters) {
    this.requireWorld();
    const bot = this.bot;
    let block = requiredString(parameters, "block").toLowerCase();
    if (!RESOURCE_ID_PATTERN.test(block)) {
      throw new Error("block must be a Minecraft resource identifier");
    }
    if (!block.includes(":")) {
      block = `minecraft:${block}`;
    }
    const mcData = minecraftData(bot.version);
    const shortName = block.split(":")[1];
    const blockType = mcData.blocksByName?.[shortName];
    if (!blockType) {
      throw new Error(`unknown block identifier: ${block}`);
    }
    const positions = bot.findBlocks({
      matching: blockType.id,
      maxDistance: 32,
      count: 1,
    });
    if (positions.length === 0) {
      throw new Error(`no ${block} block found within 32 blocks`);
    }
    const target = positions[0];
    bot.pathfinder.setGoal(new goals.GoalNear(target.x, target.y, target.z, 2));
    this.cancelPendingMine("replaced by world.mine");
    this.activeMine = {
      target: { x: target.x, y: target.y, z: target.z },
      targetPosition: target,
      blockTypeId: blockType.id,
      block,
      phase: "navigating",
    };
    this.lastActionOutcome = null;
    const facts = this.positionFacts();
    facts.executor = "pathfinder+dig";
    facts.dispatch_accepted = true;
    facts.block = block;
    facts.target = { x: target.x, y: target.y, z: target.z };
    return facts;
  }

  /** Dig the exact selected block once pathfinder reaches its bounded goal. */
  async completePendingMine(bot) {
    const pending = this.activeMine;
    if (!pending || this.bot !== bot || !this.inWorld()) {
      return;
    }
    pending.phase = "digging";
    try {
      const target = bot.blockAt(pending.targetPosition);
      if (!target || Number(target.type) !== Number(pending.blockTypeId)) {
        throw new Error(`target block changed before digging: ${pending.block}`);
      }
      if (typeof bot.canDigBlock === "function" && !bot.canDigBlock(target)) {
        throw new Error(`target block is not currently diggable: ${pending.block}`);
      }
      await bot.dig(target);
      this.lastActionOutcome = {
        operation: "world.mine",
        success: true,
        block: pending.block,
        target: { ...pending.target },
      };
    } catch (error) {
      this.lastActionOutcome = {
        operation: "world.mine",
        success: false,
        block: pending.block,
        target: { ...pending.target },
        error: String(error?.message ?? error).slice(0, 512),
      };
      this.log(`mine execution failed: ${this.lastActionOutcome.error}`);
    } finally {
      if (this.activeMine === pending) {
        this.activeMine = null;
      }
    }
  }

  cancelPendingMine(reason) {
    if (!this.activeMine) {
      return;
    }
    this.lastActionOutcome = {
      operation: "world.mine",
      success: false,
      block: this.activeMine.block,
      target: { ...this.activeMine.target },
      error: reason,
    };
    this.activeMine = null;
  }

  /** Execute one typed high-level task while TaskEngine owns the body gate. */
  async runHighLevelTask(kind, parameters, context) {
    this.requireWorld();
    assertTaskActive(context);
    switch (kind) {
      case "follow_player":
        return this.taskFollowPlayer(parameters, context);
      case "go_to_player":
        return this.taskGoToPlayer(parameters, context);
      case "go_to_position":
        return this.taskGoToPosition(parameters, context);
      case "gather_block":
        return this.taskGatherBlock(parameters, context);
      case "craft_item":
        return this.taskCraftItem(parameters, context);
      case "place_block":
        return this.taskPlaceBlock(parameters, context);
      case "eat_item":
        return this.taskEatItem(parameters, context);
      default:
        throw new Error(`unsupported high-level task kind: ${kind}`);
    }
  }

  visiblePlayer(name) {
    const player = requiredString({ player: name }, "player");
    if (!PLAYER_NAME_PATTERN.test(player)) {
      throw new Error("player must match the Minecraft account-name contract");
    }
    const target = this.bot.players[player]?.entity;
    if (!target) throw new Error(`player is not visible from this body: ${player}`);
    return { player, target };
  }

  async taskFollowPlayer(parameters, context) {
    const distance = optionalBoundedInt(parameters, "distance", 1, 16, 3);
    const { player, target } = this.visiblePlayer(parameters.player);
    this.bot.pathfinder.setGoal(new goals.GoalFollow(target, distance), true);
    context.progress("following", { player, distance });
    while (true) {
      await taskDelay(context, 1000);
      const current = this.bot.players[player]?.entity;
      if (!current) throw new Error(`follow target left visibility: ${player}`);
    }
  }

  async taskGoToPlayer(parameters, context) {
    const distance = optionalBoundedInt(parameters, "distance", 1, 16, 2);
    const { player, target } = this.visiblePlayer(parameters.player);
    context.progress("navigating", { player, distance });
    await this.bot.pathfinder.goto(new goals.GoalNear(
      Math.floor(target.position.x),
      Math.floor(target.position.y),
      Math.floor(target.position.z),
      distance,
    ));
    assertTaskActive(context);
    return { player, distance, position: this.positionFacts() };
  }

  async taskGoToPosition(parameters, context) {
    const x = boundedInt(parameters, "x", -WORLD_COORDINATE_LIMIT, WORLD_COORDINATE_LIMIT);
    const y = boundedInt(parameters, "y", -2048, 2048);
    const z = boundedInt(parameters, "z", -WORLD_COORDINATE_LIMIT, WORLD_COORDINATE_LIMIT);
    const distance = optionalBoundedInt(parameters, "distance", 0, 16, 1);
    context.progress("navigating", { target: { x, y, z }, distance });
    await this.bot.pathfinder.goto(new goals.GoalNear(x, y, z, distance));
    assertTaskActive(context);
    return { target: { x, y, z }, distance, position: this.positionFacts() };
  }

  async taskGatherBlock(parameters, context) {
    const block = resourceName(parameters, "block");
    const count = optionalBoundedInt(parameters, "count", 1, 16, 1);
    const maxDistance = optionalBoundedInt(parameters, "max_distance", 4, 64, 32);
    const mcData = minecraftData(this.bot.version);
    const blockType = mcData.blocksByName?.[shortResourceName(block)];
    if (!blockType) throw new Error(`unknown block identifier: ${block}`);
    const positions = this.bot.findBlocks({
      matching: blockType.id,
      maxDistance,
      count,
    });
    const targets = positions
      .map((position) => this.bot.blockAt(position))
      .filter((target) => target && Number(target.type) === Number(blockType.id));
    if (targets.length < count) {
      throw new Error(`found only ${targets.length}/${count} ${block} blocks`);
    }
    context.progress("collecting", { block, requested: count, found: targets.length });
    await this.bot.collectBlock.collect(targets);
    assertTaskActive(context);
    return { block, collected: targets.length, position: this.positionFacts() };
  }

  async taskCraftItem(parameters, context) {
    const item = resourceName(parameters, "item");
    const count = optionalBoundedInt(parameters, "count", 1, 64, 1);
    const mcData = minecraftData(this.bot.version);
    const itemType = mcData.itemsByName?.[shortResourceName(item)];
    if (!itemType) throw new Error(`unknown item identifier: ${item}`);
    let table = null;
    let recipes = this.bot.recipesFor(itemType.id, null, count, null);
    if (!recipes.length) {
      const tableType = mcData.blocksByName?.crafting_table;
      table = tableType ? this.bot.findBlock({
        matching: tableType.id,
        maxDistance: 16,
      }) : null;
      if (!table) throw new Error(`no available recipe or crafting table for ${item}`);
      context.progress("approaching_crafting_table", {
        x: table.position.x,
        y: table.position.y,
        z: table.position.z,
      });
      await this.bot.pathfinder.goto(new goals.GoalNear(
        table.position.x,
        table.position.y,
        table.position.z,
        2,
      ));
      assertTaskActive(context);
      recipes = this.bot.recipesFor(itemType.id, null, count, table);
    }
    const recipe = recipes[0];
    if (!recipe) throw new Error(`inventory cannot satisfy a recipe for ${item}`);
    const resultCount = Math.max(1, Number(recipe.result?.count ?? 1));
    const craftOperations = Math.ceil(count / resultCount);
    context.progress("crafting", { item, count, craft_operations: craftOperations });
    await this.bot.craft(recipe, craftOperations, table);
    assertTaskActive(context);
    return { item, requested: count, craft_operations: craftOperations };
  }

  inventoryItem(resource) {
    const shortName = shortResourceName(resource);
    const item = this.bot.inventory.items().find((candidate) => candidate.name === shortName);
    if (!item) throw new Error(`item is not present in inventory: ${resource}`);
    return item;
  }

  async taskPlaceBlock(parameters, context) {
    const itemName = resourceName(parameters, "item");
    const x = boundedInt(parameters, "reference_x", -WORLD_COORDINATE_LIMIT, WORLD_COORDINATE_LIMIT);
    const y = boundedInt(parameters, "reference_y", -2048, 2048);
    const z = boundedInt(parameters, "reference_z", -WORLD_COORDINATE_LIMIT, WORLD_COORDINATE_LIMIT);
    const faceX = boundedInt(parameters, "face_x", -1, 1);
    const faceY = boundedInt(parameters, "face_y", -1, 1);
    const faceZ = boundedInt(parameters, "face_z", -1, 1);
    if (Math.abs(faceX) + Math.abs(faceY) + Math.abs(faceZ) !== 1) {
      throw new Error("placement face must contain exactly one unit axis");
    }
    const reference = this.bot.blockAt(new Vec3(x, y, z));
    if (!reference) throw new Error("placement reference block is not loaded");
    context.progress("approaching_placement", { reference: { x, y, z } });
    await this.bot.pathfinder.goto(new goals.GoalNear(x, y, z, 3));
    assertTaskActive(context);
    await this.bot.equip(this.inventoryItem(itemName), "hand");
    const face = new Vec3(faceX, faceY, faceZ);
    await this.bot.placeBlock(reference, face);
    assertTaskActive(context);
    return {
      item: itemName,
      placed_at: { x: x + faceX, y: y + faceY, z: z + faceZ },
    };
  }

  async taskEatItem(parameters, context) {
    const itemName = resourceName(parameters, "item");
    context.progress("eating", { item: itemName });
    await this.bot.equip(this.inventoryItem(itemName), "hand");
    assertTaskActive(context);
    await this.bot.consume();
    assertTaskActive(context);
    return { item: itemName, food: Number(this.bot.food ?? 0) };
  }

  pulseAttack() {
    this.requireWorld();
    const target = this.nearestAttackableEntity();
    if (target) {
      this.bot.attack(target);
    }
    const facts = this.positionFacts();
    facts.control_pulsed = "attack";
    facts.target_entity_id = target ? Number(target.id) : null;
    return facts;
  }

  async pulseUse() {
    this.requireWorld();
    const block = this.bot.blockAtCursor?.(4.5);
    if (block) {
      await this.bot.activateBlock(block).catch((error) => {
        throw new Error(`use activation failed: ${error.message}`);
      });
    }
    const facts = this.positionFacts();
    facts.control_pulsed = "use";
    facts.target_block = block ? String(block.name ?? "") : null;
    return facts;
  }

  selectHotbar(parameters) {
    this.requireWorld();
    const slot = boundedInt(parameters, "slot", 0, 8);
    this.bot.setQuickBarSlot(slot);
    const facts = this.positionFacts();
    facts.selected_hotbar_slot = slot;
    return facts;
  }

  pulseDrop() {
    this.requireWorld();
    this.dropSelectedItem();
    const facts = this.positionFacts();
    facts.control_pulsed = "drop";
    return facts;
  }

  observationWait() {
    this.requireWorld();
    const facts = this.positionFacts();
    facts.observation_wait_dispatched = true;
    return facts;
  }

  sendChat(parameters) {
    this.requireWorld();
    const message = requiredString(parameters, "message");
    if (message.length > MAX_CHAT_MESSAGE_LENGTH) {
      throw new Error(`message must not exceed ${MAX_CHAT_MESSAGE_LENGTH} characters`);
    }
    this.bot.chat(message);
    this.emitEvent("minecraft.chat.sent", { message });
    return { message_dispatched: message };
  }

  respawn() {
    this.requireWorld();
    if (Number(this.bot.health ?? 0) > 0) {
      throw new Error("Player is not dead");
    }
    this.bot.respawn();
    return { respawn_dispatched: true };
  }

  /** Release every held control and stop pathing for one explicit reason. */
  async cancelHighLevelTask(_taskId, reason) {
    if (this.bot) {
      try {
        await this.bot.collectBlock?.cancelTask?.();
      } catch {
        // The plugin may already have completed its target queue.
      }
      try {
        this.bot.stopDigging?.();
      } catch {
        // A disconnected body cannot retain a digging action.
      }
      for (const name of HELD_CONTROL_NAMES) {
        try {
          this.bot.setControlState(name, false);
        } catch {
          // A disconnected body cannot hold controls; cleanup continues.
        }
      }
      try {
        this.bot.pathfinder.setGoal(null);
      } catch {
        // Pathfinder state is best-effort once the world is gone.
      }
    }
    this.cancelPendingMine(reason);
  }

  async releaseAll(reason) {
    const task = await this.taskEngine.stop(reason);
    await this.cancelHighLevelTask("", reason);
    return { controls_released: true, reason, ...task };
  }

  /** Interrupt one intention: stop pathing and release every held control. */
  async interrupt(intentId, reason) {
    await this.releaseAll(reason);
    this.log(`interrupted intent ${intentId}: ${reason}`);
  }
}

export { MINECRAFT_TASK_KINDS };

function entityTypeName(entity) {
  if (entity.name) {
    return entity.name.includes(":") ? entity.name : `minecraft:${entity.name}`;
  }
  if (entity.kind) {
    return String(entity.kind);
  }
  return String(entity.objectType ?? "unknown");
}

function stackFacts(index, item) {
  if (!item) {
    return { slot: index, item: "minecraft:air", count: 0, max_count: 0 };
  }
  return {
    slot: index,
    item: item.name.includes(":") ? item.name : `minecraft:${item.name}`,
    count: Number(item.count),
    max_count: Number(item.stackSize ?? 64),
  };
}
