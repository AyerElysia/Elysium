"""Read-only projection for quarantined legacy nodes/edges.

The canonical Memory model does not use ``memory_nodes`` or ``memory_edges`` as
authority.  This helper exists only so migration and diagnostics can inspect old
rows without importing an HTTP router, emitting events, or mutating activation.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from .eligibility import (
    assess_indexed_document_path,
    eligible_document_path_sql,
    register_indexed_path_sql_function,
)
from .sqlite_runtime import run_db


def _table_exists(db: sqlite3.Connection, table: str) -> bool:
    try:
        return (
            db.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type IN ('table', 'virtual table') AND name = ?",
                (table,),
            ).fetchone()
            is not None
        )
    except sqlite3.ProgrammingError:
        raise
    except sqlite3.Error:
        return False


def _table_columns(db: sqlite3.Connection, table: str) -> set[str]:
    if not _table_exists(db, table):
        return set()
    try:
        return {
            str(row[1])
            for row in db.execute(f"PRAGMA table_info({table})").fetchall()
        }
    except sqlite3.ProgrammingError:
        raise
    except sqlite3.Error:
        return set()


def _row_value(row: sqlite3.Row, name: str, default: Any = None) -> Any:
    return row[name] if name in row.keys() else default


def _is_deleted(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    try:
        return bool(int(value or 0))
    except (TypeError, ValueError):
        return bool(value)


def _node_type(value: Any) -> str:
    raw = getattr(value, "value", value)
    return str("file" if raw is None else raw).lower()


def _visible_node_type(row: sqlite3.Row) -> str | None:
    if not _row_value(row, "node_id") or _is_deleted(
        _row_value(row, "is_deleted")
    ):
        return None
    node_type = _node_type(_row_value(row, "node_type"))
    if node_type == "concept":
        return node_type
    if node_type != "file":
        return None
    if not assess_indexed_document_path(_row_value(row, "file_path")).eligible:
        return None
    return node_type


def _activation(row: sqlite3.Row) -> float:
    try:
        return float(_row_value(row, "activation_strength", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _visibility_sql(columns: set[str], alias: str) -> tuple[str, list[Any]]:
    node_type = (
        f"lower(COALESCE({alias}.node_type, 'file'))"
        if "node_type" in columns
        else "'file'"
    )
    if "file_path" in columns:
        eligible_sql, eligible_params = eligible_document_path_sql(
            f"{alias}.file_path"
        )
        file_clause = (
            f"{alias}.file_path IS NOT NULL "
            f"AND TRIM({alias}.file_path) <> '' AND {eligible_sql}"
        )
    else:
        file_clause = "0 = 1"
        eligible_params = []
    clauses = [
        f"({node_type} = 'concept' OR "
        f"({node_type} = 'file' AND ({file_clause})))"
    ]
    if "is_deleted" in columns:
        clauses.append(f"COALESCE({alias}.is_deleted, 0) = 0")
    return " AND ".join(clauses), eligible_params


def _empty_graph() -> dict[str, list[dict[str, Any]]]:
    return {"nodes": [], "links": []}


def _node_payload(
    row: sqlite3.Row,
    node_type: str,
    degree: int,
) -> dict[str, Any]:
    file_path = _row_value(row, "file_path") if node_type == "file" else None
    return {
        "id": _row_value(row, "node_id"),
        "type": node_type.upper(),
        "title": _row_value(row, "title") or file_path or "Untitled",
        "path": file_path,
        "activation": _activation(row),
        "importance": float(_row_value(row, "importance", 0.0) or 0.0),
        "valence": float(_row_value(row, "emotional_valence", 0.0) or 0.0),
        "arousal": float(_row_value(row, "emotional_arousal", 0.0) or 0.0),
        "access_count": int(_row_value(row, "access_count", 0) or 0),
        "updated_at": _row_value(row, "updated_at"),
        "last_accessed_at": _row_value(row, "last_accessed_at"),
        "degree": degree,
    }


def _link_payload(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": _row_value(row, "edge_id"),
        "source": _row_value(row, "source_id"),
        "target": _row_value(row, "target_id"),
        "type": _row_value(row, "edge_type"),
        "weight": float(_row_value(row, "weight", 0.0) or 0.0),
        "base_strength": float(_row_value(row, "base_strength", 0.0) or 0.0),
        "reinforcement": float(_row_value(row, "reinforcement", 0.0) or 0.0),
        "activation_count": int(_row_value(row, "activation_count", 0) or 0),
        "last_activated_at": _row_value(row, "last_activated_at"),
        "reason": _row_value(row, "reason") or "",
    }


async def read_legacy_graph_payload(
    db: sqlite3.Connection,
    *,
    limit_nodes: int,
    min_weight: float,
    focus_id: str | None,
) -> dict[str, list[dict[str, Any]]]:
    """Project eligible legacy rows without changing their access state."""

    node_limit = max(1, min(int(limit_nodes), 200))
    weight_floor = max(0.0, min(float(min_weight), 1.0))

    def _read() -> dict[str, list[dict[str, Any]]]:
        if not _table_exists(db, "memory_nodes"):
            return _empty_graph()
        node_columns = _table_columns(db, "memory_nodes")
        if "node_id" not in node_columns:
            return _empty_graph()
        register_indexed_path_sql_function(db)
        visible_sql, visible_params = _visibility_sql(node_columns, "n")
        cursor = db.cursor()

        if focus_id is None:
            order_sql = (
                "COALESCE(n.activation_strength, 0.0) DESC, n.node_id"
                if "activation_strength" in node_columns
                else "n.node_id"
            )
            rows = cursor.execute(
                f"SELECT n.* FROM memory_nodes AS n WHERE {visible_sql} "
                f"ORDER BY {order_sql} LIMIT ?",
                [*visible_params, node_limit],
            ).fetchall()
            selected_rows = [row for row in rows if _visible_node_type(row)]
        else:
            focus_row = cursor.execute(
                f"SELECT n.* FROM memory_nodes AS n "
                f"WHERE n.node_id = ? AND {visible_sql}",
                [focus_id, *visible_params],
            ).fetchone()
            if focus_row is None or _visible_node_type(focus_row) is None:
                return _empty_graph()
            candidate_ids = [focus_id]
            if _table_exists(db, "memory_edges"):
                edge_columns = _table_columns(db, "memory_edges")
                if {"source_id", "target_id", "weight"} <= edge_columns:
                    source_sql, source_params = _visibility_sql(
                        node_columns, "source_node"
                    )
                    target_sql, target_params = _visibility_sql(
                        node_columns, "target_node"
                    )
                    edges = cursor.execute(
                        "SELECT e.source_id, e.target_id FROM memory_edges AS e "
                        "JOIN memory_nodes AS source_node "
                        "ON source_node.node_id = e.source_id "
                        "JOIN memory_nodes AS target_node "
                        "ON target_node.node_id = e.target_id "
                        "WHERE e.weight >= ? "
                        "AND (e.source_id = ? OR e.target_id = ?) "
                        f"AND {source_sql} AND {target_sql} ORDER BY e.weight DESC",
                        [
                            weight_floor,
                            focus_id,
                            focus_id,
                            *source_params,
                            *target_params,
                        ],
                    ).fetchall()
                    seen = {focus_id}
                    for edge in edges:
                        source = str(_row_value(edge, "source_id") or "")
                        target = str(_row_value(edge, "target_id") or "")
                        neighbour = target if source == focus_id else source
                        if neighbour and neighbour not in seen:
                            candidate_ids.append(neighbour)
                            seen.add(neighbour)
                        if len(candidate_ids) >= node_limit:
                            break
            placeholders = ",".join("?" for _ in candidate_ids)
            rows = cursor.execute(
                f"SELECT n.* FROM memory_nodes AS n "
                f"WHERE n.node_id IN ({placeholders}) AND {visible_sql}",
                [*candidate_ids, *visible_params],
            ).fetchall()
            by_id = {
                str(_row_value(row, "node_id")): row
                for row in rows
                if _visible_node_type(row)
            }
            if focus_id not in by_id:
                return _empty_graph()
            selected_rows = [by_id[focus_id]] + sorted(
                (row for key, row in by_id.items() if key != focus_id),
                key=lambda row: (-_activation(row), str(_row_value(row, "node_id"))),
            )

        selected_ids = [str(_row_value(row, "node_id")) for row in selected_rows]
        if not selected_ids:
            return _empty_graph()
        degree = {node_id: 0 for node_id in selected_ids}
        links: list[dict[str, Any]] = []
        if _table_exists(db, "memory_edges"):
            edge_columns = _table_columns(db, "memory_edges")
            if {"source_id", "target_id", "weight"} <= edge_columns:
                placeholders = ",".join("?" for _ in selected_ids)
                order = "weight DESC"
                if "activation_count" in edge_columns:
                    order += ", activation_count DESC"
                edges = cursor.execute(
                    f"SELECT * FROM memory_edges WHERE weight >= ? "
                    f"AND source_id IN ({placeholders}) "
                    f"AND target_id IN ({placeholders}) ORDER BY {order}",
                    [weight_floor, *selected_ids, *selected_ids],
                ).fetchall()
                selected = set(selected_ids)
                for edge in edges:
                    source = str(_row_value(edge, "source_id") or "")
                    target = str(_row_value(edge, "target_id") or "")
                    if source not in selected or target not in selected:
                        continue
                    links.append(_link_payload(edge))
                    degree[source] += 1
                    if target != source:
                        degree[target] += 1
        nodes = [
            _node_payload(row, node_type, degree[str(_row_value(row, "node_id"))])
            for row in selected_rows
            if (node_type := _visible_node_type(row)) is not None
        ]
        return {"nodes": nodes, "links": links}

    try:
        return await run_db(_read)
    except sqlite3.ProgrammingError as exc:
        if "created in a thread" not in str(exc):
            raise
        return _read()


__all__ = ["read_legacy_graph_payload"]
