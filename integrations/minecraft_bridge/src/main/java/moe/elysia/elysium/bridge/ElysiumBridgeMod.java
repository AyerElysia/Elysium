package moe.elysia.elysium.bridge;

import com.mojang.logging.LogUtils;
import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import java.time.Instant;
import java.util.ArrayDeque;
import java.util.UUID;
import net.minecraft.client.Minecraft;
import net.neoforged.api.distmarker.Dist;
import net.neoforged.bus.api.IEventBus;
import net.neoforged.fml.common.Mod;
import net.neoforged.neoforge.client.event.ClientTickEvent;
import net.neoforged.neoforge.client.event.ClientChatReceivedEvent;
import net.neoforged.neoforge.common.NeoForge;
import org.slf4j.Logger;

/** NeoForge entrypoint for Elysia's visible Minecraft embodiment bridge. */
@Mod(value = ElysiumBridgeMod.MOD_ID, dist = Dist.CLIENT)
public final class ElysiumBridgeMod {
    static final String MOD_ID = "elysium_bridge";
    static final Logger LOGGER = LogUtils.getLogger();

    private final Minecraft client;
    private final BridgeConfig config;
    private final ControlExecutor controls;
    private final BridgeServer server;
    private int observationTicks;
    private final ArrayDeque<JsonObject> recentChat = new ArrayDeque<>();

    /** Initialize configuration, server, controls, and client lifecycle hooks. */
    public ElysiumBridgeMod(IEventBus modBus) {
        client = Minecraft.getInstance();
        config = BridgeConfig.load();
        controls = new ControlExecutor(client);
        server = new BridgeServer(
                config,
                client,
                controls,
                "minecraft_" + UUID.randomUUID());
        NeoForge.EVENT_BUS.addListener(this::afterClientTick);
        NeoForge.EVENT_BUS.addListener(this::chatReceived);
        server.start();
        Runtime.getRuntime().addShutdownHook(
                new Thread(server::stop, "elysium-bridge-shutdown"));
    }

    /** Advance held-control lifetimes and publish observations at configured cadence. */
    private void afterClientTick(ClientTickEvent.Post event) {
        controls.tick();
        observationTicks++;
        if (observationTicks >= config.observationIntervalTicks()) {
            observationTicks = 0;
            JsonObject facts = StateCollector.collect(client, config);
            JsonObject taskFacts = new JsonObject();
            taskFacts.add("high_level", controls.taskSnapshot());
            facts.add("bot_tasks", taskFacts);
            JsonArray chat = new JsonArray();
            recentChat.forEach(entry -> chat.add(entry.deepCopy()));
            facts.add("chat", chat);
            server.broadcastObservation(facts);
        }
    }

    /** Observe actual incoming chat without rewriting the player's authored text. */
    private void chatReceived(ClientChatReceivedEvent event) {
        JsonObject entry = new JsonObject();
        entry.addProperty("at", Instant.now().toString());
        entry.addProperty("kind", event.isSystem() ? "system" : "chat");
        entry.addProperty("sender_uuid", event.getSender().toString());
        String message = event instanceof ClientChatReceivedEvent.Player playerEvent
                ? playerEvent.getPlayerChatMessage().signedContent()
                : event.getMessage().getString();
        entry.addProperty("message", message);
        entry.addProperty("display_message", event.getMessage().getString());
        if (client.getConnection() != null) {
            var info = client.getConnection().getPlayerInfo(event.getSender());
            if (info != null) entry.addProperty("username", info.getProfile().getName());
        }
        if (event.getBoundChatType() != null) {
            entry.addProperty("display_name", event.getBoundChatType().name().getString());
        }
        if (event instanceof ClientChatReceivedEvent.System system) {
            entry.addProperty("overlay", system.isOverlay());
        }
        // Own outbound speech is already recorded by the action receipt.
        boolean own = client.player != null && event.getSender().equals(client.player.getUUID());
        if (!own) {
            server.publishEvent(event.isSystem()
                    ? "minecraft.system.received" : "minecraft.chat.received", entry);
        }
        recentChat.addLast(entry.deepCopy());
        while (recentChat.size() > 16) recentChat.removeFirst();
    }
}
