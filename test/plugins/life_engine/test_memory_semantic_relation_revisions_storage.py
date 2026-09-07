"""Isolated relation revision, migration, replay and bounded-page contracts.

The MySQL SQL harness exercises the real adapter queries against synthetic
SQLite rows; it does not claim to replace opt-in real MySQL concurrency tests.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import multiprocessing
import sqlite3
from contextlib import asynccontextmanager
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest

from plugins.life_engine.memory.indexing import transaction
from plugins.life_engine.memory.living import (
    SemanticRelation,
    append_semantic_relation,
    create_living_memory_schema,
    get_semantic_relation,
    list_semantic_relations,
    migrate_semantic_relation_revision_schema,
    semantic_relation_payload,
    validate_semantic_relation_revision_schema,
)
from plugins.life_engine.storage.memory.local import LocalLivingMemoryStore
from plugins.life_engine.storage.memory.mysql import MySQLLivingMemoryStore, _payload
from plugins.life_engine.storage.memory.schema import (
    MEMORY_IMMUTABILITY_MIGRATIONS,
    MEMORY_MIGRATIONS,
    MEMORY_SCHEMA_VERSION,
    _semantic_relation_revision_completion_conditions,
)
from plugins.life_engine.storage.migration.memory_copy import (
    TABLE_SPECS,
    _payload_hash,
    _transform_source_row,
    iter_transformed_source_rows,
    normalize_target_row,
)
from src.kernel.storage import canonical_json

_OLD_COLUMNS = (
    "relation_id",
    "source_ref",
    "target_ref",
    "predicate",
    "reason",
    "actor",
    "recorded_at",
    "consciousness_instance_id",
    "stream_scope",
    "metadata_json",
)


def _root(identity: str = "relation-root", **changes) -> SemanticRelation:
    return replace(
        SemanticRelation(
            relation_id=identity,
            source_ref="subject-file:synthetic-source",
            target_ref="subject-file:synthetic-target",
            predicate="synthetic open predicate",
            reason="synthetic subject-authored reason",
            actor="window-one",
            recorded_at="2026-09-08T01:00:00+00:00",
            consciousness_instance_id="window-one",
            stream_scope="synthetic-stream",
            metadata={
                "source_occurrence_id": identity,
                "subject_strength": "tentative",
            },
            owner_subject_id="elysia",
            root_relation_id=identity,
        ),
        **changes,
    )


def _next(parent: SemanticRelation, identity: str, **changes) -> SemanticRelation:
    values = {
        "relation_id": identity,
        "parent_relation_id": parent.relation_id,
        "revision": parent.revision + 1,
        "operation": "revise",
        "actor": "window-two",
        "consciousness_instance_id": "window-two",
        "recorded_at": "2026-09-08T02:00:00+00:00",
        "reason": "synthetic revised reason",
        "metadata": {"source_occurrence_id": identity},
        **changes,
    }
    return replace(parent, **values)


def _db(path: str = ":memory:") -> sqlite3.Connection:
    database = sqlite3.connect(path, check_same_thread=False, timeout=10)
    database.row_factory = sqlite3.Row
    database.execute("PRAGMA foreign_keys = ON")
    create_living_memory_schema(database)
    return database


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self

    def one_or_none(self):
        assert len(self.rows) <= 1
        return self.rows[0] if self.rows else None

    def all(self):
        return list(self.rows)


class _SQLSession:
    def __init__(self, database):
        self.database = database
        self.statements = []
        self.isolation_levels = []

    async def execute(self, statement, parameters=None):
        sql = str(statement)
        self.statements.append(sql)
        # SQLite already compares these TEXT fields byte-for-byte. Row locks
        # are checked as emitted SQL here and tested with a real DB separately.
        translated = sql.replace(" FOR UPDATE", "").replace("BINARY ", "")
        cursor = self.database.execute(translated, parameters or {})
        return _Rows(
            [dict(row) for row in cursor.fetchall()] if cursor.description else []
        )

    async def scalar(self, statement, parameters=None):
        result = await self.execute(statement, parameters)
        return next(iter(result.rows[0].values())) if result.rows else None

    async def execution_options(self, **options):
        self.isolation_levels.append(options["isolation_level"])
        return self

    @asynccontextmanager
    async def begin(self):
        with transaction(self.database):
            yield self


class _SQLEngine:
    def __init__(self, session):
        self.session = session

    @asynccontextmanager
    async def connect(self):
        yield self.session


class _MySQLSQLHarness(MySQLLivingMemoryStore):
    def __init__(self, database):
        self.database = database
        for name in ("source_ref_sha256", "target_ref_sha256", "payload_sha256"):
            database.execute(
                f"ALTER TABLE memory_semantic_relations ADD COLUMN {name} TEXT"
            )
        self.session = _SQLSession(database)
        self.runtime = SimpleNamespace(engine=_SQLEngine(self.session))

    async def _write(self, operation):
        with transaction(self.database, immediate=True):
            return await operation(self.session)


@pytest.fixture(params=["sqlite", "mysql-sql-harness"])
def store_case(request):
    database = _db()
    store = (
        LocalLivingMemoryStore(lambda: database, asyncio.Lock())
        if request.param == "sqlite"
        else _MySQLSQLHarness(database)
    )
    try:
        yield store, database, request.param
    finally:
        database.close()


async def test_revision_is_same_subject_across_instances_and_append_only(store_case):
    store, database, _kind = store_case
    first = await store.append_relation(_root())
    revised = await store.append_relation(
        _next(
            first,
            "relation-revised",
            predicate="synthetic reconsidered predicate",
        )
    )
    assert first.actor != revised.actor
    assert first.owner_subject_id == revised.owner_subject_id == "elysia"
    assert await store.get_relation(first.relation_id) == first
    assert await store.get_relation(revised.relation_id) == revised
    assert await store.list_relations(first.source_ref) == [first, revised]
    assert await store.list_relations(first.target_ref, current_only=True) == [revised]
    for statement in (
        "UPDATE memory_semantic_relations SET owner_subject_id = 'other'",
        "UPDATE memory_semantic_relations SET revision = 99",
        "DELETE FROM memory_semantic_relations",
    ):
        with pytest.raises(sqlite3.IntegrityError, match="LivingMemoryRecordImmutable"):
            database.execute(statement)


async def test_withdraw_keeps_history_and_removes_only_that_current_lineage(store_case):
    store, _database, _kind = store_case
    first = await store.append_relation(_root())
    independent = await store.append_relation(_root("independent-relation"))
    withdrawn = await store.append_relation(
        _next(
            first,
            "relation-withdrawn",
            operation="withdraw",
            reason="synthetic explicit withdrawal",
        )
    )
    assert await store.get_relation(withdrawn.relation_id) == withdrawn
    assert await store.list_relations(first.source_ref, current_only=True) == [
        independent
    ]
    assert len(await store.list_relations(first.source_ref)) == 3
    with pytest.raises(RuntimeError, match="SemanticRelationAlreadyWithdrawn"):
        await store.append_relation(_next(withdrawn, "implicit-revival"))


async def test_replay_precedes_stale_parent_check_and_conflicting_replay_fails(
    store_case,
):
    store, _database, _kind = store_case
    first = await store.append_relation(_root())
    second = await store.append_relation(_next(first, "second"))
    third = await store.append_relation(_next(second, "third"))
    assert await store.append_relation(second) == second
    assert await store.list_relations(first.source_ref, current_only=True) == [third]
    with pytest.raises(RuntimeError, match="SemanticRelationOccurrenceConflict"):
        await store.append_relation(
            replace(second, reason="different immutable payload")
        )
    with pytest.raises(RuntimeError, match="SemanticRelationStaleParent"):
        await store.append_relation(_next(first, "competing-second"))
    assert await store.get_relation("competing-second") is None


async def test_empty_recorded_time_replay_reuses_persisted_occurrence_time(store_case):
    store, _database, _kind = store_case
    proposed = _root(recorded_at="")
    stored = await store.append_relation(proposed)
    assert stored.recorded_at
    assert await store.append_relation(proposed) == stored


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        ({"owner_subject_id": "independent-subject"}, "SemanticRelationOwnerMismatch"),
        ({"target_ref": "subject-file:other"}, "SemanticRelationEndpointsImmutable"),
        ({"root_relation_id": "wrong-root"}, "SemanticRelationLineageMismatch"),
        ({"revision": 8}, "SemanticRelationLineageMismatch"),
        (
            {"operation": "withdraw", "predicate": "new meaning"},
            "WithdrawalPredicateMismatch",
        ),
    ],
)
async def test_revision_rejects_identity_or_endpoint_changes(
    store_case, changes, error
):
    store, _database, _kind = store_case
    first = await store.append_relation(_root())
    with pytest.raises((ValueError, RuntimeError, PermissionError), match=error):
        await store.append_relation(_next(first, "invalid-next", **changes))
    assert await store.list_relations(first.source_ref) == [first]


async def test_legacy_actor_or_metadata_does_not_grant_subject_ownership(store_case):
    store, _database, _kind = store_case
    legacy = await store.append_relation(
        _root(
            owner_subject_id=None,
            root_relation_id="",
            actor="elysia",
            metadata={"owner_subject_id": "elysia"},
        )
    )
    assert await store.list_relations(legacy.source_ref, current_only=True) == [legacy]
    with pytest.raises(PermissionError, match="SemanticRelationLegacyOwnerUnbound"):
        await store.append_relation(
            _next(
                legacy,
                "attempted-adoption",
                owner_subject_id="elysia",
                root_relation_id=legacy.relation_id,
            )
        )


async def test_pages_bound_rows_and_mark_real_heads_outside_the_current_page(
    store_case,
):
    store, _database, kind = store_case
    first = await store.append_relation(_root())
    second = await store.append_relation(_next(first, "second"))
    third = await store.append_relation(
        _next(
            second,
            "third",
            recorded_at="2026-09-07T23:00:00+00:00",
        )
    )
    await store.append_relation(
        _root(
            "unrelated",
            source_ref="subject-file:unrelated",
            target_ref="subject-file:another",
        )
    )
    page = await store.page_relations(first.source_ref, limit=1)
    assert page.frontier_count == 4
    assert page.matching_count == 3
    assert page.relations == (third,)
    assert page.current_relation_ids == (third.relation_id,)
    assert page.has_more is True and page.next_offset == 1
    page_two = await store.page_relations(
        first.source_ref,
        limit=1,
        offset=page.next_offset,
        expected_frontier_count=page.frontier_count,
    )
    assert page_two.relations == (first,)
    assert page_two.current_relation_ids == ()
    last = await store.page_relations(
        first.source_ref,
        limit=1,
        offset=page_two.next_offset,
        expected_frontier_count=page.frontier_count,
    )
    assert last.relations == (second,)
    assert last.current_relation_ids == ()
    assert last.has_more is False and last.next_offset is None
    current = await store.page_relations(first.source_ref, current_only=True)
    assert current.relations == (third,) and current.matching_count == 1
    if kind == "mysql-sql-harness":
        assert store.session.isolation_levels == ["REPEATABLE READ"] * 4
        assert any(
            "LIMIT :row_limit OFFSET :row_offset" in sql
            for sql in store.session.statements
        )
        assert any("FOR UPDATE" in sql for sql in store.session.statements)


async def test_any_append_invalidates_page_frontier_without_automatic_restart(
    store_case,
):
    store, _database, _kind = store_case
    root = await store.append_relation(_root())
    first = await store.page_relations(root.source_ref, limit=1)
    await store.append_relation(
        _root(
            "unrelated-later",
            source_ref="subject-file:unrelated",
            target_ref="subject-file:elsewhere",
        )
    )
    with pytest.raises(RuntimeError, match="SemanticRelationPageFrontierConflict"):
        await store.page_relations(
            root.source_ref,
            offset=1,
            expected_frontier_count=first.frontier_count,
        )


@pytest.mark.parametrize(
    "options",
    [
        {"limit": 0},
        {"limit": 101},
        {"limit": True},
        {"offset": -1},
        {"offset": 1},
        {"expected_frontier_count": -1},
        {"expected_frontier_count": True},
    ],
)
async def test_page_rejects_invalid_bounds_before_reading(store_case, options):
    store, _database, _kind = store_case
    with pytest.raises(ValueError, match="SemanticRelationPage"):
        await store.page_relations("subject-file:synthetic-source", **options)


async def test_mysql_page_hash_is_only_an_index_hint_before_count_and_limit():
    database = _db()
    store = _MySQLSQLHarness(database)
    expected = _root("z-exact")
    hash_hint = hashlib.sha256(expected.source_ref.encode()).hexdigest()
    try:
        # Deliberately false hash hints model drift/collision. Only exact byte
        # endpoints may consume the page budget or affect matching_count.
        for index, wrong_ref in enumerate(
            (
                expected.source_ref.upper(),
                expected.source_ref + " ",
                "subject-file:entirely-different",
            )
        ):
            false_row = _root(
                f"a-false-{index}",
                source_ref=wrong_ref,
                target_ref=f"subject-file:false-target-{index}",
            )
            values = {
                **asdict(false_row),
                "metadata_json": canonical_json(false_row.metadata),
                "source_ref_sha256": hash_hint,
                "target_ref_sha256": hashlib.sha256(
                    false_row.target_ref.encode()
                ).hexdigest(),
                "payload_sha256": _payload(false_row)[1],
            }
            del values["metadata"]
            columns = ", ".join(values)
            placeholders = ", ".join(":" + name for name in values)
            database.execute(
                f"INSERT INTO memory_semantic_relations ({columns}) VALUES ({placeholders})",
                values,
            )
        database.commit()
        await store.append_relation(expected)
        page = await store.page_relations(expected.source_ref, limit=1)
        assert page.frontier_count == 4
        assert page.matching_count == 1
        assert page.relations == (expected,)
        assert page.next_offset is None
        count_sql = [
            sql
            for sql in store.session.statements
            if "COUNT(*)" in sql and " r " in sql
        ]
        page_sql = [
            sql for sql in store.session.statements if "LIMIT :row_limit" in sql
        ]
        assert all(
            "BINARY r.source_ref = BINARY :entity_ref" in sql
            for sql in count_sql + page_sql
        )
    finally:
        database.close()


def _old_db() -> sqlite3.Connection:
    database = sqlite3.connect(":memory:")
    database.row_factory = sqlite3.Row
    database.execute("PRAGMA foreign_keys = ON")
    database.execute(
        """CREATE TABLE memory_semantic_relations (
            relation_id TEXT PRIMARY KEY, source_ref TEXT NOT NULL,
            target_ref TEXT NOT NULL, predicate TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT '', actor TEXT NOT NULL DEFAULT '',
            recorded_at TEXT NOT NULL, consciousness_instance_id TEXT NOT NULL DEFAULT '',
            stream_scope TEXT NOT NULL DEFAULT '', metadata_json TEXT NOT NULL DEFAULT '{}',
            CHECK (source_ref <> target_ref)
        )"""
    )
    # These are the pre-existing v1 history guards, not migration side effects.
    for event in ("update", "delete"):
        database.execute(
            f"CREATE TRIGGER memory_semantic_relations_immutable_{event} "
            f"BEFORE {event.upper()} ON memory_semantic_relations BEGIN "
            "SELECT RAISE(ABORT, 'LivingMemoryRecordImmutable'); END"
        )
    return database


def test_explicit_sqlite_migration_is_idempotent_and_never_adopts_or_rewrites_legacy():
    database = _old_db()
    try:
        legacy_values = (
            "legacy",
            "document:notes/a.md",
            "document:notes/b.md",
            "  exact open predicate  ",
            "  exact original reason  ",
            "old-window",
            "2026-09-01T01:00:00+00:00",
            "old-window",
            "old-stream",
            '{"subject_strength":"unspecified","owner_subject_id":"elysia"}',
        )
        database.execute(
            "INSERT INTO memory_semantic_relations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            legacy_values,
        )
        database.commit()
        statements = []
        database.set_trace_callback(statements.append)
        added = migrate_semantic_relation_revision_schema(database)
        assert set(added) == {
            "owner_subject_id",
            "root_relation_id",
            "parent_relation_id",
            "revision",
            "operation",
        }
        assert migrate_semantic_relation_revision_schema(database) == ()
        report = validate_semantic_relation_revision_schema(database)
        assert report["protocol_version"] == "semantic-relation-revision-v1"
        after = database.execute(
            "SELECT " + ", ".join(_OLD_COLUMNS) + " FROM memory_semantic_relations"
        ).fetchone()
        assert tuple(after) == legacy_values
        relation = get_semantic_relation(database, "legacy")
        assert relation.owner_subject_id is None and relation.root_relation_id == ""
        assert not any(
            sql.lstrip().upper().startswith(("UPDATE ", "DELETE "))
            for sql in statements
        )
        assert not any("CREATE TRIGGER" in sql.upper() for sql in statements)
    finally:
        database.close()


def test_explicit_migration_refuses_missing_existing_immutability_without_installing_it():
    database = _old_db()
    try:
        database.execute("DROP TRIGGER memory_semantic_relations_immutable_update")
        with pytest.raises(
            RuntimeError, match="SemanticRelationImmutabilityTriggerMismatch"
        ):
            migrate_semantic_relation_revision_schema(database)
        columns = {
            str(row[1])
            for row in database.execute("PRAGMA table_info(memory_semantic_relations)")
        }
        assert columns == set(_OLD_COLUMNS), "failed DDL must roll back"
        assert (
            database.execute(
                "SELECT 1 FROM sqlite_master WHERE type='trigger' "
                "AND name='memory_semantic_relations_immutable_update'"
            ).fetchone()
            is None
        )
    finally:
        database.close()


def test_read_only_validation_rejects_nonunique_or_partial_revision_index():
    database = _db()
    try:
        database.execute("DROP INDEX uq_semantic_relation_parent")
        database.execute(
            "CREATE INDEX uq_semantic_relation_parent "
            "ON memory_semantic_relations(parent_relation_id)"
        )
        with pytest.raises(RuntimeError, match="SemanticRelationRevisionIndexMismatch"):
            validate_semantic_relation_revision_schema(database)
    finally:
        database.close()


def test_legacy_payload_digest_and_copy_shape_remain_v1_compatible():
    legacy = _root(owner_subject_id=None, root_relation_id="")
    original_shape = {
        name: value
        for name, value in asdict(legacy).items()
        if name
        not in {
            "owner_subject_id",
            "root_relation_id",
            "parent_relation_id",
            "revision",
            "operation",
        }
    }
    expected_hash = hashlib.sha256(canonical_json(original_shape).encode()).hexdigest()
    assert semantic_relation_payload(legacy) == original_shape
    assert _payload(legacy)[1] == _payload_hash(legacy) == expected_hash
    raw = {**original_shape, "metadata_json": json.dumps(legacy.metadata)}
    del raw["metadata"]
    copied = _transform_source_row(
        "memory_semantic_relations", raw, SimpleNamespace(), 1
    )
    assert copied["payload_sha256"] == expected_hash
    assert copied["owner_subject_id"] is None and copied["root_relation_id"] is None
    assert set(copied) == set(TABLE_SPECS["memory_semantic_relations"].columns)
    assert (
        normalize_target_row(TABLE_SPECS["memory_semantic_relations"], copied) == copied
    )


def test_revision_copy_orders_parents_first_and_preserves_new_payload_hashes():
    database = _db()
    try:
        root = append_semantic_relation(database, _root("z-root"))
        child = append_semantic_relation(database, _next(root, "a-child"))
        withdrawn = append_semantic_relation(
            database, _next(child, "0-withdraw", operation="withdraw")
        )
        batches = list(
            iter_transformed_source_rows(
                database,
                TABLE_SPECS["memory_semantic_relations"],
                SimpleNamespace(),
                batch_size=1,
            )
        )
        copied = [row for batch in batches for row in batch]
        assert [row["relation_id"] for row in copied] == [
            root.relation_id,
            child.relation_id,
            withdrawn.relation_id,
        ]
        for record, row in zip((root, child, withdrawn), copied, strict=True):
            assert row["payload_sha256"] == _payload(record)[1]
            assert row["owner_subject_id"] == "elysia"
            assert row["revision"] == record.revision
    finally:
        database.close()


def test_additive_mysql_schema_does_not_rewrite_frozen_v1_statements_or_hashes():
    assert [item.version for item in MEMORY_MIGRATIONS] == list(
        range(1, MEMORY_SCHEMA_VERSION + 1)
    )
    added = MEMORY_MIGRATIONS[-1]
    assert added.version == 16
    assert added.name == "life_memory_semantic_relation_revisions_v1"
    assert len(added.completion_checks) == 1
    complete = added.completion_checks[0]
    assert "KEY_COLUMN_USAGE" in complete and "CHECK_CONSTRAINTS" in complete
    assert "COLUMN_TYPE" in complete and "NON_UNIQUE" in complete
    assert not any(
        sql.lstrip().upper().startswith(("UPDATE ", "DELETE "))
        for sql in added.statements
    )
    old_living = next(item for item in MEMORY_MIGRATIONS if item.version == 4)
    assert "owner_subject_id" not in "\n".join(old_living.statements)
    old_guard = MEMORY_IMMUTABILITY_MIGRATIONS[0]
    assert "owner_subject_id" not in "\n".join(old_guard.statements)
    new_guard = MEMORY_IMMUTABILITY_MIGRATIONS[-1]
    assert new_guard.version == 4
    assert "owner_subject_id" in "\n".join(new_guard.statements)
    assert "memory_semantic_relation_revision_immutable_update" in "\n".join(
        new_guard.statements
    )


@pytest.mark.parametrize(
    ("delete_rule", "update_rule", "accepted"),
    [
        ("RESTRICT", "NO ACTION", True),
        ("RESTRICT", "RESTRICT", True),
        ("NO ACTION", "RESTRICT", True),
        ("NO ACTION", "NO ACTION", True),
        ("CASCADE", "NO ACTION", False),
        ("RESTRICT", "CASCADE", False),
        ("SET NULL", "RESTRICT", False),
        ("RESTRICT", "SET NULL", False),
        ("SET DEFAULT", "RESTRICT", False),
        ("RESTRICT", "SET DEFAULT", False),
    ],
)
def test_mysql_revision_fk_postcondition_accepts_only_immediate_rejection(
    delete_rule,
    update_rule,
    accepted,
):
    # Execute the production predicate against synthetic MySQL-shaped metadata.
    # This does not claim to run the MySQL server or prove its metadata spelling.
    database = sqlite3.connect(":memory:")
    database.create_function("DATABASE", 0, lambda: "synthetic_metadata")
    try:
        database.execute("ATTACH DATABASE ':memory:' AS information_schema")
        database.execute(
            "CREATE TABLE information_schema.KEY_COLUMN_USAGE ("
            "CONSTRAINT_SCHEMA TEXT, TABLE_NAME TEXT, CONSTRAINT_NAME TEXT, "
            "COLUMN_NAME TEXT, ORDINAL_POSITION INTEGER, REFERENCED_TABLE_SCHEMA TEXT, "
            "REFERENCED_TABLE_NAME TEXT, REFERENCED_COLUMN_NAME TEXT)"
        )
        database.execute(
            "CREATE TABLE information_schema.REFERENTIAL_CONSTRAINTS ("
            "CONSTRAINT_SCHEMA TEXT, CONSTRAINT_NAME TEXT, DELETE_RULE TEXT, UPDATE_RULE TEXT)"
        )
        database.execute(
            "INSERT INTO information_schema.KEY_COLUMN_USAGE VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "synthetic_metadata",
                "memory_semantic_relations",
                "fk_semantic_relation_parent",
                "parent_relation_id",
                1,
                "synthetic_metadata",
                "memory_semantic_relations",
                "relation_id",
            ),
        )
        database.execute(
            "INSERT INTO information_schema.REFERENTIAL_CONSTRAINTS VALUES (?, ?, ?, ?)",
            (
                "synthetic_metadata",
                "fk_semantic_relation_parent",
                delete_rule,
                update_rule,
            ),
        )
        condition = _semantic_relation_revision_completion_conditions()[
            "foreign_key:fk_semantic_relation_parent"
        ]
        result = database.execute(
            f"SELECT CASE WHEN {condition} THEN 1 ELSE 0 END"
        ).fetchone()[0]
        assert bool(result) is accepted
        database.execute(
            "UPDATE information_schema.KEY_COLUMN_USAGE SET REFERENCED_COLUMN_NAME = 'wrong'"
        )
        assert (
            database.execute(
                f"SELECT CASE WHEN {condition} THEN 1 ELSE 0 END"
            ).fetchone()[0]
            == 0
        )
    finally:
        database.close()


@pytest.mark.parametrize("engine", ["InnoDB", "NDB", "MyISAM"])
def test_mysql_revision_no_action_equivalence_requires_innodb(engine):
    database = sqlite3.connect(":memory:")
    database.create_function("DATABASE", 0, lambda: "synthetic_metadata")
    try:
        database.execute("ATTACH DATABASE ':memory:' AS information_schema")
        database.execute(
            "CREATE TABLE information_schema.TABLES (TABLE_SCHEMA TEXT, TABLE_NAME TEXT, ENGINE TEXT)"
        )
        database.execute(
            "INSERT INTO information_schema.TABLES VALUES (?, ?, ?)",
            ("synthetic_metadata", "memory_semantic_relations", engine),
        )
        condition = _semantic_relation_revision_completion_conditions()[
            "engine:memory_semantic_relations"
        ]
        result = database.execute(
            f"SELECT CASE WHEN {condition} THEN 1 ELSE 0 END"
        ).fetchone()[0]
        assert bool(result) is (engine == "InnoDB")
    finally:
        database.close()


_MYSQL_REPORTED_OPERATION_CHECK = (
    r"(`operation` in (_utf8mb4\'add\',_utf8mb4\'revise\',_utf8mb4\'withdraw\'))"
)


@pytest.mark.parametrize(
    ("kind", "clause", "enforced", "accepted"),
    [
        ("operation", _MYSQL_REPORTED_OPERATION_CHECK, "YES", True),
        (
            "operation",
            "(`operation` in (_utf8mb4'add',_utf8mb4'revise',_utf8mb4'withdraw'))",
            "YES",
            True,
        ),
        (
            "operation",
            r"(`operation` in (_ascii\'add\',_ascii\'revise\',_ascii\'withdraw\'))",
            "YES",
            True,
        ),
        (
            "operation",
            "(`operation` in (_ascii'add',_ascii'revise',_ascii'withdraw'))",
            "YES",
            True,
        ),
        (
            "operation",
            r"(`operation` in (\'add\',\'revise\',\'withdraw\'))",
            "YES",
            True,
        ),
        ("operation", "(`operation` in ('add','revise','withdraw'))", "YES", True),
        ("operation", _MYSQL_REPORTED_OPERATION_CHECK, "NO", False),
        (
            "operation",
            _MYSQL_REPORTED_OPERATION_CHECK.replace("withdraw", "remove"),
            "YES",
            False,
        ),
        (
            "operation",
            _MYSQL_REPORTED_OPERATION_CHECK.replace("add", "ADD"),
            "YES",
            False,
        ),
        (
            "operation",
            _MYSQL_REPORTED_OPERATION_CHECK.replace("add", "ad d"),
            "YES",
            False,
        ),
        (
            "operation",
            _MYSQL_REPORTED_OPERATION_CHECK.replace("operation", "other"),
            "YES",
            False,
        ),
        ("operation", _MYSQL_REPORTED_OPERATION_CHECK + " OR TRUE", "YES", False),
        (
            "operation",
            "(`operation` in ('add','revise','withdraw','anything'))",
            "YES",
            False,
        ),
        ("operation", "(`operation` in ('add','revise'))", "YES", False),
        ("operation", "(`operation` in ('add','revise','revise'))", "YES", False),
        (
            "operation",
            _MYSQL_REPORTED_OPERATION_CHECK.replace(chr(92), chr(92) * 2),
            "YES",
            False,
        ),
        ("operation", "1", "YES", False),
        ("revision", "(`revision` >= 1)", "YES", True),
        ("revision", "(`revision` >= 1)", "NO", False),
        ("revision", "(`revision` >= 0)", "YES", False),
        ("revision", "(`revision` >= 1) OR TRUE", "YES", False),
    ],
)
def test_mysql_revision_check_requires_exact_supported_rendering_and_enforcement(
    kind,
    clause,
    enforced,
    accepted,
):
    # MySQL 8.0.46's actual escaped CHECK_CLAUSE is frozen above. SQLite here
    # only evaluates our exact metadata predicate against synthetic rows.
    database = sqlite3.connect(":memory:")
    database.create_function("DATABASE", 0, lambda: "synthetic_metadata")
    name = "chk_semantic_relation_" + kind
    try:
        database.execute("ATTACH DATABASE ':memory:' AS information_schema")
        database.execute(
            "CREATE TABLE information_schema.CHECK_CONSTRAINTS ("
            "CONSTRAINT_SCHEMA TEXT, CONSTRAINT_NAME TEXT, CHECK_CLAUSE TEXT)"
        )
        database.execute(
            "CREATE TABLE information_schema.TABLE_CONSTRAINTS ("
            "CONSTRAINT_SCHEMA TEXT, CONSTRAINT_NAME TEXT, TABLE_NAME TEXT, ENFORCED TEXT)"
        )
        database.execute(
            "INSERT INTO information_schema.CHECK_CONSTRAINTS VALUES (?, ?, ?)",
            ("synthetic_metadata", name, clause),
        )
        database.execute(
            "INSERT INTO information_schema.TABLE_CONSTRAINTS VALUES (?, ?, ?, ?)",
            ("synthetic_metadata", name, "memory_semantic_relations", enforced),
        )
        condition = _semantic_relation_revision_completion_conditions()["check:" + name]
        assert "HEX(c.CHECK_CLAUSE)" in condition
        assert "LOWER(" not in condition and "REPLACE(" not in condition
        result = database.execute(
            f"SELECT CASE WHEN {condition} THEN 1 ELSE 0 END"
        ).fetchone()[0]
        assert bool(result) is accepted
    finally:
        database.close()


def _append_competing_process(database_path, identity, ready, outcomes):
    database = sqlite3.connect(database_path, timeout=10)
    database.row_factory = sqlite3.Row
    database.execute("PRAGMA foreign_keys = ON")
    try:
        parent = get_semantic_relation(database, "relation-root")
        ready.wait(timeout=10)
        try:
            appended = append_semantic_relation(database, _next(parent, identity))
        except (RuntimeError, ValueError, PermissionError, sqlite3.Error) as exc:
            outcomes.put((identity, str(exc)))
        else:
            outcomes.put((appended.relation_id, "committed"))
    finally:
        database.close()


@pytest.mark.timeout(30)
def test_database_cas_across_processes_commits_one_child_and_survives_reopen(tmp_path):
    path = str(tmp_path / "synthetic-relations.sqlite3")
    database = _db(path)
    append_semantic_relation(database, _root())
    database.close()
    context = multiprocessing.get_context("spawn")
    ready = context.Barrier(2)
    outcomes = context.Queue()
    processes = [
        context.Process(
            target=_append_competing_process, args=(path, identity, ready, outcomes)
        )
        for identity in ("child-one", "child-two")
    ]
    for process in processes:
        process.start()
    try:
        results = [outcomes.get(timeout=20) for _ in processes]
        for process in processes:
            process.join(timeout=10)
            assert process.exitcode == 0
        assert sorted(result for _identity, result in results) == [
            "SemanticRelationStaleParent",
            "committed",
        ]
        reopened = sqlite3.connect(path)
        reopened.row_factory = sqlite3.Row
        try:
            history = list_semantic_relations(reopened, _root().source_ref)
            assert len(history) == 2
            assert (
                len(
                    list_semantic_relations(
                        reopened, _root().source_ref, current_only=True
                    )
                )
                == 1
            )
        finally:
            reopened.close()
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        outcomes.close()
