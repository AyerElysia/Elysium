"""核心 ORM schema 的 MySQL 方言兼容测试。"""

from sqlalchemy import Double, Float, Text, UniqueConstraint
from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateIndex, CreateTable

from src.core.models.sql_alchemy import Base
from src.core.utils.schema_sync import _normalize_type


def test_mysql_ddl_compiles_for_every_core_table_and_index() -> None:
    dialect = mysql.dialect()
    for table in Base.metadata.sorted_tables:
        assert str(CreateTable(table).compile(dialect=dialect))
        for index in table.indexes:
            assert str(CreateIndex(index).compile(dialect=dialect))


def test_mysql_keys_never_index_unbounded_text() -> None:
    """MySQL 不允许无前缀索引 TEXT；所有键字段必须有长度。"""
    for table in Base.metadata.sorted_tables:
        key_columns = set()
        key_columns.update(column for index in table.indexes for column in index.columns)
        for constraint in table.constraints:
            if isinstance(constraint, UniqueConstraint):
                key_columns.update(constraint.columns)
        for column in table.columns:
            if column.unique or column.index:
                key_columns.add(column)

        invalid = [column.name for column in key_columns if isinstance(column.type, Text)]
        assert not invalid, f"{table.name} 的键字段仍为 TEXT: {invalid}"


def test_protocol_identifiers_keep_declared_lengths() -> None:
    messages = Base.metadata.tables["messages"]
    chat_streams = Base.metadata.tables["chat_streams"]
    images = Base.metadata.tables["images"]
    assert chat_streams.c.stream_id.type.length == 128
    assert messages.c.stream_id.type.length == 128
    assert messages.c.message_id.type.length == 100
    assert messages.c.message_type.type.length == 20
    assert images.c.path.type.length == 500
    assert images.c.type.type.length == 50


def test_mysql_persisted_floats_use_double_precision() -> None:
    for table in Base.metadata.sorted_tables:
        for column in table.columns:
            if isinstance(column.type, Float):
                assert isinstance(column.type, Double)
                assert column.type.compile(dialect=mysql.dialect()) == "DOUBLE"


def test_schema_sync_preserves_mysql_float_precision_boundary() -> None:
    assert _normalize_type("DOUBLE", db_type="sqlite") == "float"
    assert _normalize_type("FLOAT", db_type="sqlite") == "float"
    assert _normalize_type("DOUBLE", db_type="mysql") == "double"
    assert _normalize_type("FLOAT", db_type="mysql") == "float"
