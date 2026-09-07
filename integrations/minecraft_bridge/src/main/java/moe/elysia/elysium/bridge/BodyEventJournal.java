package moe.elysia.elysium.bridge;

import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import java.nio.charset.StandardCharsets;
import java.time.Instant;
import java.util.ArrayDeque;
import java.util.LinkedHashSet;
import java.util.UUID;

/** Stable FIFO occurrences retained until the authenticated controller ACKs. */
final class BodyEventJournal {
    private static final int MAX_EVENT_BYTES = 16384;
    private final String instanceId;
    private final int capacity;
    private final ArrayDeque<JsonObject> pending = new ArrayDeque<>();
    private long sequence;
    private final LinkedHashSet<String> acknowledged = new LinkedHashSet<>();

    BodyEventJournal(String instanceId, int capacity) {
        if (instanceId == null || instanceId.isBlank() || capacity < 1) {
            throw new IllegalArgumentException("invalid event journal identity or capacity");
        }
        this.instanceId = instanceId;
        this.capacity = capacity;
    }

    synchronized JsonObject publish(String kind, JsonObject payload) {
        if (kind == null || !kind.startsWith("minecraft.") || payload == null) {
            throw new IllegalArgumentException("invalid Minecraft event");
        }
        if (pending.size() >= capacity) {
            throw new IllegalStateException("MinecraftBodyEventJournalCapacityExceeded");
        }
        JsonObject event = new JsonObject();
        event.addProperty("event_id", "minecraft_event_" + UUID.randomUUID());
        event.addProperty("instance_id", instanceId);
        event.addProperty("sequence", sequence + 1);
        event.addProperty("occurred_at", Instant.now().toString());
        event.addProperty("source", "neoforge-agent");
        event.addProperty("kind", kind);
        event.add("payload", payload.deepCopy());
        if (event.toString().getBytes(StandardCharsets.UTF_8).length > MAX_EVENT_BYTES) {
            throw new IllegalArgumentException("MinecraftBodyEventTooLarge");
        }
        pending.addLast(event);
        sequence++;
        return event.deepCopy();
    }

    synchronized void acknowledge(String eventId) {
        if (acknowledged.contains(eventId)) return;
        JsonObject head = pending.peekFirst();
        if (head == null || !head.get("event_id").getAsString().equals(eventId)) {
            throw new IllegalArgumentException("event_ack must match oldest pending occurrence");
        }
        pending.removeFirst();
        acknowledged.add(eventId);
        while (acknowledged.size() > capacity) {
            acknowledged.remove(acknowledged.iterator().next());
        }
    }

    synchronized JsonArray snapshot() {
        JsonArray result = new JsonArray();
        pending.forEach(event -> result.add(event.deepCopy()));
        return result;
    }

    synchronized int size() { return pending.size(); }
}
