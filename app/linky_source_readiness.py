from __future__ import annotations

import sqlite3
from datetime import date


LINKY_MIN_PREVIOUS_SOURCE_ROW_RATIO = 0.8


def is_linky_source_row_count_ready(
    *,
    current_count: int,
    previous_count: int,
) -> bool:
    if previous_count <= 0:
        return True
    minimum_count = max(
        1,
        int(previous_count * LINKY_MIN_PREVIOUS_SOURCE_ROW_RATIO),
    )
    return current_count >= minimum_count


def persisted_linky_scope_ready(
    conn: sqlite3.Connection,
    *,
    executor_key: str,
    target_date: date,
) -> bool:
    """Return whether a persisted scope is safe to reuse as complete source."""
    rows = conn.execute(
        """
        SELECT stat_date_bj, source_row_count
        FROM streamer_external_guild_revenue_daily
        WHERE app_name='linky'
          AND guild_executor_key=?
          AND stat_date_bj<=?
        ORDER BY stat_date_bj DESC
        LIMIT 2
        """,
        (executor_key, target_date.isoformat()),
    ).fetchall()
    if not rows or str(rows[0]['stat_date_bj'] or '') != target_date.isoformat():
        return False
    current_count = int(rows[0]['source_row_count'] or 0)
    previous_count = int(rows[1]['source_row_count'] or 0) if len(rows) > 1 else 0
    return is_linky_source_row_count_ready(
        current_count=current_count,
        previous_count=previous_count,
    )
