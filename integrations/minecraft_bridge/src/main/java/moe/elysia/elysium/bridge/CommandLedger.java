package moe.elysia.elysium.bridge;

import com.google.gson.Gson;
import com.google.gson.JsonArray;
import com.google.gson.JsonElement;
import com.google.gson.JsonNull;
import com.google.gson.JsonObject;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** Bounded command replay protection shared across controller reconnects. */
final class CommandLedger {
    private static final Gson GSON = new Gson();

    enum DecisionKind {
        NEW,
        PENDING_REPLAY,
        TERMINAL_REPLAY,
        CONFLICT
    }

    record Decision(DecisionKind kind, JsonObject terminalReceipt) {}

    private final int maximumTerminalEntries;
    private final LinkedHashMap<String, Entry> entries = new LinkedHashMap<>(16, 0.75f, true);

    CommandLedger(int maximumTerminalEntries) {
        if (maximumTerminalEntries < 1) {
            throw new IllegalArgumentException("maximumTerminalEntries must be positive");
        }
        this.maximumTerminalEntries = maximumTerminalEntries;
    }

    /** Reserve a command identifier, or identify a safe replay/conflict. */
    synchronized Decision begin(String commandId, JsonObject command) {
        String fingerprint = fingerprint(command);
        Entry existing = entries.get(commandId);
        if (existing == null) {
            entries.put(commandId, new Entry(fingerprint, null));
            pruneTerminalEntries();
            return new Decision(DecisionKind.NEW, null);
        }
        if (!MessageDigest.isEqual(
                existing.fingerprint.getBytes(StandardCharsets.US_ASCII),
                fingerprint.getBytes(StandardCharsets.US_ASCII))) {
            return new Decision(DecisionKind.CONFLICT, null);
        }
        if (existing.terminalReceipt == null) {
            return new Decision(DecisionKind.PENDING_REPLAY, null);
        }
        return new Decision(DecisionKind.TERMINAL_REPLAY, existing.terminalReceipt.deepCopy());
    }

    /** Store the exact terminal receipt returned by a successfully reserved command. */
    synchronized void complete(String commandId, JsonObject terminalReceipt) {
        Entry existing = entries.get(commandId);
        if (existing == null) {
            throw new IllegalStateException("command was not reserved: " + commandId);
        }
        existing.terminalReceipt = terminalReceipt.deepCopy();
        pruneTerminalEntries();
    }

    synchronized int size() {
        return entries.size();
    }

    /** Keep pending work and only evict the least recently used terminal receipts. */
    private void pruneTerminalEntries() {
        int terminalCount = 0;
        for (Entry entry : entries.values()) {
            if (entry.terminalReceipt != null) {
                terminalCount++;
            }
        }
        if (terminalCount <= maximumTerminalEntries) {
            return;
        }
        var iterator = entries.entrySet().iterator();
        while (iterator.hasNext() && terminalCount > maximumTerminalEntries) {
            Map.Entry<String, Entry> candidate = iterator.next();
            if (candidate.getValue().terminalReceipt != null) {
                iterator.remove();
                terminalCount--;
            }
        }
    }

    private static String fingerprint(JsonObject command) {
        String canonical = GSON.toJson(canonicalize(command));
        try {
            MessageDigest digest = MessageDigest.getInstance("SHA-256");
            return HexFormat.of().formatHex(digest.digest(canonical.getBytes(StandardCharsets.UTF_8)));
        } catch (NoSuchAlgorithmException exception) {
            throw new IllegalStateException("SHA-256 is unavailable", exception);
        }
    }

    private static JsonElement canonicalize(JsonElement value) {
        if (value == null || value.isJsonNull()) {
            return JsonNull.INSTANCE;
        }
        if (value.isJsonArray()) {
            JsonArray array = new JsonArray();
            for (JsonElement element : value.getAsJsonArray()) {
                array.add(canonicalize(element));
            }
            return array;
        }
        if (value.isJsonObject()) {
            JsonObject object = new JsonObject();
            List<String> keys = new ArrayList<>(value.getAsJsonObject().keySet());
            keys.sort(String::compareTo);
            for (String key : keys) {
                object.add(key, canonicalize(value.getAsJsonObject().get(key)));
            }
            return object;
        }
        return value.deepCopy();
    }

    private static final class Entry {
        private final String fingerprint;
        private JsonObject terminalReceipt;

        private Entry(String fingerprint, JsonObject terminalReceipt) {
            this.fingerprint = fingerprint;
            this.terminalReceipt = terminalReceipt;
        }
    }
}
