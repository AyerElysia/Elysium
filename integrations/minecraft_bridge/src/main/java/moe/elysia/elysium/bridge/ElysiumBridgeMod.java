package moe.elysia.elysium.bridge;

import com.mojang.logging.LogUtils;
import java.util.UUID;
import net.minecraft.client.Minecraft;
import net.neoforged.api.distmarker.Dist;
import net.neoforged.bus.api.IEventBus;
import net.neoforged.fml.common.Mod;
import net.neoforged.neoforge.client.event.ClientTickEvent;
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
            server.broadcastObservation(StateCollector.collect(client, config));
        }
    }

}
