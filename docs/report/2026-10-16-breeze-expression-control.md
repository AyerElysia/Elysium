# Breeze TTS 2 expression interface update (2026-10-16)

## Outcome

The Elysium voice interfaces now accept an optional expression_instruction for one spoken expression. It can describe emotion, delivery, speed, volume, pauses, breathing, or an inner feeling. The configured Aili reference audio and its exact transcript remain deployment-owned and are not exposed as action parameters.

## Runtime behavior

- Breeze keeps the configured breeze_instruction as the style baseline.
- expression_instruction is appended as a per-expression delivery direction with an explicit instruction not to change speaker identity, age, timbre, or reference audio.
- The primary life_send_voice action and the generic TTS action expose the field.
- Breeze inline vocal events are preserved in Breeze mode: Chinese [叹气], [笑], [咳嗽], [清嗓子], and the documented English equivalents.
- Legacy providers keep the previous cleanup behavior for those bracketed/parenthesized controls.
- The reference audio remains the style configuration path; no action or service argument can replace it.

## Validation

- Focused TTS, generic-action, and Life Chatter voice tests: 98 passed with no-cov.
- The same focused selection reports the repository's expected coverage-threshold failure when coverage enforcement is enabled; this is a selection-wide coverage issue, not a test failure.
- Configuration backup: config/plugins/tts_voice_plugin/config.toml.bak-20261016-expression.
- Source backups: each edited Python file has a .bak-20261016-breeze-expression sibling.

Elysium was not restarted. A manual restart is required before the running process loads the new action schema and service code.
