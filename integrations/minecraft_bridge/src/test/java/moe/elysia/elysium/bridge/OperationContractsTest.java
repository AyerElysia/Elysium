package moe.elysia.elysium.bridge;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;

import com.google.gson.JsonObject;
import org.junit.jupiter.api.Test;

/** Contract tests for typed operations exposed to the model planner. */
final class OperationContractsTest {
    @Test
    void gotoCommandRequiresIntegralBoundedCoordinates() {
        JsonObject parameters = new JsonObject();
        parameters.addProperty("x", 12);
        parameters.addProperty("y", 64);
        parameters.addProperty("z", -4);

        assertEquals("goto 12 64 -4", OperationContracts.gotoCommand(parameters));

        parameters.addProperty("x", 12.5);
        assertThrows(
                IllegalArgumentException.class,
                () -> OperationContracts.gotoCommand(parameters));
    }

    @Test
    void followCommandRejectsCommandInjection() {
        JsonObject parameters = new JsonObject();
        parameters.addProperty("player", "AyerElysia");
        assertEquals(
                "follow player AyerElysia",
                OperationContracts.followCommand(parameters));

        parameters.addProperty("player", "AyerElysia stop");
        assertThrows(
                IllegalArgumentException.class,
                () -> OperationContracts.followCommand(parameters));
    }

    @Test
    void mineCommandNormalizesNamespaceAndRejectsFreeText() {
        JsonObject parameters = new JsonObject();
        parameters.addProperty("block", "oak_log");
        assertEquals(
                "mine minecraft:oak_log",
                OperationContracts.mineCommand(parameters));

        parameters.addProperty("block", "oak_log; stop");
        assertThrows(
                IllegalArgumentException.class,
                () -> OperationContracts.mineCommand(parameters));
    }

    @Test
    void optionalFloatRejectsNonFiniteOrExcessiveLookDeltas() {
        JsonObject parameters = new JsonObject();
        parameters.addProperty("yaw", 45.0F);
        assertEquals(
                45.0F,
                OperationContracts.optionalBoundedFloat(
                        parameters, "yaw", -180.0F, 180.0F));

        parameters.addProperty("yaw", 181.0F);
        assertThrows(
                IllegalArgumentException.class,
                () -> OperationContracts.optionalBoundedFloat(
                        parameters, "yaw", -180.0F, 180.0F));
    }
}
