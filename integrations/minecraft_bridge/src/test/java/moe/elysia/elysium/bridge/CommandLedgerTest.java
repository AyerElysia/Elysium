package moe.elysia.elysium.bridge;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertThrows;

import com.google.gson.JsonObject;
import org.junit.jupiter.api.Test;

final class CommandLedgerTest {
    @Test
    void duplicatePendingCommandIsNotExecutedTwice() {
        CommandLedger ledger = new CommandLedger(8);
        JsonObject command = command("command_1", "navigation.goto", 1, 2);

        assertEquals(CommandLedger.DecisionKind.NEW, ledger.begin("command_1", command).kind());
        assertEquals(
                CommandLedger.DecisionKind.PENDING_REPLAY,
                ledger.begin("command_1", command.deepCopy()).kind());
    }

    @Test
    void pendingReplayReceivesTheOriginalTerminalReceipt() {
        CommandLedger ledger = new CommandLedger(8);
        JsonObject command = command("command_1", "navigation.goto", 1, 2);
        ledger.begin("command_1", command);
        CommandLedger.Decision replay = ledger.begin("command_1", command.deepCopy());
        JsonObject receipt = new JsonObject();
        receipt.addProperty("receipt_id", "receipt_original");

        ledger.complete("command_1", receipt);

        assertEquals(
                "receipt_original",
                replay.pendingCompletion().join().get("receipt_id").getAsString());
    }

    @Test
    void terminalReceiptIsReplayedExactly() {
        CommandLedger ledger = new CommandLedger(8);
        JsonObject command = command("command_1", "world.mine", 1, 2);
        ledger.begin("command_1", command);
        JsonObject receipt = new JsonObject();
        receipt.addProperty("receipt_id", "receipt_original");
        ledger.complete("command_1", receipt);

        CommandLedger.Decision replay = ledger.begin("command_1", command);

        assertEquals(CommandLedger.DecisionKind.TERMINAL_REPLAY, replay.kind());
        assertEquals("receipt_original", replay.terminalReceipt().get("receipt_id").getAsString());
    }

    @Test
    void terminalReceiptCannotBeReplacedAfterCompletion() {
        CommandLedger ledger = new CommandLedger(8);
        JsonObject command = command("command_1", "world.mine", 1, 2);
        ledger.begin("command_1", command);
        JsonObject original = new JsonObject();
        original.addProperty("receipt_id", "receipt_original");
        ledger.complete("command_1", original);

        JsonObject replacement = new JsonObject();
        replacement.addProperty("receipt_id", "receipt_replacement");

        assertThrows(
                IllegalStateException.class,
                () -> ledger.complete("command_1", replacement));
    }

    @Test
    void sameIdentifierWithDifferentPayloadIsRejected() {
        CommandLedger ledger = new CommandLedger(8);
        ledger.begin("command_1", command("command_1", "navigation.goto", 1, 2));

        CommandLedger.Decision conflict =
                ledger.begin("command_1", command("command_1", "navigation.goto", 3, 4));

        assertEquals(CommandLedger.DecisionKind.CONFLICT, conflict.kind());
        assertNull(conflict.terminalReceipt());
    }

    @Test
    void canonicalFingerprintIgnoresObjectMemberOrder() {
        CommandLedger ledger = new CommandLedger(8);
        JsonObject original = command("command_1", "navigation.goto", 1, 2);
        JsonObject reordered = new JsonObject();
        reordered.add("parameters", original.get("parameters"));
        reordered.addProperty("operation", "navigation.goto");
        reordered.addProperty("intent_id", "intent_1");
        reordered.addProperty("command_id", "command_1");

        ledger.begin("command_1", original);

        assertEquals(
                CommandLedger.DecisionKind.PENDING_REPLAY,
                ledger.begin("command_1", reordered).kind());
    }

    @Test
    void terminalCacheIsBoundedWithoutEvictingPendingCommands() {
        CommandLedger ledger = new CommandLedger(2);
        JsonObject pending = command("pending", "observation.wait", 1, 2);
        ledger.begin("pending", pending);
        for (int index = 0; index < 4; index++) {
            String commandId = "command_" + index;
            JsonObject command = command(commandId, "observation.wait", index, index);
            ledger.begin(commandId, command);
            JsonObject receipt = new JsonObject();
            receipt.addProperty("receipt_id", "receipt_" + index);
            ledger.complete(commandId, receipt);
        }

        assertEquals(3, ledger.size());
        assertEquals(CommandLedger.DecisionKind.PENDING_REPLAY, ledger.begin("pending", pending).kind());
    }

    private static JsonObject command(
            String commandId, String operation, int x, int z) {
        JsonObject parameters = new JsonObject();
        parameters.addProperty("x", x);
        parameters.addProperty("z", z);
        JsonObject command = new JsonObject();
        command.addProperty("command_id", commandId);
        command.addProperty("intent_id", "intent_1");
        command.addProperty("operation", operation);
        command.add("parameters", parameters);
        return command;
    }
}
