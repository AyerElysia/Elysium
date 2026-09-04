"""Generation registry and transactional writer coordination."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import secrets
import threading
from collections.abc import Callable
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, TypeVar
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from src.kernel.storage.migration_runner import (
    MySQLMigrationRunner,
    SchemaMigration,
)
from src.kernel.storage.outbox_primitives import canonical_json, canonical_json_sha256

from .models import AuthorityToken, BackendGeneration, BackendKind, GenerationStatus

T = TypeVar("T")
logger = logging.getLogger("life_engine.storage.authority")


class AuthorityError(RuntimeError):
    """Base class for backend-generation authority failures."""


class AuthorityConflict(AuthorityError):
    """Raised when a coordinator acts on a stale authority epoch."""


class StaleAuthorityToken(AuthorityError):
    """Raised when a writer presents an expired or superseded fencing token."""


class GenerationConflict(AuthorityError):
    """Raised when one generation identity is reused for a different manifest."""


class GenerationNotVerified(AuthorityError):
    """Raised when an unverified generation is selected as writable authority."""


class AuthorityRegistry(Protocol):
    """Control-plane contract shared by local and multi-node registries."""

    async def register_generation(self, generation: BackendGeneration) -> None: ...

    async def get_generation(
        self,
        generation_id: str,
    ) -> BackendGeneration | None: ...

    async def activate_generation(
        self,
        generation_id: str,
        *,
        expected_epoch: int,
        owner_id: str,
        lease_seconds: int,
        confirm_previous_writers_stopped: bool,
    ) -> AuthorityToken: ...

    async def renew(
        self,
        token: AuthorityToken,
        *,
        lease_seconds: int,
    ) -> AuthorityToken: ...

    async def revoke(self, token: AuthorityToken) -> int: ...

    async def validate(self, token: AuthorityToken) -> None: ...

    async def health(self) -> dict[str, Any]: ...


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _empty_file_state(registry_id: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "registry_id": registry_id,
        "active_backend": "",
        "active_generation": "",
        "authority_epoch": 0,
        "fencing_token_hash": "",
        "owner_id": "",
        "lease_until": "",
        "last_event_hash": "",
        "updated_at": "",
        "generations": {},
    }


def _lock_file(handle: Any, *, exclusive: bool) -> None:
    """Acquire a blocking shared or exclusive advisory lock."""

    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        mode = msvcrt.LK_LOCK if exclusive else msvcrt.LK_RLCK
        msvcrt.locking(handle.fileno(), mode, 1)
        return

    import fcntl

    mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    fcntl.flock(handle.fileno(), mode)


def _unlock_file(handle: Any) -> None:
    """Release a platform-specific advisory lock."""

    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@dataclass(frozen=True, slots=True)
class _VerifiedFileAuditHead:
    """One process-local proof for an unchanged file authority audit head."""

    file_identity: tuple[int, int, int, int, int] | None
    head_hash: str
    event_count: int
    generation_registrations: tuple[tuple[str, str], ...]


class FileAuthorityRegistry:
    """Host-local authority registry guarded by two advisory locks.

    Cutover lock: writers hold shared mode for one durable unit of work;
    activate/revoke take exclusive mode so a new epoch cannot overtake an
    in-flight writer.  Audit lock: brief shared/exclusive section covering
    hash-chain verify and state mutation only.  Incumbent lease renewal uses
    the audit lock and must not wait for in-flight writer fences.  Multi-host
    deployments must use :class:`MySQLAuthorityRegistry`.
    """

    def __init__(self, state_path: str | Path, *, registry_id: str = "life-domain") -> None:
        self.state_path = Path(state_path)
        self.lock_path = self.state_path.with_suffix(self.state_path.suffix + ".lock")
        self.audit_lock_path = self.state_path.with_suffix(
            self.state_path.suffix + ".audit.lock"
        )
        self.audit_path = self.state_path.with_suffix(self.state_path.suffix + ".audit.jsonl")
        self.registry_id = registry_id.strip()
        if not self.registry_id:
            raise ValueError("authority registry_id must not be empty")
        self._verified_audit_head: _VerifiedFileAuditHead | None = None
        self._keepalive_stop = threading.Event()
        self._keepalive_guard = threading.Lock()
        self._keepalive_thread: threading.Thread | None = None
        self._keepalive_token: AuthorityToken | None = None
        self._keepalive_lease_seconds = 0
        self._keepalive_interval_seconds = 0.0

    def _ensure_lock_files(self) -> None:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        for path in (self.lock_path, self.audit_lock_path):
            path.touch(exist_ok=True)

    def _prepare_lock_path(self, path: Path, *, create: bool) -> None:
        if create:
            self._ensure_lock_files()
            return
        if not self.lock_path.exists() and not self.audit_lock_path.exists():
            raise AuthorityError("authority registry has not been initialized")
        self._ensure_lock_files()
        if not path.exists():
            raise AuthorityError("authority registry has not been initialized")

    @contextmanager
    def _lock_file_ctx(self, path: Path, *, exclusive: bool, create: bool) -> Any:
        self._prepare_lock_path(path, create=create)
        mode = "a+b" if create else "rb"
        with path.open(mode) as handle:
            _lock_file(handle, exclusive=exclusive)
            try:
                yield
            finally:
                _unlock_file(handle)

    @contextmanager
    def _lock(self, *, exclusive: bool, create: bool) -> Any:
        """Acquire the cutover lock used to isolate epoch changes from writers."""

        with self._lock_file_ctx(
            self.lock_path, exclusive=exclusive, create=create
        ):
            yield

    @contextmanager
    def _audit_lock(self, *, exclusive: bool, create: bool) -> Any:
        """Acquire the brief lock used for audit verify and state mutation."""

        with self._lock_file_ctx(
            self.audit_lock_path, exclusive=exclusive, create=create
        ):
            yield

    def _read_state_unlocked(self, *, allow_missing: bool) -> dict[str, Any]:
        if not self.state_path.exists():
            if allow_missing:
                return _empty_file_state(self.registry_id)
            raise AuthorityError("authority registry state is missing")
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AuthorityError("authority registry state is unreadable") from exc
        if not isinstance(value, dict):
            raise AuthorityError("authority registry state root must be an object")
        if value.get("schema_version") != 1:
            raise AuthorityError("authority registry schema is incompatible")
        if value.get("registry_id") != self.registry_id:
            raise AuthorityError("authority registry identity mismatch")
        if not isinstance(value.get("generations"), dict):
            raise AuthorityError("authority generation map is invalid")
        return value

    def _write_state_unlocked(self, state: dict[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_name(
            f".{self.state_path.name}.{uuid4().hex}.tmp"
        )
        encoded = (json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
            "utf-8"
        )
        try:
            with temporary.open("xb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(self.state_path)
            if os.name != "nt":
                directory_fd = os.open(self.state_path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            if temporary.exists():
                temporary.unlink()

    def _audit_file_identity_unlocked(
        self,
    ) -> tuple[int, int, int, int, int] | None:
        """Return the audit identity used to invalidate a verified head."""

        if not self.audit_path.exists():
            return None
        try:
            stat = self.audit_path.stat()
        except OSError as exc:
            raise AuthorityError("authority audit is unreadable") from exc
        return (
            int(stat.st_dev),
            int(stat.st_ino),
            int(stat.st_size),
            int(stat.st_mtime_ns),
            int(stat.st_ctime_ns),
        )

    def _append_event_unlocked(
        self,
        state: dict[str, Any],
        *,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        previous_hash = str(state.get("last_event_hash") or "")
        previous_identity = self._audit_file_identity_unlocked()
        verified = self._verified_audit_head
        body = {
            "event_id": str(uuid4()),
            "registry_id": self.registry_id,
            "event_type": event_type,
            "recorded_at": _iso(_utc_now()),
            "previous_event_hash": previous_hash,
            "payload": payload,
        }
        event_hash = canonical_json_sha256(body)
        event = {**body, "event_hash": event_hash}
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        with self.audit_path.open("a", encoding="utf-8") as handle:
            handle.write(canonical_json(event) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        state["last_event_hash"] = event_hash
        current_identity = self._audit_file_identity_unlocked()
        if (
            verified is not None
            and verified.file_identity == previous_identity
            and secrets.compare_digest(verified.head_hash, previous_hash)
        ):
            registrations = verified.generation_registrations
            if event_type == "generation_registered":
                registrations = (
                    *registrations,
                    (
                        str(payload.get("generation_id") or ""),
                        str(payload.get("manifest_sha256") or ""),
                    ),
                )
            self._verified_audit_head = _VerifiedFileAuditHead(
                file_identity=current_identity,
                head_hash=event_hash,
                event_count=verified.event_count + 1,
                generation_registrations=registrations,
            )
        else:
            self._verified_audit_head = None

    def _scan_audit_unlocked(self) -> _VerifiedFileAuditHead:
        """Replay the full hash chain and collect immutable registrations."""

        identity_before = self._audit_file_identity_unlocked()
        expected_previous = ""
        event_count = 0
        registrations: list[tuple[str, str]] = []
        if self.audit_path.exists():
            try:
                with self.audit_path.open(encoding="utf-8") as handle:
                    for line_number, raw_line in enumerate(handle, start=1):
                        if not raw_line.strip():
                            raise AuthorityError(
                                f"authority audit contains a blank line at {line_number}"
                            )
                        event = json.loads(raw_line)
                        if not isinstance(event, dict):
                            raise AuthorityError("authority audit event must be an object")
                        event_hash = str(event.pop("event_hash", ""))
                        if event.get("registry_id") != self.registry_id:
                            raise AuthorityError("authority audit registry identity mismatch")
                        if event.get("previous_event_hash") != expected_previous:
                            raise AuthorityError("authority audit hash chain is discontinuous")
                        if canonical_json_sha256(event) != event_hash:
                            raise AuthorityError("authority audit event hash mismatch")
                        if event.get("event_type") == "generation_registered":
                            payload = event.get("payload")
                            if not isinstance(payload, dict):
                                raise AuthorityError(
                                    "generation registration payload is invalid"
                                )
                            registrations.append(
                                (
                                    str(payload.get("generation_id") or ""),
                                    str(payload.get("manifest_sha256") or ""),
                                )
                            )
                        expected_previous = event_hash
                        event_count += 1
            except (OSError, json.JSONDecodeError) as exc:
                raise AuthorityError("authority audit is unreadable") from exc
        identity_after = self._audit_file_identity_unlocked()
        if identity_after != identity_before:
            raise AuthorityError("authority audit changed during verification")
        return _VerifiedFileAuditHead(
            file_identity=identity_after,
            head_hash=expected_previous,
            event_count=event_count,
            generation_registrations=tuple(registrations),
        )

    def _verify_audit_unlocked(self, state: dict[str, Any]) -> int:
        """Verify a changed audit head once, then reuse that exact proof."""

        expected_head = str(state.get("last_event_hash") or "")
        identity = self._audit_file_identity_unlocked()
        verified = self._verified_audit_head
        if (
            verified is not None
            and verified.file_identity == identity
            and secrets.compare_digest(verified.head_hash, expected_head)
        ):
            return verified.event_count
        verified = self._scan_audit_unlocked()
        if not secrets.compare_digest(verified.head_hash, expected_head):
            raise AuthorityError("authority audit head does not match registry state")
        self._verified_audit_head = verified
        return verified.event_count

    def _registered_manifest_sha256_unlocked(
        self,
        state: dict[str, Any],
        generation_id: str,
    ) -> str:
        """Return the target generation's unique immutable registration hash."""

        self._verify_audit_unlocked(state)
        verified = self._verified_audit_head
        if verified is None:
            raise AuthorityError("authority audit verification proof is missing")
        registered = [
            manifest_sha256
            for registered_generation_id, manifest_sha256 in (
                verified.generation_registrations
            )
            if registered_generation_id == generation_id
        ]
        if len(registered) != 1 or len(registered[0]) != 64:
            raise AuthorityError(
                "generation must have exactly one valid registration audit event"
            )
        return registered[0]

    def _assert_generation_immutable_unlocked(
        self,
        state: dict[str, Any],
        generation_id: str,
    ) -> BackendGeneration:
        raw = dict(state["generations"]).get(generation_id)
        if raw is None:
            raise GenerationNotVerified(f"generation is not registered: {generation_id}")
        generation = BackendGeneration.from_dict(dict(raw))
        registered_sha256 = self._registered_manifest_sha256_unlocked(
            state,
            generation_id,
        )
        if not secrets.compare_digest(
            generation.manifest_sha256,
            registered_sha256,
        ):
            raise AuthorityError(
                f"generation manifest differs from immutable registration: {generation_id}"
            )
        return generation

    def _assert_token_unlocked(
        self,
        state: dict[str, Any],
        token: AuthorityToken,
        *,
        require_live_lease: bool = True,
    ) -> None:
        if token.registry_id != self.registry_id:
            raise StaleAuthorityToken("authority registry identity mismatch")
        generation = self._assert_generation_immutable_unlocked(
            state,
            token.generation_id,
        )
        if generation.backend != token.backend:
            raise StaleAuthorityToken("authority token backend differs from generation")
        expected = {
            "active_backend": token.backend.value,
            "active_generation": token.generation_id,
            "authority_epoch": int(token.authority_epoch),
            "owner_id": token.owner_id,
            "fencing_token_hash": token.fencing_token_sha256,
        }
        for key, value in expected.items():
            if state.get(key) != value:
                raise StaleAuthorityToken(f"authority token rejected by {key}")
        try:
            lease_until = datetime.fromisoformat(str(state.get("lease_until") or ""))
        except ValueError as exc:
            raise StaleAuthorityToken("authority lease timestamp is invalid") from exc
        if lease_until.tzinfo is None:
            lease_until = lease_until.replace(tzinfo=UTC)
        if require_live_lease and lease_until <= _utc_now():
            raise StaleAuthorityToken("authority token lease has expired")

    def _register_generation_sync(self, generation: BackendGeneration) -> None:
        with self._audit_lock(exclusive=True, create=True):
            state = self._read_state_unlocked(allow_missing=True)
            self._verify_audit_unlocked(state)
            generations = dict(state["generations"])
            existing = generations.get(generation.generation_id)
            body = generation.to_dict()
            if existing is not None:
                restored = self._assert_generation_immutable_unlocked(
                    state,
                    generation.generation_id,
                )
                if restored.manifest_sha256 != generation.manifest_sha256:
                    raise GenerationConflict(
                        f"generation identity reused with different manifest: {generation.generation_id}"
                    )
                return
            generations[generation.generation_id] = body
            state["generations"] = generations
            state["updated_at"] = _iso(_utc_now())
            self._append_event_unlocked(
                state,
                event_type="generation_registered",
                payload={
                    "generation_id": generation.generation_id,
                    "backend": generation.backend.value,
                    "status": generation.status.value,
                    "manifest_sha256": generation.manifest_sha256,
                },
            )
            self._write_state_unlocked(state)

    async def register_generation(self, generation: BackendGeneration) -> None:
        await asyncio.to_thread(self._register_generation_sync, generation)

    def _get_generation_sync(self, generation_id: str) -> BackendGeneration | None:
        with self._audit_lock(exclusive=False, create=False):
            state = self._read_state_unlocked(allow_missing=False)
            self._verify_audit_unlocked(state)
            raw = dict(state["generations"]).get(generation_id)
            if raw is None:
                return None
            return self._assert_generation_immutable_unlocked(state, generation_id)

    async def get_generation(
        self,
        generation_id: str,
    ) -> BackendGeneration | None:
        return await asyncio.to_thread(self._get_generation_sync, generation_id)

    def _activate_generation_sync(
        self,
        generation_id: str,
        *,
        expected_epoch: int,
        owner_id: str,
        lease_seconds: int,
        confirm_previous_writers_stopped: bool,
    ) -> AuthorityToken:
        if int(lease_seconds) <= 0:
            raise ValueError("authority lease_seconds must be positive")
        owner = owner_id.strip()
        if not owner:
            raise ValueError("authority owner_id must not be empty")
        with (
            self._lock(exclusive=True, create=True),
            self._audit_lock(exclusive=True, create=True),
        ):
                state = self._read_state_unlocked(allow_missing=True)
                self._verify_audit_unlocked(state)
                current_epoch = int(state.get("authority_epoch") or 0)
                if current_epoch != int(expected_epoch):
                    raise AuthorityConflict(
                        f"authority epoch conflict: expected {expected_epoch}, actual {current_epoch}"
                    )
                raw = dict(state["generations"]).get(generation_id)
                if raw is None:
                    raise GenerationNotVerified(f"generation is not registered: {generation_id}")
                generation = self._assert_generation_immutable_unlocked(
                    state,
                    generation_id,
                )
                if generation.status != GenerationStatus.VERIFIED:
                    raise GenerationNotVerified(
                        f"generation is not verified: {generation_id} ({generation.status.value})"
                    )
                if state.get("active_generation") and not confirm_previous_writers_stopped:
                    raise AuthorityConflict(
                        "active authority exists; explicit writer isolation confirmation is required"
                    )
                next_epoch = current_epoch + 1
                secret = secrets.token_urlsafe(32)
                lease_until = _utc_now() + timedelta(seconds=int(lease_seconds))
                state.update(
                    {
                        "active_backend": generation.backend.value,
                        "active_generation": generation.generation_id,
                        "authority_epoch": next_epoch,
                        "fencing_token_hash": hashlib.sha256(secret.encode("utf-8")).hexdigest(),
                        "owner_id": owner,
                        "lease_until": _iso(lease_until),
                        "updated_at": _iso(_utc_now()),
                    }
                )
                self._append_event_unlocked(
                    state,
                    event_type="generation_activated",
                    payload={
                        "backend": generation.backend.value,
                        "generation_id": generation.generation_id,
                        "authority_epoch": next_epoch,
                        "owner_id": owner,
                        "lease_until": _iso(lease_until),
                    },
                )
                self._write_state_unlocked(state)
                return AuthorityToken(
                    registry_id=self.registry_id,
                    backend=generation.backend,
                    generation_id=generation.generation_id,
                    authority_epoch=next_epoch,
                    owner_id=owner,
                    lease_until=_iso(lease_until),
                    fencing_token=secret,
                )

    async def activate_generation(
        self,
        generation_id: str,
        *,
        expected_epoch: int,
        owner_id: str,
        lease_seconds: int,
        confirm_previous_writers_stopped: bool,
    ) -> AuthorityToken:
        return await asyncio.to_thread(
            self._activate_generation_sync,
            generation_id,
            expected_epoch=expected_epoch,
            owner_id=owner_id,
            lease_seconds=lease_seconds,
            confirm_previous_writers_stopped=confirm_previous_writers_stopped,
        )

    def _renew_sync(self, token: AuthorityToken, lease_seconds: int) -> AuthorityToken:
        if int(lease_seconds) <= 0:
            raise ValueError("authority lease_seconds must be positive")
        with self._audit_lock(exclusive=True, create=False):
            state = self._read_state_unlocked(allow_missing=False)
            self._verify_audit_unlocked(state)
            self._assert_token_unlocked(state, token, require_live_lease=False)
            try:
                previous_lease = datetime.fromisoformat(
                    str(state.get("lease_until") or "")
                )
            except ValueError:
                previous_lease = None
            else:
                if previous_lease.tzinfo is None:
                    previous_lease = previous_lease.replace(tzinfo=UTC)
            if previous_lease is not None and previous_lease <= _utc_now():
                logger.warning(
                    "incumbent file authority lease had expired; renewing "
                    "without waiting for writer fences"
                )
            lease_until = _utc_now() + timedelta(seconds=int(lease_seconds))
            state["lease_until"] = _iso(lease_until)
            state["updated_at"] = _iso(_utc_now())
            self._append_event_unlocked(
                state,
                event_type="authority_renewed",
                payload={**token.safe_dict(), "lease_until": _iso(lease_until)},
            )
            self._write_state_unlocked(state)
            renewed = AuthorityToken(
                registry_id=token.registry_id,
                backend=token.backend,
                generation_id=token.generation_id,
                authority_epoch=token.authority_epoch,
                owner_id=token.owner_id,
                lease_until=_iso(lease_until),
                fencing_token=token.fencing_token,
            )
            self._keepalive_token = renewed
            return renewed

    async def renew(
        self,
        token: AuthorityToken,
        *,
        lease_seconds: int,
    ) -> AuthorityToken:
        return await asyncio.to_thread(self._renew_sync, token, lease_seconds)

    def _revoke_sync(self, token: AuthorityToken) -> int:
        with (
            self._lock(exclusive=True, create=False),
            self._audit_lock(exclusive=True, create=False),
        ):
                state = self._read_state_unlocked(allow_missing=False)
                self._verify_audit_unlocked(state)
                self._assert_token_unlocked(state, token)
                next_epoch = int(state["authority_epoch"]) + 1
                self._append_event_unlocked(
                    state,
                    event_type="authority_revoked",
                    payload={**token.safe_dict(), "next_epoch": next_epoch},
                )
                state.update(
                    {
                        "active_backend": "",
                        "active_generation": "",
                        "authority_epoch": next_epoch,
                        "fencing_token_hash": "",
                        "owner_id": "",
                        "lease_until": "",
                        "updated_at": _iso(_utc_now()),
                    }
                )
                self._write_state_unlocked(state)
                return next_epoch

    async def revoke(self, token: AuthorityToken) -> int:
        await asyncio.to_thread(self.stop_keepalive)
        return await asyncio.to_thread(self._revoke_sync, token)

    def _validate_sync(self, token: AuthorityToken) -> None:
        with self._audit_lock(exclusive=False, create=False):
            state = self._read_state_unlocked(allow_missing=False)
            self._verify_audit_unlocked(state)
            self._assert_token_unlocked(state, token)

    async def validate(self, token: AuthorityToken) -> None:
        await asyncio.to_thread(self._validate_sync, token)

    def _run_fenced_sync(self, token: AuthorityToken, operation: Callable[[], T]) -> T:
        with self._lock(exclusive=False, create=False):
            with self._audit_lock(exclusive=False, create=False):
                state = self._read_state_unlocked(allow_missing=False)
                self._verify_audit_unlocked(state)
                self._assert_token_unlocked(state, token)
            return operation()

    async def run_fenced(self, token: AuthorityToken, operation: Callable[[], T]) -> T:
        """Run one synchronous local write while holding a shared cutover lock."""

        return await asyncio.to_thread(self._run_fenced_sync, token, operation)

    def _acquire_fence_sync(self, token: AuthorityToken) -> Any:
        self._prepare_lock_path(self.lock_path, create=False)
        handle = self.lock_path.open("rb")
        try:
            _lock_file(handle, exclusive=False)
            with self._audit_lock(exclusive=False, create=False):
                state = self._read_state_unlocked(allow_missing=False)
                self._verify_audit_unlocked(state)
                self._assert_token_unlocked(state, token)
            return handle
        except BaseException:
            _unlock_file(handle)
            handle.close()
            raise

    @staticmethod
    def _release_fence_sync(handle: Any) -> None:
        try:
            _unlock_file(handle)
        finally:
            handle.close()

    @asynccontextmanager
    async def fenced(self, token: AuthorityToken) -> Any:
        """Hold a shared cutover lock for one complete async local transaction."""

        handle = await asyncio.to_thread(self._acquire_fence_sync, token)
        try:
            yield
        finally:
            await asyncio.to_thread(self._release_fence_sync, handle)

    def start_keepalive(
        self,
        token: AuthorityToken,
        *,
        lease_seconds: int,
        interval_seconds: float,
    ) -> None:
        """Keep the incumbent file lease alive off the asyncio event loop."""

        if int(lease_seconds) <= 0:
            raise ValueError("authority lease_seconds must be positive")
        interval = float(interval_seconds)
        if interval <= 0:
            return
        with self._keepalive_guard:
            self._keepalive_token = token
            self._keepalive_lease_seconds = int(lease_seconds)
            self._keepalive_interval_seconds = interval
            if self._keepalive_thread is not None and self._keepalive_thread.is_alive():
                return
            self._keepalive_stop.clear()
            thread = threading.Thread(
                target=self._keepalive_loop,
                name=f"file-authority-keepalive:{self.registry_id}",
                daemon=True,
            )
            self._keepalive_thread = thread
            thread.start()

    def stop_keepalive(self) -> None:
        """Stop the incumbent keepalive thread and wait for it to exit."""

        self._keepalive_stop.set()
        thread: threading.Thread | None
        with self._keepalive_guard:
            thread = self._keepalive_thread
            self._keepalive_thread = None
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=5.0)

    def _keepalive_loop(self) -> None:
        interval = max(0.05, float(self._keepalive_interval_seconds))
        while not self._keepalive_stop.wait(timeout=interval):
            token = self._keepalive_token
            lease_seconds = int(self._keepalive_lease_seconds)
            if token is None or lease_seconds <= 0:
                continue
            try:
                self._renew_sync(token, lease_seconds)
            except StaleAuthorityToken:
                logger.error(
                    "file authority keepalive stopped after incumbent token was rejected"
                )
                return
            except Exception:
                logger.warning(
                    "file authority keepalive renew failed; will retry",
                    exc_info=True,
                )

    def _health_sync(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {
                "status": "disabled",
                "registry_id": self.registry_id,
                "reason": "authority registry is not initialized",
            }
        with self._audit_lock(exclusive=False, create=False):
            state = self._read_state_unlocked(allow_missing=False)
            audit_event_count = self._verify_audit_unlocked(state)
            lease_until = str(state.get("lease_until") or "")
            expired = False
            if lease_until:
                value = datetime.fromisoformat(lease_until)
                if value.tzinfo is None:
                    value = value.replace(tzinfo=UTC)
                expired = value <= _utc_now()
            active_generation = str(state.get("active_generation") or "")
            active = bool(active_generation)
            if active:
                self._assert_generation_immutable_unlocked(
                    state,
                    active_generation,
                )
            keepalive_thread = self._keepalive_thread
            return {
                "status": "disabled" if not active else ("degraded" if expired else "healthy"),
                "registry_id": self.registry_id,
                "active_backend": str(state.get("active_backend") or ""),
                "active_generation": str(state.get("active_generation") or ""),
                "authority_epoch": int(state.get("authority_epoch") or 0),
                "owner_id": str(state.get("owner_id") or ""),
                "lease_until": lease_until,
                "lease_expired": expired,
                "generation_count": len(dict(state.get("generations") or {})),
                "audit_event_count": audit_event_count,
                "audit_chain_valid": True,
                "incumbent_keepalive": (
                    "running"
                    if keepalive_thread is not None and keepalive_thread.is_alive()
                    else "stopped"
                ),
                "updated_at": str(state.get("updated_at") or ""),
            }

    async def health(self) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(self._health_sync)
        except Exception as exc:  # noqa: BLE001 - return bounded health state
            return {
                "status": "failed",
                "registry_id": self.registry_id,
                "error_type": type(exc).__name__,
            }


_AUTHORITY_SCHEMA = SchemaMigration(
    version=2,
    name="life_storage_shared_generation_authority",
    statements=(
        """CREATE TABLE IF NOT EXISTS storage_backend_generations (
            generation_id VARCHAR(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin PRIMARY KEY,
            backend VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            schema_version BIGINT UNSIGNED NOT NULL,
            status VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            manifest_json JSON NOT NULL,
            manifest_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            source_snapshot_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            created_at DATETIME(6) NOT NULL,
            verified_at DATETIME(6) NULL,
            registered_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
            UNIQUE KEY uq_storage_generation_manifest (manifest_sha256)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
        """CREATE TABLE IF NOT EXISTS storage_authority_registry (
            registry_id VARCHAR(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin PRIMARY KEY,
            active_backend VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL DEFAULT '',
            active_generation VARCHAR(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL DEFAULT '',
            authority_epoch BIGINT UNSIGNED NOT NULL DEFAULT 0,
            fencing_token_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL DEFAULT '',
            owner_id VARCHAR(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL DEFAULT '',
            lease_until DATETIME(6) NULL,
            last_event_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL DEFAULT '',
            updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
        """CREATE TABLE IF NOT EXISTS storage_authority_events (
            event_position BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
            occurrence_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            registry_id VARCHAR(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            event_type VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            authority_epoch BIGINT UNSIGNED NOT NULL,
            backend VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL DEFAULT '',
            generation_id VARCHAR(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL DEFAULT '',
            owner_id VARCHAR(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL DEFAULT '',
            payload_json JSON NOT NULL,
            previous_event_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL DEFAULT '',
            event_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            recorded_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
            UNIQUE KEY uq_storage_authority_occurrence (occurrence_id),
            KEY idx_storage_authority_registry_position (registry_id, event_position)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
    ),
)


class MySQLAuthorityRegistry:
    """MySQL control plane for multi-process generation and writer fencing."""

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        registry_id: str = "life-domain",
    ) -> None:
        self.engine = engine
        self.registry_id = registry_id.strip()
        if not self.registry_id:
            raise ValueError("authority registry_id must not be empty")
        self._schema_ready = False
        self._schema_lock = asyncio.Lock()
        self._verified_audit_head: str | None = None
        self._verified_audit_count = 0
        self._verified_generation_registrations: dict[str, str] = {}
        self._audit_lock = asyncio.Lock()
        lock_suffix = hashlib.sha256(self.registry_id.encode("utf-8")).hexdigest()[:16]
        self._activation_lock_name = f"elysium:authority:{lock_suffix}"

    async def ensure_schema(self) -> None:
        if self._schema_ready:
            return
        async with self._schema_lock:
            if self._schema_ready:
                return
            runner = MySQLMigrationRunner(self.engine)
            await runner.apply((_AUTHORITY_SCHEMA,))
            self._schema_ready = True

    async def register_generation(self, generation: BackendGeneration) -> None:
        await self.ensure_schema()
        body = canonical_json(generation.to_dict())
        async with self.engine.begin() as connection:
            existing = (
                await connection.execute(
                    text(
                        "SELECT generation_id, manifest_json, manifest_sha256 "
                        "FROM storage_backend_generations "
                        "WHERE generation_id = :generation_id FOR UPDATE"
                    ),
                    {"generation_id": generation.generation_id},
                )
            ).mappings().one_or_none()
            if existing is not None:
                current = (
                    await connection.execute(
                        text(
                            "SELECT last_event_hash FROM storage_authority_registry "
                            "WHERE registry_id = :registry_id FOR UPDATE"
                        ),
                        {"registry_id": self.registry_id},
                    )
                ).mappings().one_or_none()
                if current is None:
                    raise AuthorityError(
                        "registered generation has no authority registry"
                    )
                await self._verify_audit(
                    connection,
                    expected_head=str(current["last_event_hash"] or ""),
                )
                restored = await self._decode_verified_generation_row(
                    connection,
                    existing,
                )
                if restored.manifest_sha256 != generation.manifest_sha256:
                    raise GenerationConflict(
                        f"generation identity reused with different manifest: {generation.generation_id}"
                    )
                return
            await connection.execute(
                text(
                    "INSERT INTO storage_backend_generations ("
                    "generation_id, backend, schema_version, status, manifest_json, "
                    "manifest_sha256, source_snapshot_sha256, created_at, verified_at"
                    ") VALUES ("
                    ":generation_id, :backend, :schema_version, :status, :manifest_json, "
                    ":manifest_sha256, :source_snapshot_sha256, :created_at, :verified_at)"
                ),
                {
                    "generation_id": generation.generation_id,
                    "backend": generation.backend.value,
                    "schema_version": generation.schema_version,
                    "status": generation.status.value,
                    "manifest_json": body,
                    "manifest_sha256": generation.manifest_sha256,
                    "source_snapshot_sha256": generation.source_snapshot_sha256,
                    "created_at": datetime.fromisoformat(generation.created_at),
                    "verified_at": (
                        datetime.fromisoformat(generation.verified_at)
                        if generation.verified_at
                        else None
                    ),
                },
            )
            await connection.execute(
                text(
                    "INSERT IGNORE INTO storage_authority_registry (registry_id) "
                    "VALUES (:registry_id)"
                ),
                {"registry_id": self.registry_id},
            )
            current = (
                await connection.execute(
                    text(
                        "SELECT authority_epoch, last_event_hash "
                        "FROM storage_authority_registry "
                        "WHERE registry_id = :registry_id FOR UPDATE"
                    ),
                    {"registry_id": self.registry_id},
                )
            ).mappings().one()
            await self._verify_audit(
                connection,
                expected_head=str(current["last_event_hash"] or ""),
            )
            event_hash = await self._append_event(
                connection,
                previous_hash=str(current["last_event_hash"] or ""),
                event_type="generation_registered",
                epoch=int(current["authority_epoch"]),
                backend=generation.backend.value,
                generation_id=generation.generation_id,
                owner_id="",
                payload={
                    "status": generation.status.value,
                    "manifest_sha256": generation.manifest_sha256,
                },
            )
            await connection.execute(
                text(
                    "UPDATE storage_authority_registry SET last_event_hash = :event_hash, "
                    "updated_at = CURRENT_TIMESTAMP(6) WHERE registry_id = :registry_id"
                ),
                {"event_hash": event_hash, "registry_id": self.registry_id},
            )

    async def get_generation(
        self,
        generation_id: str,
    ) -> BackendGeneration | None:
        await self.ensure_schema()
        async with self.engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        "SELECT generation_id, manifest_json, manifest_sha256 "
                        "FROM storage_backend_generations "
                        "WHERE generation_id = :generation_id"
                    ),
                    {"generation_id": generation_id},
                )
            ).mappings().one_or_none()
            if row is None:
                return None
            current = (
                await connection.execute(
                    text(
                        "SELECT last_event_hash FROM storage_authority_registry "
                        "WHERE registry_id = :registry_id"
                    ),
                    {"registry_id": self.registry_id},
                )
            ).mappings().one_or_none()
            if current is None:
                raise AuthorityError("registered generation has no authority registry")
            await self._verify_audit(
                connection,
                expected_head=str(current["last_event_hash"] or ""),
            )
            return await self._decode_verified_generation_row(connection, row)

    async def _verify_generation_registration(
        self,
        connection: AsyncConnection,
        *,
        generation_id: str,
        manifest_sha256: str,
    ) -> None:
        """Verify one generation row against its immutable registration event."""

        generation_key = str(generation_id)
        manifest_hash = str(manifest_sha256)
        registered = self._verified_generation_registrations.get(generation_key)
        if registered is not None:
            if not secrets.compare_digest(manifest_hash, registered):
                raise AuthorityError(
                    f"generation manifest differs from immutable registration: {generation_key}"
                )
            return
        rows = (
            await connection.execute(
                text(
                    "SELECT payload_json FROM storage_authority_events "
                    "WHERE registry_id = :registry_id "
                    "AND event_type = 'generation_registered' "
                    "AND generation_id = :generation_id ORDER BY event_position"
                ),
                {
                    "registry_id": self.registry_id,
                    "generation_id": generation_id,
                },
            )
        ).mappings().all()
        if len(rows) != 1:
            raise AuthorityError(
                "generation must have exactly one registration audit event"
            )
        payload = self._json_object(rows[0]["payload_json"])
        registered_sha256 = str(payload.get("manifest_sha256") or "")
        if not secrets.compare_digest(manifest_hash, registered_sha256):
            raise AuthorityError(
                f"generation manifest differs from immutable registration: {generation_key}"
            )
        self._verified_generation_registrations[generation_key] = registered_sha256

    async def _decode_verified_generation_row(
        self,
        connection: AsyncConnection,
        row: Any,
    ) -> BackendGeneration:
        raw = row["manifest_json"]
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8")
        value = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(value, dict):
            raise AuthorityError("generation manifest is not a JSON object")
        generation = BackendGeneration.from_dict(value)
        row_generation_id = str(row["generation_id"])
        if generation.generation_id != row_generation_id:
            raise AuthorityError("generation manifest identity mismatch")
        stored_sha256 = str(row["manifest_sha256"])
        if not secrets.compare_digest(generation.manifest_sha256, stored_sha256):
            raise AuthorityError("generation manifest checksum mismatch")
        await self._verify_generation_registration(
            connection,
            generation_id=generation.generation_id,
            manifest_sha256=stored_sha256,
        )
        return generation

    @staticmethod
    def _audit_hash(
        *,
        previous_hash: str,
        registry_id: str,
        event_type: str,
        epoch: int,
        backend: str,
        generation_id: str,
        owner_id: str,
        payload: dict[str, Any],
    ) -> str:
        return canonical_json_sha256(
            {
                "previous_event_hash": previous_hash,
                "registry_id": registry_id,
                "event_type": event_type,
                "authority_epoch": int(epoch),
                "backend": backend,
                "generation_id": generation_id,
                "owner_id": owner_id,
                "payload": payload,
            }
        )

    async def _append_event(
        self,
        connection: AsyncConnection,
        *,
        previous_hash: str,
        event_type: str,
        epoch: int,
        backend: str,
        generation_id: str,
        owner_id: str,
        payload: dict[str, Any],
    ) -> str:
        event_hash = self._audit_hash(
            previous_hash=previous_hash,
            registry_id=self.registry_id,
            event_type=event_type,
            epoch=epoch,
            backend=backend,
            generation_id=generation_id,
            owner_id=owner_id,
            payload=payload,
        )
        await connection.execute(
            text(
                "INSERT INTO storage_authority_events ("
                "occurrence_id, registry_id, event_type, authority_epoch, backend, "
                "generation_id, owner_id, payload_json, previous_event_hash, event_hash"
                ") VALUES ("
                ":occurrence_id, :registry_id, :event_type, :authority_epoch, :backend, "
                ":generation_id, :owner_id, :payload_json, :previous_event_hash, :event_hash)"
            ),
            {
                "occurrence_id": str(uuid4()),
                "registry_id": self.registry_id,
                "event_type": event_type,
                "authority_epoch": epoch,
                "backend": backend,
                "generation_id": generation_id,
                "owner_id": owner_id,
                "payload_json": canonical_json(payload),
                "previous_event_hash": previous_hash,
                "event_hash": event_hash,
            },
        )
        return event_hash

    @staticmethod
    def _json_object(value: Any) -> dict[str, Any]:
        if isinstance(value, (bytes, bytearray)):
            value = value.decode("utf-8")
        value = json.loads(value) if isinstance(value, str) else value
        if not isinstance(value, dict):
            raise AuthorityError("authority event payload is not a JSON object")
        return value

    async def _verify_audit(
        self,
        connection: AsyncConnection,
        *,
        expected_head: str,
    ) -> int:
        """Verify one audit head once per process, then reuse the exact proof."""

        expected = str(expected_head or "")
        async with self._audit_lock:
            if expected == self._verified_audit_head:
                return self._verified_audit_count
            rows = (
                await connection.execute(
                    text(
                        "SELECT event_type, authority_epoch, backend, generation_id, "
                        "owner_id, payload_json, previous_event_hash, event_hash "
                        "FROM storage_authority_events WHERE registry_id = :registry_id "
                        "ORDER BY event_position"
                    ),
                    {"registry_id": self.registry_id},
                )
            ).mappings()
            previous_hash = ""
            count = 0
            for row in rows:
                if str(row["previous_event_hash"] or "") != previous_hash:
                    raise AuthorityError(
                        "MySQL authority audit hash chain is discontinuous"
                    )
                calculated = self._audit_hash(
                    previous_hash=previous_hash,
                    registry_id=self.registry_id,
                    event_type=str(row["event_type"]),
                    epoch=int(row["authority_epoch"]),
                    backend=str(row["backend"] or ""),
                    generation_id=str(row["generation_id"] or ""),
                    owner_id=str(row["owner_id"] or ""),
                    payload=self._json_object(row["payload_json"]),
                )
                if not secrets.compare_digest(
                    calculated, str(row["event_hash"] or "")
                ):
                    raise AuthorityError("MySQL authority audit event hash mismatch")
                previous_hash = calculated
                count += 1
            if previous_hash != expected:
                raise AuthorityError(
                    "MySQL authority audit head does not match registry state"
                )
            self._verified_audit_head = expected
            self._verified_audit_count = count
            return count

    async def _acquire_activation_lock(self, connection: AsyncConnection) -> None:
        acquired = await connection.scalar(
            text("SELECT GET_LOCK(:name, 10)"),
            {"name": self._activation_lock_name},
        )
        await connection.commit()
        if int(acquired or 0) != 1:
            raise AuthorityConflict("could not acquire authority activation lock")

    async def _release_activation_lock(self, connection: AsyncConnection) -> None:
        await connection.execute(
            text("SELECT RELEASE_LOCK(:name)"),
            {"name": self._activation_lock_name},
        )
        await connection.commit()

    async def activate_generation(
        self,
        generation_id: str,
        *,
        expected_epoch: int,
        owner_id: str,
        lease_seconds: int,
        confirm_previous_writers_stopped: bool,
    ) -> AuthorityToken:
        await self.ensure_schema()
        if int(lease_seconds) <= 0:
            raise ValueError("authority lease_seconds must be positive")
        owner = owner_id.strip()
        if not owner:
            raise ValueError("authority owner_id must not be empty")
        secret = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(secret.encode("utf-8")).hexdigest()
        async with self.engine.connect() as connection:
            await self._acquire_activation_lock(connection)
            primary_error: BaseException | None = None
            try:
                transaction = await connection.begin()
                try:
                    generation = (
                        await connection.execute(
                            text(
                                "SELECT generation_id, backend, status, manifest_json, "
                                "manifest_sha256 FROM storage_backend_generations "
                                "WHERE generation_id = :generation_id FOR UPDATE"
                            ),
                            {"generation_id": generation_id},
                        )
                    ).mappings().one_or_none()
                    if generation is None or str(generation["status"]) != "verified":
                        raise GenerationNotVerified(
                            f"generation is not verified: {generation_id}"
                        )
                    await connection.execute(
                        text(
                            "INSERT IGNORE INTO storage_authority_registry (registry_id) "
                            "VALUES (:registry_id)"
                        ),
                        {"registry_id": self.registry_id},
                    )
                    current = (
                        await connection.execute(
                            text(
                                "SELECT * FROM storage_authority_registry "
                                "WHERE registry_id = :registry_id FOR UPDATE"
                            ),
                            {"registry_id": self.registry_id},
                        )
                    ).mappings().one()
                    actual_epoch = int(current["authority_epoch"])
                    await self._verify_audit(
                        connection,
                        expected_head=str(current["last_event_hash"] or ""),
                    )
                    verified_generation = await self._decode_verified_generation_row(
                        connection,
                        generation,
                    )
                    if actual_epoch != int(expected_epoch):
                        raise AuthorityConflict(
                            f"authority epoch conflict: expected {expected_epoch}, actual {actual_epoch}"
                        )
                    active_generation = str(current["active_generation"] or "")
                    lease_until = current["lease_until"]
                    database_now = await connection.scalar(
                        text("SELECT CURRENT_TIMESTAMP(6)")
                    )
                    live_authority = bool(active_generation) and (
                        lease_until is None
                        or database_now is None
                        or lease_until > database_now
                    )
                    if live_authority and not confirm_previous_writers_stopped:
                        raise AuthorityConflict(
                            "live authority exists; writer isolation is not proven"
                        )
                    next_epoch = actual_epoch + 1
                    backend = verified_generation.backend.value
                    await connection.execute(
                        text(
                            "UPDATE storage_authority_registry SET "
                            "active_backend = :backend, active_generation = :generation_id, "
                            "authority_epoch = :epoch, fencing_token_hash = :token_hash, "
                            "owner_id = :owner_id, "
                            "lease_until = TIMESTAMPADD(SECOND, :lease_seconds, CURRENT_TIMESTAMP(6)), "
                            "updated_at = CURRENT_TIMESTAMP(6) "
                            "WHERE registry_id = :registry_id"
                        ),
                        {
                            "backend": backend,
                            "generation_id": generation_id,
                            "epoch": next_epoch,
                            "token_hash": token_hash,
                            "owner_id": owner,
                            "lease_seconds": int(lease_seconds),
                            "registry_id": self.registry_id,
                        },
                    )
                    lease_until = await connection.scalar(
                        text(
                            "SELECT lease_until FROM storage_authority_registry "
                            "WHERE registry_id = :registry_id"
                        ),
                        {"registry_id": self.registry_id},
                    )
                    payload = {
                        "generation_id": generation_id,
                        "backend": backend,
                        "authority_epoch": next_epoch,
                        "owner_id": owner,
                        "lease_seconds": int(lease_seconds),
                    }
                    event_hash = await self._append_event(
                        connection,
                        previous_hash=str(current["last_event_hash"] or ""),
                        event_type="generation_activated",
                        epoch=next_epoch,
                        backend=backend,
                        generation_id=generation_id,
                        owner_id=owner,
                        payload=payload,
                    )
                    await connection.execute(
                        text(
                            "UPDATE storage_authority_registry SET last_event_hash = :event_hash "
                            "WHERE registry_id = :registry_id"
                        ),
                        {"event_hash": event_hash, "registry_id": self.registry_id},
                    )
                    await transaction.commit()
                except BaseException:
                    await transaction.rollback()
                    raise
            except BaseException as exc:
                primary_error = exc
                raise
            finally:
                try:
                    await self._release_activation_lock(connection)
                except BaseException:
                    if primary_error is None:
                        raise
        if not isinstance(lease_until, datetime):
            raise AuthorityError("MySQL did not return an authority lease timestamp")
        lease_until = lease_until.replace(tzinfo=lease_until.tzinfo or UTC)
        return AuthorityToken(
            registry_id=self.registry_id,
            backend=BackendKind(backend),
            generation_id=generation_id,
            authority_epoch=next_epoch,
            owner_id=owner,
            lease_until=_iso(lease_until),
            fencing_token=secret,
        )

    async def join_generation(
        self,
        generation_id: str,
        *,
        owner_id: str,
    ) -> AuthorityToken:
        """Join the active verified generation as a concurrent MySQL writer.

        The token identifies the active generation revision rather than one
        process lease. InnoDB transactions, stable identities, CAS revisions,
        and idempotency constraints remain the write-conflict boundary.
        """

        await self.ensure_schema()
        owner = owner_id.strip()
        if not owner:
            raise ValueError("authority owner_id must not be empty")
        async with self.engine.begin() as connection:
            row = (
                await connection.execute(
                    text(
                        "SELECT g.generation_id, g.backend, g.status, "
                        "g.manifest_json, g.manifest_sha256, r.active_backend, "
                        "r.active_generation, r.authority_epoch, r.last_event_hash "
                        "FROM storage_backend_generations AS g "
                        "JOIN storage_authority_registry AS r "
                        "ON r.registry_id = :registry_id "
                        "WHERE g.generation_id = :generation_id FOR SHARE"
                    ),
                    {
                        "generation_id": generation_id,
                        "registry_id": self.registry_id,
                    },
                )
            ).mappings().one_or_none()
            if row is None or str(row["status"]) != "verified":
                raise GenerationNotVerified(
                    f"generation is not verified: {generation_id}"
                )
            if not str(row["active_generation"] or ""):
                raise AuthorityConflict("authority registry is not activated")
            await self._verify_audit(
                connection,
                expected_head=str(row["last_event_hash"] or ""),
            )
            generation = await self._decode_verified_generation_row(
                connection,
                row,
            )
            if str(row["active_generation"]) != generation_id:
                raise AuthorityConflict(
                    "configured generation is not the active shared generation"
                )
            if str(row["active_backend"]) != generation.backend.value:
                raise AuthorityConflict("active backend does not match generation")
            epoch = int(row["authority_epoch"])
            backend = generation.backend.value
        return AuthorityToken(
            registry_id=self.registry_id,
            backend=BackendKind(backend),
            generation_id=generation_id,
            authority_epoch=epoch,
            owner_id=owner,
            lease_until="9999-12-31T23:59:59+00:00",
            fencing_token="shared-generation",
        )

    async def validate_shared_in_transaction(
        self,
        connection: AsyncConnection,
        token: AuthorityToken,
    ) -> None:
        """Validate that a transaction still targets the active generation."""

        row = (
            await connection.execute(
                text(
                    "SELECT r.active_backend, r.active_generation, "
                    "r.authority_epoch, r.last_event_hash, "
                    "g.generation_id, g.manifest_json, g.manifest_sha256 "
                    "FROM storage_authority_registry AS r "
                    "JOIN storage_backend_generations AS g "
                    "ON g.generation_id = r.active_generation "
                    "WHERE r.registry_id = :registry_id FOR SHARE"
                ),
                {"registry_id": self.registry_id},
            )
        ).mappings().one_or_none()
        if row is None:
            raise StaleAuthorityToken("authority registry is not initialized")
        await self._verify_audit(
            connection,
            expected_head=str(row["last_event_hash"] or ""),
        )
        generation = await self._decode_verified_generation_row(connection, row)
        checks = {
            "registry_id": token.registry_id == self.registry_id,
            "backend": str(row["active_backend"]) == generation.backend.value
            == token.backend.value,
            "generation": str(row["active_generation"]) == token.generation_id,
            "epoch": int(row["authority_epoch"]) == token.authority_epoch,
        }
        for name, valid in checks.items():
            if not valid:
                raise StaleAuthorityToken(
                    f"shared generation token rejected by {name}"
                )

    async def validate_shared(self, token: AuthorityToken) -> None:
        await self.ensure_schema()
        async with self.engine.begin() as connection:
            await self.validate_shared_in_transaction(connection, token)

    async def validate_in_transaction(
        self,
        connection: AsyncConnection,
        token: AuthorityToken,
        *,
        for_update: bool = False,
    ) -> None:
        """Fence one write inside the exact transaction that mutates domain data."""

        suffix = " FOR UPDATE" if for_update else " FOR SHARE"
        row = (
            await connection.execute(
                text(
                    "SELECT r.active_backend, r.active_generation, "
                    "r.authority_epoch, r.fencing_token_hash, r.owner_id, "
                    "r.lease_until, r.last_event_hash, "
                    "CURRENT_TIMESTAMP(6) AS database_now, "
                    "g.generation_id, g.manifest_json, g.manifest_sha256 "
                    "FROM storage_authority_registry AS r "
                    "JOIN storage_backend_generations AS g "
                    "ON g.generation_id = r.active_generation "
                    "WHERE r.registry_id = :registry_id" + suffix
                ),
                {"registry_id": self.registry_id},
            )
        ).mappings().one_or_none()
        if row is None:
            raise StaleAuthorityToken("authority registry is not initialized")
        await self._verify_audit(
            connection,
            expected_head=str(row["last_event_hash"] or ""),
        )
        generation = await self._decode_verified_generation_row(connection, row)
        checks = {
            "registry_id": token.registry_id == self.registry_id,
            "backend": str(row["active_backend"]) == generation.backend.value
            == token.backend.value,
            "generation": str(row["active_generation"]) == token.generation_id,
            "epoch": int(row["authority_epoch"]) == token.authority_epoch,
            "token": secrets.compare_digest(
                str(row["fencing_token_hash"]), token.fencing_token_sha256
            ),
            "owner": str(row["owner_id"]) == token.owner_id,
            "lease": row["lease_until"] is not None
            and row["lease_until"] > row["database_now"],
        }
        for name, valid in checks.items():
            if not valid:
                raise StaleAuthorityToken(f"authority token rejected by {name}")

    async def validate(self, token: AuthorityToken) -> None:
        await self.ensure_schema()
        async with self.engine.begin() as connection:
            await self.validate_in_transaction(connection, token)

    async def renew(
        self,
        token: AuthorityToken,
        *,
        lease_seconds: int,
    ) -> AuthorityToken:
        await self.ensure_schema()
        if int(lease_seconds) <= 0:
            raise ValueError("authority lease_seconds must be positive")
        async with self.engine.begin() as connection:
            await self.validate_in_transaction(connection, token, for_update=True)
            current = (
                await connection.execute(
                    text(
                        "SELECT last_event_hash FROM storage_authority_registry "
                        "WHERE registry_id = :registry_id"
                    ),
                    {"registry_id": self.registry_id},
                )
            ).mappings().one()
            await self._verify_audit(
                connection,
                expected_head=str(current["last_event_hash"] or ""),
            )
            await connection.execute(
                text(
                    "UPDATE storage_authority_registry SET "
                    "lease_until = TIMESTAMPADD(SECOND, :lease_seconds, CURRENT_TIMESTAMP(6)), "
                    "updated_at = CURRENT_TIMESTAMP(6) WHERE registry_id = :registry_id"
                ),
                {
                    "lease_seconds": int(lease_seconds),
                    "registry_id": self.registry_id,
                },
            )
            lease_until = await connection.scalar(
                text(
                    "SELECT lease_until FROM storage_authority_registry "
                    "WHERE registry_id = :registry_id"
                ),
                {"registry_id": self.registry_id},
            )
            event_hash = await self._append_event(
                connection,
                previous_hash=str(current["last_event_hash"] or ""),
                event_type="authority_renewed",
                epoch=token.authority_epoch,
                backend=token.backend.value,
                generation_id=token.generation_id,
                owner_id=token.owner_id,
                payload={"lease_seconds": int(lease_seconds)},
            )
            await connection.execute(
                text(
                    "UPDATE storage_authority_registry SET last_event_hash = :event_hash "
                    "WHERE registry_id = :registry_id"
                ),
                {"event_hash": event_hash, "registry_id": self.registry_id},
            )
        if not isinstance(lease_until, datetime):
            raise AuthorityError("MySQL did not return a renewed lease timestamp")
        lease_until = lease_until.replace(tzinfo=lease_until.tzinfo or UTC)
        return AuthorityToken(
            registry_id=token.registry_id,
            backend=token.backend,
            generation_id=token.generation_id,
            authority_epoch=token.authority_epoch,
            owner_id=token.owner_id,
            lease_until=_iso(lease_until),
            fencing_token=token.fencing_token,
        )

    async def revoke(self, token: AuthorityToken) -> int:
        await self.ensure_schema()
        async with self.engine.begin() as connection:
            await self.validate_in_transaction(connection, token, for_update=True)
            current = (
                await connection.execute(
                    text(
                        "SELECT last_event_hash FROM storage_authority_registry "
                        "WHERE registry_id = :registry_id"
                    ),
                    {"registry_id": self.registry_id},
                )
            ).mappings().one()
            await self._verify_audit(
                connection,
                expected_head=str(current["last_event_hash"] or ""),
            )
            next_epoch = token.authority_epoch + 1
            event_hash = await self._append_event(
                connection,
                previous_hash=str(current["last_event_hash"] or ""),
                event_type="authority_revoked",
                epoch=next_epoch,
                backend=token.backend.value,
                generation_id=token.generation_id,
                owner_id=token.owner_id,
                payload={"previous_epoch": token.authority_epoch},
            )
            await connection.execute(
                text(
                    "UPDATE storage_authority_registry SET active_backend = '', "
                    "active_generation = '', authority_epoch = :next_epoch, "
                    "fencing_token_hash = '', owner_id = '', lease_until = NULL, "
                    "last_event_hash = :event_hash, updated_at = CURRENT_TIMESTAMP(6) "
                    "WHERE registry_id = :registry_id"
                ),
                {
                    "next_epoch": next_epoch,
                    "event_hash": event_hash,
                    "registry_id": self.registry_id,
                },
            )
        return next_epoch

    async def health(self) -> dict[str, Any]:
        try:
            await self.ensure_schema()
            async with self.engine.connect() as connection:
                row = (
                    await connection.execute(
                        text(
                            "SELECT r.active_backend, r.active_generation, "
                            "r.authority_epoch, r.owner_id, r.lease_until, "
                            "r.last_event_hash, r.updated_at, "
                            "CURRENT_TIMESTAMP(6) AS database_now, "
                            "g.generation_id, g.manifest_json, g.manifest_sha256 "
                            "FROM storage_authority_registry AS r "
                            "LEFT JOIN storage_backend_generations AS g "
                            "ON g.generation_id = r.active_generation "
                            "WHERE r.registry_id = :registry_id"
                        ),
                        {"registry_id": self.registry_id},
                    )
                ).mappings().one_or_none()
                if row is None:
                    return {
                        "status": "disabled",
                        "registry_id": self.registry_id,
                        "reason": "authority registry is not activated",
                    }
                audit_event_count = await self._verify_audit(
                    connection,
                    expected_head=str(row["last_event_hash"] or ""),
                )
                active = bool(str(row["active_generation"] or ""))
                if active:
                    await self._decode_verified_generation_row(connection, row)
            expired = (
                row["lease_until"] is not None
                and row["lease_until"] <= row["database_now"]
            )
            return {
                "status": "healthy" if active else "disabled",
                "registry_id": self.registry_id,
                "active_backend": str(row["active_backend"] or ""),
                "active_generation": str(row["active_generation"] or ""),
                "authority_epoch": int(row["authority_epoch"]),
                "owner_id": str(row["owner_id"] or ""),
                "lease_until": (
                    _iso(row["lease_until"].replace(tzinfo=row["lease_until"].tzinfo or UTC))
                    if row["lease_until"] is not None
                    else ""
                ),
                "lease_expired": expired,
                "audit_event_count": audit_event_count,
                "audit_chain_valid": True,
                "updated_at": str(row["updated_at"] or ""),
            }
        except Exception as exc:  # noqa: BLE001 - return bounded health state
            return {
                "status": "failed",
                "registry_id": self.registry_id,
                "error_type": type(exc).__name__,
            }


__all__ = [
    "AuthorityConflict",
    "AuthorityError",
    "AuthorityRegistry",
    "FileAuthorityRegistry",
    "GenerationConflict",
    "GenerationNotVerified",
    "MySQLAuthorityRegistry",
    "StaleAuthorityToken",
]
