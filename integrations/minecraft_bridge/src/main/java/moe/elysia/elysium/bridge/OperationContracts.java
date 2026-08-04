package moe.elysia.elysium.bridge;

import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import java.util.Locale;
import java.util.regex.Pattern;

/** Pure validation and command construction for advertised body operations. */
final class OperationContracts {
    private static final int WORLD_COORDINATE_LIMIT = 30_000_000;
    private static final Pattern PLAYER_NAME = Pattern.compile("[A-Za-z0-9_]{1,16}");
    private static final Pattern RESOURCE_ID = Pattern.compile(
            "(?:[a-z0-9_.-]+:)?[a-z0-9_./-]+");

    private OperationContracts() {}

    /** Construct a bounded Baritone goto command from exact block coordinates. */
    static String gotoCommand(JsonObject parameters) {
        int x = boundedInt(parameters, "x", -WORLD_COORDINATE_LIMIT, WORLD_COORDINATE_LIMIT);
        int y = boundedInt(parameters, "y", -2048, 2048);
        int z = boundedInt(parameters, "z", -WORLD_COORDINATE_LIMIT, WORLD_COORDINATE_LIMIT);
        return String.format(Locale.ROOT, "goto %d %d %d", x, y, z);
    }

    /** Construct a follow command without allowing arbitrary command injection. */
    static String followCommand(JsonObject parameters) {
        String player = requiredString(parameters, "player");
        if (!PLAYER_NAME.matcher(player).matches()) {
            throw new IllegalArgumentException(
                    "player must match the Minecraft account-name contract");
        }
        return "follow player " + player;
    }

    /** Construct a bounded mine command for one exact block identifier. */
    static String mineCommand(JsonObject parameters) {
        String block = requiredString(parameters, "block").toLowerCase(Locale.ROOT);
        if (!RESOURCE_ID.matcher(block).matches()) {
            throw new IllegalArgumentException("block must be a Minecraft resource identifier");
        }
        if (!block.contains(":")) {
            block = "minecraft:" + block;
        }
        return "mine " + block;
    }

    /** Read one mandatory string field without inventing a default. */
    static String requiredString(JsonObject parameters, String name) {
        JsonElement value = parameters.get(name);
        if (value == null || !value.isJsonPrimitive()
                || !value.getAsJsonPrimitive().isString()
                || value.getAsString().isBlank()) {
            throw new IllegalArgumentException("Missing non-empty string parameter: " + name);
        }
        return value.getAsString();
    }

    /** Read one required integral JSON number inside an inclusive range. */
    static int boundedInt(JsonObject parameters, String name, int minimum, int maximum) {
        JsonElement value = parameters.get(name);
        if (value == null || !value.isJsonPrimitive()
                || !value.getAsJsonPrimitive().isNumber()) {
            throw new IllegalArgumentException("Missing numeric parameter: " + name);
        }
        double number = value.getAsDouble();
        if (!Double.isFinite(number) || number != Math.rint(number)) {
            throw new IllegalArgumentException(name + " must be a finite integer");
        }
        if (number < minimum || number > maximum) {
            throw new IllegalArgumentException(
                    name + " must be between " + minimum + " and " + maximum);
        }
        return (int) number;
    }

    /** Read one optional finite number inside an inclusive range. */
    static float optionalBoundedFloat(
            JsonObject parameters,
            String name,
            float minimum,
            float maximum) {
        JsonElement value = parameters.get(name);
        if (value == null) {
            return 0.0F;
        }
        if (!value.isJsonPrimitive() || !value.getAsJsonPrimitive().isNumber()) {
            throw new IllegalArgumentException(name + " must be numeric");
        }
        float number = value.getAsFloat();
        if (!Float.isFinite(number) || number < minimum || number > maximum) {
            throw new IllegalArgumentException(
                    name + " must be finite and between " + minimum + " and " + maximum);
        }
        return number;
    }
}
