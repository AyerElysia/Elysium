package moe.elysia.elysium.bridge;

import static org.junit.jupiter.api.Assertions.*;
import com.google.gson.JsonObject;
import org.junit.jupiter.api.Test;

final class BodyEventJournalTest {
    @Test void replayPreservesExactOccurrenceAndFifoAcknowledgement() {
        BodyEventJournal journal = new BodyEventJournal("native-one", 2);
        JsonObject payload = new JsonObject();
        payload.addProperty("message", "原文不能被重写");
        JsonObject first = journal.publish("minecraft.chat.received", payload);
        JsonObject second = journal.publish("minecraft.task.progress", new JsonObject());
        assertEquals(first, journal.snapshot().get(0));
        payload.addProperty("message", "later change");
        assertEquals(first, journal.snapshot().get(0));
        assertThrows(IllegalArgumentException.class,
                () -> journal.acknowledge(second.get("event_id").getAsString()));
        journal.acknowledge(first.get("event_id").getAsString());
        journal.acknowledge(first.get("event_id").getAsString());
        assertEquals(1, journal.size());
        assertEquals(second, journal.snapshot().get(0));
    }

    @Test void capacityAndUtf8LimitsNeverEvictUnacknowledgedEventsOrAdvanceSequence() {
        BodyEventJournal journal = new BodyEventJournal("native-one", 1);
        JsonObject huge = new JsonObject();
        huge.addProperty("message", "汉".repeat(6000));
        assertThrows(IllegalArgumentException.class,
                () -> journal.publish("minecraft.chat.received", huge));
        JsonObject first = journal.publish("minecraft.chat.received", new JsonObject());
        assertEquals(1, first.get("sequence").getAsInt());
        assertThrows(IllegalStateException.class,
                () -> journal.publish("minecraft.chat.received", new JsonObject()));
        assertEquals(first, journal.snapshot().get(0));
        journal.acknowledge(first.get("event_id").getAsString());
        assertEquals(2, journal.publish("minecraft.chat.received", new JsonObject())
                .get("sequence").getAsInt());
    }
}
