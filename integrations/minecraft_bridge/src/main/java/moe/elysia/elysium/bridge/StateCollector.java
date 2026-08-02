package moe.elysia.elysium.bridge;

import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Method;
import net.minecraft.client.Minecraft;
import net.minecraft.client.gui.screens.Screen;
import net.minecraft.client.player.AbstractClientPlayer;
import net.minecraft.core.component.DataComponents;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.world.effect.MobEffectInstance;
import net.minecraft.world.entity.Entity;
import net.minecraft.world.inventory.Slot;
import net.minecraft.world.item.ItemStack;
import net.minecraft.world.level.block.state.BlockState;
import net.minecraft.world.phys.BlockHitResult;
import net.minecraft.world.phys.EntityHitResult;
import net.minecraft.world.phys.HitResult;

/** Collect complete factual observations available to the local client. */
final class StateCollector {
    private StateCollector() {}

    /** Collect one world-state snapshot without assigning subjective meaning. */
    static JsonObject collect(Minecraft client, BridgeConfig config) {
        JsonObject facts = new JsonObject();
        facts.addProperty("client_paused", client.isPaused());
        facts.addProperty("window_active", client.isWindowActive());
        facts.addProperty("fps", client.getFps());
        if (client.player == null || client.level == null) {
            facts.addProperty("world_loaded", false);
            Screen screen = client.screen;
            if (screen != null) {
                facts.add("screen", screen(client, screen));
            }
            return facts;
        }

        facts.addProperty("world_loaded", true);
        facts.addProperty("dimension", client.level.dimension().location().toString());
        facts.addProperty("game_time", client.level.getGameTime());
        facts.addProperty("day_time", client.level.getDayTime());
        facts.addProperty("raining", client.level.isRaining());
        facts.addProperty("thundering", client.level.isThundering());

        JsonObject player = new JsonObject();
        player.addProperty("uuid", client.player.getUUID().toString());
        player.addProperty("name", client.player.getName().getString());
        player.addProperty("x", client.player.getX());
        player.addProperty("y", client.player.getY());
        player.addProperty("z", client.player.getZ());
        player.addProperty("yaw", client.player.getYRot());
        player.addProperty("pitch", client.player.getXRot());
        player.addProperty("health", client.player.getHealth());
        player.addProperty("max_health", client.player.getMaxHealth());
        player.addProperty("alive", client.player.isAlive());
        player.addProperty("dead_or_dying", client.player.isDeadOrDying());
        player.addProperty("absorption", client.player.getAbsorptionAmount());
        player.addProperty("food", client.player.getFoodData().getFoodLevel());
        player.addProperty("saturation", client.player.getFoodData().getSaturationLevel());
        player.addProperty("air", client.player.getAirSupply());
        player.addProperty("experience_level", client.player.experienceLevel);
        player.addProperty("experience_progress", client.player.experienceProgress);
        player.addProperty("on_ground", client.player.onGround());
        player.addProperty("sprinting", client.player.isSprinting());
        player.addProperty("crouching", client.player.isCrouching());
        player.addProperty("swimming", client.player.isSwimming());
        player.addProperty("fall_flying", client.player.isFallFlying());
        player.addProperty("selected_hotbar_slot", client.player.getInventory().selected);
        player.add("effects", effects(client));
        facts.add("player", player);

        client.level.getBiome(client.player.blockPosition()).unwrapKey().ifPresent(
                key -> facts.addProperty("biome", key.location().toString()));
        facts.add("inventory", inventory(client));
        facts.add("players", players(client));
        facts.add("entities", entities(client, config.entityRadiusBlocks()));
        facts.add("crosshair", crosshair(client));
        facts.add("controls", controls(client));
        facts.add("baritone", baritone());
        if (client.screen != null) {
            facts.add("screen", screen(client, client.screen));
        }
        return facts;
    }

    /** Collect every inventory slot exposed by the player's inventory. */
    private static JsonArray inventory(Minecraft client) {
        JsonArray result = new JsonArray();
        for (int index = 0; index < client.player.getInventory().getContainerSize(); index++) {
            ItemStack stack = client.player.getInventory().getItem(index);
            JsonObject slot = stack(index, stack);
            result.add(slot);
        }
        return result;
    }

    /** Collect every player currently represented by the client level. */
    private static JsonArray players(Minecraft client) {
        JsonArray result = new JsonArray();
        for (AbstractClientPlayer player : client.level.players()) {
            JsonObject value = new JsonObject();
            value.addProperty("uuid", player.getUUID().toString());
            value.addProperty("name", player.getName().getString());
            value.addProperty("x", player.getX());
            value.addProperty("y", player.getY());
            value.addProperty("z", player.getZ());
            value.addProperty("health", player.getHealth());
            result.add(value);
        }
        return result;
    }

    /** Collect every loaded entity inside the configured sensor radius. */
    private static JsonArray entities(Minecraft client, double radius) {
        JsonArray result = new JsonArray();
        double radiusSquared = radius * radius;
        for (Entity entity : client.level.entitiesForRendering()) {
            if (entity == client.player || entity.distanceToSqr(client.player) > radiusSquared) {
                continue;
            }
            JsonObject value = new JsonObject();
            value.addProperty("id", entity.getId());
            value.addProperty("uuid", entity.getUUID().toString());
            value.addProperty("type", BuiltInRegistries.ENTITY_TYPE
                    .getKey(entity.getType()).toString());
            value.addProperty("name", entity.getName().getString());
            value.addProperty("x", entity.getX());
            value.addProperty("y", entity.getY());
            value.addProperty("z", entity.getZ());
            value.addProperty("distance", entity.distanceTo(client.player));
            value.addProperty("alive", entity.isAlive());
            result.add(value);
        }
        return result;
    }

    /** Describe the exact block, entity, or miss under the crosshair. */
    private static JsonObject crosshair(Minecraft client) {
        JsonObject result = new JsonObject();
        HitResult hit = client.hitResult;
        if (hit == null) {
            result.addProperty("kind", "unavailable");
            return result;
        }
        result.addProperty("kind", hit.getType().name());
        result.addProperty("x", hit.getLocation().x);
        result.addProperty("y", hit.getLocation().y);
        result.addProperty("z", hit.getLocation().z);
        if (hit instanceof BlockHitResult blockHit && client.level != null) {
            BlockState state = client.level.getBlockState(blockHit.getBlockPos());
            result.addProperty("block", BuiltInRegistries.BLOCK
                    .getKey(state.getBlock()).toString());
            result.addProperty("block_x", blockHit.getBlockPos().getX());
            result.addProperty("block_y", blockHit.getBlockPos().getY());
            result.addProperty("block_z", blockHit.getBlockPos().getZ());
            result.addProperty("face", blockHit.getDirection().getName());
        } else if (hit instanceof EntityHitResult entityHit) {
            result.addProperty("entity_id", entityHit.getEntity().getId());
            result.addProperty("entity_uuid", entityHit.getEntity().getUUID().toString());
            result.addProperty("entity_type", BuiltInRegistries.ENTITY_TYPE
                    .getKey(entityHit.getEntity().getType()).toString());
        }
        return result;
    }

    /** Describe the current GUI and every slot in the active container menu. */
    private static JsonObject screen(Minecraft client, Screen screen) {
        JsonObject result = new JsonObject();
        result.addProperty("class", screen.getClass().getName());
        result.addProperty("title", screen.getTitle().getString());
        if (client.player != null && client.player.containerMenu != null) {
            result.addProperty("container_id", client.player.containerMenu.containerId);
            JsonArray slots = new JsonArray();
            for (Slot menuSlot : client.player.containerMenu.slots) {
                slots.add(stack(menuSlot.index, menuSlot.getItem()));
            }
            result.add("slots", slots);
            result.addProperty("carried_item", itemId(client.player.containerMenu.getCarried()));
            result.addProperty("carried_count", client.player.containerMenu.getCarried().getCount());
        }
        return result;
    }

    /** Collect active effect identifiers, amplifier, and exact remaining duration. */
    private static JsonArray effects(Minecraft client) {
        JsonArray result = new JsonArray();
        for (MobEffectInstance effect : client.player.getActiveEffects()) {
            JsonObject value = new JsonObject();
            effect.getEffect().unwrapKey().ifPresent(
                    key -> value.addProperty("effect", key.location().toString()));
            value.addProperty("amplifier", effect.getAmplifier());
            value.addProperty("duration_ticks", effect.getDuration());
            value.addProperty("ambient", effect.isAmbient());
            result.add(value);
        }
        return result;
    }

    /** Collect exact down-state for all controls managed by the bridge. */
    private static JsonObject controls(Minecraft client) {
        JsonObject result = new JsonObject();
        result.addProperty("forward", client.options.keyUp.isDown());
        result.addProperty("back", client.options.keyDown.isDown());
        result.addProperty("left", client.options.keyLeft.isDown());
        result.addProperty("right", client.options.keyRight.isDown());
        result.addProperty("jump", client.options.keyJump.isDown());
        result.addProperty("sneak", client.options.keyShift.isDown());
        result.addProperty("sprint", client.options.keySprint.isDown());
        result.addProperty("attack", client.options.keyAttack.isDown());
        result.addProperty("use", client.options.keyUse.isDown());
        return result;
    }

    /** Inspect Baritone availability and current pathing state through its public API. */
    private static JsonObject baritone() {
        JsonObject result = new JsonObject();
        try {
            Object primary = primaryBaritone();
            Object behavior = invokeNoArguments(primary, "getPathingBehavior");
            Object pathing = invokeNoArguments(behavior, "isPathing");
            result.addProperty("available", true);
            result.addProperty("pathing", Boolean.TRUE.equals(pathing));
        } catch (ReflectiveOperationException | LinkageError exception) {
            result.addProperty("available", false);
        }
        return result;
    }

    /** Return whether the named Baritone API is present and initialized. */
    static boolean baritoneAvailable() {
        try {
            return primaryBaritone() != null;
        } catch (ReflectiveOperationException | LinkageError exception) {
            return false;
        }
    }

    /** Resolve the primary Baritone instance without linking it at build time. */
    static Object primaryBaritone() throws ReflectiveOperationException {
        Class<?> api = Class.forName("baritone.api.BaritoneAPI");
        Object provider = api.getMethod("getProvider").invoke(null);
        return invokeNoArguments(provider, "getPrimaryBaritone");
    }

    /** Invoke a public no-argument method by name on a reflected API object. */
    static Object invokeNoArguments(Object target, String name)
            throws NoSuchMethodException, InvocationTargetException, IllegalAccessException {
        Method method = target.getClass().getMethod(name);
        return method.invoke(target);
    }

    /** Serialize one item slot without omitting empty slots. */
    private static JsonObject stack(int index, ItemStack stack) {
        JsonObject value = new JsonObject();
        value.addProperty("slot", index);
        value.addProperty("item", itemId(stack));
        value.addProperty("count", stack.getCount());
        value.addProperty("max_count", stack.getMaxStackSize());
        value.addProperty("damage", stack.getDamageValue());
        value.addProperty("max_damage", stack.getMaxDamage());
        value.addProperty("custom_name", stack.has(DataComponents.CUSTOM_NAME)
                ? stack.getHoverName().getString() : "");
        return value;
    }

    /** Return the registry identifier for an item stack. */
    private static String itemId(ItemStack stack) {
        return BuiltInRegistries.ITEM.getKey(stack.getItem()).toString();
    }
}
