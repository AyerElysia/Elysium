// Exclusive, idempotent high-level task ownership for the Mineflayer body.

import { createHash } from "node:crypto";

export const MINECRAFT_TASK_KINDS = Object.freeze([
  "follow_player",
  "go_to_player",
  "go_to_position",
  "gather_block",
  "craft_item",
  "place_block",
  "eat_item",
]);

const TERMINAL = new Set(["completed", "failed", "cancelled"]);
const TASK_ID = /^[A-Za-z0-9_.:-]{1,160}$/;
const MAX_RECORDS = 128;
const CANCEL_GRACE_MS = 3000;
const DEFAULT_TASK_TIMEOUT_MS = 180_000;
const MIN_TASK_TIMEOUT_SECONDS = 5;
const MAX_TASK_TIMEOUT_SECONDS = 600;

function now() { return new Date().toISOString(); }

function canonical(value) {
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.keys(value).sort().map((key) => (
      `${JSON.stringify(key)}:${canonical(value[key])}`
    )).join(",")}}`;
  }
  return JSON.stringify(value);
}

function digest(kind, args, timeoutMs) {
  return createHash("sha256")
    .update(canonical({
      kind,
      arguments: args,
      max_duration_seconds: timeoutMs / 1000,
    }))
    .digest("hex");
}

function requiredTaskId(parameters) {
  const raw = parameters?.task_id;
  if (typeof raw !== "string") {
    throw new Error("task_id must be 1-160 safe identifier characters");
  }
  const value = raw.trim();
  if (!TASK_ID.test(value)) {
    throw new Error("task_id must be 1-160 safe identifier characters");
  }
  return value;
}

function sleep(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function taskTimeoutMs(parameters, defaultTimeoutMs) {
  if (parameters?.max_duration_seconds === undefined) {
    return defaultTimeoutMs;
  }
  const seconds = parameters.max_duration_seconds;
  if (
    typeof seconds !== "number"
    || !Number.isFinite(seconds)
    || seconds < MIN_TASK_TIMEOUT_SECONDS
    || seconds > MAX_TASK_TIMEOUT_SECONDS
  ) {
    throw new Error(
      "max_duration_seconds must be "
      + MIN_TASK_TIMEOUT_SECONDS
      + ".."
      + MAX_TASK_TIMEOUT_SECONDS,
    );
  }
  return Math.round(seconds * 1000);
}

/** One body gate. High-level work never competes for pathfinder or controls. */
export class MinecraftTaskEngine {
  constructor(body, emit = () => {}, options = {}) {
    const configuredTimeout = Number(
      options.defaultTaskTimeoutMs ?? DEFAULT_TASK_TIMEOUT_MS,
    );
    if (!Number.isFinite(configuredTimeout) || configuredTimeout <= 0) {
      throw new Error("defaultTaskTimeoutMs must be positive");
    }
    this.body = body;
    this.emit = emit;
    this.defaultTaskTimeoutMs = configuredTimeout;
    this.records = new Map();
    this.active = null;
    this.generation = 0;
  }

  snapshot() {
    return {
      active: this.active ? this._public(this.active) : null,
      recent_terminal: [...this.records.values()]
        .filter((record) => TERMINAL.has(record.status))
        .slice(-8)
        .map((record) => this._public(record)),
    };
  }

  async start(parameters) {
    const taskId = requiredTaskId(parameters);
    if (
      parameters?.replace_current !== undefined
      && typeof parameters.replace_current !== "boolean"
    ) {
      throw new Error("replace_current must be boolean");
    }
    const kind = String(parameters?.kind ?? "").trim();
    if (!MINECRAFT_TASK_KINDS.includes(kind)) {
      throw new Error(`unsupported high-level task kind: ${kind || "empty"}`);
    }
    const args = parameters?.arguments ?? {};
    if (!args || typeof args !== "object" || Array.isArray(args)) {
      throw new Error("task arguments must be a JSON object");
    }
    const timeoutMs = taskTimeoutMs(parameters, this.defaultTaskTimeoutMs);
    const payloadDigest = digest(kind, args, timeoutMs);
    const existing = this.records.get(taskId);
    if (existing) {
      if (existing.digest !== payloadDigest) {
        throw new Error("task_id was already used for another task payload");
      }
      return { ...this._public(existing), replayed: true };
    }
    if (this.active) {
      if (parameters?.replace_current !== true) {
        throw new Error(
          `body gate is occupied by task ${this.active.taskId}; `
          + "set replace_current=true to replace it",
        );
      }
      await this.cancel(
        { task_id: this.active.taskId },
        `superseded by ${taskId}`,
      );
    }

    const record = {
      taskId,
      kind,
      args: { ...args },
      digest: payloadDigest,
      generation: ++this.generation,
      status: "accepted",
      phase: "accepted",
      acceptedAt: now(),
      startedAt: "",
      finishedAt: "",
      error: "",
      result: {},
      cancelReason: "",
      timeoutMs,
      timeoutHandle: null,
      timedOut: false,
      controller: new AbortController(),
      promise: null,
    };
    this.records.set(taskId, record);
    this.active = record;
    this._trim();
    try {
      this._emit(record, "accepted", {
        replace_current: parameters?.replace_current === true,
      });
    } catch (error) {
      if (this.active === record) this.active = null;
      this.records.delete(taskId);
      record.controller.abort("accepted task event delivery failed");
      throw error;
    }
    record.promise = this._run(record);
    return { ...this._public(record), task_accepted: true, replayed: false };
  }

  async cancel(parameters, reason = "task cancellation requested") {
    const taskId = requiredTaskId(parameters);
    const record = this.records.get(taskId);
    if (!record) throw new Error(`unknown task_id: ${taskId}`);
    if (TERMINAL.has(record.status)) {
      return { ...this._public(record), replayed: true };
    }
    if (this.active !== record) {
      throw new Error(`task ${taskId} does not own the body gate`);
    }
    record.cancelReason = String(reason || "task cancellation requested").slice(0, 240);
    record.status = "cancelling";
    record.phase = "releasing_body";
    record.controller.abort(record.cancelReason);
    await this.body.cancelHighLevelTask?.(record.taskId, record.cancelReason);
    if (record.promise) {
      await Promise.race([record.promise, sleep(CANCEL_GRACE_MS)]);
    }
    if (this.active === record) {
      throw new Error(`task ${taskId} cancellation exceeded ${CANCEL_GRACE_MS}ms`);
    }
    return this._public(record);
  }

  status(parameters = {}) {
    const rawTaskId = parameters?.task_id;
    if (rawTaskId !== undefined && typeof rawTaskId !== "string") {
      throw new Error("task_id must be 1-160 safe identifier characters");
    }
    const taskId = String(rawTaskId ?? "").trim();
    if (!taskId) return this.snapshot();
    if (!TASK_ID.test(taskId)) {
      throw new Error("task_id must be 1-160 safe identifier characters");
    }
    const record = this.records.get(taskId);
    if (!record) throw new Error(`unknown task_id: ${taskId}`);
    return this._public(record);
  }

  async stop(reason) {
    if (!this.active) {
      await this.body.cancelHighLevelTask?.("", reason);
      return { task_cancelled: false };
    }
    const result = await this.cancel(
      { task_id: this.active.taskId },
      reason,
    );
    return { task_cancelled: true, task: result };
  }

  ownsBody() { return this.active !== null; }

  async _run(record) {
    await Promise.resolve();
    if (this.active !== record || record.controller.signal.aborted) {
      this._cancelled(record);
      return;
    }
    try {
      record.status = "running";
      record.phase = "running";
      record.startedAt = now();
      this._emit(record, "progress", { phase: "running" });
      const progress = (phase, facts = {}) => {
        if (this.active !== record || record.controller.signal.aborted) return;
        record.phase = String(phase || "running").slice(0, 120);
        this._emit(record, "progress", { phase: record.phase, facts });
      };
      const execution = this.body.runHighLevelTask(
        record.kind,
        record.args,
        {
          taskId: record.taskId,
          generation: record.generation,
          signal: record.controller.signal,
          progress,
          ownsBody: () => this.active === record,
        },
      );
      const deadline = new Promise((_, reject) => {
        record.timeoutHandle = setTimeout(() => {
          if (TERMINAL.has(record.status)) return;
          record.timedOut = true;
          record.cancelReason = (
            "technical task deadline exceeded after "
            + record.timeoutMs / 1000
            + "s"
          );
          record.controller.abort(record.cancelReason);
          Promise.resolve(
            this.body.cancelHighLevelTask?.(record.taskId, record.cancelReason),
          ).catch(() => {});
          reject(new Error(record.cancelReason));
        }, record.timeoutMs);
      });
      const result = await Promise.race([execution, deadline]);
      if (record.controller.signal.aborted || this.active !== record) {
        this._cancelled(record);
        return;
      }
      record.status = "completed";
      record.phase = "completed";
      record.finishedAt = now();
      record.result = result && typeof result === "object" ? { ...result } : {};
      this.active = null;
      this._emit(record, "completed", { result: record.result });
    } catch (error) {
      if (record.timedOut) {
        record.status = "failed";
        record.phase = "timed_out";
        record.finishedAt = now();
        record.error = record.cancelReason;
        if (this.active === record) this.active = null;
        this._emit(record, "failed", {
          error: record.error,
          timeout_seconds: record.timeoutMs / 1000,
        });
        return;
      }
      if (record.controller.signal.aborted) {
        this._cancelled(record);
        return;
      }
      record.status = "failed";
      record.phase = "failed";
      record.finishedAt = now();
      record.error = String(error?.message ?? error ?? "unknown task failure").slice(0, 512);
      if (this.active === record) this.active = null;
      this._emit(record, "failed", { error: record.error });
    } finally {
      if (record.timeoutHandle) {
        clearTimeout(record.timeoutHandle);
        record.timeoutHandle = null;
      }
    }
  }

  _cancelled(record) {
    if (TERMINAL.has(record.status)) return;
    record.status = "cancelled";
    record.phase = "cancelled";
    record.finishedAt = now();
    if (this.active === record) this.active = null;
    this._emit(record, "cancelled", { reason: record.cancelReason });
  }

  _emit(record, transition, detail) {
    this.emit(`minecraft.task.${transition}`, {
      task: this._public(record),
      ...detail,
    });
  }

  _public(record) {
    return {
      task_id: record.taskId,
      kind: record.kind,
      generation: record.generation,
      status: record.status,
      phase: record.phase,
      accepted_at: record.acceptedAt,
      started_at: record.startedAt || null,
      finished_at: record.finishedAt || null,
      max_duration_seconds: record.timeoutMs / 1000,
      arguments_sha256: record.digest,
      error: record.error || null,
      result: { ...record.result },
    };
  }

  _trim() {
    for (const [taskId, record] of this.records) {
      if (this.records.size <= MAX_RECORDS) break;
      if (record !== this.active && TERMINAL.has(record.status)) {
        this.records.delete(taskId);
      }
    }
  }
}
