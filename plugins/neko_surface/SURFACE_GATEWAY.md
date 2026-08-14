# N.E.K.O Surface Gateway

Elysium owns identity, memory, LifeChatter, autonomy, and permissions. N.E.K.O
owns avatar rendering, local TTS/audio playback, and user interaction. The gateway
only transports versioned events between those boundaries.

## Endpoint

`ws://127.0.0.1:18000/api/neko-surface/ws`

The client must authenticate with `Authorization: Bearer <token>` (or the
`X-NEKO-Surface-Token` header) and send an `elysia.surface.v1` `hello` event first.

## Elysium environment

```bash
NEKO_SURFACE_TOKEN=replace-with-a-long-random-token
NEKO_SURFACE_QUEUE_SIZE=128
NEKO_SURFACE_MAX_CLIENTS=8
NEKO_SURFACE_MIRROR_ALL=0
```

`NEKO_SURFACE_MIRROR_ALL=1` mirrors messages delivered on other platforms into
N.E.K.O as display-only events. It is off by default and never feeds those mirrored
events back through `MessageSender`, so it cannot create duplicate history or loops.

## Contract

Every event contains `schema_version`, `event_id`, `sequence`, `session_id`,
`turn_id`, `surface_id`, `character`, `origin`, `type`, `payload`, and `priority`.
Client inputs use `user.text`, `user.transcript.final`, `user.audio`, and
`user.interaction`. `user.audio` carries a bounded base64 WAV/MP3 payload; Neo
materializes it as a voice attachment so an audio-capable primary model can hear
the original recording directly.
Neo outputs use `assistant.text`, `assistant.voice`, `presentation.expression`,
`presentation.motion`, and `turn.end`. Both directions use `ack`, `error`, and
`state` for transport control.
