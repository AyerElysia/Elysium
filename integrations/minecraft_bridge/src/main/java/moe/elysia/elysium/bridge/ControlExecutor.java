package moe.elysia.elysium.bridge;

import com.google.gson.JsonArray;
import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Method;
import java.util.IdentityHashMap;
import java.util.LinkedHashSet;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Set;
import net.minecraft.client.KeyMapping;
import net.minecraft.client.Minecraft;
import net.minecraft.util.Mth;

/** Execute exact operational commands on the Minecraft client thread. */
final class ControlExecutor {
    private static final Set<String> BASE_OPERATIONS = Set.of(
            "native.input_batch",
            "chat.send",
            "player.respawn",
            "control.release_all");

    private final Minecraft client;
    private final Map<KeyMapping, Integer> pulseReleases = new IdentityHashMap<>();

    /** Bind control execution to the singleton Minecraft client. */
    ControlExecutor(Minecraft client) {
        this.client = client;
    }

    /** Return every operation implemented by this bridge build. */
    Set<String> operations() {
        Set<String> result = new LinkedHashSet<>(BASE_OPERATIONS);
        if (StateCollector.baritoneAvailable()) {
            result.add("baritone.command");
        }
        return Set.copyOf(result);
    }

    /** Return whether an exact operation name is supported. */
    boolean supports(String operation) {
        return operations().contains(operation);
    }

    /** Execute one validated operation and return dispatch facts. */
    JsonObject execute(String operation, JsonObject parameters) {
        return switch (operation) {
            case "native.input_batch" -> nativeInput(parameters);
            case "baritone.command" -> baritoneCommand(requiredString(parameters, "command"));
            case "chat.send" -> sendChat(requiredString(parameters, "message"));
            case "player.respawn" -> respawn();
            case "control.release_all" -> releaseAll("command");
            default -> throw new IllegalArgumentException("Unsupported operation: " + operation);
        };
    }

    /** Release pulse keys whose exact tick lifetimes have elapsed. */
    void tick() {
        pulseReleases.replaceAll((mapping, ticks) -> ticks - 1);
        pulseReleases.entrySet().removeIf(entry -> {
            if (entry.getValue() <= 0) {
                entry.getKey().setDown(false);
                return true;
            }
            return false;
        });
    }

    /** Release all bridge-managed controls and stop Baritone pathing. */
    JsonObject interrupt(String reason) {
        JsonObject facts = releaseAll(reason);
        try {
            executeBaritoneCommand("stop");
            facts.addProperty("baritone_stop_dispatched", true);
        } catch (ReflectiveOperationException | LinkageError exception) {
            facts.addProperty("baritone_stop_dispatched", false);
            facts.addProperty("baritone_error", exception.getClass().getSimpleName());
        }
        return facts;
    }

    /** Apply a complete held-control snapshot and optional atomic pulses. */
    private JsonObject nativeInput(JsonObject parameters) {
        requireWorld();
        Map<String, KeyMapping> controls = controls();
        JsonObject holds = parameters.has("holds")
                ? parameters.getAsJsonObject("holds") : new JsonObject();
        for (Map.Entry<String, KeyMapping> entry : controls.entrySet()) {
            boolean down = holds.has(entry.getKey())
                    && holds.get(entry.getKey()).getAsBoolean();
            entry.getValue().setDown(down);
        }

        if (parameters.has("pulses")) {
            JsonArray pulses = parameters.getAsJsonArray("pulses");
            for (JsonElement element : pulses) {
                String name = element.getAsString();
                KeyMapping mapping = controls.get(name);
                if (mapping == null) {
                    throw new IllegalArgumentException("Unknown control pulse: " + name);
                }
                mapping.setDown(true);
                pulseReleases.put(mapping, 1);
            }
        }

        if (parameters.has("look_delta")) {
            JsonObject look = parameters.getAsJsonObject("look_delta");
            float yaw = look.has("yaw") ? look.get("yaw").getAsFloat() : 0.0F;
            float pitch = look.has("pitch") ? look.get("pitch").getAsFloat() : 0.0F;
            client.player.setYRot(client.player.getYRot() + yaw);
            client.player.setXRot(Mth.clamp(client.player.getXRot() + pitch, -90.0F, 90.0F));
        }

        if (parameters.has("hotbar_slot")) {
            int slot = parameters.get("hotbar_slot").getAsInt();
            if (slot < 0 || slot > 8) {
                throw new IllegalArgumentException("hotbar_slot must be between 0 and 8");
            }
            client.player.getInventory().selected = slot;
        }
        if (parameters.has("chat")) {
            client.player.connection.sendChat(parameters.get("chat").getAsString());
        }

        JsonObject facts = positionFacts();
        facts.add("holds", holds.deepCopy());
        return facts;
    }

    /** Dispatch one Baritone command through its public command manager API. */
    private JsonObject baritoneCommand(String command) {
        requireWorld();
        try {
            executeBaritoneCommand(command);
            JsonObject facts = positionFacts();
            facts.addProperty("command_dispatched", command);
            return facts;
        } catch (ReflectiveOperationException exception) {
            throw new IllegalStateException("Baritone command dispatch failed", exception);
        } catch (LinkageError error) {
            throw new IllegalStateException("Baritone is unavailable", error);
        }
    }

    /** Invoke Baritone's command manager while retaining arbitrary command text. */
    private static void executeBaritoneCommand(String command)
            throws ReflectiveOperationException {
        Object primary = StateCollector.primaryBaritone();
        Object manager = StateCollector.invokeNoArguments(primary, "getCommandManager");
        Method execute = manager.getClass().getMethod("execute", String.class);
        try {
            execute.invoke(manager, command);
        } catch (InvocationTargetException exception) {
            Throwable cause = exception.getCause();
            if (cause instanceof RuntimeException runtime) {
                throw runtime;
            }
            throw exception;
        }
    }

    /** Send one exact chat message through the active client connection. */
    private JsonObject sendChat(String message) {
        requireWorld();
        client.player.connection.sendChat(message);
        JsonObject facts = new JsonObject();
        facts.addProperty("message_dispatched", message);
        return facts;
    }

    /** Request respawn only while the represented player is actually dead. */
    private JsonObject respawn() {
        requireWorld();
        if (!client.player.isDeadOrDying()) {
            throw new IllegalStateException("Player is not dead");
        }
        client.player.respawn();
        JsonObject facts = new JsonObject();
        facts.addProperty("respawn_dispatched", true);
        return facts;
    }

    /** Release every key mapping managed by bridge input batches. */
    JsonObject releaseAll(String reason) {
        for (KeyMapping mapping : controls().values()) {
            mapping.setDown(false);
        }
        pulseReleases.clear();
        JsonObject facts = new JsonObject();
        facts.addProperty("controls_released", true);
        facts.addProperty("reason", reason);
        return facts;
    }

    /** Return bridge-managed controls in stable trace order. */
    private Map<String, KeyMapping> controls() {
        Map<String, KeyMapping> result = new LinkedHashMap<>();
        result.put("forward", client.options.keyUp);
        result.put("back", client.options.keyDown);
        result.put("left", client.options.keyLeft);
        result.put("right", client.options.keyRight);
        result.put("jump", client.options.keyJump);
        result.put("sneak", client.options.keyShift);
        result.put("sprint", client.options.keySprint);
        result.put("attack", client.options.keyAttack);
        result.put("use", client.options.keyUse);
        result.put("drop", client.options.keyDrop);
        return result;
    }

    /** Return current position facts after an operation. */
    private JsonObject positionFacts() {
        JsonObject facts = new JsonObject();
        facts.addProperty("x", client.player.getX());
        facts.addProperty("y", client.player.getY());
        facts.addProperty("z", client.player.getZ());
        facts.addProperty("yaw", client.player.getYRot());
        facts.addProperty("pitch", client.player.getXRot());
        return facts;
    }

    /** Require an active local player and world. */
    private void requireWorld() {
        if (client.player == null || client.level == null) {
            throw new IllegalStateException("No Minecraft world is loaded");
        }
    }

    /** Read one mandatory string field without inventing a default. */
    private static String requiredString(JsonObject parameters, String name) {
        if (!parameters.has(name) || parameters.get(name).getAsString().isBlank()) {
            throw new IllegalArgumentException("Missing non-empty parameter: " + name);
        }
        return parameters.get(name).getAsString();
    }
}
