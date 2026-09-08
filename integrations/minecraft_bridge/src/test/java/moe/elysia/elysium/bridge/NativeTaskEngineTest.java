package moe.elysia.elysium.bridge;

import static org.junit.jupiter.api.Assertions.*;
import com.google.gson.JsonObject;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.atomic.AtomicLong;
import org.junit.jupiter.api.Test;

final class NativeTaskEngineTest {
    private static final class Backend implements NativeTaskEngine.Backend {
        int starts;
        int stops;
        boolean complete;
        boolean stopFails;
        @Override public void validate(String kind, JsonObject args) {}
        @Override public void start(String kind, JsonObject args) { starts++; }
        @Override public NativeTaskEngine.Progress poll() {
            JsonObject facts = new JsonObject();
            facts.addProperty("distance_to_target", complete ? 0 : 12);
            return new NativeTaskEngine.Progress(complete, "moving", facts);
        }
        @Override public void stop(String reason) {
            stops++;
            if (stopFails) throw new IllegalStateException("stop failed");
        }
    }

    private static JsonObject task(String id) {
        JsonObject value = new JsonObject();
        value.addProperty("task_id", id);
        value.addProperty("kind", "go_to_position");
        value.addProperty("max_duration_seconds", 5);
        return value;
    }

    @Test void exactReplayCannotExecuteTwiceAndConflictCannotTakeBody() {
        Backend body = new Backend();
        List<String> events = new ArrayList<>();
        NativeTaskEngine engine = new NativeTaskEngine(body, (kind, event) -> events.add(kind));
        engine.start(task("one"));
        assertTrue(engine.start(task("one")).get("replayed").getAsBoolean());
        JsonObject changed = task("one");
        changed.addProperty("kind", "follow_player");
        assertThrows(IllegalArgumentException.class, () -> engine.start(changed));
        assertThrows(IllegalStateException.class, () -> engine.start(task("two")));
        assertEquals(1, body.starts);
        assertEquals(List.of("minecraft.task.accepted", "minecraft.task.progress"), events);
    }

    @Test void completionRequiresWorldEvidenceAndStopsBeforeGateRelease() {
        Backend body = new Backend();
        NativeTaskEngine engine = new NativeTaskEngine(body, (kind, event) -> {});
        engine.start(task("one"));
        engine.tick();
        assertTrue(engine.ownsBody());
        body.complete = true;
        engine.tick();
        assertFalse(engine.ownsBody());
        assertEquals(1, body.stops);
        JsonObject status = engine.status(task("one"));
        assertEquals("completed", status.get("status").getAsString());
        assertEquals(0, status.getAsJsonObject("result").get("distance_to_target").getAsInt());
    }

    @Test void cancellationIsIdempotentAndReplacementStopsPreviousGeneration() {
        Backend body = new Backend();
        NativeTaskEngine engine = new NativeTaskEngine(body, (kind, event) -> {});
        engine.start(task("one"));
        JsonObject next = task("two");
        next.addProperty("replace_current", true);
        engine.start(next);
        assertEquals("cancelled", engine.status(task("one")).get("status").getAsString());
        assertEquals(2, body.starts);
        assertEquals(1, body.stops);
        engine.cancel(task("two"), "test");
        engine.cancel(task("two"), "repeat");
        engine.stop("repeat stop");
        assertEquals(2, body.stops);
        assertFalse(engine.ownsBody());
    }

    @Test void deadlineIsAnExplicitFailureWithControlCleanup() {
        Backend body = new Backend();
        AtomicLong clock = new AtomicLong();
        NativeTaskEngine engine = new NativeTaskEngine(body, (kind, event) -> {}, clock::get);
        engine.start(task("one"));
        clock.set(5001);
        engine.tick();
        assertEquals("failed", engine.status(task("one")).get("status").getAsString());
        assertEquals("timed_out", engine.status(task("one")).get("phase").getAsString());
        assertEquals(1, body.stops);
        assertFalse(engine.ownsBody());
    }

    @Test void acceptedEventFailureCannotStartPhysicalWork() {
        Backend body = new Backend();
        NativeTaskEngine engine = new NativeTaskEngine(body, (kind, event) -> {
            throw new IllegalStateException("journal full");
        });
        assertThrows(IllegalStateException.class, () -> engine.start(task("one")));
        assertEquals(0, body.starts);
        assertFalse(engine.ownsBody());
    }

    @Test void terminalEventRetryKeepsOriginalResultAndExclusiveGate() {
        Backend body = new Backend();
        boolean[] fail = {true};
        List<JsonObject> completed = new ArrayList<>();
        NativeTaskEngine engine = new NativeTaskEngine(body, (kind, event) -> {
            if (kind.equals("minecraft.task.completed")) {
                if (fail[0]) throw new IllegalStateException("journal full");
                completed.add(event.deepCopy());
            }
        });
        engine.start(task("one"));
        body.complete = true;
        engine.tick();
        assertTrue(engine.ownsBody());
        JsonObject next = task("two");
        next.addProperty("replace_current", true);
        assertThrows(IllegalStateException.class, () -> engine.start(next));
        fail[0] = false;
        engine.tick();
        assertFalse(engine.ownsBody());
        assertEquals(1, completed.size());
        assertEquals(1, body.stops);
        assertEquals("completed", completed.getFirst().getAsJsonObject("task")
                .get("status").getAsString());
    }

    @Test void failedPhysicalStopDoesNotReleaseOwnership() {
        Backend body = new Backend();
        NativeTaskEngine engine = new NativeTaskEngine(body, (kind, event) -> {});
        engine.start(task("one"));
        body.stopFails = true;
        assertThrows(IllegalStateException.class, () -> engine.cancel(task("one"), "cancel"));
        assertTrue(engine.ownsBody());
        body.stopFails = false;
        engine.cancel(task("one"), "retry");
        assertFalse(engine.ownsBody());
    }
}
