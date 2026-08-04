"""阶段三认证的本地耐久 store。

该 store 只保存哈希、身份、授权和状态，不保存可回显的明文凭据。
真实部署可替换为同一协议的受管共享 store；路由不直接依赖 SQLite 表。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .policy import (
    ADMIN_FRONTEND_AUDIENCE,
    ALL_EXPORTED_SCOPES,
    PLATFORM_SERVICE_AUDIENCE,
)
from .tokens import SignedValueCodec


@dataclass(frozen=True, slots=True)
class SessionRecord:
    """认证 store 中的短时会话投影。"""

    session_id: str
    actor_id: str
    audience: str
    role: str
    scopes: tuple[str, ...]
    resource_grants: tuple[str, ...]
    access_expires_at: datetime
    refresh_expires_at: datetime
    revoked_at: datetime | None = None
    credential_id: str | None = None


@dataclass(frozen=True, slots=True)
class TicketRecord:
    """绑定到 session、资源和 Origin 的单次 ticket。"""

    ticket_id: str
    session_id: str
    actor_id: str
    audience: str
    resource: str
    subprotocol: str
    scopes: tuple[str, ...]
    origin: str | None
    expires_at: datetime
    consumed_at: datetime | None = None


class AuthStore:
    """通过 SQLite 保存认证状态，并为测试提供内存数据库。"""

    def __init__(
        self,
        database_path: str | Path = ":memory:",
        *,
        installation_id: str = "local",
    ) -> None:
        if not installation_id.strip():
            raise ValueError("installation_id must not be empty")
        path = Path(database_path) if database_path != ":memory:" else None
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
        self._installation_id = installation_id
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(str(database_path), check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._create_schema()

    def close(self) -> None:
        """关闭本 store 的数据库连接。"""

        with self._lock:
            self._connection.close()

    def _create_schema(self) -> None:
        with self._lock, self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS api_credentials (
                    credential_id TEXT PRIMARY KEY,
                    actor_id TEXT NOT NULL,
                    audience TEXT NOT NULL,
                    role TEXT NOT NULL,
                    secret_hash TEXT NOT NULL,
                    scopes_json TEXT NOT NULL,
                    resource_grants_json TEXT NOT NULL,
                    revoked_at TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS api_sessions (
                    session_id TEXT PRIMARY KEY,
                    actor_id TEXT NOT NULL,
                    audience TEXT NOT NULL,
                    role TEXT NOT NULL,
                    scopes_json TEXT NOT NULL,
                    resource_grants_json TEXT NOT NULL,
                    access_expires_at TEXT NOT NULL,
                    refresh_expires_at TEXT NOT NULL,
                    refresh_hash TEXT NOT NULL,
                    revoked_at TEXT,
                    credential_id TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS api_challenges (
                    nonce TEXT PRIMARY KEY,
                    installation_id TEXT NOT NULL,
                    audience TEXT NOT NULL,
                    origin TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    consumed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS api_tickets (
                    ticket_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    audience TEXT NOT NULL,
                    resource TEXT NOT NULL,
                    subprotocol TEXT NOT NULL,
                    scopes_json TEXT NOT NULL,
                    origin TEXT,
                    expires_at TEXT NOT NULL,
                    consumed_at TEXT
                );
                """
            )

    def add_credential(
        self,
        *,
        actor_id: str,
        audience: str,
        role: str,
        secret: str,
        scopes: Iterable[str],
        resource_grants: Iterable[str] = (),
        credential_id: str | None = None,
    ) -> str:
        """登记服务凭据的哈希，并返回凭据 id。"""

        if audience != PLATFORM_SERVICE_AUDIENCE:
            raise ValueError("service credentials require platform service audience")
        if role != "platform_service":
            raise ValueError("service credentials require platform_service role")
        normalized_scopes = self._normalize_scopes(scopes)
        credential_id = credential_id or f"cred_{secrets.token_urlsafe(18)}"
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO api_credentials (
                    credential_id, actor_id, audience, role, secret_hash,
                    scopes_json, resource_grants_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    credential_id,
                    actor_id,
                    audience,
                    role,
                    self._hash_secret(secret),
                    self._encode_values(normalized_scopes),
                    self._encode_values(tuple(resource_grants)),
                    self._now().isoformat(),
                ),
            )
        return credential_id

    def revoke_credential(self, credential_id: str) -> bool:
        """撤销服务凭据，并使其派生的现有 session 立即失效。"""

        now = self._now().isoformat()
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                UPDATE api_credentials
                SET revoked_at = COALESCE(revoked_at, ?)
                WHERE credential_id = ?
                """,
                (now, credential_id),
            )
            self._connection.execute(
                """
                UPDATE api_sessions
                SET revoked_at = COALESCE(revoked_at, ?)
                WHERE credential_id = ?
                """,
                (now, credential_id),
            )
        return cursor.rowcount > 0

    def issue_session_from_credential(
        self,
        *,
        credential: str,
        audience: str,
        codec: SignedValueCodec,
        access_ttl: timedelta,
        refresh_ttl: timedelta,
    ) -> tuple[SessionRecord, str, str]:
        """验证 service credential，并签发 session token 对。"""

        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM api_credentials WHERE revoked_at IS NULL"
            ).fetchall()
            row = next(
                (
                    candidate
                    for candidate in rows
                    if self._verify_secret(credential, candidate["secret_hash"])
                ),
                None,
            )
            if row is None or row["audience"] != audience:
                raise ValueError("credential_invalid")
            return self._create_session(
                actor_id=row["actor_id"],
                audience=row["audience"],
                role=row["role"],
                scopes=self._decode_values(row["scopes_json"]),
                resource_grants=self._decode_values(row["resource_grants_json"]),
                credential_id=row["credential_id"],
                codec=codec,
                access_ttl=access_ttl,
                refresh_ttl=refresh_ttl,
            )

    def issue_session_from_bootstrap(
        self,
        *,
        challenge: str,
        audience: str,
        origin: str,
        codec: SignedValueCodec,
        access_ttl: timedelta,
        refresh_ttl: timedelta,
    ) -> tuple[SessionRecord, str, str]:
        """原子消费绑定安装实例与 Origin 的一次性 bootstrap challenge。"""

        decoded = codec.decode(challenge, purpose="bootstrap")
        payload = decoded.payload
        if (
            payload.get("installation_id") != self._installation_id
            or payload.get("origin") != origin
            or payload.get("audience") != audience
        ):
            raise ValueError("bootstrap_invalid")
        nonce = payload.get("nonce")
        actor_id = payload.get("actor_id")
        scopes = payload.get("scopes")
        resource_grants = payload.get("resource_grants")
        if (
            not isinstance(nonce, str)
            or not nonce
            or not isinstance(actor_id, str)
            or not isinstance(scopes, list)
            or not isinstance(resource_grants, list)
            or decoded.expires_at is None
        ):
            raise ValueError("bootstrap_invalid")
        with self._lock, self._connection:
            try:
                self._connection.execute(
                    """
                    INSERT INTO api_challenges (
                        nonce, installation_id, audience, origin,
                        expires_at, consumed_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        nonce,
                        self._installation_id,
                        audience,
                        origin,
                        decoded.expires_at.isoformat(),
                        self._now().isoformat(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("bootstrap_replayed") from exc
            return self._create_session(
                actor_id=actor_id,
                audience=audience,
                role="administrator" if audience == ADMIN_FRONTEND_AUDIENCE else "user",
                scopes=self._normalize_scopes(scopes),
                resource_grants=tuple(str(value) for value in resource_grants),
                codec=codec,
                access_ttl=access_ttl,
                refresh_ttl=refresh_ttl,
                commit=False,
            )

    def create_bootstrap_challenge(
        self,
        *,
        codec: SignedValueCodec,
        audience: str,
        origin: str,
        actor_id: str = "local_user",
        scopes: Iterable[str] = (),
        resource_grants: Iterable[str] = (),
        ttl: timedelta = timedelta(minutes=2),
    ) -> str:
        """为受信本机启动器生成一次性 challenge。"""

        if audience == PLATFORM_SERVICE_AUDIENCE:
            raise ValueError(
                "bootstrap challenge cannot issue platform service sessions"
            )
        if not origin.strip():
            raise ValueError("bootstrap challenge requires origin")
        normalized = self._normalize_scopes(scopes)
        return codec.encode(
            purpose="bootstrap",
            payload={
                "nonce": secrets.token_urlsafe(18),
                "installation_id": self._installation_id,
                "audience": audience,
                "origin": origin,
                "actor_id": actor_id,
                "scopes": list(normalized),
                "resource_grants": list(resource_grants),
            },
            ttl=ttl,
        )

    def refresh_session(
        self,
        *,
        refresh_token: str,
        codec: SignedValueCodec,
        access_ttl: timedelta,
        refresh_ttl: timedelta,
    ) -> tuple[SessionRecord, str, str]:
        """原子轮换 session；旧 access 与 refresh token 立即失效。"""

        decoded = codec.decode(refresh_token, purpose="refresh")
        session_id = decoded.payload.get("session_id")
        if not isinstance(session_id, str):
            raise TypeError("refresh_invalid")
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT * FROM api_sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            if row is None or row["revoked_at"] is not None:
                raise ValueError("session_revoked")
            if self._parse_time(row["refresh_expires_at"]) <= self._now():
                raise ValueError("refresh_expired")
            if not self._verify_secret(refresh_token, row["refresh_hash"]):
                raise ValueError("refresh_invalid")
            self._require_active_credential(row["credential_id"])
            self._connection.execute(
                "UPDATE api_sessions SET revoked_at = ? WHERE session_id = ?",
                (self._now().isoformat(), session_id),
            )
            return self._create_session(
                actor_id=row["actor_id"],
                audience=row["audience"],
                role=row["role"],
                scopes=self._decode_values(row["scopes_json"]),
                resource_grants=self._decode_values(row["resource_grants_json"]),
                credential_id=row["credential_id"],
                codec=codec,
                access_ttl=access_ttl,
                refresh_ttl=refresh_ttl,
                commit=False,
            )

    def authenticate_access(
        self,
        *,
        access_token: str,
        codec: SignedValueCodec,
        allow_revoked: bool = False,
    ) -> SessionRecord:
        """校验签名 token 后重新读取 session 与 credential 状态。"""

        decoded = codec.decode(access_token, purpose="access")
        session_id = decoded.payload.get("session_id")
        if not isinstance(session_id, str):
            raise TypeError("access_invalid")
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM api_sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            if row is None:
                raise ValueError("session_invalid")
            if row["revoked_at"] is not None and not allow_revoked:
                raise ValueError("session_revoked")
            if self._parse_time(row["access_expires_at"]) <= self._now():
                raise ValueError("access_expired")
            if not allow_revoked:
                self._require_active_credential(row["credential_id"])
            return self._row_to_session(row)

    def get_active_session(self, session_id: str) -> SessionRecord:
        """Read current session state and enforce revocation, expiry, and credential state."""

        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM api_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                raise ValueError("session_invalid")
            if row["revoked_at"] is not None:
                raise ValueError("session_revoked")
            if self._parse_time(row["access_expires_at"]) <= self._now():
                raise ValueError("access_expired")
            self._require_active_credential(row["credential_id"])
            return self._row_to_session(row)

    def revoke_session(self, session_id: str) -> bool:
        """撤销 session；重复撤销保持幂等。"""

        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                UPDATE api_sessions
                SET revoked_at = COALESCE(revoked_at, ?)
                WHERE session_id = ?
                """,
                (self._now().isoformat(), session_id),
            )
        return cursor.rowcount > 0

    def issue_ws_ticket(
        self,
        *,
        session: SessionRecord,
        codec: SignedValueCodec,
        resource: str,
        subprotocol: str,
        scopes: Iterable[str],
        origin: str | None,
        ttl: timedelta,
    ) -> tuple[TicketRecord, str]:
        """从耐久 session 状态原子签发资源绑定的单次 ticket。"""

        requested = self._normalize_scopes(scopes)
        if ttl <= timedelta(0):
            raise ValueError("ticket_ttl_invalid")
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT * FROM api_sessions WHERE session_id = ?",
                (session.session_id,),
            ).fetchone()
            if row is None or row["revoked_at"] is not None:
                raise ValueError("session_revoked")
            if self._parse_time(row["access_expires_at"]) <= self._now():
                raise ValueError("access_expired")
            self._require_active_credential(row["credential_id"])
            live_session = self._row_to_session(row)
            if not set(requested).issubset(live_session.scopes):
                raise ValueError("scope_forbidden")

            ticket_id = f"ticket_{secrets.token_urlsafe(18)}"
            expires_at = self._now() + ttl
            ticket = TicketRecord(
                ticket_id=ticket_id,
                session_id=live_session.session_id,
                actor_id=live_session.actor_id,
                audience=live_session.audience,
                resource=resource,
                subprotocol=subprotocol,
                scopes=requested,
                origin=origin,
                expires_at=expires_at,
            )
            token = codec.encode(
                purpose="ws_ticket",
                payload={
                    "ticket_id": ticket_id,
                    "session_id": live_session.session_id,
                    "actor_id": live_session.actor_id,
                    "audience": live_session.audience,
                    "resource": resource,
                    "subprotocol": subprotocol,
                    "scopes": list(requested),
                    "origin": origin,
                },
                ttl=ttl,
            )
            self._connection.execute(
                """
                INSERT INTO api_tickets (
                    ticket_id, session_id, actor_id, audience, resource,
                    subprotocol, scopes_json, origin, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ticket_id,
                    live_session.session_id,
                    live_session.actor_id,
                    live_session.audience,
                    resource,
                    subprotocol,
                    self._encode_values(requested),
                    origin,
                    expires_at.isoformat(),
                ),
            )
        return ticket, token

    def consume_ws_ticket(
        self,
        *,
        token: str,
        codec: SignedValueCodec,
        resource: str,
        subprotocol: str,
        origin: str | None,
    ) -> TicketRecord:
        """原子消费 ticket，并检查 session、凭据、目标和 Origin。"""

        decoded = codec.decode(token, purpose="ws_ticket")
        ticket_id = decoded.payload.get("ticket_id")
        if not isinstance(ticket_id, str):
            raise TypeError("ticket_invalid")
        now = self._now()
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT * FROM api_tickets WHERE ticket_id = ?", (ticket_id,)
            ).fetchone()
            if row is None or row["consumed_at"] is not None:
                raise ValueError("ticket_replayed")
            if self._parse_time(row["expires_at"]) <= now:
                raise ValueError("ticket_expired")
            if row["resource"] != resource or row["subprotocol"] != subprotocol:
                raise ValueError("ticket_binding_invalid")
            if row["origin"] != origin:
                raise ValueError("ticket_origin_invalid")
            session = self._connection.execute(
                "SELECT * FROM api_sessions WHERE session_id = ?",
                (row["session_id"],),
            ).fetchone()
            if session is None or session["revoked_at"] is not None:
                raise ValueError("session_revoked")
            self._require_active_credential(session["credential_id"])
            cursor = self._connection.execute(
                """
                UPDATE api_tickets SET consumed_at = ?
                WHERE ticket_id = ? AND consumed_at IS NULL
                """,
                (now.isoformat(), ticket_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("ticket_replayed")
        return TicketRecord(
            ticket_id=ticket_id,
            session_id=row["session_id"],
            actor_id=row["actor_id"],
            audience=row["audience"],
            resource=row["resource"],
            subprotocol=row["subprotocol"],
            scopes=self._decode_values(row["scopes_json"]),
            origin=row["origin"],
            expires_at=self._parse_time(row["expires_at"]),
            consumed_at=now,
        )

    def _create_session(
        self,
        *,
        actor_id: str,
        audience: str,
        role: str,
        scopes: Iterable[str],
        resource_grants: Iterable[str],
        codec: SignedValueCodec,
        access_ttl: timedelta,
        refresh_ttl: timedelta,
        credential_id: str | None = None,
        commit: bool = True,
    ) -> tuple[SessionRecord, str, str]:
        if access_ttl <= timedelta(0) or refresh_ttl <= timedelta(0):
            raise ValueError("session TTL must be positive")
        session_id = f"sess_{secrets.token_urlsafe(18)}"
        now = self._now()
        access_expires_at = now + access_ttl
        refresh_expires_at = now + refresh_ttl
        access_token = codec.encode(
            purpose="access",
            payload={"session_id": session_id},
            ttl=access_ttl,
            now=now,
        )
        refresh_token = codec.encode(
            purpose="refresh",
            payload={"session_id": session_id},
            ttl=refresh_ttl,
            now=now,
        )
        session = SessionRecord(
            session_id=session_id,
            actor_id=actor_id,
            audience=audience,
            role=role,
            scopes=self._normalize_scopes(scopes),
            resource_grants=tuple(resource_grants),
            access_expires_at=access_expires_at,
            refresh_expires_at=refresh_expires_at,
            credential_id=credential_id,
        )
        self._connection.execute(
            """
            INSERT INTO api_sessions (
                session_id, actor_id, audience, role, scopes_json,
                resource_grants_json, access_expires_at, refresh_expires_at,
                refresh_hash, credential_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                actor_id,
                audience,
                role,
                self._encode_values(session.scopes),
                self._encode_values(session.resource_grants),
                access_expires_at.isoformat(),
                refresh_expires_at.isoformat(),
                self._hash_secret(refresh_token),
                credential_id,
                now.isoformat(),
            ),
        )
        if commit:
            self._connection.commit()
        return session, access_token, refresh_token

    def _require_active_credential(self, credential_id: str | None) -> None:
        if credential_id is None:
            return
        row = self._connection.execute(
            "SELECT revoked_at FROM api_credentials WHERE credential_id = ?",
            (credential_id,),
        ).fetchone()
        if row is None or row["revoked_at"] is not None:
            raise ValueError("credential_revoked")

    @staticmethod
    def _hash_secret(value: str) -> str:
        salt = secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac("sha256", value.encode(), salt, 210_000)
        return f"pbkdf2$210000${salt.hex()}${digest.hex()}"

    @staticmethod
    def _verify_secret(value: str, encoded: str) -> bool:
        try:
            algorithm, iterations, salt_hex, digest_hex = encoded.split("$", 3)
            if algorithm != "pbkdf2":
                return False
            digest = hashlib.pbkdf2_hmac(
                "sha256",
                value.encode(),
                bytes.fromhex(salt_hex),
                int(iterations),
            )
            return hmac.compare_digest(digest.hex(), digest_hex)
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _encode_values(values: Iterable[str]) -> str:
        return json.dumps(tuple(values), ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _decode_values(value: str) -> tuple[str, ...]:
        decoded = json.loads(value)
        if not isinstance(decoded, list):
            raise TypeError("stored values invalid")
        return tuple(str(item) for item in decoded)

    @staticmethod
    def _normalize_scopes(scopes: Iterable[str]) -> tuple[str, ...]:
        normalized = tuple(dict.fromkeys(str(scope) for scope in scopes))
        unknown = set(normalized) - ALL_EXPORTED_SCOPES
        if unknown:
            raise ValueError(f"unknown_scope:{','.join(sorted(unknown))}")
        return normalized

    def _row_to_session(self, row: sqlite3.Row) -> SessionRecord:
        return SessionRecord(
            session_id=row["session_id"],
            actor_id=row["actor_id"],
            audience=row["audience"],
            role=row["role"],
            scopes=self._decode_values(row["scopes_json"]),
            resource_grants=self._decode_values(row["resource_grants_json"]),
            access_expires_at=self._parse_time(row["access_expires_at"]),
            refresh_expires_at=self._parse_time(row["refresh_expires_at"]),
            revoked_at=(
                self._parse_time(row["revoked_at"]) if row["revoked_at"] else None
            ),
            credential_id=row["credential_id"],
        )

    @staticmethod
    def _parse_time(value: str) -> datetime:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError("stored time must be timezone-aware")
        return parsed.astimezone(UTC)

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC)
