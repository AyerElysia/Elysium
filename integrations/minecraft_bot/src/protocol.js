// Body-side implementation of the elysium.minecraft.bridge/1 wire protocol.
//
// The body dials the controller listener (reverse connection, no inbound
// firewall rule), proves token knowledge with an HMAC-SHA256 nonce challenge,
// then publishes a contiguous observation stream and executes correlated
// commands. This module is transport and semantics only: it never decides
// what the body should do. Facts in, receipts out.

import { createHash, createHmac, randomBytes, timingSafeEqual } from "node:crypto";
import WebSocket from "ws";

export const BRIDGE_PROTOCOL = "elysium.minecraft.bridge/1";
export const BRIDGE_VERSION = "0.2.1";

const AUTHENTICATION_DEADLINE_MS = 5000;
const RECONNECT_DELAY_MS = 1000;
const MAX_TERMINAL_COMMAND_RECEIPTS = 1024;
const MAX_OUTBOUND_MESSAGES = 256;

function uuidIdentifier(prefix) {
  return `${prefix}_${randomBytes(16).toString("hex")}`;
}

function isoNow() {
  return new Date().toISOString();
}

function constantTimeEquals(expected, supplied) {
  const expectedBytes = Buffer.from(expected, "ascii");
  const suppliedBytes = Buffer.from(supplied, "ascii");
  return (
    expectedBytes.length === suppliedBytes.length &&
    timingSafeEqual(expectedBytes, suppliedBytes)
  );
}

function commandPayloadDigest(command) {
  return createHash("sha256").update(JSON.stringify(command)).digest("hex");
}

/** Bounded idempotence ledger mirroring the NeoForge bridge CommandLedger. */
class CommandLedger {
  constructor(limit) {
    this.limit = limit;
    this.entries = new Map();
  }

  /** Classify one inbound command: NEW, CONFLICT, PENDING_REPLAY, TERMINAL_REPLAY. */
  begin(commandId, command) {
    const digest = commandPayloadDigest(command);
    const existing = this.entries.get(commandId);
    if (existing) {
      if (existing.digest !== digest) {
        return { kind: "CONFLICT" };
      }
      if (existing.terminal) {
        return { kind: "TERMINAL_REPLAY", terminal: existing.terminal };
      }
      return { kind: "PENDING_REPLAY", entry: existing };
    }
    const entry = { digest, terminal: null, waiters: [] };
    this.entries.set(commandId, entry);
    this._evictIfNeeded();
    return { kind: "NEW", entry };
  }

  complete(commandId, terminalReceipt) {
    const entry = this.entries.get(commandId);
    if (!entry) {
      return;
    }
    entry.terminal = terminalReceipt;
    for (const waiter of entry.waiters.splice(0)) {
      waiter(terminalReceipt);
    }
  }

  /** Subscribe to the terminal receipt of an already pending command. */
  awaitPending(entry) {
    return new Promise((resolve) => {
      if (entry.terminal) {
        resolve(entry.terminal);
        return;
      }
      entry.waiters.push(resolve);
    });
  }

  _evictIfNeeded() {
    while (this.entries.size > this.limit) {
      const oldest = this.entries.keys().next().value;
      this.entries.delete(oldest);
    }
  }
}

/**
 * One outbound bridge connection with authentication and bounded replay.
 *
 * Handlers:
 *  - collectFacts(): returns the current factual snapshot (may be pre-world).
 *  - onCommand(operation, parameters): executes and returns dispatch facts.
 *  - onInterrupt(reason), onReleaseAll(reason): release held controls.
 */
export class BridgeBodyEndpoint {
  constructor(config, handlers, log = console.error) {
    this.config = config;
    this.handlers = handlers;
    this.log = log;
    this.socket = null;
    this.authenticated = false;
    this.stopped = false;
    this.sequence = 0;
    this.ledger = new CommandLedger(MAX_TERMINAL_COMMAND_RECEIPTS);
    this.reconnectTimer = null;
    this.authTimer = null;
    this.sendQueue = [];
    this.flushing = false;
    this.droppedObservations = 0;
  }

  start() {
    this.connect();
  }

  /** Stop reconnecting, drop the socket, and leave controls to the caller. */
  stop() {
    this.stopped = true;
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    if (this.authTimer) {
      clearTimeout(this.authTimer);
      this.authTimer = null;
    }
    const socket = this.socket;
    this.socket = null;
    this.authenticated = false;
    this.sendQueue.length = 0;
    if (socket && socket.readyState === WebSocket.OPEN) {
      socket.close(1000, "body stopping");
    }
  }

  /** Publish one factual snapshot with a contiguous connection-local sequence. */
  broadcastObservation(facts) {
    if (!this.socket || !this.authenticated) {
      return;
    }
    this.sequence += 1;
    const observation = {
      observation_id: uuidIdentifier("observation"),
      instance_id: this.config.instanceId,
      sequence: this.sequence,
      observed_at: isoNow(),
      source: this.config.bodyType,
      facts,
    };
    this.send({ type: "observation", observation });
  }

  currentSequence() {
    return this.sequence;
  }

  /** Return bounded transport facts suitable for the next observation. */
  transportFacts() {
    return {
      pending_messages: this.sendQueue.length,
      dropped_observations: this.droppedObservations,
    };
  }

  connect() {
    if (this.stopped || this.socket) {
      return;
    }
    let socket;
    try {
      socket = new WebSocket(this.config.bridgeUri);
    } catch (error) {
      this.log(`bridge socket creation failed: ${error.message}`);
      this.scheduleReconnect();
      return;
    }
    this.socket = socket;
    socket.addEventListener("open", () => this.onOpen(socket));
    socket.addEventListener("message", (event) => this.onMessage(socket, event));
    socket.addEventListener("close", () => this.onDisconnected("controller disconnected"));
    socket.addEventListener("error", (event) => {
      const detail = event?.message || "transport error";
      this.log(`bridge transport error: ${detail}`);
    });
  }

  scheduleReconnect() {
    if (this.stopped || this.reconnectTimer) {
      return;
    }
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      this.connect();
    }, RECONNECT_DELAY_MS);
  }

  onOpen(socket) {
    this.nonce = randomBytes(32).toString("base64url");
    this.authenticated = false;
    this.sequence = 0;
    this.authTimer = setTimeout(() => {
      if (this.socket === socket && !this.authenticated) {
        socket.close(1000, "authentication deadline expired");
      }
    }, AUTHENTICATION_DEADLINE_MS);
    const capabilities = [...this.config.capabilities].sort();
    this.sendOn(socket, {
      type: "hello",
      protocol: BRIDGE_PROTOCOL,
      body_type: this.config.bodyType,
      bridge_version: BRIDGE_VERSION,
      minecraft_version: this.config.minecraftVersion,
      nonce: this.nonce,
      instance_id: this.config.instanceId,
      capabilities,
    });
  }

  async onMessage(socket, event) {
    if (this.socket !== socket) {
      return;
    }
    let message;
    try {
      message = JSON.parse(String(event.data));
      if (!message || typeof message !== "object" || typeof message.type !== "string") {
        throw new Error("message must be a JSON object with a type");
      }
    } catch (error) {
      this.log(`rejecting invalid bridge message: ${error.message}`);
      socket.close(1000, "invalid bridge message");
      return;
    }
    if (!this.authenticated) {
      this.authenticate(socket, message);
      return;
    }
    try {
      switch (message.type) {
        case "command":
          await this.handleCommand(socket, message);
          break;
        case "interrupt":
          this.handleInterrupt(message);
          break;
        case "release_all":
          this.handlers.onReleaseAll?.(
            typeof message.reason === "string" ? message.reason : "release_all",
          );
          break;
        default:
          throw new Error(`unsupported message type: ${message.type}`);
      }
    } catch (error) {
      this.log(`rejecting invalid bridge message: ${error.message}`);
      socket.close(1000, "invalid bridge message");
    }
  }

  /** Verify the controller's HMAC challenge without leaking token details. */
  authenticate(socket, message) {
    const valid =
      message.type === "authenticate" &&
      message.protocol === BRIDGE_PROTOCOL &&
      typeof message.digest === "string";
    const digestMatches =
      valid &&
      constantTimeEquals(
        createHmac("sha256", this.config.token).update(this.nonce).digest("hex"),
        message.digest,
      );
    if (!valid || !digestMatches) {
      this.sendOn(socket, { type: "authentication", accepted: false });
      socket.close(1000, "authentication rejected");
      return;
    }
    this.authenticated = true;
    if (this.authTimer) {
      clearTimeout(this.authTimer);
      this.authTimer = null;
    }
    this.sendOn(socket, { type: "authentication", accepted: true });
    this.handlers.onAuthenticated?.();
  }

  /** Acknowledge one correlated command, then report its terminal receipt. */
  async handleCommand(socket, message) {
    const command = message.command;
    if (!command || typeof command !== "object") {
      throw new Error("missing command object");
    }
    const commandId = requiredText(command, "command_id");
    const intentId = requiredText(command, "intent_id");
    const operation = requiredText(command, "operation");
    const parameters =
      command.parameters && typeof command.parameters === "object"
        ? command.parameters
        : {};
    const decision = this.ledger.begin(commandId, command);
    switch (decision.kind) {
      case "CONFLICT":
        this.sendReceipt(receipt(
          commandId, intentId, false, true, false, {},
          "command_id was already used for another payload",
          this.currentSequence(),
        ));
        return;
      case "PENDING_REPLAY":
        this.sendReceipt(receipt(
          commandId, intentId, true, false, false, {}, null,
          this.currentSequence(),
        ));
        this.ledger.awaitPending(decision.entry).then((terminal) => {
          this.sendReceipt(terminal);
        });
        return;
      case "TERMINAL_REPLAY":
        this.sendReceipt(decision.terminal);
        return;
      case "NEW":
        break;
    }
    if (!this.config.capabilities.includes(operation)) {
      const rejected = receipt(
        commandId, intentId, false, true, false, {},
        `Unsupported operation: ${operation}`,
        this.currentSequence(),
      );
      this.ledger.complete(commandId, rejected);
      this.sendReceipt(rejected);
      return;
    }
    this.sendReceipt(receipt(
      commandId, intentId, true, false, false, {}, null, this.currentSequence(),
    ));
    let terminal;
    try {
      const facts = await this.handlers.onCommand(operation, parameters);
      terminal = receipt(
        commandId, intentId, true, true, false, facts ?? {}, null,
        this.currentSequence(),
      );
    } catch (error) {
      const facts = { exception_type: error?.constructor?.name ?? "Error" };
      terminal = receipt(
        commandId, intentId, true, true, false, facts,
        String(error?.message ?? error),
        this.currentSequence(),
      );
    }
    this.ledger.complete(commandId, terminal);
    this.sendReceipt(terminal);
  }

  handleInterrupt(message) {
    const intentId = requiredText(message, "intent_id");
    const reason = requiredText(message, "reason");
    this.handlers.onInterrupt?.(intentId, reason);
  }

  sendReceipt(receiptEnvelopePayload) {
    this.send({ type: "receipt", receipt: receiptEnvelopePayload });
  }

  onDisconnected(reason) {
    if (!this.socket) {
      return;
    }
    this.socket = null;
    this.authenticated = false;
    this.sendQueue.length = 0;
    if (this.authTimer) {
      clearTimeout(this.authTimer);
      this.authTimer = null;
    }
    this.handlers.onDisconnected?.(reason);
    this.scheduleReconnect();
  }

  /** Serialize one complete JSON text frame without interleaving. */
  send(message) {
    const socket = this.socket;
    if (!socket || this.authenticated === false && message.type !== "hello"
        && message.type !== "authentication") {
      return;
    }
    this.sendOn(socket, message);
  }

  sendOn(socket, message) {
    const payload = JSON.stringify(message);
    if (this.sendQueue.length >= MAX_OUTBOUND_MESSAGES) {
      const observationIndex = this.sendQueue.findIndex(
        (queued) => queued.type === "observation",
      );
      if (observationIndex >= 0) {
        this.sendQueue.splice(observationIndex, 1);
        this.droppedObservations += 1;
      } else if (message.type === "observation") {
        this.droppedObservations += 1;
        return;
      } else {
        socket.close(1013, "bridge outbound queue is saturated");
        return;
      }
    }
    this.sendQueue.push({ payload, type: message.type });
    if (!this.flushing) {
      this.flush(socket);
    }
  }

  async flush(socket) {
    this.flushing = true;
    try {
      while (this.sendQueue.length > 0) {
        const queued = this.sendQueue.shift();
        if (socket.readyState !== WebSocket.OPEN) {
          this.sendQueue.length = 0;
          return;
        }
        await new Promise((resolve, reject) => {
          socket.send(queued.payload, (error) => {
            if (error) {
              reject(error);
            } else {
              resolve();
            }
          });
        });
      }
    } catch (error) {
      this.log(`bridge outbound send failed: ${error.message}`);
      this.sendQueue.length = 0;
      try {
        socket.close(1000, "controller outbound transport failed");
      } catch {
        // Already closing; the close handler drives reconnect.
      }
    } finally {
      this.flushing = false;
    }
  }
}

function requiredText(value, name) {
  const field = value?.[name];
  if (typeof field !== "string" || field.trim() === "") {
    throw new Error(`missing non-empty field: ${name}`);
  }
  return field;
}

/** Construct one acknowledgement or terminal action receipt payload. */
function receipt(
  commandId,
  intentId,
  accepted,
  completed,
  interrupted,
  facts,
  error,
  observationSequence,
) {
  const payload = {
    receipt_id: uuidIdentifier("receipt"),
    command_id: commandId,
    intent_id: intentId,
    accepted,
    completed,
    interrupted,
    recorded_at: isoNow(),
    facts,
  };
  if (error !== null && error !== undefined) {
    payload.error = error;
  }
  payload.observation_sequence = observationSequence;
  return payload;
}
