# Elysium Embodiment Bridge

Client-only NeoForge 1.21.1 bridge for Elysia's visible Minecraft body. It
publishes complete structured observations and accepts correlated operations
over an authenticated WebSocket connection.

The bridge connects outbound to `ws://127.0.0.1:18765/elysium` by default. The
Windows-to-WSL localhost forwarding path avoids inbound firewall rules. On
first start it creates
`config/elysium_bridge.json` with a random token. The token is never printed to
the game log. Authentication remains mandatory; keep the generated token
private.

Build with `./gradlew clean test build --no-daemon`. The distributable jar is
written under `build/libs/`. Its version and digest are pinned in
`bridge-artifact-lock.json`; use `deploy_bridge.ps1` instead of copying it by
hand. `prepare_launcher.ps1` configures exact single-player quick play, and
`agent_live_smoke.py` proves one safe observation/action/evidence loop. The
full operational procedure is documented in
`docs/operations/minecraft_production_runbook.md`.
