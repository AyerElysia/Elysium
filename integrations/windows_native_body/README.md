# Windows native-input body

This sidecar is the genuinely biomimetic route. It captures first-person pixels
from the visible Windows desktop through DXGI or Windows Graphics Capture and
uses one batched `SendInput` call to maintain simultaneous keyboard and mouse
state. It does not call Minecraft gameplay APIs.

The first run creates
`G:\Game\Minecraft\.minecraft\config\elysium_native_bridge.json` with a random
authentication token. The token is never logged. The sidecar connects outward
to Elysium's authenticated WSL listener and releases every held key or mouse
button on an interrupt, disconnect, or process shutdown.

Run `install.ps1` once, then use `run.ps1` for normal startup. Both this sidecar
and the NeoForge bridge connect from Windows to WSL through localhost
forwarding. Installation therefore requires no inbound firewall rule or
transport relay; authentication remains end-to-end between Elysium and the
selected body.

Run `live_smoke.py` from WSL while Minecraft is in a world to cross-validate a
physical keyboard and mouse batch against the independent NeoForge state
sensor. The smoke test also hashes immutable before/after first-person frames.

Because Windows delivers physical input to the foreground window, this route
owns focus while acting. To let Ayer and Elysia independently control two game
clients at the same time, run Elysia's biomimetic body in a separate Windows
session, VM with GPU acceleration, or second machine. The structured NeoForge
agent route does not have this limitation.
