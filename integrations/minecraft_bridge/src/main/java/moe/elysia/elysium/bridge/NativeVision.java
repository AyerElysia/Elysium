package moe.elysia.elysium.bridge;

import com.google.gson.JsonObject;
import com.mojang.blaze3d.platform.NativeImage;
import java.io.IOException;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.time.Instant;
import java.util.Base64;
import java.util.HexFormat;
import net.minecraft.client.Minecraft;
import net.minecraft.client.Screenshot;

/** A bounded first-person image obtained exclusively from the represented game. */
final class NativeVision {
    private NativeVision() {}

    static JsonObject capture(Minecraft client, int maxWidth)
            throws IOException, NoSuchAlgorithmException {
        if (client.player == null || client.level == null) {
            throw new IllegalStateException("native world is not loaded");
        }
        try (NativeImage source = Screenshot.takeScreenshot(client.getMainRenderTarget())) {
            int width = Math.min(maxWidth, source.getWidth());
            int height = Math.max(1, (int) ((long) source.getHeight() * width / source.getWidth()));
            try (NativeImage scaled = new NativeImage(width, height, false)) {
                source.resizeSubRectTo(0, 0, source.getWidth(), source.getHeight(), scaled);
                byte[] bytes = scaled.asByteArray();
                if (bytes.length > 4 * 1024 * 1024) {
                    throw new IllegalStateException("native frame exceeds transport budget");
                }
                JsonObject result = new JsonObject();
                result.addProperty("captured_at", Instant.now().toString());
                result.addProperty("source", "minecraft-render-target");
                result.addProperty("mime_type", "image/png");
                result.addProperty("width", width);
                result.addProperty("height", height);
                result.addProperty("sha256", HexFormat.of().formatHex(
                        MessageDigest.getInstance("SHA-256").digest(bytes)));
                result.addProperty("data_base64", Base64.getEncoder().encodeToString(bytes));
                return result;
            }
        }
    }
}
