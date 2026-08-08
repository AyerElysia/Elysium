"""Fail-closed backend factory for selectable life-domain storage."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

from sqlalchemy.ext.asyncio import async_sessionmaker

from src.kernel.storage import (
    MySQLStorageConfig,
    SQLiteStorageConfig,
    create_mysql_storage_engine,
    create_sqlite_storage_engine,
)
from src.kernel.storage.engine import MySQLTLSMode

from .authority import FileAuthorityRegistry, MySQLAuthorityRegistry
from .contracts import StorageBackendRuntime
from .models import AuthorityToken, BackendGeneration, BackendKind, GenerationStatus


class StorageConfigurationError(RuntimeError):
    """Raised when selectable storage cannot be opened without guessing."""


class GenerationGuardError(StorageConfigurationError):
    """Raised when configuration and the registered generation disagree."""


AuthorityProvider = Literal["file", "mysql"]


@dataclass(frozen=True, slots=True)
class LocalBackendSettings:
    database_path: Path = Path("data/life_storage/local.sqlite3")
    authority_state_path: Path = Path("data/life_storage/authority.json")
    busy_timeout_seconds: int = 10


@dataclass(frozen=True, slots=True)
class MySQLBackendSettings:
    host: str = "127.0.0.1"
    port: int = 3306
    database: str = "elysium"
    user: str = "elysium"
    password_env: str = "ELYSIUM_LIFE_STORAGE_MYSQL_PASSWORD"
    password: str = field(default="", repr=False)
    ssl_mode: MySQLTLSMode = "disabled"
    ssl_ca: str = ""
    ssl_cert: str = ""
    ssl_key: str = ""
    pool_size: int = 20
    max_overflow: int = 20
    pool_recycle_seconds: int = 1800
    connect_timeout_seconds: int = 5
    pool_timeout_seconds: int = 10
    query_timeout_seconds: int = 10
    lock_wait_timeout_seconds: int = 5
    idle_session_timeout_seconds: int = 180


@dataclass(frozen=True, slots=True)
class StorageFactorySettings:
    """Secret-free startup settings; passwords/tokens come from named env vars."""

    enabled: bool = False
    authoritative_backend: BackendKind = BackendKind.LOCAL
    backend_generation: str = ""
    schema_version: int = 1
    multi_writer_enabled: bool = False
    multi_writer_protocol_version: int = 1
    registry_id: str = "life-domain"
    authority_provider: AuthorityProvider = "file"
    authority_epoch: int = 0
    authority_owner_id: str = ""
    fencing_token_env: str = "ELYSIUM_LIFE_STORAGE_FENCING_TOKEN"
    require_verified_generation: bool = True
    authority_lease_seconds: int = 120
    authority_renew_interval_seconds: int = 40
    local: LocalBackendSettings = field(default_factory=LocalBackendSettings)
    mysql: MySQLBackendSettings = field(default_factory=MySQLBackendSettings)


def settings_from_life_engine_config(
    config: Any,
    *,
    global_config: Any | None = None,
) -> StorageFactorySettings:
    """Build Life Engine storage from the single Core configuration source."""

    local = config.storage_local
    if global_config is None:
        from src.core.config import get_core_config

        try:
            global_config = get_core_config()
        except RuntimeError as exc:
            raise StorageConfigurationError(
                "global storage configuration is not initialized"
            ) from exc

    storage = global_config.storage
    database = global_config.database
    try:
        backend = BackendKind(str(storage.backend))
    except ValueError as exc:
        raise StorageConfigurationError(
            "global storage.backend must be local or mysql"
        ) from exc
    return StorageFactorySettings(
        enabled=backend == BackendKind.MYSQL,
        authoritative_backend=backend,
        backend_generation=(
            str(storage.backend_generation) if backend == BackendKind.MYSQL else ""
        ),
        schema_version=int(storage.schema_version),
        multi_writer_enabled=bool(storage.multi_writer_enabled),
        multi_writer_protocol_version=int(storage.multi_writer_protocol_version),
        registry_id=str(storage.registry_id),
        authority_provider=("mysql" if backend == BackendKind.MYSQL else "file"),
        authority_epoch=0,
        authority_owner_id=str(storage.authority_owner_id),
        require_verified_generation=bool(storage.require_verified_generation),
        authority_lease_seconds=int(storage.authority_lease_seconds),
        authority_renew_interval_seconds=int(
            storage.authority_renew_interval_seconds
        ),
        local=LocalBackendSettings(
            database_path=Path(local.database_path),
            authority_state_path=Path(local.authority_state_path),
            busy_timeout_seconds=int(local.busy_timeout_seconds),
        ),
        mysql=MySQLBackendSettings(
            host=str(database.mysql_host),
            port=int(database.mysql_port),
            database=str(database.mysql_database),
            user=str(database.mysql_user),
            password_env="ELYSIUM_MYSQL_PASSWORD",
            password=str(database.mysql_password),
            ssl_mode=cast(MySQLTLSMode, str(database.mysql_ssl_mode)),
            ssl_ca=str(database.mysql_ssl_ca),
            ssl_cert=str(database.mysql_ssl_cert),
            ssl_key=str(database.mysql_ssl_key),
            pool_size=int(database.connection_pool_size),
            max_overflow=int(database.mysql_max_overflow),
            pool_recycle_seconds=int(database.mysql_pool_recycle_seconds),
            connect_timeout_seconds=int(database.connection_timeout),
            pool_timeout_seconds=int(database.mysql_pool_timeout_seconds),
            query_timeout_seconds=int(database.mysql_query_timeout_seconds),
            lock_wait_timeout_seconds=int(database.mysql_lock_wait_timeout_seconds),
            idle_session_timeout_seconds=180,
        ),
    )


def _guard_generation(
    generation: BackendGeneration,
    settings: StorageFactorySettings,
) -> None:
    if generation.backend != settings.authoritative_backend:
        raise GenerationGuardError(
            "configured backend does not match generation manifest"
        )
    if generation.schema_version != int(settings.schema_version):
        raise GenerationGuardError(
            "configured schema version does not match generation manifest"
        )
    if (
        settings.require_verified_generation
        and generation.status != GenerationStatus.VERIFIED
    ):
        raise GenerationGuardError("configured generation is not verified")


def _build_token(
    settings: StorageFactorySettings,
    generation: BackendGeneration,
    authority_health: dict[str, object],
    environment: Mapping[str, str],
    *,
    authority_epoch: int | None = None,
) -> AuthorityToken:
    if not settings.authority_owner_id.strip():
        raise StorageConfigurationError("authority_owner_id must not be empty")
    if not settings.fencing_token_env.strip():
        raise StorageConfigurationError("fencing_token_env must not be empty")
    fencing_token = environment.get(settings.fencing_token_env, "")
    if not fencing_token:
        raise StorageConfigurationError(
            f"fencing token environment variable is missing: {settings.fencing_token_env}"
        )
    epoch = int(
        settings.authority_epoch if authority_epoch is None else authority_epoch
    )
    if epoch <= 0:
        raise StorageConfigurationError("authority epoch must be positive")
    expected = {
        "active_backend": generation.backend.value,
        "active_generation": generation.generation_id,
        "authority_epoch": epoch,
        "owner_id": settings.authority_owner_id,
    }
    for key, value in expected.items():
        if authority_health.get(key) != value:
            raise GenerationGuardError(f"authority registry mismatch: {key}")
    lease_until = str(authority_health.get("lease_until") or "")
    if not lease_until:
        raise GenerationGuardError("authority registry has no active lease")
    return AuthorityToken(
        registry_id=settings.registry_id,
        backend=generation.backend,
        generation_id=generation.generation_id,
        authority_epoch=epoch,
        owner_id=settings.authority_owner_id,
        lease_until=lease_until,
        fencing_token=fencing_token,
    )


async def open_storage_backend(
    settings: StorageFactorySettings,
    *,
    environment: Mapping[str, str] | None = None,
) -> StorageBackendRuntime:
    """Open exactly one configured backend; never fall back to the other one."""

    if not settings.enabled:
        return StorageBackendRuntime.disabled(settings.authoritative_backend)
    if not settings.backend_generation:
        raise StorageConfigurationError("backend_generation must not be empty")
    environment = os.environ if environment is None else environment

    if settings.authoritative_backend == BackendKind.LOCAL:
        sqlite_config = SQLiteStorageConfig(
            database_path=settings.local.database_path,
            busy_timeout_seconds=settings.local.busy_timeout_seconds,
        )
        engine = create_sqlite_storage_engine(sqlite_config)
        backend_identity = sqlite_config.safe_identity
    elif settings.authoritative_backend == BackendKind.MYSQL:
        password = settings.mysql.password
        password_env = settings.mysql.password_env.strip()
        if not password and password_env:
            password = environment.get(password_env, "")
        if not password:
            raise StorageConfigurationError(
                "global MySQL password is empty; configure "
                "database.mysql_password with an environment reference"
            )
        mysql_config = MySQLStorageConfig(
            host=settings.mysql.host,
            port=settings.mysql.port,
            database=settings.mysql.database,
            user=settings.mysql.user,
            password=password,
            ssl_mode=settings.mysql.ssl_mode,
            ssl_ca=settings.mysql.ssl_ca,
            ssl_cert=settings.mysql.ssl_cert,
            ssl_key=settings.mysql.ssl_key,
            pool_size=settings.mysql.pool_size,
            max_overflow=settings.mysql.max_overflow,
            pool_recycle_seconds=settings.mysql.pool_recycle_seconds,
            connect_timeout_seconds=settings.mysql.connect_timeout_seconds,
            pool_timeout_seconds=settings.mysql.pool_timeout_seconds,
            application_query_timeout_seconds=settings.mysql.query_timeout_seconds,
            innodb_lock_wait_timeout_seconds=settings.mysql.lock_wait_timeout_seconds,
            idle_session_timeout_seconds=settings.mysql.idle_session_timeout_seconds,
        )
        engine = create_mysql_storage_engine(mysql_config)
        backend_identity = mysql_config.safe_identity
    else:  # pragma: no cover - enum exhaustiveness guard
        raise StorageConfigurationError(
            f"unsupported backend: {settings.authoritative_backend}"
        )

    if settings.authority_provider == "file":
        registry = FileAuthorityRegistry(
            settings.local.authority_state_path,
            registry_id=settings.registry_id,
        )
    elif settings.authority_provider == "mysql":
        if settings.authoritative_backend != BackendKind.MYSQL:
            await engine.dispose()
            raise StorageConfigurationError(
                "mysql authority provider requires the mysql backend; "
                "multi-host local authority is unsupported"
            )
        registry = MySQLAuthorityRegistry(engine, registry_id=settings.registry_id)
    else:
        await engine.dispose()
        raise StorageConfigurationError("authority_provider must be file or mysql")

    try:
        generation = await registry.get_generation(settings.backend_generation)
        if generation is None:
            raise GenerationGuardError(
                f"generation is not registered: {settings.backend_generation}"
            )
        _guard_generation(generation, settings)
        authority_health = await registry.health()
        authority_status = str(authority_health.get("status") or "")
        if authority_status not in {"healthy", "degraded", "disabled"}:
            raise GenerationGuardError("authority registry is unavailable")

        if settings.authority_provider == "mysql":
            owner_id = settings.authority_owner_id.strip()
            if not owner_id:
                raise StorageConfigurationError(
                    "authority_owner_id must not be empty for MySQL writers"
                )
            active_generation = str(authority_health.get("active_generation") or "")
            if active_generation:
                token = await registry.join_generation(
                    generation.generation_id,
                    owner_id=owner_id,
                )
                shared_writers = True
            else:
                token = await registry.activate_generation(
                    generation.generation_id,
                    expected_epoch=int(authority_health.get("authority_epoch") or 0),
                    owner_id=owner_id,
                    lease_seconds=settings.authority_lease_seconds,
                    confirm_previous_writers_stopped=False,
                )
                shared_writers = True
        else:
            token = _build_token(settings, generation, authority_health, environment)
            await registry.validate(token)
            shared_writers = False

        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        runtime = StorageBackendRuntime(
            enabled=True,
            backend=settings.authoritative_backend,
            backend_identity=backend_identity,
            generation=generation,
            authority_registry=registry,
            authority_token=token,
            engine=engine,
            session_factory=session_factory,
            shared_writers=shared_writers,
        )
        from .writer_claims import SQLSingletonWriterClaimStore

        runtime._singleton_writer_claims = SQLSingletonWriterClaimStore(runtime)
        if shared_writers:

            async def _validate_shared_before_commit(session: Any) -> None:
                connection = await session.connection()
                await registry.validate_shared_in_transaction(connection, token)

            runtime._write_fence = _validate_shared_before_commit
            runtime._writer_validator = lambda: registry.validate_shared(token)
        return runtime
    except BaseException:
        await engine.dispose()
        raise


__all__ = [
    "AuthorityProvider",
    "GenerationGuardError",
    "LocalBackendSettings",
    "MySQLBackendSettings",
    "StorageConfigurationError",
    "StorageFactorySettings",
    "open_storage_backend",
    "settings_from_life_engine_config",
]
