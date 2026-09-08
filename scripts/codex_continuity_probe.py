#!/usr/bin/env python3
"""Explicit Codex-to-Elysia continuity probe through the normal authenticated API.

Run from this repository with uv: scripts/codex_continuity_probe.py
init | read [--limit 20] | send (--text TEXT | --text-file FILE) | close.
Init registers one restricted local client, never starts services or sends chat.
A send performs one injection POST only. Unknown delivery blocks further sends;
inspect the normal stream and retain the audit before any manual reconciliation.
Secrets stay in a private, git-ignored runtime state; close revokes only this client.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import sqlite3
import stat
import sys
import tempfile
from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import uuid4

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from src.app.api.v1.auth_store import AuthStore
from src.app.api.v1.policy import PLATFORM_SERVICE_AUDIENCE
from src.app.runtime.env_local import load_local_env
from src.core.models.stream import ChatStream

ACTOR = "codex-continuity-tester"
SENDER_NAME = "Codex（测试协作者）"
PLATFORM = "ayla"
STREAM_ID = ChatStream.generate_stream_id(PLATFORM, user_id=ACTOR)
SCOPES = ("chat:write", "chat:read", "events:read", "auth:session")
BASE_URL = "http://127.0.0.1:18000"
_INSTALLATION_ENV = "ELYSIUM_INSTALLATION_ID"
_IDENTITY = {
    "actor_id": ACTOR, "sender_id": ACTOR, "sender_name": SENDER_NAME,
    "platform": PLATFORM, "stream_id": STREAM_ID,
}


class ProbeError(RuntimeError):
    """Content-free diagnostic safe for command-line display."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ProbeError("API redirects are forbidden")


def http_request(
    method: str, path: str, payload: dict[str, Any] | None = None, token: str = "",
) -> tuple[int, dict[str, Any]]:
    """Use loopback only, without proxy, redirect, or retry behavior."""
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(BASE_URL + "/api/v1" + path, data=data, headers=headers, method=method)
    try:
        with build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=20) as response:
            body = response.read(4 * 1024 * 1024 + 1)
            if len(body) > 4 * 1024 * 1024:
                raise ProbeError("API response exceeded the probe read budget")
            value = json.loads(body)
            if not isinstance(value, dict):
                raise ProbeError("API response is not an object")
            return response.status, value
    except HTTPError as exc:
        return exc.code, {}
    except ProbeError:
        raise
    except Exception as exc:
        raise ProbeError("API transport or response outcome is unknown") from exc


class Probe:
    """One locally owned external tester; never an Elysia consciousness instance."""

    def __init__(self, root: Path, transport=http_request) -> None:
        self.root = root.resolve(strict=True)
        self.folder = self.root / "runtime" / "codex_continuity_probe"
        self.path = self.folder / "state.json"
        self.transport = transport
        self._locked = False

    def _safe_path(self, path: Path, *, required: bool = False) -> None:
        relative = path.relative_to(self.root)
        current = self.root
        for part in relative.parts:
            current /= part
            if current.is_symlink():
                raise ProbeError("symlink paths are not allowed for probe state or auth")
        if required and not path.is_file():
            raise ProbeError("required existing local file is missing")

    @contextmanager
    def locked(self):
        """Serialize setup and all sends across CLI processes."""
        self._safe_path(self.folder)
        self.folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.folder.stat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ProbeError("probe directory must be owned by this user and mode 0700")
        lock = self.folder / "lock"
        self._safe_path(lock)
        descriptor = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._locked = True
            yield self
        except BlockingIOError as exc:
            raise ProbeError("another probe command currently owns the state") from exc
        finally:
            self._locked = False
            os.close(descriptor)

    def _save(self, state: dict[str, Any]) -> None:
        if not self._locked:
            raise ProbeError("probe state lock is required")
        self._safe_path(self.path)
        fd, temporary = tempfile.mkstemp(prefix=".state-", dir=self.folder)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                os.fchmod(output.fileno(), 0o600)
                json.dump(state, output, ensure_ascii=False, indent=2)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
            directory_fd = os.open(self.folder, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _load(self) -> dict[str, Any]:
        if not self._locked:
            raise ProbeError("probe state lock is required")
        self._safe_path(self.path, required=True)
        info = self.path.stat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise ProbeError("probe state must be owned by this user and mode 0600")
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            raise ProbeError("probe state cannot be read; do not recreate credentials") from exc
        if not isinstance(state, dict) or any(state.get(k) != v for k, v in _IDENTITY.items()):
            raise ProbeError("probe state identity mismatch")
        if not state.get("credential_id"):
            raise ProbeError("incomplete credential setup requires manual inspection")
        if not isinstance(state.get("attempts"), list):
            raise ProbeError("probe audit is missing or invalid")
        return state

    def _store(self) -> AuthStore:
        database = self.root / "runtime" / "app_api_v1" / "auth.sqlite3"
        self._safe_path(database, required=True)
        env_path = self.root / "runtime" / "app_api_v1_env.local"
        self._safe_path(env_path, required=True)
        injected = load_local_env(env_path)
        try:
            installation = os.environ.get(_INSTALLATION_ENV, "").strip()
        finally:
            for name in injected:
                os.environ.pop(name, None)
        if not installation:
            raise ProbeError("existing API installation identity is missing")
        with closing(sqlite3.connect(database.as_uri() + "?mode=rw", uri=True)) as check:
            tables = {row[0] for row in check.execute("SELECT name FROM sqlite_master")}
        if not {"api_credentials", "api_sessions", "api_challenges", "api_tickets"} <= tables:
            raise ProbeError("existing auth database is not initialized; refusing to create it")
        return AuthStore(database, installation_id=installation)

    @staticmethod
    def _check_credential(record, *, allow_revoked: bool = False) -> None:
        if (
            record.actor_id != ACTOR or record.audience != PLATFORM_SERVICE_AUDIENCE
            or record.role != "platform_service" or set(record.scopes) != set(SCOPES)
            or tuple(record.resource_grants) != (f"stream:{STREAM_ID}",)
        ):
            raise ProbeError("credential ownership or exact authorization mismatch")
        if record.revoked_at is not None and not allow_revoked:
            raise ProbeError("probe credential is revoked")

    def _validated(self, *, allow_closed: bool = False) -> dict[str, Any]:
        state = self._load()
        with closing(self._store()) as store:
            record = store.get_credential(state["credential_id"])
            self._check_credential(record, allow_revoked=allow_closed)
        if state.get("closed") and not allow_closed:
            raise ProbeError("probe is closed; no credential will be recreated")
        return state

    def init(self) -> dict[str, Any]:
        """Create at most one credential; preserve an incomplete-setup guard."""
        if self.path.exists() or self.path.is_symlink():
            state = self._validated()
        else:
            with closing(self._store()) as store:
                state = {**_IDENTITY, "attempts": [], "closed": False}
                self._save(state)
                record, secret = store.create_credential_secret(
                    actor_id=ACTOR, scopes=SCOPES, resource_grants=(f"stream:{STREAM_ID}",),
                )
                state.update(credential_id=record.credential_id, secret=secret)
                self._save(state)
        return {**_IDENTITY, "credential_id": state["credential_id"], "status": "ready"}

    @staticmethod
    def _check_session_identity(identity: dict[str, Any], state: dict[str, Any]) -> None:
        if (
            identity.get("actor_id") != ACTOR
            or identity.get("credential_id") != state["credential_id"]
            or identity.get("audience") != PLATFORM_SERVICE_AUDIENCE
            or identity.get("role") != "platform_service"
            or set(identity.get("scopes", [])) != set(SCOPES)
            or identity.get("resource_grants") != [f"stream:{STREAM_ID}"]
        ):
            raise ProbeError("session identity or authorization mismatch")

    def _session(self, state: dict[str, Any]) -> str:
        expiry = state.get("expires_at", "")
        if state.get("access_token") and expiry:
            try:
                if datetime.fromisoformat(expiry) > datetime.now(UTC) + timedelta(seconds=10):
                    status, identity = self.transport(
                        "GET", "/auth/me", token=state["access_token"],
                    )
                    if status != 200:
                        raise ProbeError(f"cached session verification failed with HTTP {status}")
                    self._check_session_identity(identity, state)
                    return state["access_token"]
            except (ValueError, TypeError):
                raise ProbeError("stored session expiry is invalid") from None
        status, response = self.transport("POST", "/auth/sessions", {
            "grant_type": "service_credential", "audience": PLATFORM_SERVICE_AUDIENCE,
            "service_credential": state["secret"],
        })
        if status != 200:
            raise ProbeError(f"session request failed with HTTP {status}")
        self._check_session_identity(response.get("identity", {}), state)
        if not response.get("access_token") or not response.get("expires_at"):
            raise ProbeError("issued session has no access token or expiry")
        state.update(access_token=response["access_token"], expires_at=response["expires_at"])
        self._save(state)
        return state["access_token"]

    def read(self, limit: int = 20) -> dict[str, Any]:
        """Read only this tester's stream; do not automatically resolve unknown sends."""
        if not 1 <= limit <= 100:
            raise ProbeError("read limit must be between 1 and 100")
        state = self._validated()
        token = self._session(state)
        path = f"/chat/streams/{STREAM_ID}/messages?" + urlencode({"limit": limit})
        status, response = self.transport("GET", path, token=token)
        if status != 200:
            raise ProbeError(f"stream read failed with HTTP {status}")
        return response

    def send(self, text: str) -> dict[str, Any]:
        """Journal uncertainty before exactly one non-idempotent injection request."""
        if not text.strip() or len(text) > 100_000:
            raise ProbeError("message must contain 1 to 100000 characters")
        state = self._validated()
        if any(item.get("status") != "accepted" for item in state["attempts"]):
            raise ProbeError("an unresolved send exists; inspect the stream before reconciliation")
        token = self._session(state)
        attempt = {
            "attempt_id": uuid4().hex, "started_at": datetime.now(UTC).isoformat(),
            "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "utf8_bytes": len(text.encode("utf-8")), "status": "unknown",
        }
        state["attempts"].append(attempt)
        self._save(state)
        status, response = self.transport("POST", "/chat/messages:inject", {
            "stream_id": STREAM_ID, "content": text, "sender_id": ACTOR,
            "sender_name": SENDER_NAME, "platform": PLATFORM, "chat_type": "private",
        }, token=token)
        if (
            status != 202 or response.get("accepted") is not True
            or response.get("stream_id") != STREAM_ID
            or not isinstance(response.get("message_id"), str) or not response["message_id"]
        ):
            raise ProbeError(f"injection outcome unknown (HTTP {status}); do not retry")
        attempt.update(status="accepted", message_id=response["message_id"])
        self._save(state)
        return {"accepted": True, "stream_id": STREAM_ID, "message_id": attempt["message_id"]}

    def close(self) -> dict[str, Any]:
        """Revoke exactly the verified owned credential; retain local and server audit."""
        state = self._validated(allow_closed=True)
        with closing(self._store()) as store:
            self._check_credential(
                store.get_credential(state["credential_id"]), allow_revoked=True,
            )
            store.revoke_credential(state["credential_id"])
        state["closed"] = True
        state["closed_at"] = state.get("closed_at") or datetime.now(UTC).isoformat()
        state.pop("secret", None)
        state.pop("access_token", None)
        self._save(state)
        return {"closed": True, "actor_id": ACTOR, "credential_id": state["credential_id"]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init")
    commands.add_parser("close")
    reader = commands.add_parser("read")
    reader.add_argument("--limit", type=int, default=20)
    sender = commands.add_parser("send")
    content = sender.add_mutually_exclusive_group(required=True)
    content.add_argument("--text")
    content.add_argument("--text-file", type=Path)
    args = parser.parse_args(argv)
    try:
        with Probe(_REPOSITORY_ROOT).locked() as probe:
            if args.command == "send":
                text = args.text
                if text is None:
                    with args.text_file.open("r", encoding="utf-8", newline="") as source:
                        text = source.read(100_001)
                result = probe.send(text)
            elif args.command == "read":
                result = probe.read(args.limit)
            else:
                result = getattr(probe, args.command)()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except ProbeError as exc:
        print(str(exc), file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 - never echo third-party secrets at the CLI
        print(f"probe stopped safely: {type(exc).__name__}; no automatic retry", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
