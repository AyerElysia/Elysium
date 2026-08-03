"""SQLite 核心业务库到 MySQL 的无损迁移工具。

源 SQLite 永远只读。目标库必须为空；所有业务数据在一个 InnoDB 事务中
写入并完成逐表内容指纹校验，校验失败会回滚整批数据。DDL 与迁移审计记录
独立提交，因此失败后会留下可诊断的空 schema，但不会留下半份业务数据。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import sqlite3
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    func,
    inspect,
    literal,
    select,
    text,
    update,
)
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    create_async_engine,
)

MIGRATION_TABLE_NAME = "elysium_core_migration_runs"

_migration_metadata = MetaData()
_migration_runs = Table(
    MIGRATION_TABLE_NAME,
    _migration_metadata,
    Column("run_id", String(36), primary_key=True),
    Column("source_file_sha256", String(64), nullable=False, index=True),
    Column("source_data_sha256", String(64), nullable=False, index=True),
    Column("status", String(20), nullable=False),
    Column("started_at", DateTime, nullable=False),
    Column("completed_at", DateTime, nullable=True),
    Column("manifest_json", Text, nullable=True),
    Column("error_summary", Text, nullable=True),
)


class MigrationSafetyError(RuntimeError):
    """迁移前置条件或无损校验失败。"""


@dataclass(frozen=True, slots=True)
class TableDigest:
    """单表按稳定主键顺序计算的内容摘要。"""

    name: str
    row_count: int
    sha256: str


@dataclass(frozen=True, slots=True)
class DatabaseDigest:
    """业务数据库中所有 ORM 表的聚合摘要。"""

    sha256: str
    tables: tuple[TableDigest, ...]

    def to_dict(self) -> dict[str, Any]:
        """转换为可序列化结构。"""
        return {
            "sha256": self.sha256,
            "tables": [asdict(table) for table in self.tables],
        }


@dataclass(frozen=True, slots=True)
class StringLengthObservation:
    """源数据中一个有界字符串列的真实最大长度。"""

    name: str
    declared_length: int
    actual_max_length: int


@dataclass(frozen=True, slots=True)
class SnapshotResult:
    """在线 SQLite 备份结果。"""

    source: str
    snapshot: str
    file_sha256: str
    data: DatabaseDigest
    nullable_columns_filled_with_null: tuple[str, ...] = ()
    string_lengths: tuple[StringLengthObservation, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """转换为可写入 manifest 的结构。"""
        return {
            "source": self.source,
            "snapshot": self.snapshot,
            "file_sha256": self.file_sha256,
            "data": self.data.to_dict(),
            "nullable_columns_filled_with_null": list(
                self.nullable_columns_filled_with_null
            ),
            "string_lengths": [asdict(item) for item in self.string_lengths],
        }


@dataclass(frozen=True, slots=True)
class MigrationResult:
    """迁移或重复验证结果。"""

    run_id: str
    source_file_sha256: str
    source_data: DatabaseDigest
    target_data: DatabaseDigest
    nullable_columns_filled_with_null: tuple[str, ...] = ()
    string_lengths: tuple[StringLengthObservation, ...] = ()
    already_applied: bool = False

    def to_dict(self) -> dict[str, Any]:
        """转换为不包含连接凭据的结果结构。"""
        return {
            "run_id": self.run_id,
            "source_file_sha256": self.source_file_sha256,
            "source_data": self.source_data.to_dict(),
            "target_data": self.target_data.to_dict(),
            "nullable_columns_filled_with_null": list(
                self.nullable_columns_filled_with_null
            ),
            "string_lengths": [asdict(item) for item in self.string_lengths],
            "already_applied": self.already_applied,
        }


def _utc_now_naive() -> dt.datetime:
    """返回可安全写入 MySQL DATETIME 的 UTC 时间。"""
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


def file_sha256(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    """流式计算文件 SHA-256。"""
    digest = hashlib.sha256()
    with path.open("rb") as file_handle:
        while chunk := file_handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _sqlite_readonly_url(path: Path) -> str:
    encoded_path = quote(path.resolve().as_posix(), safe="/:")
    return f"sqlite+aiosqlite:///file:{encoded_path}?mode=ro&uri=true"


def create_sqlite_readonly_engine(path: Path) -> AsyncEngine:
    """创建不会创建、更新或修复源文件的 SQLite 引擎。"""
    if not path.is_file():
        raise MigrationSafetyError(f"SQLite 源文件不存在: {path}")
    return create_async_engine(_sqlite_readonly_url(path), future=True)


def create_mysql_migration_engine(target_url: str) -> AsyncEngine:
    """验证并创建迁移专用 MySQL asyncmy 引擎。"""
    url = make_url(target_url)
    if url.get_backend_name() != "mysql" or url.get_driver_name() != "asyncmy":
        raise MigrationSafetyError(
            "目标 URL 必须使用 mysql+asyncmy://，不接受其他数据库或驱动"
        )
    query = dict(url.query)
    if query.get("charset", "").lower() != "utf8mb4":
        raise MigrationSafetyError("目标 URL 必须显式包含 charset=utf8mb4")
    return create_async_engine(
        url,
        future=True,
        pool_pre_ping=True,
        pool_recycle=900,
        connect_args={"charset": "utf8mb4"},
    )


def _normalize_datetime(value: Any) -> str:
    if isinstance(value, str):
        candidate = value.strip().replace("Z", "+00:00")
        try:
            parsed = dt.datetime.fromisoformat(candidate)
        except ValueError:
            return value
    elif isinstance(value, dt.datetime):
        parsed = value
    elif isinstance(value, dt.date):
        parsed = dt.datetime.combine(value, dt.time.min)
    else:
        raise MigrationSafetyError(f"无法规范化 datetime 值类型: {type(value).__name__}")

    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(dt.UTC).replace(tzinfo=None)
    return parsed.isoformat(timespec="microseconds")


def _canonical_value(value: Any, column: Column[Any]) -> Any:
    if value is None:
        return None
    if isinstance(column.type, Boolean):
        return bool(value)
    if isinstance(column.type, DateTime):
        return _normalize_datetime(value)
    if isinstance(column.type, Integer):
        return int(value)
    if isinstance(column.type, Float):
        number = float(value)
        if math.isnan(number):
            return "NaN"
        if math.isinf(number):
            return "Infinity" if number > 0 else "-Infinity"
        return format(number, ".17g")
    if isinstance(value, bytes):
        return {"hex": value.hex()}
    return str(value)


def _coerce_target_value(value: Any, column: Column[Any]) -> Any:
    """在不编造缺失值的前提下转换方言表示。"""
    if value is None:
        if not column.nullable and column.server_default is None:
            raise MigrationSafetyError(
                f"{column.table.name}.{column.name} 出现 NULL，但目标列不允许 NULL"
            )
        return None
    if isinstance(column.type, Boolean):
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"1", "true"}:
                return True
            if normalized in {"0", "false"}:
                return False
            raise MigrationSafetyError(
                f"{column.table.name}.{column.name} 包含非法布尔表示"
            )
        return bool(value)
    if isinstance(column.type, DateTime):
        normalized = _normalize_datetime(value)
        return dt.datetime.fromisoformat(normalized)
    if isinstance(column.type, Integer):
        return int(value)
    if isinstance(column.type, Float):
        return float(value)
    if isinstance(column.type, String):
        string_value = str(value)
        max_length = getattr(column.type, "length", None)
        if max_length is not None and len(string_value) > max_length:
            raise MigrationSafetyError(
                f"{column.table.name}.{column.name} 长度 {len(string_value)} "
                f"超过 schema 上限 {max_length}"
            )
        return string_value
    return value


async def _table_names(connection: AsyncConnection) -> set[str]:
    return await connection.run_sync(
        lambda sync_connection: set(inspect(sync_connection).get_table_names())
    )


async def assert_sqlite_integrity(connection: AsyncConnection) -> None:
    """在任何迁移读取前验证 SQLite 文件自身没有损坏。"""
    result = await connection.execute(text("PRAGMA quick_check"))
    values = [str(row[0]) for row in result]
    if values != ["ok"]:
        raise MigrationSafetyError(f"SQLite quick_check 失败: {values[:5]}")


async def analyze_source_schema(
    connection: AsyncConnection,
    metadata: MetaData,
) -> tuple[str, ...]:
    """验证源 schema 不会被静默截断，并返回可安全补 NULL 的新列。"""
    existing_tables = await _table_names(connection)
    expected_tables = {table.name for table in metadata.sorted_tables}
    missing_tables = sorted(expected_tables - existing_tables)
    if missing_tables:
        raise MigrationSafetyError(
            f"数据库缺少核心表: {', '.join(missing_tables)}"
        )

    def inspect_columns(sync_connection: Any) -> dict[str, set[str]]:
        inspector = inspect(sync_connection)
        return {
            table.name: {
                str(column["name"])
                for column in inspector.get_columns(table.name)
            }
            for table in metadata.sorted_tables
        }

    source_columns = await connection.run_sync(inspect_columns)
    nullable_fills: list[str] = []
    incompatible_missing: list[str] = []
    unexpected_columns: list[str] = []
    for table in metadata.sorted_tables:
        actual = source_columns[table.name]
        expected = {column.name for column in table.columns}
        unexpected_columns.extend(
            f"{table.name}.{name}" for name in sorted(actual - expected)
        )
        for column in table.columns:
            if column.name in actual:
                continue
            qualified_name = f"{table.name}.{column.name}"
            if column.nullable:
                nullable_fills.append(qualified_name)
            else:
                incompatible_missing.append(qualified_name)

    if unexpected_columns:
        raise MigrationSafetyError(
            "源数据库包含 ORM 未声明的列；为避免静默丢失数据，拒绝迁移: "
            + ", ".join(unexpected_columns)
        )
    if incompatible_missing:
        raise MigrationSafetyError(
            "源数据库缺少不可空列，不能无损映射到目标 schema: "
            + ", ".join(incompatible_missing)
        )
    return tuple(nullable_fills)


async def analyze_source_string_lengths(
    connection: AsyncConnection,
    metadata: MetaData,
    *,
    nullable_columns_filled_with_null: tuple[str, ...] = (),
) -> tuple[StringLengthObservation, ...]:
    """一次扫描每张源表，得到所有有界字符串列的真实最大字符数。"""
    null_fills = set(nullable_columns_filled_with_null)
    observations: list[StringLengthObservation] = []
    for table in metadata.sorted_tables:
        bounded_columns = [
            column
            for column in table.columns
            if isinstance(column.type, String)
            and column.type.length is not None
            and f"{table.name}.{column.name}" not in null_fills
        ]
        if not bounded_columns:
            continue
        statement = select(
            *[
                func.max(func.length(column)).label(column.name)
                for column in bounded_columns
            ]
        ).select_from(table)
        row = (await connection.execute(statement)).mappings().one()
        observations.extend(
            StringLengthObservation(
                name=f"{table.name}.{column.name}",
                declared_length=int(column.type.length),
                actual_max_length=int(row[column.name] or 0),
            )
            for column in bounded_columns
        )
    return tuple(observations)


def assert_source_string_lengths_fit(
    observations: tuple[StringLengthObservation, ...],
) -> None:
    """在接触目标库前一次性报告所有可能被 MySQL 截断的列。"""
    overflows = [
        item
        for item in observations
        if item.actual_max_length > item.declared_length
    ]
    if not overflows:
        return
    details = ", ".join(
        f"{item.name}={item.actual_max_length}>{item.declared_length}"
        for item in overflows
    )
    raise MigrationSafetyError(
        f"源数据超过 MySQL schema 字符长度上限；目标库尚未写入: {details}"
    )


async def database_digest(
    connection: AsyncConnection,
    metadata: MetaData,
    *,
    nullable_columns_filled_with_null: tuple[str, ...] = (),
) -> DatabaseDigest:
    """对 metadata 中所有表按主键顺序计算稳定内容摘要。"""
    existing = await _table_names(connection)
    expected = {table.name for table in metadata.sorted_tables}
    missing = sorted(expected - existing)
    if missing:
        raise MigrationSafetyError(f"数据库缺少核心表: {', '.join(missing)}")

    table_digests: list[TableDigest] = []
    for table in metadata.sorted_tables:
        digest = hashlib.sha256()
        row_count = 0
        order_columns = list(table.primary_key.columns) or list(table.columns)
        null_fills = set(nullable_columns_filled_with_null)
        selected_columns = [
            literal(None).label(column.name)
            if f"{table.name}.{column.name}" in null_fills
            else column
            for column in table.columns
        ]
        statement = (
            select(*selected_columns)
            .select_from(table)
            .order_by(*order_columns)
        )
        result = await connection.stream(statement)
        async for row in result.mappings():
            canonical = [
                _canonical_value(row[column.name], column) for column in table.columns
            ]
            payload = json.dumps(
                canonical,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            digest.update(len(payload).to_bytes(8, "big"))
            digest.update(payload)
            row_count += 1
        table_digests.append(
            TableDigest(name=table.name, row_count=row_count, sha256=digest.hexdigest())
        )

    aggregate_payload = json.dumps(
        [asdict(table) for table in table_digests],
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return DatabaseDigest(
        sha256=hashlib.sha256(aggregate_payload).hexdigest(),
        tables=tuple(table_digests),
    )


async def snapshot_sqlite_database(
    source: Path,
    destination: Path,
    metadata: MetaData,
) -> SnapshotResult:
    """使用 SQLite 在线备份 API 创建不可覆盖的一致性快照。"""
    source = source.resolve()
    destination = destination.resolve()
    manifest_path = destination.with_suffix(destination.suffix + ".manifest.json")
    if not source.is_file():
        raise MigrationSafetyError(f"SQLite 源文件不存在: {source}")
    if destination.exists() or manifest_path.exists():
        raise MigrationSafetyError("快照或 manifest 已存在；为避免覆盖，请使用新路径")
    destination.parent.mkdir(parents=True, exist_ok=True)

    source_connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    destination_connection = sqlite3.connect(destination)
    try:
        quick_check = source_connection.execute("PRAGMA quick_check").fetchall()
        if quick_check != [("ok",)]:
            raise MigrationSafetyError(f"SQLite quick_check 失败: {quick_check[:5]}")
        source_connection.backup(destination_connection)
    finally:
        destination_connection.close()
        source_connection.close()

    verification_connection = sqlite3.connect(
        f"file:{destination}?mode=ro", uri=True
    )
    try:
        integrity = verification_connection.execute("PRAGMA integrity_check").fetchall()
        if integrity != [("ok",)]:
            raise MigrationSafetyError(f"快照 integrity_check 失败: {integrity[:5]}")
    finally:
        verification_connection.close()

    engine = create_sqlite_readonly_engine(destination)
    try:
        async with engine.connect() as connection:
            nullable_fills = await analyze_source_schema(connection, metadata)
            string_lengths = await analyze_source_string_lengths(
                connection,
                metadata,
                nullable_columns_filled_with_null=nullable_fills,
            )
            data = await database_digest(
                connection,
                metadata,
                nullable_columns_filled_with_null=nullable_fills,
            )
    finally:
        await engine.dispose()

    result = SnapshotResult(
        source=str(source),
        snapshot=str(destination),
        file_sha256=file_sha256(destination),
        data=data,
        nullable_columns_filled_with_null=nullable_fills,
        string_lengths=string_lengths,
    )
    manifest_path.write_text(
        json.dumps(result.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return result


async def _validate_mysql_target(connection: AsyncConnection) -> None:
    result = await connection.execute(
        text(
            "SELECT @@character_set_database AS charset, "
            "@@collation_database AS collation, "
            "@@transaction_isolation AS isolation_level"
        )
    )
    row = result.mappings().one()
    if str(row["charset"]).lower() != "utf8mb4":
        raise MigrationSafetyError(
            f"目标数据库字符集为 {row['charset']}，必须先调整为 utf8mb4"
        )


async def _assert_target_scope_is_safe(
    connection: AsyncConnection,
    metadata: MetaData,
) -> None:
    existing = await _table_names(connection)
    allowed = {table.name for table in metadata.sorted_tables} | {
        MIGRATION_TABLE_NAME
    }
    unexpected = sorted(existing - allowed)
    if unexpected:
        raise MigrationSafetyError(
            "目标数据库包含不属于核心迁移的表，拒绝写入: " + ", ".join(unexpected)
        )


async def _nonempty_core_tables(
    connection: AsyncConnection,
    metadata: MetaData,
) -> dict[str, int]:
    existing = await _table_names(connection)
    nonempty: dict[str, int] = {}
    for table in metadata.sorted_tables:
        if table.name not in existing:
            continue
        count = int(
            (await connection.execute(select(func.count()).select_from(table))).scalar_one()
        )
        if count:
            nonempty[table.name] = count
    return nonempty


async def _validate_innodb_tables(
    connection: AsyncConnection,
    metadata: MetaData,
) -> None:
    table_names = [table.name for table in metadata.sorted_tables]
    result = await connection.execute(
        text(
            "SELECT TABLE_NAME AS name, ENGINE AS storage_engine "
            "FROM information_schema.tables "
            "WHERE table_schema = DATABASE()"
        )
    )
    engines = {
        str(row["name"]): str(row["storage_engine"]).upper()
        for row in result.mappings()
        if str(row["name"]) in table_names
    }
    invalid = sorted(name for name in table_names if engines.get(name) != "INNODB")
    if invalid:
        raise MigrationSafetyError(
            "以下目标表不是 InnoDB，无法保证原子回滚: " + ", ".join(invalid)
        )


async def _copy_table(
    source: AsyncConnection,
    target: AsyncConnection,
    table: Table,
    *,
    batch_size: int,
    nullable_columns_filled_with_null: tuple[str, ...] = (),
) -> int:
    order_columns = list(table.primary_key.columns) or list(table.columns)
    null_fills = set(nullable_columns_filled_with_null)
    selected_columns = [
        literal(None).label(column.name)
        if f"{table.name}.{column.name}" in null_fills
        else column
        for column in table.columns
    ]
    result = await source.stream(
        select(*selected_columns).select_from(table).order_by(*order_columns)
    )
    batch: list[dict[str, Any]] = []
    copied = 0
    async for row in result.mappings():
        converted = {
            column.name: _coerce_target_value(row[column.name], column)
            for column in table.columns
        }
        batch.append(converted)
        if len(batch) >= batch_size:
            await target.execute(table.insert(), batch)
            copied += len(batch)
            batch.clear()
    if batch:
        await target.execute(table.insert(), batch)
        copied += len(batch)
    return copied


def _digests_match(left: DatabaseDigest, right: DatabaseDigest) -> bool:
    return left.sha256 == right.sha256 and left.tables == right.tables


def _digest_difference(left: DatabaseDigest, right: DatabaseDigest) -> str:
    """生成不包含业务内容、但足以定位差异表的诊断摘要。"""
    left_tables = {table.name: table for table in left.tables}
    right_tables = {table.name: table for table in right.tables}
    differences: list[str] = []
    for name in sorted(set(left_tables) | set(right_tables)):
        source = left_tables.get(name)
        target = right_tables.get(name)
        if source == target:
            continue
        if source is None or target is None:
            differences.append(f"{name}: missing")
            continue
        differences.append(
            f"{name}: rows {source.row_count}/{target.row_count}, "
            f"sha {source.sha256[:12]}/{target.sha256[:12]}"
        )
    return "; ".join(differences) or "aggregate digest only"


class SqliteToMySQLMigrator:
    """将一个只读 SQLite 快照原子复制到一个空 MySQL 数据库。"""

    def __init__(
        self,
        source: Path,
        target_url: str,
        metadata: MetaData,
        *,
        batch_size: int = 1000,
    ) -> None:
        if batch_size <= 0:
            raise ValueError("batch_size 必须大于 0")
        self.source = source.resolve()
        self.target_url = target_url
        self.metadata = metadata
        self.batch_size = batch_size

    async def migrate(self) -> MigrationResult:
        """执行迁移；目标已有数据或任何内容不一致都会显式失败。"""
        source_engine = create_sqlite_readonly_engine(self.source)
        target_engine = create_mysql_migration_engine(self.target_url)
        run_id = str(uuid.uuid4())
        source_file_digest = file_sha256(self.source)
        try:
            async with source_engine.connect() as source_connection:
                await assert_sqlite_integrity(source_connection)
                nullable_fills = await analyze_source_schema(
                    source_connection, self.metadata
                )
                source_data = await database_digest(
                    source_connection,
                    self.metadata,
                    nullable_columns_filled_with_null=nullable_fills,
                )
                string_lengths = await analyze_source_string_lengths(
                    source_connection,
                    self.metadata,
                    nullable_columns_filled_with_null=nullable_fills,
                )
                assert_source_string_lengths_fit(string_lengths)

                async with target_engine.begin() as target_connection:
                    await _validate_mysql_target(target_connection)
                    await _assert_target_scope_is_safe(target_connection, self.metadata)
                    await target_connection.run_sync(
                        lambda sync_connection: _migration_metadata.create_all(
                            sync_connection
                        )
                    )

                previous = await self._find_completed_run(
                    target_engine, source_file_digest, source_data.sha256
                )
                if previous is not None:
                    async with target_engine.connect() as target_connection:
                        target_data = await database_digest(
                            target_connection, self.metadata
                        )
                    if not _digests_match(source_data, target_data):
                        raise MigrationSafetyError(
                            "目标声称已迁移，但当前内容指纹不同；拒绝继续"
                        )
                    return MigrationResult(
                        run_id=previous,
                        source_file_sha256=source_file_digest,
                        source_data=source_data,
                        target_data=target_data,
                        nullable_columns_filled_with_null=nullable_fills,
                        string_lengths=string_lengths,
                        already_applied=True,
                    )

                async with target_engine.begin() as target_connection:
                    nonempty = await _nonempty_core_tables(
                        target_connection, self.metadata
                    )
                    if nonempty:
                        details = ", ".join(
                            f"{name}={count}" for name, count in sorted(nonempty.items())
                        )
                        raise MigrationSafetyError(
                            f"目标核心表非空且无匹配迁移记录，拒绝覆盖: {details}"
                        )
                    await target_connection.run_sync(
                        lambda sync_connection: self.metadata.create_all(sync_connection)
                    )
                    await _validate_innodb_tables(target_connection, self.metadata)
                    await target_connection.execute(
                        _migration_runs.insert().values(
                            run_id=run_id,
                            source_file_sha256=source_file_digest,
                            source_data_sha256=source_data.sha256,
                            status="started",
                            started_at=_utc_now_naive(),
                        )
                    )

                try:
                    async with target_engine.begin() as target_connection:
                        for table in self.metadata.sorted_tables:
                            await _copy_table(
                                source_connection,
                                target_connection,
                                table,
                                batch_size=self.batch_size,
                                nullable_columns_filled_with_null=nullable_fills,
                            )
                        target_data = await database_digest(
                            target_connection, self.metadata
                        )
                        if not _digests_match(source_data, target_data):
                            raise MigrationSafetyError(
                                "迁移后逐表内容指纹不一致，已回滚全部业务数据: "
                                + _digest_difference(source_data, target_data)
                            )
                        manifest = MigrationResult(
                            run_id=run_id,
                            source_file_sha256=source_file_digest,
                            source_data=source_data,
                            target_data=target_data,
                            nullable_columns_filled_with_null=nullable_fills,
                            string_lengths=string_lengths,
                        )
                        await target_connection.execute(
                            update(_migration_runs)
                            .where(_migration_runs.c.run_id == run_id)
                            .values(
                                status="completed",
                                completed_at=_utc_now_naive(),
                                manifest_json=json.dumps(
                                    manifest.to_dict(),
                                    ensure_ascii=False,
                                    separators=(",", ":"),
                                ),
                                error_summary=None,
                            )
                        )
                    return manifest
                except Exception as error:
                    await self._mark_failed(target_engine, run_id, error)
                    raise
        finally:
            await target_engine.dispose()
            await source_engine.dispose()

    async def verify(self) -> MigrationResult:
        """只读比较源快照与目标库，不创建或修改任何表。"""
        source_engine = create_sqlite_readonly_engine(self.source)
        target_engine = create_mysql_migration_engine(self.target_url)
        try:
            async with source_engine.connect() as source_connection:
                await assert_sqlite_integrity(source_connection)
                nullable_fills = await analyze_source_schema(
                    source_connection, self.metadata
                )
                source_data = await database_digest(
                    source_connection,
                    self.metadata,
                    nullable_columns_filled_with_null=nullable_fills,
                )
                string_lengths = await analyze_source_string_lengths(
                    source_connection,
                    self.metadata,
                    nullable_columns_filled_with_null=nullable_fills,
                )
                assert_source_string_lengths_fit(string_lengths)
            async with target_engine.connect() as target_connection:
                await _validate_mysql_target(target_connection)
                target_data = await database_digest(target_connection, self.metadata)
            if not _digests_match(source_data, target_data):
                raise MigrationSafetyError("源与目标逐表内容指纹不一致")
            run_id = await self._find_completed_run(
                target_engine, file_sha256(self.source), source_data.sha256
            )
            if run_id is None:
                raise MigrationSafetyError("内容一致，但缺少已完成的迁移审计记录")
            return MigrationResult(
                run_id=run_id,
                source_file_sha256=file_sha256(self.source),
                source_data=source_data,
                target_data=target_data,
                nullable_columns_filled_with_null=nullable_fills,
                string_lengths=string_lengths,
                already_applied=True,
            )
        finally:
            await target_engine.dispose()
            await source_engine.dispose()

    @staticmethod
    async def _find_completed_run(
        engine: AsyncEngine,
        source_file_digest: str,
        source_data_digest: str,
    ) -> str | None:
        async with engine.connect() as connection:
            if MIGRATION_TABLE_NAME not in await _table_names(connection):
                return None
            result = await connection.execute(
                select(_migration_runs.c.run_id)
                .where(
                    _migration_runs.c.source_data_sha256 == source_data_digest,
                    _migration_runs.c.status == "completed",
                )
                .order_by(
                    (
                        _migration_runs.c.source_file_sha256
                        == source_file_digest
                    ).desc(),
                    _migration_runs.c.completed_at.desc(),
                )
                .limit(1)
            )
            value = result.scalar_one_or_none()
            return str(value) if value is not None else None

    @staticmethod
    async def _mark_failed(
        engine: AsyncEngine,
        run_id: str,
        error: Exception,
    ) -> None:
        summary = f"{type(error).__name__}: {error}"[:1000]
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    update(_migration_runs)
                    .where(_migration_runs.c.run_id == run_id)
                    .values(
                        status="failed",
                        completed_at=_utc_now_naive(),
                        error_summary=summary,
                    )
                )
        except Exception:  # noqa: BLE001 - 不允许审计失败覆盖原始迁移异常
            # 原始异常更重要；审计写入失败不能伪装迁移成功，也不应覆盖根因。
            return


__all__ = [
    "DatabaseDigest",
    "MigrationResult",
    "MigrationSafetyError",
    "SnapshotResult",
    "SqliteToMySQLMigrator",
    "StringLengthObservation",
    "TableDigest",
    "analyze_source_schema",
    "analyze_source_string_lengths",
    "assert_source_string_lengths_fit",
    "database_digest",
    "file_sha256",
    "snapshot_sqlite_database",
]
