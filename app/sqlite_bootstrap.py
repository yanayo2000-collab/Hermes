from __future__ import annotations

import logging
import re
import sqlite3
import threading
from pathlib import Path


logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "sqlite_config.yaml"
DEFAULT_BUSY_TIMEOUT_MS = {"online": 3000, "batch": 10000}
DEFAULT_WRITE_WINDOW_TIMEOUT_SECONDS = 5.0

_READY_PATHS: set[str] = set()
_READY_PATHS_LOCK = threading.Lock()


def _sqlite_config(config_path: str | Path | None = None) -> dict[str, object]:
    path = Path(config_path or DEFAULT_CONFIG_PATH)
    values: dict[str, object] = {
        "online": DEFAULT_BUSY_TIMEOUT_MS["online"],
        "batch": DEFAULT_BUSY_TIMEOUT_MS["batch"],
        "write_window_timeout_seconds": DEFAULT_WRITE_WINDOW_TIMEOUT_SECONDS,
    }
    if not path.is_file():
        return values
    section = ""
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        stripped = line.strip()
        if stripped == "busy_timeout:":
            section = "busy_timeout"
            continue
        match = re.fullmatch(r"([a-z_]+)\s*:\s*([0-9]+(?:\.[0-9]+)?)", stripped)
        if not match:
            continue
        key, raw_value = match.groups()
        if section == "busy_timeout" and key in {"online", "batch"}:
            values[key] = int(float(raw_value))
        elif key == "write_window_timeout_seconds":
            values[key] = float(raw_value)
    return values


def sqlite_busy_timeout_ms(
    profile: str,
    *,
    config_path: str | Path | None = None,
) -> int:
    normalized = str(profile or "").strip().lower()
    if normalized not in DEFAULT_BUSY_TIMEOUT_MS:
        raise ValueError(f"sqlite_profile_invalid:{normalized or 'missing'}")
    return int(_sqlite_config(config_path)[normalized])


def sqlite_write_window_timeout_seconds(
    *,
    config_path: str | Path | None = None,
) -> float:
    return float(_sqlite_config(config_path)["write_window_timeout_seconds"])


def ensure_sqlite_ready(
    db_path: str | Path,
    *,
    profile: str = "online",
    config_path: str | Path | None = None,
) -> dict[str, object]:
    path = str(db_path)
    if path == ":memory:":
        return {
            "journal_mode": "memory",
            "busy_timeout_ms": sqlite_busy_timeout_ms(profile, config_path=config_path),
        }
    resolved = str(Path(path).resolve())
    Path(resolved).parent.mkdir(parents=True, exist_ok=True)
    with _READY_PATHS_LOCK:
        if resolved in _READY_PATHS:
            with sqlite3.connect(resolved) as conn:
                mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
            return {
                "journal_mode": mode,
                "busy_timeout_ms": sqlite_busy_timeout_ms(profile, config_path=config_path),
            }
        with sqlite3.connect(resolved, timeout=5.0, isolation_level=None) as conn:
            if conn.in_transaction:
                raise RuntimeError("sqlite_bootstrap_active_transaction")
            mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
            if mode != "wal":
                mode = str(conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower()
            if mode != "wal":
                raise RuntimeError(f"sqlite_bootstrap_wal_unavailable:{mode}")
        _READY_PATHS.add(resolved)
    busy_timeout_ms = sqlite_busy_timeout_ms(profile, config_path=config_path)
    logger.warning(
        "sqlite_ready journal_mode=%s busy_timeout_ms=%s db=%s",
        mode,
        busy_timeout_ms,
        resolved,
    )
    return {"journal_mode": mode, "busy_timeout_ms": busy_timeout_ms}
