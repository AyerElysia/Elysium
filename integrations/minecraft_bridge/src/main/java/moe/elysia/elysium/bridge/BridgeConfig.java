package moe.elysia.elysium.bridge;

import com.google.gson.Gson;
import com.google.gson.GsonBuilder;
import com.google.gson.annotations.SerializedName;
import java.io.IOException;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.SecureRandom;
import java.util.Base64;
import net.neoforged.fml.loading.FMLPaths;

/** Persistent bridge configuration and authentication secret. */
final class BridgeConfig {
    private static final Gson GSON = new GsonBuilder().setPrettyPrinting().create();
    private static final SecureRandom RANDOM = new SecureRandom();

    @SerializedName("bridge_uri")
    private String bridgeUri = "ws://127.0.0.1:18765/elysium";

    @SerializedName("observation_interval_ticks")
    private int observationIntervalTicks = 2;

    @SerializedName("entity_radius_blocks")
    private double entityRadiusBlocks = 32.0;

    @SerializedName("authentication_token")
    private String authenticationToken = "";

    private BridgeConfig() {}

    /** Load configuration, creating a cryptographically random token when absent. */
    static BridgeConfig load() {
        Path path = path();
        BridgeConfig config = new BridgeConfig();
        try {
            if (Files.exists(path)) {
                BridgeConfig loaded = GSON.fromJson(
                        Files.readString(path, StandardCharsets.UTF_8),
                        BridgeConfig.class);
                if (loaded != null) {
                    config = loaded;
                }
            }
            config.validate();
            if (config.authenticationToken.isBlank()) {
                byte[] token = new byte[32];
                RANDOM.nextBytes(token);
                config.authenticationToken = Base64.getUrlEncoder().withoutPadding().encodeToString(token);
            }
            Files.createDirectories(path.getParent());
            Files.writeString(path, GSON.toJson(config), StandardCharsets.UTF_8);
            return config;
        } catch (IOException exception) {
            throw new IllegalStateException("Unable to load Elysium bridge configuration", exception);
        }
    }

    /** Return the bridge configuration path. */
    static Path path() {
        return FMLPaths.CONFIGDIR.get().resolve("elysium_bridge.json");
    }

    /** Validate all operational bounds without silently replacing values. */
    private void validate() {
        if (bridgeUri == null || bridgeUri.isBlank()) {
            throw new IllegalArgumentException("bridge_uri must not be empty");
        }
        URI uri = URI.create(bridgeUri);
        if (!"ws".equals(uri.getScheme()) || uri.getHost() == null || uri.getPort() < 1) {
            throw new IllegalArgumentException(
                    "bridge_uri must be an explicit ws://host:port URI");
        }
        if (observationIntervalTicks < 1) {
            throw new IllegalArgumentException("observation_interval_ticks must be positive");
        }
        if (!Double.isFinite(entityRadiusBlocks) || entityRadiusBlocks <= 0.0) {
            throw new IllegalArgumentException("entity_radius_blocks must be positive");
        }
    }

    /** Return the configured outbound WSL bridge listener URI. */
    String bridgeUri() {
        return bridgeUri;
    }

    /** Return the observation cadence in client ticks. */
    int observationIntervalTicks() {
        return observationIntervalTicks;
    }

    /** Return the structured entity sensor radius. */
    double entityRadiusBlocks() {
        return entityRadiusBlocks;
    }

    /** Return the authentication token without logging it. */
    String authenticationToken() {
        return authenticationToken;
    }
}
