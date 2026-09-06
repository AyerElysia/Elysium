package moe.elysia.elysium.bridge;

import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import java.time.Instant;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Set;
import java.util.function.BiConsumer;
import java.util.function.LongSupplier;
import java.util.regex.Pattern;

/** Tick-driven single-owner tasks; choosing a task remains the scene's decision. */
final class NativeTaskEngine {
    static final Set<String> KINDS = Set.of(
            "follow_player", "go_to_player", "go_to_position", "gather_block");
    private static final Pattern TASK_ID = Pattern.compile("[A-Za-z0-9_.:-]{1,160}");
    private static final int MAX_RECORDS = 128;

    interface Backend {
        void validate(String kind, JsonObject arguments);
        void start(String kind, JsonObject arguments);
        Progress poll();
        void stop(String reason);
    }

    record Progress(boolean completed, String phase, JsonObject facts) {}

    private final Backend backend;
    private final BiConsumer<String, JsonObject> emit;
    private final LongSupplier clock;
    private final LinkedHashMap<String, Task> records = new LinkedHashMap<>();
    private Task active;
    private long generation;

    NativeTaskEngine(Backend backend, BiConsumer<String, JsonObject> emit) {
        this(backend, emit, () -> System.nanoTime() / 1_000_000L);
    }

    NativeTaskEngine(Backend backend, BiConsumer<String, JsonObject> emit, LongSupplier clock) {
        this.backend = backend;
        this.emit = emit;
        this.clock = clock;
    }

    boolean ownsBody() { return active != null; }

    JsonObject start(JsonObject parameters) {
        String id = taskId(parameters);
        String kind = OperationContracts.requiredString(parameters, "kind");
        if (!KINDS.contains(kind)) {
            throw new IllegalArgumentException("unsupported high-level task: " + kind);
        }
        boolean replace = false;
        if (parameters.has("replace_current")) {
            var value = parameters.get("replace_current");
            if (!value.isJsonPrimitive() || !value.getAsJsonPrimitive().isBoolean()) {
                throw new IllegalArgumentException("replace_current must be boolean");
            }
            replace = value.getAsBoolean();
        }
        JsonObject arguments = new JsonObject();
        if (parameters.has("arguments")) {
            if (!parameters.get("arguments").isJsonObject()) {
                throw new IllegalArgumentException("task arguments must be an object");
            }
            arguments = parameters.getAsJsonObject("arguments").deepCopy();
        }
        int seconds = parameters.has("max_duration_seconds")
                ? OperationContracts.boundedInt(parameters, "max_duration_seconds", 5, 600) : 180;
        JsonObject identity = new JsonObject();
        identity.addProperty("kind", kind);
        identity.add("arguments", arguments);
        identity.addProperty("max_duration_seconds", seconds);
        String digest = CommandLedger.fingerprint(identity);
        Task existing = records.get(id);
        if (existing != null) {
            if (!existing.digest.equals(digest)) {
                throw new IllegalArgumentException("task_id was already used for another payload");
            }
            JsonObject result = publicTask(existing);
            result.addProperty("replayed", true);
            return result;
        }
        backend.validate(kind, arguments);
        if (active != null) {
            if (active.pendingTerminal) {
                throw new IllegalStateException("body gate awaits terminal event persistence");
            }
            if (!replace) {
                throw new IllegalStateException("body gate is occupied by task " + active.id);
            }
            finish(active, "cancelled", "superseded by " + id, new JsonObject());
        }
        Task task = new Task(id, kind, digest, seconds, ++generation, clock.getAsLong());
        records.put(id, task);
        active = task;
        try {
            emit(task, "accepted");
        } catch (RuntimeException exception) {
            active = null;
            records.remove(id);
            throw exception;
        }
        try {
            backend.start(kind, arguments);
            task.status = "running";
            task.phase = "running";
            task.startedAt = Instant.now().toString();
            emit(task, "progress");
        } catch (RuntimeException exception) {
            finish(task, "failed", exception.getMessage(), new JsonObject());
        }
        trim();
        JsonObject result = publicTask(task);
        result.addProperty("task_accepted", true);
        result.addProperty("replayed", false);
        return result;
    }

    void tick() {
        Task task = active;
        if (task == null) return;
        if (task.pendingTerminal) {
            emit(task, task.status);
            task.pendingTerminal = false;
            active = null;
            return;
        }
        try {
            if (clock.getAsLong() - task.acceptedMillis >= task.seconds * 1000L) {
                task.phase = "timed_out";
                finish(task, "failed", "technical task deadline exceeded", new JsonObject());
                return;
            }
            Progress progress = backend.poll();
            task.result = progress.facts().deepCopy();
            if (progress.completed()) {
                finish(task, "completed", "", task.result);
            } else if (!task.phase.equals(progress.phase())
                    || clock.getAsLong() - task.lastProgressMillis >= 5000) {
                task.phase = progress.phase();
                task.lastProgressMillis = clock.getAsLong();
                emit(task, "progress");
            }
        } catch (RuntimeException exception) {
            if (!task.pendingTerminal) {
                finish(task, "failed", exception.getMessage(), task.result);
            }
        }
    }

    JsonObject cancel(JsonObject parameters, String reason) {
        Task task = records.get(taskId(parameters));
        if (task == null) throw new IllegalArgumentException("unknown task_id");
        if (task == active && !task.pendingTerminal) {
            finish(task, "cancelled", reason, task.result);
        }
        return publicTask(task);
    }

    void stop(String reason) {
        if (active != null && !active.pendingTerminal) {
            finish(active, "cancelled", reason, active.result);
        } else {
            if (active != null) backend.stop(reason);
        }
    }

    JsonObject status(JsonObject parameters) {
        if (!parameters.has("task_id")) return snapshot();
        Task task = records.get(taskId(parameters));
        if (task == null) throw new IllegalArgumentException("unknown task_id");
        return publicTask(task);
    }

    JsonObject snapshot() {
        JsonObject result = new JsonObject();
        result.add("active", active == null ? null : publicTask(active));
        JsonArray terminal = new JsonArray();
        var finished = records.values().stream().filter(task -> task != active).toList();
        finished.stream().skip(Math.max(0, finished.size() - 8))
                .forEach(task -> terminal.add(publicTask(task)));
        result.add("recent_terminal", terminal);
        return result;
    }

    private void finish(Task task, String status, String error, JsonObject facts) {
        if (error == null) error = "native executor failure without an error message";
        // Never release the gate if the native executor failed to stop.
        backend.stop(error.isBlank() ? status : error);
        task.status = status;
        if (!"timed_out".equals(task.phase)) task.phase = status;
        task.error = error;
        task.result = facts.deepCopy();
        task.finishedAt = Instant.now().toString();
        task.pendingTerminal = true;
        emit(task, status);
        task.pendingTerminal = false;
        if (active == task) active = null;
    }

    private void emit(Task task, String transition) {
        JsonObject payload = new JsonObject();
        payload.add("task", publicTask(task));
        emit.accept("minecraft.task." + transition, payload);
    }

    private static JsonObject publicTask(Task task) {
        JsonObject result = new JsonObject();
        result.addProperty("task_id", task.id);
        result.addProperty("kind", task.kind);
        result.addProperty("generation", task.generation);
        result.addProperty("status", task.status);
        result.addProperty("phase", task.phase);
        result.addProperty("accepted_at", task.acceptedAt);
        result.addProperty("started_at", task.startedAt);
        result.addProperty("finished_at", task.finishedAt);
        result.addProperty("max_duration_seconds", task.seconds);
        result.addProperty("arguments_sha256", task.digest);
        result.addProperty("error", task.error);
        result.add("result", task.result.deepCopy());
        return result;
    }

    private static String taskId(JsonObject parameters) {
        String id = OperationContracts.requiredString(parameters, "task_id");
        if (!TASK_ID.matcher(id).matches()) throw new IllegalArgumentException("invalid task_id");
        return id;
    }

    private void trim() {
        var iterator = records.entrySet().iterator();
        while (records.size() > MAX_RECORDS && iterator.hasNext()) {
            Map.Entry<String, Task> entry = iterator.next();
            if (entry.getValue() != active) iterator.remove();
        }
    }

    private static final class Task {
        final String id;
        final String kind;
        final String digest;
        final int seconds;
        final long generation;
        final long acceptedMillis;
        final String acceptedAt = Instant.now().toString();
        long lastProgressMillis;
        String status = "accepted";
        String phase = "accepted";
        String startedAt;
        String finishedAt;
        String error = "";
        JsonObject result = new JsonObject();
        boolean pendingTerminal;

        Task(String id, String kind, String digest, int seconds, long generation, long now) {
            this.id = id;
            this.kind = kind;
            this.digest = digest;
            this.seconds = seconds;
            this.generation = generation;
            this.acceptedMillis = now;
            this.lastProgressMillis = now;
        }
    }
}
