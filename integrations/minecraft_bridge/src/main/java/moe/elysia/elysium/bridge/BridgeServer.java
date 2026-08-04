package moe.elysia.elysium.bridge;

import com.google.gson.Gson;
import com.google.gson.JsonObject;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.WebSocket;
import java.nio.charset.StandardCharsets;
import java.security.GeneralSecurityException;
import java.security.MessageDigest;
import java.time.Duration;
import java.time.Instant;
import java.util.Base64;
import java.util.HexFormat;
import java.util.UUID;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CompletionStage;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicLong;
import java.util.concurrent.atomic.AtomicReference;
import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;
import net.minecraft.client.Minecraft;

/** Authenticated outbound WebSocket endpoint for one embodiment controller. */
final class BridgeServer {
    static final String PROTOCOL = "elysium.minecraft.bridge/1";
    static final String BRIDGE_VERSION = "0.2.1";
    private static final long AUTHENTICATION_DEADLINE_SECONDS = 5L;
    private static final int MAX_DROPPABLE_OUTBOUND_MESSAGES = 32;
    private static final int MAX_TERMINAL_COMMAND_RECEIPTS = 1024;
    private static final Gson GSON = new Gson();

    private final BridgeConfig config;
    private final Minecraft client;
    private final ControlExecutor controls;
    private final String instanceId;
    private final HttpClient httpClient;
    private final ScheduledExecutorService networkExecutor;
    private final AtomicReference<WebSocket> connection = new AtomicReference<>();
    private final AtomicReference<ConnectionState> connectionState = new AtomicReference<>();
    private final AtomicBoolean connecting = new AtomicBoolean();
    private final AtomicInteger outboundPending = new AtomicInteger();
    private final AtomicLong droppedObservations = new AtomicLong();
    private final CommandLedger commandLedger = new CommandLedger(MAX_TERMINAL_COMMAND_RECEIPTS);
    private final Object outboundLock = new Object();
    private CompletableFuture<Void> outboundTail = CompletableFuture.completedFuture(null);
    private volatile boolean stopped;

    /** Create a stopped bridge connection for the current game process. */
    BridgeServer(
            BridgeConfig config,
            Minecraft client,
            ControlExecutor controls,
            String instanceId) {
        this.config = config;
        this.client = client;
        this.controls = controls;
        this.instanceId = instanceId;
        this.networkExecutor = Executors.newSingleThreadScheduledExecutor(runnable -> {
            Thread thread = new Thread(runnable, "elysium-bridge-network");
            thread.setDaemon(true);
            return thread;
        });
        this.httpClient = HttpClient.newBuilder()
                .connectTimeout(Duration.ofSeconds(5L))
                .executor(networkExecutor)
                .build();
    }

    /** Start connecting without blocking the Minecraft startup thread. */
    void start() {
        networkExecutor.execute(this::connect);
    }

    /** Stop accepting work, release controls, and close the active connection. */
    void stop() {
        stopped = true;
        client.execute(() -> controls.interrupt("bridge stopping"));
        WebSocket socket = connection.getAndSet(null);
        connectionState.set(null);
        if (socket != null) {
            socket.sendClose(WebSocket.NORMAL_CLOSURE, "bridge stopping");
        }
        networkExecutor.shutdownNow();
    }

    /** Publish one factual state with a contiguous connection-local sequence. */
    void broadcastObservation(JsonObject facts) {
        WebSocket socket = connection.get();
        ConnectionState state = connectionState.get();
        if (socket == null || state == null || !state.authenticated) {
            return;
        }
        long nextSequence = state.observationSequence.get() + 1L;
        JsonObject transport = new JsonObject();
        transport.addProperty("pending_messages", outboundPending.get());
        transport.addProperty("dropped_observations", droppedObservations.get());
        facts.add("bridge_transport", transport);
        JsonObject observation = new JsonObject();
        observation.addProperty("observation_id", "observation_" + UUID.randomUUID());
        observation.addProperty("instance_id", instanceId);
        observation.addProperty("sequence", nextSequence);
        observation.addProperty("observed_at", Instant.now().toString());
        observation.addProperty("source", "neoforge-client");
        observation.add("facts", facts.deepCopy());
        JsonObject envelope = new JsonObject();
        envelope.addProperty("type", "observation");
        envelope.add("observation", observation);
        if (send(socket, envelope, true)) {
            state.observationSequence.set(nextSequence);
        } else {
            droppedObservations.incrementAndGet();
        }
    }

    /** Connect to the WSL listener; retry only while the configured endpoint is absent. */
    private void connect() {
        if (stopped || connection.get() != null || !connecting.compareAndSet(false, true)) {
            return;
        }
        httpClient.newWebSocketBuilder()
                .connectTimeout(Duration.ofSeconds(5L))
                .buildAsync(URI.create(config.bridgeUri()), new ProtocolListener())
                .whenComplete((socket, exception) -> {
                    connecting.set(false);
                    if (exception != null) {
                        ElysiumBridgeMod.LOGGER.debug(
                                "Elysium bridge waiting for {}: {}",
                                config.bridgeUri(), exception.getMessage());
                        scheduleReconnect();
                    } else {
                        ElysiumBridgeMod.LOGGER.info(
                                "Elysium bridge connected to {}; token file: {}",
                                config.bridgeUri(), BridgeConfig.path());
                    }
                });
    }

    /** Schedule another outbound attempt after a disconnected or absent listener. */
    private void scheduleReconnect() {
        if (!stopped && !networkExecutor.isShutdown()) {
            networkExecutor.schedule(this::connect, 1L, TimeUnit.SECONDS);
        }
    }

    /** Send one JSON object as a complete WebSocket text message. */
    private boolean send(WebSocket socket, JsonObject message, boolean droppable) {
        if (droppable && outboundPending.get() >= MAX_DROPPABLE_OUTBOUND_MESSAGES) {
            return false;
        }
        String payload = GSON.toJson(message);
        outboundPending.incrementAndGet();
        synchronized (outboundLock) {
            outboundTail = outboundTail
                    .handle((ignored, error) -> (Void) null)
                    .thenCompose(ignored -> socket.sendText(payload, true)
                            .thenApply(sent -> (Void) null))
                    .whenComplete((ignored, error) -> {
                        outboundPending.decrementAndGet();
                        if (error != null) {
                            ElysiumBridgeMod.LOGGER.debug(
                                    "Elysium bridge outbound send failed: {}",
                                    error.getMessage());
                            disconnected(socket, "controller outbound transport failed");
                        }
                    });
        }
        return true;
    }

    /** Construct one acknowledgement or terminal action receipt envelope. */
    private JsonObject receipt(
            String commandId,
            String intentId,
            boolean accepted,
            boolean completed,
            boolean interrupted,
            JsonObject facts,
            String error,
            long observationSequence) {
        JsonObject receipt = new JsonObject();
        receipt.addProperty("receipt_id", "receipt_" + UUID.randomUUID());
        receipt.addProperty("command_id", commandId);
        receipt.addProperty("intent_id", intentId);
        receipt.addProperty("accepted", accepted);
        receipt.addProperty("completed", completed);
        receipt.addProperty("interrupted", interrupted);
        receipt.addProperty("recorded_at", Instant.now().toString());
        receipt.add("facts", facts);
        if (error != null) {
            receipt.addProperty("error", error);
        }
        receipt.addProperty("observation_sequence", observationSequence);
        JsonObject envelope = new JsonObject();
        envelope.addProperty("type", "receipt");
        envelope.add("receipt", receipt);
        return envelope;
    }

    /** Send an already constructed receipt envelope. */
    private void sendReceipt(WebSocket socket, JsonObject receiptEnvelope) {
        send(socket, receiptEnvelope, false);
    }

    /** Send a fresh authentication challenge after the connection opens. */
    private void sendHello(WebSocket socket, ConnectionState state) {
        JsonObject hello = new JsonObject();
        hello.addProperty("type", "hello");
        hello.addProperty("protocol", PROTOCOL);
        hello.addProperty("body_type", "neoforge-agent");
        hello.addProperty("bridge_version", BRIDGE_VERSION);
        hello.addProperty("minecraft_version", "1.21.1");
        hello.addProperty("neoforge_version", "21.1.219");
        hello.addProperty("nonce", state.nonce);
        hello.addProperty("instance_id", instanceId);
        com.google.gson.JsonArray capabilities = new com.google.gson.JsonArray();
        controls.operations().stream().sorted().forEach(capabilities::add);
        hello.add("capabilities", capabilities);
        send(socket, hello, false);
    }

    /** Reject a connection that did not authenticate before the security deadline. */
    private void closeIfUnauthenticated(WebSocket socket, ConnectionState state) {
        if (connection.get() == socket
                && connectionState.get() == state
                && !state.authenticated) {
            socket.sendClose(WebSocket.NORMAL_CLOSURE, "authentication deadline expired");
        }
    }

    /** Decode and dispatch one JSON object from the selected controller. */
    private void handleText(WebSocket socket, String text) {
        ConnectionState state = connectionState.get();
        if (state == null || connection.get() != socket) {
            socket.abort();
            return;
        }
        try {
            JsonObject message = GSON.fromJson(text, JsonObject.class);
            if (message == null || !message.has("type")) {
                throw new IllegalArgumentException("Message must be a JSON object with a type");
            }
            String type = message.get("type").getAsString();
            if (!state.authenticated) {
                authenticate(socket, state, type, message);
                return;
            }
            switch (type) {
                case "command" -> command(socket, state, message);
                case "interrupt" -> interrupt(message);
                case "release_all" -> client.execute(
                        () -> controls.releaseAll(message.has("reason")
                                ? message.get("reason").getAsString() : "release_all"));
                default -> throw new IllegalArgumentException("Unsupported message type: " + type);
            }
        } catch (RuntimeException exception) {
            ElysiumBridgeMod.LOGGER.warn(
                    "Rejecting invalid bridge message: {}", exception.getMessage());
            socket.sendClose(WebSocket.NORMAL_CLOSURE, "invalid bridge message");
        }
    }

    /** Validate the HMAC challenge and grant the one outbound controller lease. */
    private void authenticate(
            WebSocket socket,
            ConnectionState state,
            String type,
            JsonObject message) {
        if (!"authenticate".equals(type)
                || !message.has("protocol")
                || !PROTOCOL.equals(message.get("protocol").getAsString())
                || !message.has("digest")) {
            rejectAuthentication(socket);
            return;
        }
        String expected = hmac(config.authenticationToken(), state.nonce);
        byte[] expectedBytes = expected.getBytes(StandardCharsets.US_ASCII);
        byte[] suppliedBytes = message.get("digest").getAsString()
                .getBytes(StandardCharsets.US_ASCII);
        if (!MessageDigest.isEqual(expectedBytes, suppliedBytes)) {
            rejectAuthentication(socket);
            return;
        }
        state.authenticated = true;
        JsonObject response = new JsonObject();
        response.addProperty("type", "authentication");
        response.addProperty("accepted", true);
        send(socket, response, false);
    }

    /** Reject authentication without revealing whether the token was malformed. */
    private void rejectAuthentication(WebSocket socket) {
        JsonObject response = new JsonObject();
        response.addProperty("type", "authentication");
        response.addProperty("accepted", false);
        send(socket, response, false);
        socket.sendClose(WebSocket.NORMAL_CLOSURE, "authentication rejected");
    }

    /** Acknowledge one correlated command and execute it on the game thread. */
    private void command(WebSocket socket, ConnectionState state, JsonObject message) {
        if (!message.has("command")) {
            throw new IllegalArgumentException("Missing command object");
        }
        JsonObject command = message.getAsJsonObject("command");
        String commandId = required(command, "command_id");
        String intentId = required(command, "intent_id");
        String operation = required(command, "operation");
        JsonObject parameters = command.has("parameters")
                ? command.getAsJsonObject("parameters") : new JsonObject();
        CommandLedger.Decision decision = commandLedger.begin(commandId, command);
        switch (decision.kind()) {
            case CONFLICT -> {
                sendReceipt(socket, receipt(
                        commandId, intentId, false, true, false,
                        new JsonObject(), "command_id was already used for another payload",
                        state.observationSequence.get()));
                return;
            }
            case PENDING_REPLAY -> {
                sendReceipt(socket, receipt(
                        commandId, intentId, true, false, false,
                        new JsonObject(), null, state.observationSequence.get()));
                decision.pendingCompletion().thenAccept(
                        terminal -> sendReceipt(socket, terminal.deepCopy()));
                return;
            }
            case TERMINAL_REPLAY -> {
                sendReceipt(socket, decision.terminalReceipt());
                return;
            }
            case NEW -> {
                // Continue into the single execution path below.
            }
        }
        if (!controls.supports(operation)) {
            JsonObject rejected = receipt(
                    commandId, intentId, false, true, false,
                    new JsonObject(), "Unsupported operation: " + operation,
                    state.observationSequence.get());
            commandLedger.complete(commandId, rejected);
            sendReceipt(socket, rejected);
            return;
        }
        sendReceipt(socket, receipt(
                commandId, intentId, true, false, false,
                new JsonObject(), null, state.observationSequence.get()));
        client.execute(() -> {
            JsonObject terminal;
            try {
                JsonObject facts = controls.execute(operation, parameters);
                terminal = receipt(
                        commandId, intentId, true, true, false,
                        facts, null, state.observationSequence.get());
            } catch (RuntimeException exception) {
                JsonObject facts = new JsonObject();
                facts.addProperty("exception_type", exception.getClass().getName());
                Throwable cause = exception.getCause();
                if (cause != null) {
                    facts.addProperty("cause_type", cause.getClass().getName());
                }
                ElysiumBridgeMod.LOGGER.warn(
                        "Minecraft bridge operation {} failed for command {}",
                        operation,
                        commandId,
                        exception);
                terminal = receipt(
                        commandId, intentId, true, true, false,
                        facts, exception.getMessage(), state.observationSequence.get());
            }
            commandLedger.complete(commandId, terminal);
            sendReceipt(socket, terminal);
        });
    }

    /** Interrupt the active intention and release all control state. */
    private void interrupt(JsonObject message) {
        String intentId = required(message, "intent_id");
        String reason = required(message, "reason");
        client.execute(() -> {
            controls.interrupt(reason);
            ElysiumBridgeMod.LOGGER.info("Interrupted Minecraft intent {}", intentId);
        });
    }

    /** Mark a socket disconnected, release held controls, and reconnect later. */
    private void disconnected(WebSocket socket, String reason) {
        if (connection.compareAndSet(socket, null)) {
            connectionState.set(null);
            client.execute(() -> controls.interrupt(reason));
            scheduleReconnect();
        }
    }

    /** Read one mandatory non-empty string field. */
    private static String required(JsonObject value, String name) {
        if (!value.has(name) || value.get(name).getAsString().isBlank()) {
            throw new IllegalArgumentException("Missing non-empty field: " + name);
        }
        return value.get(name).getAsString();
    }

    /** Calculate the protocol HMAC-SHA256 digest. */
    private static String hmac(String token, String nonce) {
        try {
            Mac mac = Mac.getInstance("HmacSHA256");
            mac.init(new SecretKeySpec(token.getBytes(StandardCharsets.UTF_8), "HmacSHA256"));
            return HexFormat.of().formatHex(mac.doFinal(nonce.getBytes(StandardCharsets.UTF_8)));
        } catch (GeneralSecurityException exception) {
            throw new IllegalStateException("HMAC-SHA256 is unavailable", exception);
        }
    }

    /** JDK WebSocket listener with explicit fragmented-text reassembly. */
    private final class ProtocolListener implements WebSocket.Listener {
        private final StringBuilder text = new StringBuilder();

        @Override
        public void onOpen(WebSocket socket) {
            ConnectionState state = new ConnectionState();
            if (!connection.compareAndSet(null, socket)) {
                socket.abort();
                return;
            }
            connectionState.set(state);
            sendHello(socket, state);
            networkExecutor.schedule(
                    () -> closeIfUnauthenticated(socket, state),
                    AUTHENTICATION_DEADLINE_SECONDS,
                    TimeUnit.SECONDS);
            socket.request(1L);
        }

        @Override
        public CompletionStage<?> onText(
                WebSocket socket,
                CharSequence data,
                boolean last) {
            synchronized (text) {
                text.append(data);
                if (last) {
                    String message = text.toString();
                    text.setLength(0);
                    handleText(socket, message);
                }
            }
            socket.request(1L);
            return CompletableFuture.completedFuture(null);
        }

        @Override
        public CompletionStage<?> onClose(WebSocket socket, int statusCode, String reason) {
            disconnected(socket, "controller disconnected");
            return CompletableFuture.completedFuture(null);
        }

        @Override
        public void onError(WebSocket socket, Throwable error) {
            ElysiumBridgeMod.LOGGER.debug(
                    "Elysium bridge connection ended: {}", error.getMessage());
            disconnected(socket, "controller transport failed");
        }
    }

    /** Mutable state scoped to the active WebSocket connection. */
    private static final class ConnectionState {
        private final String nonce;
        private volatile boolean authenticated;
        private final AtomicLong observationSequence = new AtomicLong();

        /** Create a fresh challenge and empty observation stream. */
        private ConnectionState() {
            byte[] random = new byte[32];
            new java.security.SecureRandom().nextBytes(random);
            nonce = Base64.getUrlEncoder().withoutPadding().encodeToString(random);
        }
    }
}
