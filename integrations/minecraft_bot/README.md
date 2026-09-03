# Elysium Minecraft Bot Body

Headless Minecraft body for Elysia built on [mineflayer]. It implements the
body side of the `elysium.minecraft.bridge/1` wire protocol, so Elysia can
join a server or LAN world as an independent player while her human plays in
their own client. No game window is occupied.

The shipped authentication mode is deliberately `offline`. It works with an
Open-to-LAN world and an offline-mode dedicated server. An online-mode server
requires a separate licensed Microsoft account and an explicit interactive
account-provisioning flow; this integration fails at the server login boundary
instead of pretending the configured display name is an authenticated account.

This body shares one contract with the NeoForge `minecraft_bridge` mod:

- Outbound WebSocket dial to the WSL controller listener (default
  `ws://127.0.0.1:18767/elysium`), so no inbound firewall rule is needed.
- HMAC-SHA256 nonce authentication with the session-generated token.
- A contiguous observation stream with monotonically increasing sequence.
- Correlated commands: quick acknowledgement receipt, then a terminal receipt.
  Receipts prove dispatch only; outcome evidence comes from later observations.
- Idempotent command ledger: replaying the same `command_id` replays the same
  receipt; reusing it with a different payload is rejected.
- A bounded FIFO body-event journal. Chat, player, health, lifecycle and task
  events remain pending across reconnects until the controller durably stores
  and acknowledges them.
- A bounded outbound queue drops stale observations before it can drop command
  receipts and reports the accumulated drop count in later observations.

The body publishes facts and executes validated commands. It never decides
what Elysia should want: goals, priorities, and conclusions belong to her
consciousness layer, per the project zero-rule policy.

## Layout

```text
integrations/minecraft_bot/
├── package.json
└── src/
    ├── index.js     # env configuration, wiring, lifecycle, shutdown
    ├── protocol.js  # elysium.minecraft.bridge/1 body side (auth, receipts)
    ├── body.js      # mineflayer facts and validated operation dispatch
    └── task_engine.js # exclusive high-level tasks, replay, timeout, terminal events
```

## Operations

The bot advertises the full agent operation set; semantics follow the NeoForge
ControlExecutor contract:

| Operation | Parameters | Notes |
|---|---|---|
| `chat.send` | `{message}` | Sent through the server chat channel. |
| `movement.input` | `{holds, pulses, look_delta, hotbar_slot}` | Held controls are forward/back/left/right/jump/sneak/sprint. |
| `navigation.goto` | `{x, y, z}` | Dispatched through mineflayer-pathfinder (Baritone analogue). |
| `navigation.follow` | `{player}` | The player must be visible from this body. |
| `navigation.stop` | `{}` | Clears the pathfinder goal. |
| `world.mine` | `{block}` | Finds the nearest matching block within 32 blocks, paths to it, and digs. |
| `interaction.attack` | `{}` | Attacks the nearest valid entity within 4.5 blocks. |
| `interaction.use` | `{}` | Activates the block under the body's view vector. |
| `inventory.select_hotbar` | `{slot}` | Zero-based slot 0 through 8. |
| `item.drop` | `{}` | Drops the selected hotbar stack. |
| `observation.wait` | `{}` | Correlated no-op for awaiting fresh state. |
| `player.respawn` | `{}` | Rejected unless the body is actually dead. |
| `control.release_all` | `{}` | Releases held controls and clears pathing. |

The dedicated scene consciousness normally uses a smaller high-level task
surface rather than issuing each low-level operation:

| Task kind | Purpose |
|---|---|
| `follow_player` | Continuously follow one currently visible player. |
| `go_to_player` | Reach one currently visible player and finish. |
| `go_to_position` | Reach an exact block coordinate. |
| `gather_block` | Locate and collect a bounded number of one block type. |
| `craft_item` | Craft a bounded item count from the current inventory. |
| `place_block` | Place one inventory block against an explicit reference face. |
| `eat_item` | Equip and consume one exact food resource. |

`task.start` returns immediately after acceptance. A single BodyGate prevents
high-level work from racing pathfinder, digging or low-level controls; chat,
status and explicit cancellation remain available. The default task deadline
is 180 seconds, the wire override is restricted to 5–600 seconds, and timeout
is a failed terminal state after best-effort body release. Stable task IDs bind
kind, arguments and duration so a retry cannot silently change the task.

Observations add a bounded `chat` ring buffer (last 16 chat/whisper/system/
join/leave events),
at most 64 visible players and 128 nearby entities,
so Elysia can perceive in-game conversation, plus the same world/player/
players/entities/inventory/crosshair/controls shapes as the NeoForge body.
Mining is a two-stage dispatch: pathfinder approaches the exact selected block,
then the body verifies that the block is unchanged and diggable before calling
Mineflayer's dig operation. The next observation carries bounded task/outcome
facts; it remains the authority for whether the world actually changed.

The chat ring is useful current-state context, but not the delivery mechanism.
New occurrences are pushed as `minecraft.*` events and kept until FIFO ACK.
After a controller transport failure the body reconnects with the same process
instance and replays every unacknowledged event; observation and event
sequences remain process-lifetime contiguous.

## Lifecycle

The Elysium session (`plugins/life_engine/minecraft/bot_launcher.py`) owns
this process:

1. The session generates its workspace-relative `minecraft/bot_bridge_token.json`
   once using atomic first-creation semantics and restrictive permissions.
2. It starts `node src/index.js` with explicit environment variables, then
   opens the controller listener and waits for the authenticated hello.
3. It waits for two advancing playable observations (`world_loaded=true`).
4. It keeps the reverse listener alive for exact-contract reconnects and only
   ACKs body events after their Life Event append succeeds.
5. On stop, the session releases controls through the bridge, then terminates
   this process. SIGTERM triggers the same graceful shutdown path.

## Manual smoke test

```bash
cd integrations/minecraft_bot
npm ci
ELYSIUM_BOT_BRIDGE_URI=ws://127.0.0.1:18767/elysium \
ELYSIUM_BOT_TOKEN=<token from the generated token file> \
ELYSIUM_BOT_SERVER_HOST=127.0.0.1 \
ELYSIUM_BOT_SERVER_PORT=<server or LAN port> \
ELYSIUM_BOT_USERNAME=AyerElysia \
ELYSIUM_BOT_INSTANCE_ID=bot_smoke \
node src/index.js
```

The controller side is normally the Elysium session itself; for a standalone
protocol check use the live smoke harness in the Python plugin tests.

[mineflayer]: https://github.com/PrismarineJS/mineflayer
