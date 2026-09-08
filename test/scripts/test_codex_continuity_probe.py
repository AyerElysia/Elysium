"""Isolated probe contracts: no live API, formal database, or messages."""

from __future__ import annotations

import hashlib
import json
import stat
from collections.abc import Callable, Iterator
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from scripts import codex_continuity_probe as probe_module
from scripts.codex_continuity_probe import (
    ACTOR,
    PLATFORM,
    SCOPES,
    SENDER_NAME,
    STREAM_ID,
    Probe,
    ProbeError,
)
from src.app.api.v1.auth_store import AuthStore
from src.app.api.v1.policy import PLATFORM_SERVICE_AUDIENCE
from src.app.api.v1.tokens import SignedValueCodec
from src.core.models.stream import ChatStream

INSTALLATION = "isolated-probe-tests"
IDENTITY = {
    "actor_id": ACTOR,
    "sender_id": ACTOR,
    "sender_name": SENDER_NAME,
    "platform": PLATFORM,
    "stream_id": STREAM_ID,
}


class Harness:
    """Issue real temporary sessions, but fake every HTTP operation."""

    def __init__(self, root: Path, store: AuthStore) -> None:
        self.root = root
        self.store = store
        self.calls: list[tuple[str, str, dict[str, Any] | None, str]] = []
        self.identity: dict[str, Any] = {}
        self.me_override: dict[str, Any] = {}
        self.inject: Callable[[], tuple[int, dict[str, Any]]] | None = None
        self.probe = Probe(root, transport=self.transport)

    def state(self) -> dict[str, Any]:
        return json.loads(self.probe.path.read_text(encoding="utf-8"))

    def save_state(self, state: dict[str, Any]) -> None:
        self.probe.path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

    def transport(
        self, method: str, path: str, payload: dict[str, Any] | None = None, token: str = "",
    ) -> tuple[int, dict[str, Any]]:
        self.calls.append((method, path, payload, token))
        if (method, path) == ("POST", "/auth/sessions"):
            assert payload == {
                "grant_type": "service_credential",
                "audience": PLATFORM_SERVICE_AUDIENCE,
                "service_credential": self.state()["secret"],
            }
            session, access, refresh = self.store.issue_session_from_credential(
                credential=payload["service_credential"],
                audience=PLATFORM_SERVICE_AUDIENCE,
                codec=SignedValueCodec("isolated-signing-key-for-tests-only"),
                access_ttl=timedelta(minutes=5),
                refresh_ttl=timedelta(hours=1),
            )
            self.identity = {
                "actor_id": session.actor_id,
                "credential_id": session.credential_id,
                "audience": session.audience,
                "role": session.role,
                "scopes": list(session.scopes),
                "resource_grants": list(session.resource_grants),
                "expires_at": session.access_expires_at.isoformat(),
                "session_id": session.session_id,
            }
            return 200, {
                "identity": dict(self.identity),
                "access_token": access,
                "refresh_token": refresh,
                "expires_at": session.access_expires_at.isoformat(),
            }
        assert token
        if (method, path) == ("GET", "/auth/me"):
            return 200, {**self.identity, **self.me_override}
        if method == "GET":
            assert path.startswith(f"/chat/streams/{STREAM_ID}/messages?")
            return 200, {"items": [{"message_id": "own-message", "stream_id": STREAM_ID}]}
        if (method, path) == ("POST", "/chat/messages:inject"):
            assert self.state()["attempts"][-1]["status"] == "unknown"
            if self.inject is not None:
                return self.inject()
            return 202, {"accepted": True, "stream_id": STREAM_ID, "message_id": "accepted-message"}
        raise AssertionError(f"unexpected fake operation: {method} {path}")

    def injections(self) -> list[tuple[str, str, dict[str, Any] | None, str]]:
        return [call for call in self.calls if call[:2] == ("POST", "/chat/messages:inject")]


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Harness]:
    monkeypatch.delenv("ELYSIUM_INSTALLATION_ID", raising=False)
    database = tmp_path / "runtime" / "app_api_v1" / "auth.sqlite3"
    with closing(AuthStore(database, installation_id=INSTALLATION)) as store:
        (tmp_path / "runtime" / "app_api_v1_env.local").write_text(
            f"ELYSIUM_INSTALLATION_ID={INSTALLATION}\n", encoding="utf-8",
        )
        yield Harness(tmp_path, store)


def test_init_reuses_one_exactly_scoped_identity_and_keeps_secret_private(harness: Harness) -> None:
    with harness.probe.locked():
        first = harness.probe.init()
        second = harness.probe.init()
    with Probe(harness.root, transport=harness.transport).locked() as restarted:
        assert restarted.init() == first
    assert first == second
    assert {key: first[key] for key in IDENTITY} == IDENTITY
    assert STREAM_ID == ChatStream.generate_stream_id("ayla", user_id="codex-continuity-tester")
    records = harness.store.list_credentials()
    assert len(records) == 1
    assert records[0].credential_id == first["credential_id"]
    assert records[0].actor_id == ACTOR
    assert records[0].role == "platform_service"
    assert records[0].audience == PLATFORM_SERVICE_AUDIENCE
    assert set(records[0].scopes) == set(SCOPES)
    assert records[0].resource_grants == (f"stream:{STREAM_ID}",)
    state = harness.state()
    assert state["secret"]
    assert state["secret"] not in json.dumps(first)
    assert "secret" not in first and "access_token" not in first
    assert stat.S_IMODE(harness.probe.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(harness.probe.folder.stat().st_mode) == 0o700
    assert harness.calls == []


@pytest.mark.parametrize("field", tuple(IDENTITY))
def test_tampered_state_identity_blocks_init_send_read_and_close(harness: Harness, field: str) -> None:
    with harness.probe.locked():
        original = harness.probe.init()
    state = harness.state()
    state[field] = "unrelated-identity"
    harness.save_state(state)
    with harness.probe.locked():
        for action in (harness.probe.init, harness.probe.read, lambda: harness.probe.send("test"), harness.probe.close):
            with pytest.raises(ProbeError, match="identity mismatch"):
                action()
    assert harness.calls == []
    assert harness.store.get_credential(original["credential_id"]).revoked_at is None


def test_foreign_credential_id_cannot_revoke_another_actor(harness: Harness) -> None:
    with harness.probe.locked():
        owned = harness.probe.init()
    foreign, _secret = harness.store.create_credential_secret(
        actor_id="another-client", scopes=SCOPES, resource_grants=(f"stream:{STREAM_ID}",),
    )
    state = harness.state()
    state["credential_id"] = foreign.credential_id
    harness.save_state(state)
    with harness.probe.locked(), pytest.raises(ProbeError, match="ownership"):
        harness.probe.close()
    assert harness.store.get_credential(foreign.credential_id).revoked_at is None
    assert harness.store.get_credential(owned["credential_id"]).revoked_at is None
    assert harness.calls == []


def test_send_journals_unknown_before_one_post_and_records_known_acceptance(harness: Harness) -> None:
    text = "Codex 的隔离测试输入"
    with harness.probe.locked():
        harness.probe.init()
        result = harness.probe.send(text)
    injections = harness.injections()
    assert len(injections) == 1
    assert injections[0][2] == {
        "stream_id": STREAM_ID, "content": text, "sender_id": ACTOR,
        "sender_name": SENDER_NAME, "platform": PLATFORM, "chat_type": "private",
    }
    assert result == {"accepted": True, "stream_id": STREAM_ID, "message_id": "accepted-message"}
    attempts = harness.state()["attempts"]
    assert len(attempts) == 1
    assert attempts[0]["status"] == "accepted"
    assert attempts[0]["message_id"] == result["message_id"]
    assert attempts[0]["text_sha256"] == hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert attempts[0]["utf8_bytes"] == len(text.encode("utf-8"))
    assert text not in harness.probe.path.read_text(encoding="utf-8")


def test_network_unknown_is_durable_and_never_retried_even_after_restart(harness: Harness) -> None:
    def disconnected() -> tuple[int, dict[str, Any]]:
        raise OSError("simulated disconnect after remote acceptance")

    harness.inject = disconnected
    with harness.probe.locked():
        harness.probe.init()
        with pytest.raises((OSError, ProbeError)):
            harness.probe.send("first send")
        before = harness.state()["attempts"]
        assert before[0]["status"] == "unknown"
        with pytest.raises(ProbeError, match="unresolved"):
            harness.probe.send("must not resend")
        harness.probe.read()
        assert harness.state()["attempts"] == before
    with (
        Probe(harness.root, transport=harness.transport).locked() as restarted,
        pytest.raises(ProbeError, match="unresolved"),
    ):
        restarted.send("restart must not resend")
    assert harness.state()["attempts"] == before
    assert len(harness.injections()) == 1


@pytest.mark.parametrize(
    ("status", "response"),
    [
        (500, {}),
        (202, {"accepted": False, "stream_id": STREAM_ID, "message_id": "m"}),
        (202, {"accepted": True, "stream_id": "other-stream", "message_id": "m"}),
        (202, {"accepted": True, "stream_id": STREAM_ID, "message_id": ""}),
    ],
)
def test_unverified_acceptance_remains_unknown_and_blocks_resend(
    harness: Harness, status: int, response: dict[str, Any],
) -> None:
    harness.inject = lambda: (status, response)
    with harness.probe.locked():
        harness.probe.init()
        with pytest.raises(ProbeError, match="unknown"):
            harness.probe.send("one attempt")
        with pytest.raises(ProbeError, match="unresolved"):
            harness.probe.send("blocked")
    assert len(harness.injections()) == 1
    assert harness.state()["attempts"][0]["status"] == "unknown"


def test_read_is_restricted_to_own_stream_with_bounded_limit(harness: Harness) -> None:
    with harness.probe.locked():
        harness.probe.init()
        result = harness.probe.read(limit=7)
        with pytest.raises(ProbeError, match="limit"):
            harness.probe.read(limit=101)
    reads = [call for call in harness.calls if call[0] == "GET"]
    assert [call[1] for call in reads] == [f"/chat/streams/{STREAM_ID}/messages?limit=7"]
    assert result["items"][0]["stream_id"] == STREAM_ID
    assert harness.injections() == []
    assert harness.state()["attempts"] == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("actor_id", "another-client"),
        ("credential_id", "another-credential"),
        ("audience", "admin"),
        ("role", "admin"),
        ("scopes", [*SCOPES, "admin:write"]),
        ("resource_grants", ["stream:*"]),
    ],
)
def test_cached_token_identity_mismatch_blocks_chat(
    harness: Harness, field: str, value: str | list[str],
) -> None:
    with harness.probe.locked():
        harness.probe.init()
        harness.probe.read()
        harness.me_override = {field: value}
        calls_before = len(harness.calls)
        with pytest.raises(ProbeError, match="identity|authorization"):
            harness.probe.send("must not impersonate another credential")
    assert [(call[0], call[1]) for call in harness.calls[calls_before:]] == [("GET", "/auth/me")]
    assert harness.injections() == []
    assert harness.state()["attempts"] == []


def test_close_revokes_only_owned_credential_sessions_and_preserves_audit(harness: Harness) -> None:
    foreign, secret = harness.store.create_credential_secret(
        actor_id="unrelated-client", scopes=("chat:read",), resource_grants=("stream:unrelated",),
    )
    foreign_session, _access, _refresh = harness.store.issue_session_from_credential(
        credential=secret, audience=PLATFORM_SERVICE_AUDIENCE,
        codec=SignedValueCodec("isolated-signing-key-for-tests-only"),
        access_ttl=timedelta(minutes=5), refresh_ttl=timedelta(hours=1),
    )
    with harness.probe.locked():
        owned = harness.probe.init()
        harness.probe.send("retained audit")
        before = harness.state()["attempts"]
        result = harness.probe.close()
        closed_state = harness.state()
        assert harness.probe.close() == result
        with pytest.raises(ProbeError, match="closed|revoked"):
            harness.probe.init()
    assert result == {"closed": True, "actor_id": ACTOR, "credential_id": owned["credential_id"]}
    assert closed_state["closed"] is True
    assert closed_state["attempts"] == before
    assert closed_state["closed_at"] == harness.state()["closed_at"]
    assert "secret" not in closed_state and "access_token" not in closed_state
    assert harness.probe.path.exists()
    assert harness.store.get_credential(owned["credential_id"]).revoked_at is not None
    assert harness.store.get_credential(foreign.credential_id).revoked_at is None
    sessions = {item.session.session_id: item.session for item in harness.store.list_sessions()}
    assert sessions[foreign_session.session_id].revoked_at is None
    owned_sessions = [session for session in sessions.values() if session.credential_id == owned["credential_id"]]
    assert len(owned_sessions) == 1
    assert owned_sessions[0].revoked_at is not None
    assert len(harness.injections()) == 1


def test_missing_auth_database_is_not_created(tmp_path: Path) -> None:
    def forbidden_transport(*args, **kwargs):
        pytest.fail("missing database must not access HTTP")

    probe = Probe(tmp_path, transport=forbidden_transport)
    with probe.locked(), pytest.raises(ProbeError, match="missing"):
        probe.init()
    assert not (tmp_path / "runtime" / "app_api_v1" / "auth.sqlite3").exists()
    assert not probe.path.exists()


@pytest.mark.parametrize("target", ["state", "folder", "database", "environment", "lock"])
def test_symlink_paths_are_rejected_without_following_target(harness: Harness, target: str) -> None:
    with harness.probe.locked():
        harness.probe.init()
    sentinel = harness.root / "sentinel"
    sentinel.write_text("unchanged", encoding="utf-8")
    paths = {
        "state": harness.probe.path,
        "folder": harness.probe.folder,
        "database": harness.root / "runtime" / "app_api_v1" / "auth.sqlite3",
        "environment": harness.root / "runtime" / "app_api_v1_env.local",
        "lock": harness.probe.folder / "lock",
    }
    selected = paths[target]
    preserved = selected.with_name(selected.name + ".preserved")
    selected.rename(preserved)
    selected.symlink_to(preserved, target_is_directory=target == "folder")
    with pytest.raises(ProbeError, match="symlink"), harness.probe.locked():
        harness.probe.init()
    assert sentinel.read_text(encoding="utf-8") == "unchanged"
    assert len(harness.store.list_credentials()) == 1
    assert harness.calls == []


def test_cli_text_file_preserves_exact_newlines(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    text = "first\r\nsecond\rthird\n"
    source = harness.root / "message.txt"
    source.write_bytes(text.encode("utf-8"))
    with harness.probe.locked():
        harness.probe.init()
    monkeypatch.setattr(probe_module, "_REPOSITORY_ROOT", harness.root)
    monkeypatch.setattr(probe_module, "Probe", lambda root: harness.probe)
    assert probe_module.main(["send", "--text-file", str(source)]) == 0
    assert harness.injections()[0][2]["content"] == text
    assert harness.state()["attempts"][0]["text_sha256"] == hashlib.sha256(text.encode("utf-8")).hexdigest()
    output = capsys.readouterr()
    assert output.err == ""
    assert text not in output.out


def test_cli_redacts_unexpected_exception_without_live_root(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    class FailingProbe:
        def __init__(self, root: Path) -> None:
            assert root == harness.root

        def locked(self):
            raise OSError("private-token-and-message-body")

    monkeypatch.setattr(probe_module, "_REPOSITORY_ROOT", harness.root)
    monkeypatch.setattr(probe_module, "Probe", FailingProbe)
    assert probe_module.main(["read"]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert "OSError" in output.err
    assert "private-token-and-message-body" not in output.err
