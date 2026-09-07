package moe.elysia.elysium.bridge;

import com.google.gson.JsonObject;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Method;
import net.minecraft.client.Minecraft;
import net.minecraft.core.BlockPos;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.resources.ResourceLocation;
import net.minecraft.world.entity.player.Player;
import net.minecraft.world.item.ItemStack;
import net.minecraft.world.phys.Vec3;

/** Native world evidence and bounded work through the installed Baritone API. */
final class NativeTaskBackend implements NativeTaskEngine.Backend {
    private final Minecraft client;
    private String kind;
    private JsonObject arguments;
    private Vec3 origin;
    private BlockPos lastGoal;
    private int initialCount;
    private String itemId;
    private long lastGoalTick;

    NativeTaskBackend(Minecraft client) { this.client = client; }

    @Override
    public void validate(String taskKind, JsonObject args) {
        requireWorld();
        if (!StateCollector.baritoneAvailable()) {
            throw new IllegalStateException("Baritone is required for this task");
        }
        switch (taskKind) {
            case "follow_player", "go_to_player" -> {
                OperationContracts.followCommand(args);
                optionalInt(args, "distance", 1, 16, "follow_player".equals(taskKind) ? 3 : 2);
                visiblePlayer(OperationContracts.requiredString(args, "player"));
            }
            case "go_to_position" -> {
                OperationContracts.gotoCommand(args);
                optionalInt(args, "distance", 0, 16, 1);
            }
            case "gather_block" -> {
                ResourceLocation block = ResourceLocation.parse(
                        OperationContracts.mineCommand(args).substring("mine ".length()));
                if (!BuiltInRegistries.BLOCK.containsKey(block)) {
                    throw new IllegalArgumentException("unknown block: " + block);
                }
                optionalInt(args, "count", 1, 16, 1);
                optionalInt(args, "max_distance", 4, 64, 32);
            }
            default -> throw new IllegalArgumentException("unsupported native task: " + taskKind);
        }
    }

    @Override
    public void start(String taskKind, JsonObject args) {
        requireWorld();
        kind = taskKind;
        arguments = args.deepCopy();
        origin = client.player.position();
        lastGoal = null;
        lastGoalTick = -100;
        if ("gather_block".equals(kind)) {
            String block = OperationContracts.mineCommand(args).substring("mine ".length());
            var target = BuiltInRegistries.BLOCK.get(ResourceLocation.parse(block));
            itemId = BuiltInRegistries.ITEM.getKey(target.asItem()).toString();
            if ("minecraft:air".equals(itemId)) {
                throw new IllegalArgumentException("requested block has no inventory item");
            }
            initialCount = inventoryCount(itemId);
            int desiredTotal = initialCount + optionalInt(args, "count", 1, 16, 1);
            try {
                Object process = StateCollector.invokeNoArguments(
                        StateCollector.primaryBaritone(), "getMineProcess");
                Method mine = Class.forName("baritone.api.process.IMineProcess")
                        .getMethod("mineByName", int.class, String[].class);
                mine.invoke(process, desiredTotal, new String[] {block});
            } catch (ReflectiveOperationException exception) {
                throw dispatchFailure(exception);
            }
        } else {
            updateGoal(true);
        }
    }

    @Override
    public NativeTaskEngine.Progress poll() {
        requireWorld();
        if (client.player.isDeadOrDying()) {
            throw new IllegalStateException("represented player died during task");
        }
        JsonObject facts = position();
        if ("gather_block".equals(kind)) {
            int radius = optionalInt(arguments, "max_distance", 4, 64, 32);
            double fromStart = client.player.position().distanceTo(origin);
            if (fromStart > radius) {
                throw new IllegalStateException("gather exceeded its bounded search radius");
            }
            int collected = Math.max(0, inventoryCount(itemId) - initialCount);
            int count = optionalInt(arguments, "count", 1, 16, 1);
            facts.addProperty("item", itemId);
            facts.addProperty("inventory_before", initialCount);
            facts.addProperty("inventory_after", inventoryCount(itemId));
            facts.addProperty("collected", collected);
            facts.addProperty("requested", count);
            facts.addProperty("distance_from_start", fromStart);
            return new NativeTaskEngine.Progress(collected >= count, "gathering", facts);
        }
        Vec3 target;
        int distance;
        if ("go_to_position".equals(kind)) {
            BlockPos goal = positionArgument(arguments);
            target = Vec3.atBottomCenterOf(goal);
            distance = optionalInt(arguments, "distance", 0, 16, 1);
        } else {
            Player player = visiblePlayer(OperationContracts.requiredString(arguments, "player"));
            target = player.position();
            distance = optionalInt(arguments, "distance", 1, 16,
                    "follow_player".equals(kind) ? 3 : 2);
            facts.addProperty("player", player.getGameProfile().getName());
        }
        double actual = client.player.position().distanceTo(target);
        facts.addProperty("distance_to_target", actual);
        facts.addProperty("target_x", target.x);
        facts.addProperty("target_y", target.y);
        facts.addProperty("target_z", target.z);
        // A zero-radius block goal still has a half-block player collision box.
        boolean arrived = actual <= Math.max(0.75, distance);
        if ("follow_player".equals(kind)) {
            updateGoal(false);
            return new NativeTaskEngine.Progress(false, arrived ? "following_nearby" : "following", facts);
        }
        if (!arrived) updateGoal(false);
        return new NativeTaskEngine.Progress(arrived, "navigating", facts);
    }

    private void updateGoal(boolean force) {
        BlockPos target = "go_to_position".equals(kind)
                ? positionArgument(arguments)
                : visiblePlayer(OperationContracts.requiredString(arguments, "player")).blockPosition();
        long tick = client.level.getGameTime();
        if (!force && target.equals(lastGoal) && tick - lastGoalTick < 40) return;
        if (!force && tick - lastGoalTick < 10) return;
        int distance = optionalInt(arguments, "distance", "go_to_position".equals(kind) ? 0 : 1,
                16, "follow_player".equals(kind) ? 3 : "go_to_player".equals(kind) ? 2 : 1);
        try {
            Object goal = Class.forName("baritone.api.pathing.goals.GoalNear")
                    .getConstructor(BlockPos.class, int.class).newInstance(target, distance);
            Object process = StateCollector.invokeNoArguments(
                    StateCollector.primaryBaritone(), "getCustomGoalProcess");
            Class.forName("baritone.api.process.ICustomGoalProcess")
                    .getMethod("setGoalAndPath", Class.forName("baritone.api.pathing.goals.Goal"))
                    .invoke(process, goal);
            lastGoal = target;
            lastGoalTick = tick;
        } catch (ReflectiveOperationException exception) {
            throw dispatchFailure(exception);
        }
    }

    @Override
    public void stop(String reason) {
        try {
            Object behavior = StateCollector.invokeNoArguments(
                    StateCollector.primaryBaritone(), "getPathingBehavior");
            Class.forName("baritone.api.behavior.IPathingBehavior")
                    .getMethod("forceCancel").invoke(behavior);
        } catch (ReflectiveOperationException exception) {
            throw dispatchFailure(exception);
        }
        lastGoal = null;
    }

    private static IllegalStateException dispatchFailure(ReflectiveOperationException exception) {
        Throwable cause = exception instanceof InvocationTargetException invocation
                ? invocation.getCause() : exception;
        return new IllegalStateException("Baritone native task failed: " + cause, cause);
    }

    private Player visiblePlayer(String name) {
        return client.level.players().stream()
                .filter(player -> player != client.player && player.getGameProfile().getName().equals(name))
                .findFirst().orElseThrow(() -> new IllegalStateException("player is not visible: " + name));
    }

    private int inventoryCount(String resourceId) {
        int total = 0;
        for (int slot = 0; slot < client.player.getInventory().getContainerSize(); slot++) {
            ItemStack stack = client.player.getInventory().getItem(slot);
            if (BuiltInRegistries.ITEM.getKey(stack.getItem()).toString().equals(resourceId)) {
                total += stack.getCount();
            }
        }
        return total;
    }

    private JsonObject position() {
        JsonObject result = new JsonObject();
        result.addProperty("x", client.player.getX());
        result.addProperty("y", client.player.getY());
        result.addProperty("z", client.player.getZ());
        return result;
    }

    private static BlockPos positionArgument(JsonObject args) {
        return new BlockPos(
                OperationContracts.boundedInt(args, "x", -30_000_000, 30_000_000),
                OperationContracts.boundedInt(args, "y", -2048, 2048),
                OperationContracts.boundedInt(args, "z", -30_000_000, 30_000_000));
    }

    private static int optionalInt(JsonObject args, String name, int min, int max, int fallback) {
        return args.has(name) ? OperationContracts.boundedInt(args, name, min, max) : fallback;
    }

    private void requireWorld() {
        if (client.player == null || client.level == null) {
            throw new IllegalStateException("no native Minecraft world is loaded");
        }
    }
}
