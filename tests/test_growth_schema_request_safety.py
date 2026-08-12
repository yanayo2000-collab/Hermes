from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor

from app.growth.ad_experiment_service import AdExperimentService
from app.growth.schema import GROWTH_SCHEMA_VERSION, ensure_growth_schema


def test_current_growth_schema_bootstrap_is_read_only_on_repeat() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_growth_schema(conn)

    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    ensure_growth_schema(conn)

    assert conn.execute(
        "SELECT metadata_value FROM growth_schema_metadata WHERE metadata_key='schema_version'"
    ).fetchone()[0] == GROWTH_SCHEMA_VERSION
    assert not any(
        statement.lstrip().upper().startswith(("CREATE ", "DROP ", "ALTER "))
        for statement in statements
    )


def test_concurrent_growth_readers_do_not_execute_schema_ddl(tmp_path) -> None:
    database_path = tmp_path / "growth.db"
    setup = sqlite3.connect(database_path)
    setup.row_factory = sqlite3.Row
    ensure_growth_schema(setup)
    setup.close()

    def read_once() -> tuple[int, list[str]]:
        conn = sqlite3.connect(database_path, timeout=5)
        conn.row_factory = sqlite3.Row
        statements: list[str] = []
        conn.set_trace_callback(statements.append)
        try:
            result = AdExperimentService(conn).list(limit=1)
            return result["count"], statements
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: read_once(), range(24)))

    assert [count for count, _ in results] == [0] * 24
    assert not any(
        statement.lstrip().upper().startswith(("CREATE ", "DROP ", "ALTER "))
        for _, statements in results
        for statement in statements
    )
