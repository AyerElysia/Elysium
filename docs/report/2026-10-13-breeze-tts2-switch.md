# BreezeTTS2 switch report (2026-10-13)

## Outcome

BreezeTTS2 is running on the Spark host at `http://127.0.0.1:7862`. The service health endpoint returned HTTP 200 with a 24 kHz sample rate. A real reference-voice synthesis request returned HTTP 200 and produced 138,240 bytes of signed 16-bit PCM audio; it was converted to `/tmp/breeze-test3.wav` for playback.

## Changes

- Started container `breeze-tts2` from the locally available GPU/SoX image, with the Breeze model mounted read-only.
- Built `breeze-vllm-pinned2:latest` from the local image and pinned Transformers 4.57.3 plus huggingface-hub 0.36.0. The earlier Transformers 5.14 compatibility experiment was stopped; temporary source edits were restored from their backups.
- Container restart policy is `unless-stopped`; the service listens only on loopback port 7862.
- Updated `config/plugins/tts_voice_plugin/config.toml`:
  - `backend = "breeze_tts2"`
  - `server = "http://127.0.0.1:7862"`
  - `timeout = 1200`
  - `auto_start = false`
- Original Elysium TTS configuration is backed up at `config/plugins/tts_voice_plugin/config.toml.bak-breeze-20261013`.

## Diagnosis

The old GPT-SoVITS process was an orphaned long-running process whose stderr pipe no longer had a reader. Elysium logged HTTP 400 Broken pipe failures. The old backend was not used for the Breeze test.

## Restart boundary

Elysium itself was not restarted. Its running process will keep its old configuration until the operator manually restarts it. After that manual restart, verify a voice request through Elysium and keep the Breeze container running. To roll back, restore the configuration backup and manually restart Elysium.

## Notes

The Breeze model/license is for research/non-commercial use as documented by the checked-out Breeze package. The Breeze service currently emits raw PCM with `X-Sample-Rate: 24000` and `X-Sample-Format: s16le`.

## Chinese synthesis validation

A Chinese reference transcript and Chinese output request returned HTTP 200 and produced 149760 bytes of PCM. The playable sample is /tmp/breeze-cn.wav and was copied to the local workspace as breeze-tts2-cn-test.wav.
