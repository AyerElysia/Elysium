"""Independent MySQL engine primitives for selectable domain storage.

The application Core database uses a process-global engine.  Life-domain
storage must be selectable independently, so this module deliberately creates
caller-owned engines and never mutates the Core engine singleton.
"""

from __future__ import annotations

import ssl
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import URL, event, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

MySQLTLSMode = Literal["disabled", "required", "verify-ca", "verify-full"]

_STRICT_SQL_MODES = (
    "STRICT_TRANS_TABLES",
    "ERROR_FOR_DIVISION_BY_ZERO",
    "NO_ENGINE_SUBSTITUTION",
)


@dataclass(frozen=True, slots=True)
class SQLiteStorageConfig:
    """Settings for a caller-owned local SQLAlchemy storage engine."""

    database_path: Path
    busy_timeout_seconds: int = 10

    def __post_init__(self) -> None:
        if int(self.busy_timeout_seconds) <= 0:
            raise ValueError("SQLite busy_timeout_seconds must be positive")

    @property
    def safe_identity(self) -> str:
        return f"sqlite:///{self.database_path.resolve()}"


@dataclass(frozen=True, slots=True)
class MySQLStorageConfig:
    """Secret-bearing connection settings for one caller-owned MySQL engine."""

    host: str
    port: int
    database: str
    user: str
    password: str
    charset: str = "utf8mb4"
    ssl_mode: MySQLTLSMode = "disabled"
    ssl_ca: str = ""
    ssl_cert: str = ""
    ssl_key: str = ""
    pool_size: int = 20
    max_overflow: int = 20
    pool_recycle_seconds: int = 1800
    connect_timeout_seconds: int = 5
    pool_timeout_seconds: int = 10
    application_query_timeout_seconds: int = 10
    innodb_lock_wait_timeout_seconds: int = 5
    idle_session_timeout_seconds: int = 180
    isolation_level: Literal["READ COMMITTED"] = "READ COMMITTED"

    def __post_init__(self) -> None:
        """Reject lossy or unbounded settings before opening any connection."""

        if not self.host.strip():
            raise ValueError("MySQL storage host must not be empty")
        if not 1 <= int(self.port) <= 65535:
            raise ValueError("MySQL storage port must be between 1 and 65535")
        if not self.database.strip():
            raise ValueError("MySQL storage database must not be empty")
        if not self.user.strip():
            raise ValueError("MySQL storage user must not be empty")
        if self.charset.lower() != "utf8mb4":
            raise ValueError("Elysium life-domain MySQL charset must be utf8mb4")
        if self.ssl_mode not in {
            "disabled",
            "required",
            "verify-ca",
            "verify-full",
        }:
            raise ValueError(f"unsupported MySQL TLS mode: {self.ssl_mode}")
        if bool(self.ssl_cert) != bool(self.ssl_key):
            raise ValueError("MySQL client certificate and key must be configured together")
        for name in (
            "pool_size",
            "pool_recycle_seconds",
            "connect_timeout_seconds",
            "pool_timeout_seconds",
            "application_query_timeout_seconds",
            "innodb_lock_wait_timeout_seconds",
            "idle_session_timeout_seconds",
        ):
            if int(getattr(self, name)) <= 0:
                raise ValueError(f"{name} must be positive")
        if int(self.max_overflow) < 0:
            raise ValueError("max_overflow must not be negative")

    @property
    def safe_identity(self) -> str:
        """Return a secret-free endpoint identity suitable for health output."""

        host = "127.0.0.1" if self.host.lower() == "localhost" else self.host
        return f"mysql://{self.user}@{host}:{self.port}/{self.database}"


def build_mysql_ssl_context(config: MySQLStorageConfig) -> ssl.SSLContext | None:
    """Build the explicit asyncmy TLS context requested by ``config``."""

    if config.ssl_mode == "disabled":
        return None
    context = ssl.create_default_context(cafile=config.ssl_ca or None)
    if config.ssl_mode == "required":
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    elif config.ssl_mode == "verify-ca":
        context.check_hostname = False
        context.verify_mode = ssl.CERT_REQUIRED
    else:
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
    if config.ssl_cert:
        context.load_cert_chain(
            certfile=str(Path(config.ssl_cert)),
            keyfile=str(Path(config.ssl_key)),
        )
    return context


def _install_mysql_session_contract(
    engine: AsyncEngine,
    config: MySQLStorageConfig,
) -> None:
    """Apply mandatory storage invariants to every pooled DBAPI connection."""

    strict_modes = ",".join(_STRICT_SQL_MODES)
    statements = (
        "SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED",
        "SET SESSION time_zone = '+00:00'",
        f"SET SESSION innodb_lock_wait_timeout = {int(config.innodb_lock_wait_timeout_seconds)}",
        f"SET SESSION max_execution_time = {int(config.application_query_timeout_seconds) * 1000}",
        f"SET SESSION wait_timeout = {int(config.idle_session_timeout_seconds)}",
        f"SET SESSION sql_mode = '{strict_modes}'",
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _configure_connection(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            for statement in statements:
                cursor.execute(statement)
        finally:
            cursor.close()


def create_mysql_storage_engine(config: MySQLStorageConfig) -> AsyncEngine:
    """Create a caller-owned asyncmy engine without logging its password."""

    host = "127.0.0.1" if config.host.lower() == "localhost" else config.host
    url = URL.create(
        "mysql+asyncmy",
        username=config.user,
        password=config.password,
        host=host,
        port=int(config.port),
        database=config.database,
        query={"charset": "utf8mb4"},
    )
    connect_args: dict[str, Any] = {
        "connect_timeout": int(config.connect_timeout_seconds),
        "charset": "utf8mb4",
    }
    ssl_context = build_mysql_ssl_context(config)
    if ssl_context is not None:
        connect_args["ssl"] = ssl_context
    engine = create_async_engine(
        url,
        future=True,
        pool_size=int(config.pool_size),
        max_overflow=int(config.max_overflow),
        pool_timeout=int(config.pool_timeout_seconds),
        pool_recycle=int(config.pool_recycle_seconds),
        pool_pre_ping=True,
        pool_reset_on_return="rollback",
        connect_args=connect_args,
    )
    _install_mysql_session_contract(engine, config)
    return engine


def create_sqlite_storage_engine(config: SQLiteStorageConfig) -> AsyncEngine:
    """Create a caller-owned WAL SQLite engine for the local storage backend."""

    database_path = config.database_path.resolve()
    database_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_async_engine(
        URL.create("sqlite+aiosqlite", database=str(database_path)),
        future=True,
        pool_pre_ping=True,
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _configure_sqlite_connection(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys = ON")
            cursor.execute("PRAGMA journal_mode = WAL")
            cursor.execute(
                f"PRAGMA busy_timeout = {int(config.busy_timeout_seconds) * 1000}"
            )
        finally:
            cursor.close()

    return engine


async def sqlite_storage_health(
    engine: AsyncEngine,
    *,
    backend_identity: str,
) -> dict[str, Any]:
    """Return a bounded, read-only local backend health snapshot."""

    try:
        async with engine.connect() as connection:
            quick_check = await connection.scalar(text("PRAGMA quick_check"))
            foreign_keys = await connection.scalar(text("PRAGMA foreign_keys"))
            journal_mode = await connection.scalar(text("PRAGMA journal_mode"))
        reasons: list[str] = []
        if str(quick_check).lower() != "ok":
            reasons.append("SQLite quick_check failed")
        if int(foreign_keys or 0) != 1:
            reasons.append("SQLite foreign keys are disabled")
        if str(journal_mode).lower() != "wal":
            reasons.append("SQLite journal mode is not WAL")
        return {
            "status": "degraded" if reasons else "healthy",
            "backend_identity": backend_identity,
            "quick_check": str(quick_check),
            "foreign_keys": bool(foreign_keys),
            "journal_mode": str(journal_mode),
            "degraded_reasons": reasons,
        }
    except Exception as exc:  # noqa: BLE001 - health must return a diagnostic state
        return {
            "status": "failed",
            "backend_identity": backend_identity,
            "error_type": type(exc).__name__,
        }


async def mysql_storage_health(
    engine: AsyncEngine,
    *,
    backend_identity: str,
) -> dict[str, Any]:
    """Return a read-only, secret-free MySQL compatibility snapshot."""

    try:
        async with engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        "SELECT DATABASE() AS database_name, VERSION() AS server_version, "
                        "@@session.time_zone AS session_time_zone, "
                        "@@session.transaction_isolation AS isolation_level, "
                        "@@session.sql_mode AS sql_mode"
                    )
                )
            ).mappings().one()
        version = str(row["server_version"] or "")
        major_text = version.split(".", 1)[0]
        major = int(major_text) if major_text.isdigit() else 0
        sql_modes = {
            item.strip().upper()
            for item in str(row["sql_mode"] or "").split(",")
            if item.strip()
        }
        missing_modes = [mode for mode in _STRICT_SQL_MODES if mode not in sql_modes]
        reasons: list[str] = []
        if major < 8:
            reasons.append("MySQL 8 or newer is required")
        if str(row["session_time_zone"]) != "+00:00":
            reasons.append("session timezone is not UTC")
        if str(row["isolation_level"]).upper().replace("-", " ") != "READ COMMITTED":
            reasons.append("session isolation is not READ COMMITTED")
        if missing_modes:
            reasons.append("strict SQL modes are missing")
        return {
            "status": "degraded" if reasons else "healthy",
            "backend_identity": backend_identity,
            "database": str(row["database_name"] or ""),
            "server_version": version,
            "session_time_zone": str(row["session_time_zone"] or ""),
            "isolation_level": str(row["isolation_level"] or ""),
            "strict_sql_mode": not missing_modes,
            "degraded_reasons": reasons,
            "pool": {
                "size": getattr(engine.pool, "size", lambda: None)(),
                "checked_out": getattr(engine.pool, "checkedout", lambda: 0)(),
                "overflow": getattr(engine.pool, "overflow", lambda: 0)(),
            },
        }
    except Exception as exc:  # noqa: BLE001 - health must return a diagnostic state
        return {
            "status": "failed",
            "backend_identity": backend_identity,
            "error_type": type(exc).__name__,
        }


__all__ = [
    "MySQLStorageConfig",
    "MySQLTLSMode",
    "SQLiteStorageConfig",
    "build_mysql_ssl_context",
    "create_mysql_storage_engine",
    "create_sqlite_storage_engine",
    "mysql_storage_health",
    "sqlite_storage_health",
]
