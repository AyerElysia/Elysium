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
            "chat.send",
            "control.release_all",
            "interaction.attack",
            "interaction.use",
            "inventory.select_hotbar",
            "item.drop",
            "movement.input",
            "observation.wait",
            "player.respawn");
    private static final Set<String> BARITONE_OPERATIONS = Set.of(
            "navigation.follow",
            "navigation.goto",
            "navigation.stop",
            "world.mine");

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
            result.addAll(BARITONE_OPERATIONS);
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
            case "movement.input" -> nativeInput(parameters);
            case "navigation.goto" -> navigationGoto(parameters);
            case "navigation.follow" -> navigationFollow(parameters);
            case "navigation.stop" -> navigationStop();
            case "world.mine" -> mine(parameters);
            case "interaction.attack" -> pulse("attack");
            case "interaction.use" -> pulse("use");
            case "inventory.select_hotbar" -> selectHotbar(parameters);
            case "item.drop" -> pulse("drop");
            case "observation.wait" -> waitForObservation();
            case "chat.send" -> sendChat(OperationContracts.requiredString(parameters, "message"));
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
        JsonObject holds = new JsonObject();
        if (parameters.has("holds")) {
            if (!parameters.get("holds").isJsonObject()) {
                throw new IllegalArgumentException("holds must be an object");
            }
            holds = parameters.getAsJsonObject("holds");
            for (Map.Entry<String, JsonElement> entry : holds.entrySet()) {
                if (!controls.containsKey(entry.getKey())) {
                    throw new IllegalArgumentException(
                            "Unknown held control: " + entry.getKey());
                }
                if (!entry.getValue().isJsonPrimitive()
                        || !entry.getValue().getAsJsonPrimitive().isBoolean()) {
                    throw new IllegalArgumentException(
                            "Held control must be boolean: " + entry.getKey());
                }
            }
        }
        for (Map.Entry<String, KeyMapping> entry : controls.entrySet()) {
            boolean down = holds.has(entry.getKey())
                    && holds.get(entry.getKey()).getAsBoolean();
            entry.getValue().setDown(down);
        }

        if (parameters.has("pulses")) {
            if (!parameters.get("pulses").isJsonArray()) {
                throw new IllegalArgumentException("pulses must be an array");
            }
            JsonArray pulses = parameters.getAsJsonArray("pulses");
            for (JsonElement element : pulses) {
                if (!element.isJsonPrimitive()
                        || !element.getAsJsonPrimitive().isString()) {
                    throw new IllegalArgumentException("control pulse must be a string");
                }
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
            if (!parameters.get("look_delta").isJsonObject()) {
                throw new IllegalArgumentException("look_delta must be an object");
            }
            JsonObject look = parameters.getAsJsonObject("look_delta");
            float yaw = OperationContracts.optionalBoundedFloat(
                    look, "yaw", -180.0F, 180.0F);
            float pitch = OperationContracts.optionalBoundedFloat(
                    look, "pitch", -90.0F, 90.0F);
            client.player.setYRot(client.player.getYRot() + yaw);
            client.player.setXRot(Mth.clamp(client.player.getXRot() + pitch, -90.0F, 90.0F));
        }

        if (parameters.has("hotbar_slot")) {
            int slot = OperationContracts.boundedInt(parameters, "hotbar_slot", 0, 8);
            client.player.getInventory().selected = slot;
        }

        JsonObject facts = positionFacts();
        facts.add("holds", holds.deepCopy());
        return facts;
    }

    /** Navigate to one exact block coordinate through a validated typed operation. */
    private JsonObject navigationGoto(JsonObject parameters) {
        String command = OperationContracts.gotoCommand(parameters);
        JsonObject facts = dispatchBaritone(command);
        JsonObject target = new JsonObject();
        target.addProperty("x", OperationContracts.boundedInt(
                parameters, "x", -30_000_000, 30_000_000));
        target.addProperty("y", OperationContracts.boundedInt(parameters, "y", -2048, 2048));
        target.addProperty("z", OperationContracts.boundedInt(
                parameters, "z", -30_000_000, 30_000_000));
        facts.add("target", target);
        return facts;
    }

    /** Follow one validated account name without exposing arbitrary Baritone text. */
    private JsonObject navigationFollow(JsonObject parameters) {
        String command = OperationContracts.followCommand(parameters);
        JsonObject facts = dispatchBaritone(command);
        facts.addProperty("player", OperationContracts.requiredString(parameters, "player"));
        return facts;
    }

    /** Stop Baritone navigation while retaining ordinary client control. */
    private JsonObject navigationStop() {
        JsonObject facts = dispatchBaritone("stop");
        facts.addProperty("navigation_stopped", true);
        return facts;
    }

    /** Ask Baritone to mine one validated block identifier. */
    private JsonObject mine(JsonObject parameters) {
        String command = OperationContracts.mineCommand(parameters);
        JsonObject facts = dispatchBaritone(command);
        facts.addProperty("block", command.substring("mine ".length()));
        return facts;
    }

    /** Dispatch an internal Baritone command assembled only from typed parameters. */
    private JsonObject dispatchBaritone(String command) {
        requireWorld();
        try {
            executeBaritoneCommand(command);
            JsonObject facts = positionFacts();
            facts.addProperty("executor", "baritone");
            facts.addProperty("dispatch_accepted", true);
            return facts;
        } catch (ReflectiveOperationException exception) {
            throw new IllegalStateException("Baritone command dispatch failed", exception);
        } catch (LinkageError error) {
            throw new IllegalStateException("Baritone is unavailable", error);
        }
    }

    /** Invoke Baritone's command manager with an internally assembled command. */
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

    /** Pulse one bridge-managed control for exactly one client tick. */
    private JsonObject pulse(String name) {
        requireWorld();
        KeyMapping mapping = controls().get(name);
        if (mapping == null) {
            throw new IllegalArgumentException("Unknown control pulse: " + name);
        }
        mapping.setDown(true);
        pulseReleases.put(mapping, 1);
        JsonObject facts = positionFacts();
        facts.addProperty("control_pulsed", name);
        return facts;
    }

    /** Select one exact zero-based hotbar slot. */
    private JsonObject selectHotbar(JsonObject parameters) {
        requireWorld();
        int slot = OperationContracts.boundedInt(parameters, "slot", 0, 8);
        client.player.getInventory().selected = slot;
        JsonObject facts = positionFacts();
        facts.addProperty("selected_hotbar_slot", slot);
        return facts;
    }

    /** Produce a correlated no-op receipt so the controller can await fresh state. */
    private JsonObject waitForObservation() {
        requireWorld();
        JsonObject facts = positionFacts();
        facts.addProperty("observation_wait_dispatched", true);
        return facts;
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

}
