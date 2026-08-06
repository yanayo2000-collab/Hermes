from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import sqlite3
import threading
import time
from typing import Any, Dict, Optional


GLOBAL_SCOPE = "__global__"
RATE_LIMIT_CODES = {4, 17, 32, 613, 80004}
RATE_LIMIT_SUBCODES = {2446079}


class MetaRateLimitBlocked(RuntimeError):
    def __init__(self, account_id: str, retry_after_seconds: int, reason: str) -> None:
        self.account_id = normalize_account_id(account_id) or GLOBAL_SCOPE
        self.retry_after_seconds = max(1, int(retry_after_seconds or 1))
        self.reason = str(reason or "meta_rate_limit_blocked")
        super().__init__(self.reason)


def normalize_account_id(value: Any) -> str:
    return str(value or "").strip().removeprefix("act_")


def _header(response: Any, name: str) -> str:
    headers = getattr(response, "headers", None)
    if not headers:
        return ""
    for key, value in dict(headers).items():
        if str(key).lower() == name.lower():
            return str(value or "").strip()
    return ""


def _json_object(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    try:
        parsed = json.loads(str(value or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _response_body(response: Any) -> Dict[str, Any]:
    try:
        body = response.json()
    except Exception:
        return {}
    return dict(body) if isinstance(body, dict) else {}


def _usage_percent(value: Any) -> float:
    try:
        return max(0.0, min(float(value or 0), 1000.0))
    except (TypeError, ValueError):
        return 0.0


@dataclass(frozen=True)
class MetaRateLimitSnapshot:
    account_id: str
    call_count: float
    total_cputime: float
    total_time: float
    account_util_pct: float
    access_tier: str
    blocked_until: float
    updated_at: float
    reason: str

    @property
    def max_usage_percent(self) -> float:
        return max(self.call_count, self.total_cputime, self.total_time, self.account_util_pct)

    def as_dict(self, *, now: Optional[float] = None) -> Dict[str, Any]:
        current = float(now if now is not None else time.time())
        retry_after_seconds = max(0, int(self.blocked_until - current))
        usage = self.max_usage_percent
        if retry_after_seconds > 0:
            status = "blocked"
        elif usage >= 85:
            status = "critical"
        elif usage >= 70:
            status = "high"
        elif usage >= 50:
            status = "guarded"
        else:
            status = "normal"
        return {
            "account_id": self.account_id,
            "status": status,
            "call_count": self.call_count,
            "total_cputime": self.total_cputime,
            "total_time": self.total_time,
            "account_util_pct": self.account_util_pct,
            "max_usage_percent": usage,
            "access_tier": self.access_tier,
            "retry_after_seconds": retry_after_seconds,
            "blocked_until": self.blocked_until,
            "updated_at": self.updated_at,
            "reason": self.reason,
        }


class MetaApiBudgetManager:
    """Small shared SQLite guard for Meta Graph API account budgets.

    The state database intentionally stays separate from the large business
    database so a rate-limit check never scans or locks business facts.
    """

    def __init__(
        self,
        path: str,
        *,
        hard_limit_percent: float = 85.0,
        stale_after_seconds: int = 3600,
        default_block_seconds: int = 3600,
        recovery_buffer_seconds: int = 300,
        clock: Any = time.time,
    ) -> None:
        self.path = str(path or ":memory:")
        self.hard_limit_percent = max(1.0, min(float(hard_limit_percent), 100.0))
        self.stale_after_seconds = max(60, int(stale_after_seconds))
        self.default_block_seconds = max(60, int(default_block_seconds))
        self.recovery_buffer_seconds = max(0, int(recovery_buffer_seconds))
        self.clock = clock
        self._lock = threading.RLock()
        if self.path != ":memory:":
            Path(self.path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, timeout=5.0, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout = 5000")
        if self.path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS meta_api_rate_state (
                account_id TEXT PRIMARY KEY,
                call_count REAL NOT NULL DEFAULT 0,
                total_cputime REAL NOT NULL DEFAULT 0,
                total_time REAL NOT NULL DEFAULT 0,
                account_util_pct REAL NOT NULL DEFAULT 0,
                access_tier TEXT NOT NULL DEFAULT '',
                blocked_until REAL NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL DEFAULT 0,
                reason TEXT NOT NULL DEFAULT ''
            )
            """
        )
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def snapshot(self, account_id: str) -> MetaRateLimitSnapshot:
        normalized = normalize_account_id(account_id) or GLOBAL_SCOPE
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM meta_api_rate_state WHERE account_id = ?",
                (normalized,),
            ).fetchone()
        if row is None:
            return MetaRateLimitSnapshot(normalized, 0, 0, 0, 0, "", 0, 0, "")
        return MetaRateLimitSnapshot(
            account_id=normalized,
            call_count=float(row["call_count"] or 0),
            total_cputime=float(row["total_cputime"] or 0),
            total_time=float(row["total_time"] or 0),
            account_util_pct=float(row["account_util_pct"] or 0),
            access_tier=str(row["access_tier"] or ""),
            blocked_until=float(row["blocked_until"] or 0),
            updated_at=float(row["updated_at"] or 0),
            reason=str(row["reason"] or ""),
        )

    def list_snapshots(self) -> list[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT account_id FROM meta_api_rate_state ORDER BY account_id"
            ).fetchall()
        return [self.snapshot(str(row["account_id"])).as_dict(now=self.clock()) for row in rows]

    def before_request(self, account_id: str) -> None:
        now = float(self.clock())
        account = self.snapshot(account_id)
        global_state = self.snapshot(GLOBAL_SCOPE)
        for state in (account, global_state):
            if state.blocked_until > now:
                raise MetaRateLimitBlocked(
                    account.account_id,
                    int(state.blocked_until - now),
                    state.reason or "meta_rate_limit_circuit_open",
                )
            fresh = state.updated_at > 0 and now - state.updated_at < self.stale_after_seconds
            if fresh and state.max_usage_percent >= self.hard_limit_percent:
                retry = max(60, int(self.stale_after_seconds - (now - state.updated_at)))
                raise MetaRateLimitBlocked(account.account_id, retry, "meta_rate_limit_headroom_exhausted")

    def guard_state(self, account_id: str) -> Dict[str, Any]:
        """Return the same fail-closed decision used before a real Meta request."""
        now = float(self.clock())
        normalized = normalize_account_id(account_id) or GLOBAL_SCOPE
        try:
            self.before_request(normalized)
        except MetaRateLimitBlocked as exc:
            retry_after_seconds = max(1, int(exc.retry_after_seconds))
            return {
                "account_id": normalized,
                "blocked": True,
                "status": "blocked",
                "retry_after_seconds": retry_after_seconds,
                "blocked_until": now + retry_after_seconds,
                "reason": str(exc.reason or "meta_rate_limit_circuit_open"),
            }
        return {
            "account_id": normalized,
            "blocked": False,
            "status": "available",
            "retry_after_seconds": 0,
            "blocked_until": 0,
            "reason": "",
        }

    def force_refresh_allowed(self, account_id: str) -> bool:
        now = float(self.clock())
        states = (self.snapshot(account_id), self.snapshot(GLOBAL_SCOPE))
        return all(
            state.blocked_until <= now
            and (
                state.updated_at <= 0
                or now - state.updated_at >= self.stale_after_seconds
                or state.max_usage_percent < 50
            )
            for state in states
        )

    def observe_response(self, account_id: str, response: Any) -> None:
        now = float(self.clock())
        normalized = normalize_account_id(account_id) or GLOBAL_SCOPE
        app_usage = _json_object(_header(response, "x-app-usage"))
        account_usage = _json_object(_header(response, "x-ad-account-usage"))
        business_usage = _json_object(_header(response, "x-business-use-case-usage"))
        touched: Dict[str, Dict[str, Any]] = {}

        if app_usage:
            touched[GLOBAL_SCOPE] = {
                "call_count": _usage_percent(app_usage.get("call_count")),
                "total_cputime": _usage_percent(app_usage.get("total_cputime")),
                "total_time": _usage_percent(app_usage.get("total_time")),
            }
        if account_usage:
            touched.setdefault(normalized, {}).update({
                "account_util_pct": _usage_percent(account_usage.get("acc_id_util_pct")),
                "access_tier": str(account_usage.get("ads_api_access_tier") or "")[:64],
            })
        for raw_account, raw_entries in business_usage.items():
            usage_account = normalize_account_id(raw_account) or normalized
            entries = raw_entries if isinstance(raw_entries, list) else []
            for raw_entry in entries:
                entry = dict(raw_entry or {})
                current = touched.setdefault(usage_account, {})
                for key in ("call_count", "total_cputime", "total_time"):
                    current[key] = max(
                        float(current.get(key) or 0),
                        _usage_percent(entry.get(key)),
                    )
                regain_minutes = max(0, int(float(entry.get("estimated_time_to_regain_access") or 0)))
                if regain_minutes:
                    current["blocked_until"] = max(
                        float(current.get("blocked_until") or 0),
                        now + regain_minutes * 60 + self.recovery_buffer_seconds,
                    )
                    current["reason"] = "meta_estimated_time_to_regain_access"

        body = _response_body(response)
        error = dict(body.get("error") or {})
        code = int(error.get("code") or 0)
        subcode = int(error.get("error_subcode") or 0)
        rate_limited = code in RATE_LIMIT_CODES or subcode in RATE_LIMIT_SUBCODES
        if rate_limited:
            estimated_blocked_until = max(
                (float(item.get("blocked_until") or 0) for item in touched.values()),
                default=0,
            )
            target = touched.setdefault(normalized, {})
            target["blocked_until"] = max(
                float(target.get("blocked_until") or 0),
                estimated_blocked_until
                or now + self.default_block_seconds + self.recovery_buffer_seconds,
            )
            target["reason"] = f"meta_rate_limit:{code}:{subcode}"

        for target_account, values in touched.items():
            self._upsert(target_account, values, now)

        if rate_limited:
            state = self.snapshot(normalized)
            raise MetaRateLimitBlocked(
                normalized,
                max(1, int(state.blocked_until - now)),
                state.reason,
            )

    def _upsert(self, account_id: str, values: Dict[str, Any], now: float) -> None:
        current = self.snapshot(account_id)
        row = {
            "call_count": values.get("call_count", current.call_count),
            "total_cputime": values.get("total_cputime", current.total_cputime),
            "total_time": values.get("total_time", current.total_time),
            "account_util_pct": values.get("account_util_pct", current.account_util_pct),
            "access_tier": values.get("access_tier", current.access_tier),
            "blocked_until": max(float(values.get("blocked_until") or 0), current.blocked_until),
            "reason": values.get("reason", current.reason),
        }
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO meta_api_rate_state (
                    account_id, call_count, total_cputime, total_time,
                    account_util_pct, access_tier, blocked_until, updated_at, reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id) DO UPDATE SET
                    call_count = excluded.call_count,
                    total_cputime = excluded.total_cputime,
                    total_time = excluded.total_time,
                    account_util_pct = excluded.account_util_pct,
                    access_tier = excluded.access_tier,
                    blocked_until = excluded.blocked_until,
                    updated_at = excluded.updated_at,
                    reason = excluded.reason
                """,
                (
                    normalize_account_id(account_id) or GLOBAL_SCOPE,
                    float(row["call_count"] or 0),
                    float(row["total_cputime"] or 0),
                    float(row["total_time"] or 0),
                    float(row["account_util_pct"] or 0),
                    str(row["access_tier"] or "")[:64],
                    float(row["blocked_until"] or 0),
                    now,
                    str(row["reason"] or "")[:128],
                ),
            )
            self._conn.commit()


class BudgetedMetaSession:
    """Requests-compatible session that applies the shared Meta budget guard."""

    _ACCOUNT_RE = re.compile(r"/act_(\d+)(?:/|$)")

    def __init__(self, session: Any, manager: MetaApiBudgetManager) -> None:
        self.session = session
        self.manager = manager

    def get(self, url: str, **kwargs: Any) -> Any:
        return self._request("get", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> Any:
        return self._request("post", url, **kwargs)

    def delete(self, url: str, **kwargs: Any) -> Any:
        return self._request("delete", url, **kwargs)

    def _request(self, method: str, url: str, **kwargs: Any) -> Any:
        match = self._ACCOUNT_RE.search(str(url or ""))
        account_id = match.group(1) if match else GLOBAL_SCOPE
        self.manager.before_request(account_id)
        response = getattr(self.session, method)(url, **kwargs)
        self.manager.observe_response(account_id, response)
        return response

    def __getattr__(self, name: str) -> Any:
        return getattr(self.session, name)


def default_meta_rate_limit_db_path(business_db_path: str) -> str:
    normalized = str(business_db_path or "").strip()
    if not normalized or normalized == ":memory:":
        return ":memory:"
    return str(Path(normalized).expanduser().resolve().with_name("meta_api_rate_limit.db"))
