from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from plugins.voice_live.config import VoiceLiveConfig
from plugins.voice_live.router import VoiceLiveRouter

ROOT = Path(__file__).resolve().parents[3]


def test_browser_uses_worklet_clocked_pcm_and_immediate_barge_in() -> None:
    html = (ROOT / "plugins/voice_live/static/voice_live.html").read_text(
        encoding="utf-8"
    )
    assert "AudioWorkletNode" in html
    assert "createScriptProcessor" not in html
    assert "nextPlayTime" in html
    assert "playback.clear" in html
    assert "echoCancellation:true" in html
    assert "noiseSuppression:true" in html
    assert "channelCountMode:'explicit'" in html
    assert "bestEnergy" in html
    assert "startAudioPump(generation)" in html
    assert "MAX_MIC_QUEUE" in html
    assert "MIC_SILENCE_MS" in html
    assert "麦克风正在上传静音" in html
    assert "micHealth" in html
    assert "micRate" in html
    assert "voiceProfile" in html
    assert "voice_conversion" in html
    assert "method:'POST'" in html
    assert (
        "VL1" not in html
    )  # The binary magic is emitted as exact bytes, not text guessing.


def test_browser_fully_retires_previous_audio_generation() -> None:
    html = (ROOT / "plugins/voice_live/static/voice_live.html").read_text(
        encoding="utf-8"
    )
    assert "audioGeneration" in html
    assert "generation===audioGeneration" in html
    assert "micSource.disconnect()" in html
    assert "worklet.port.onmessage=null" in html
    assert "worklet.port.close()" in html
    assert "await oldContext.close()" in html
    assert "audioCtx=null;nextPlayTime=0;playbackStartedAt=0" in html
    assert "await stopAudio();const generation=++audioGeneration" in html
    assert "generation!==audioGeneration||explicitStop" in html
    assert "const current=()=>generation===audioGeneration&&ws===socket" in html
    assert "await stopAudio();el('start').disabled=false" in html


def test_obs_overlay_is_read_only_and_receives_audio() -> None:
    html = (ROOT / "plugins/voice_live/static/overlay.html").read_text(encoding="utf-8")
    assert "/observe?ticket=" in html
    assert "getUserMedia" not in html
    assert "createBufferSource" in html
    assert "background:transparent" in html
    assert "m.voice_profile" in html


def test_ticket_is_signed_single_use_and_routes_are_renderable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = VoiceLiveConfig()
    config.observability.trace_root = str(tmp_path)
    plugin = SimpleNamespace(config=config)
    router = VoiceLiveRouter(plugin)
    client = TestClient(router.app)
    headers = {"origin": "http://127.0.0.1:8000", "host": "127.0.0.1:8000"}
    response = client.post("/ticket", headers=headers)
    assert response.status_code == 200
    ticket = response.json()["ticket"]
    assert router._consume_ticket(ticket) is True
    assert router._consume_ticket(ticket) is False
    assert client.get("/").status_code == 200
    assert client.get("/overlay").status_code == 200
    health = client.get("/health").json()
    assert health["protocol"] == 1
    assert health["provider"] == "minicpm_omni"

    monkeypatch.delenv("VOICE_LIVE_API_KEY", raising=False)
    config.full_duplex.provider_type = "qwen_realtime"
    config.full_duplex.api_key_file = str(tmp_path / "missing.key")
    degraded = client.get("/health").json()
    assert degraded["status"] == "degraded"
    assert degraded["configured"] is False
    assert degraded["readiness"]["provider_credential"] is False


def test_voice_live_tree_contains_no_inline_api_key() -> None:
    for path in (ROOT / "plugins/voice_live").rglob("*"):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        assert "sk-" not in text
        assert 'api_key="' not in text


def test_manifest_makes_voice_live_discoverable_with_life_engine_dependency() -> None:
    manifest = json.loads(
        (ROOT / "plugins/voice_live/manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["name"] == "Voice-Live"
    assert manifest["dependencies"]["plugins"] == ["life_engine"]
    components = {
        (item["component_type"], item["component_name"]) for item in manifest["include"]
    }
    assert components == {
        ("router", "voice_live"),
        ("event_handler", "voice_live_handler"),
    }
