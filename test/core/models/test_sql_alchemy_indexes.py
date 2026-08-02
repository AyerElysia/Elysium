"""Index-shape contracts for the core SQLAlchemy schema."""

from __future__ import annotations

from collections import defaultdict

from src.core.models.sql_alchemy import Base


def test_metadata_has_no_duplicate_index_column_sets() -> None:
    """A table must not maintain two indexes over the same ordered columns."""
    duplicates: list[tuple[str, tuple[str, ...], list[str]]] = []

    for table in Base.metadata.tables.values():
        by_columns: dict[tuple[str, ...], list[str]] = defaultdict(list)
        for index in table.indexes:
            columns = tuple(column.name for column in index.columns)
            by_columns[columns].append(index.name or "<unnamed>")
        for columns, names in by_columns.items():
            if len(names) > 1:
                duplicates.append((table.name, columns, sorted(names)))

    assert duplicates == []
