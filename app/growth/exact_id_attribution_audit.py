"""Bounded, read-only exact-ID attribution feasibility audit for GLE Gate 0.

The audit measures whether selected experiment traffic can reach existing CRM
truth through explicit top-level ``lead_id`` and/or ``customer_id`` keys.  It
never infers identity from customer_user_id, user_key, bind_id, event_id, PII,
names, or timestamps, and it never decides business qualification.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import quote

from app.growth.common import canonical_json


AUDIT_SCHEMA_VERSION = "gle-g0-01-exact-id-attribution-audit-v1"
ATTRIBUTION_VERSION = "gle_exact_meta_to_canonical_crm_identity_v1"
DEDUPE_VERSION = "gle_exact_event_then_canonical_identity_v1"
QUALIFICATION_RULE_VERSION = "UNFROZEN"
BUSY_TIMEOUT_MS = 5000
MAX_EVENTS_HARD_LIMIT = 100000
MAX_EXPERIMENTS = 32

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_REQUIRED_COLUMNS = {
    "ad_dashboard_fact_rows": {
        "date",
        "account_id",
        "country",
        "campaign_id",
        "adset_id",
        "ad_id",
    },
    "ad_experiment": {
        "experiment_id",
        "account_id",
        "country",
        "source_campaign_id",
        "source_adset_id",
        "source_ad_id",
        "control_definition_json",
    },
    "tugao_bind_success_raw_events": {
        "event_id",
        "bind_status",
        "occurred_at_utc",
        "updated_at_utc",
        "business_date",
        "project",
        "country",
        "campaign_id",
        "adset_id",
        "ad_id",
        "raw_payload_sha256",
        "raw_payload_json",
    },
    "leads": {
        "lead_id",
        "matched_customer_id",
        "crm_verified_at",
        "updated_at",
    },
    "customer_projection": {"customer_id", "lead_id", "updated_at"},
}
_REQUIRED_PRIMARY_KEYS = {
    "ad_experiment": "experiment_id",
    "tugao_bind_success_raw_events": "event_id",
    "leads": "lead_id",
    "customer_projection": "customer_id",
}


class AuditContractError(ValueError):
    """The requested bounded audit contract is invalid."""


class SourceAuditError(RuntimeError):
    """The immutable/read-only source cannot satisfy the audit contract."""


@dataclass(frozen=True)
class AuditInput:
    db_path: Path
    expected_db_sha256: str
    account_id: str
    market: str
    experiment_ids: Tuple[str, ...]
    window_start: str
    window_end: str
    project: str
    max_events: int


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_json(value: Any) -> str:
    return _sha256_bytes(canonical_json(value).encode("utf-8"))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise SourceAuditError("SNAPSHOT_UNREADABLE") from exc
    return digest.hexdigest()


def _parse_timestamp(value: str, code: str) -> datetime:
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AuditContractError(code) from exc
    if parsed.tzinfo is None:
        raise AuditContractError(code)
    return parsed


def _normalized_input(value: AuditInput) -> Tuple[AuditInput, datetime, datetime]:
    path = Path(value.db_path).resolve()
    expected_hash = str(value.expected_db_sha256 or "").strip().lower()
    account_value = value.account_id
    account_id = account_value if isinstance(account_value, str) else ""
    market = str(value.market or "").strip().upper()
    project = str(value.project or "").strip().upper()
    experiment_values = []
    for item in value.experiment_ids:
        if item is None or item == "":
            continue
        if not isinstance(item, str) or item != item.strip():
            raise AuditContractError("EXPERIMENT_ID_INVALID")
        experiment_values.append(item)
    experiments = tuple(sorted(set(experiment_values)))
    if not path.is_file():
        raise SourceAuditError("SNAPSHOT_UNREADABLE")
    if not _SHA256_RE.fullmatch(expected_hash):
        raise AuditContractError("EXPECTED_DB_SHA256_INVALID")
    if not account_id:
        raise AuditContractError("ACCOUNT_ID_REQUIRED")
    if account_id != account_id.strip():
        raise AuditContractError("ACCOUNT_ID_INVALID")
    if not market:
        raise AuditContractError("MARKET_REQUIRED")
    if not project:
        raise AuditContractError("PROJECT_REQUIRED")
    if not experiments:
        raise AuditContractError("EXPERIMENT_ID_REQUIRED")
    if len(experiments) > MAX_EXPERIMENTS:
        raise AuditContractError("EXPERIMENT_COUNT_INVALID")
    if int(value.max_events) < 1 or int(value.max_events) > MAX_EVENTS_HARD_LIMIT:
        raise AuditContractError("MAX_EVENTS_INVALID")
    start = _parse_timestamp(value.window_start, "WINDOW_START_INVALID")
    end = _parse_timestamp(value.window_end, "WINDOW_END_INVALID")
    if start >= end:
        raise AuditContractError("WINDOW_RANGE_INVALID")
    normalized = AuditInput(
        db_path=path,
        expected_db_sha256=expected_hash,
        account_id=account_id,
        market=market,
        experiment_ids=experiments,
        window_start=start.isoformat(),
        window_end=end.isoformat(),
        project=project,
        max_events=int(value.max_events),
    )
    return normalized, start, end


@contextmanager
def open_readonly_snapshot(path: Path) -> Iterator[sqlite3.Connection]:
    """Open an existing SQLite snapshot with writes disabled at two layers."""

    resolved = Path(path).resolve()
    uri = f"file:{quote(str(resolved), safe='/')}?mode=ro&immutable=1"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=BUSY_TIMEOUT_MS / 1000)
    except sqlite3.Error as exc:
        raise SourceAuditError("SNAPSHOT_OPEN_FAILED") from exc
    try:
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA query_only=ON")
        yield conn
    finally:
        conn.close()


def _sidecar_state(path: Path) -> Dict[str, Dict[str, Any]]:
    state: Dict[str, Dict[str, Any]] = {}
    for suffix in ("-wal", "-journal", "-shm"):
        sidecar = Path(f"{path}{suffix}")
        if not sidecar.exists():
            state[suffix] = {"exists": False}
            continue
        stat = sidecar.stat()
        state[suffix] = {
            "exists": True,
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "sha256": _sha256_file(sidecar),
        }
    return state


def _require_checkpointed_sidecars(state: Mapping[str, Mapping[str, Any]]) -> None:
    for suffix, details in state.items():
        if details.get("exists") and int(details.get("size") or 0) > 0:
            raise SourceAuditError(f"SNAPSHOT_SIDECAR_PRESENT:{suffix}")


def _source_schema(conn: sqlite3.Connection) -> Tuple[Dict[str, List[Dict[str, Any]]], str]:
    schema: Dict[str, List[Dict[str, Any]]] = {}
    for table in sorted(_REQUIRED_COLUMNS):
        rows = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
        if not rows:
            raise SourceAuditError(f"SOURCE_SCHEMA_MISSING:{table}")
        columns = {
            str(row["name"]): {
                "name": str(row["name"]),
                "type": str(row["type"] or "").upper(),
                "notnull": int(row["notnull"] or 0),
                "pk": int(row["pk"] or 0),
            }
            for row in rows
        }
        missing = sorted(_REQUIRED_COLUMNS[table] - set(columns))
        if missing:
            raise SourceAuditError(
                f"SOURCE_SCHEMA_MISSING:{table}:{','.join(missing)}"
            )
        required_pk = _REQUIRED_PRIMARY_KEYS.get(table)
        if required_pk and int(columns[required_pk]["pk"]) <= 0:
            raise SourceAuditError(f"SOURCE_SCHEMA_PRIMARY_KEY_MISSING:{table}")
        schema[table] = [columns[name] for name in sorted(columns)]
    return schema, _sha256_json(schema)


def _placeholders(values: Sequence[Any]) -> str:
    return ",".join("?" for _ in values)


def _decode_object(value: Any) -> Dict[str, Any]:
    try:
        decoded = json.loads(str(value or "{}"))
    except (TypeError, ValueError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _exact_id_state(value: Any) -> str:
    if value is None or value == "":
        return "missing"
    if not isinstance(value, str):
        return "invalid"
    if value != value.strip():
        return "invalid"
    return "valid"


def _experiment_rows(
    conn: sqlite3.Connection, audit_input: AuditInput
) -> Tuple[List[Dict[str, Any]], List[str]]:
    ids = audit_input.experiment_ids
    rows = conn.execute(
        f"""
        SELECT experiment_id,account_id,country,source_campaign_id,source_adset_id,
               source_ad_id,control_definition_json
        FROM ad_experiment
        WHERE account_id=? AND UPPER(country)=? AND experiment_id IN ({_placeholders(ids)})
        ORDER BY experiment_id
        LIMIT ?
        """,
        (audit_input.account_id, audit_input.market, *ids, len(ids) + 1),
    ).fetchall()
    serialized = [dict(row) for row in rows]
    found = {_text(row["experiment_id"]) for row in rows}
    reasons = []
    if found != set(ids) or len(rows) > len(ids):
        reasons.append("EXPERIMENT_SCOPE_INCOMPLETE")
    return serialized[: len(ids)], reasons


def _experiment_index(
    rows: Sequence[Mapping[str, Any]],
) -> Tuple[Dict[str, List[Dict[str, Any]]], List[str]]:
    by_ad: Dict[str, List[Dict[str, Any]]] = {}
    reasons: List[str] = []
    for raw in rows:
        row = dict(raw)
        ad_id_value = row.get("source_ad_id")
        ad_id_state = _exact_id_state(ad_id_value)
        ad_id = _text(ad_id_value)
        if ad_id_state == "missing":
            reasons.append("MISSING_META_AD_ID")
            continue
        if ad_id_state == "invalid":
            reasons.append("META_ID_INVALID")
            continue
        control = _decode_object(row.get("control_definition_json"))
        randomization = control.get("meta_randomization")
        randomization = randomization if isinstance(randomization, dict) else {}
        row["study_id"] = _text(randomization.get("study_id"))
        row["study_cell_id"] = _text(randomization.get("study_cell_id"))
        row["readback_verified"] = randomization.get("readback_verified") is True
        row["preflight_reason"] = ""
        study_id_state = _exact_id_state(randomization.get("study_id"))
        study_cell_state = _exact_id_state(randomization.get("study_cell_id"))
        if study_id_state == "missing":
            row["preflight_reason"] = "MISSING_STUDY_ID"
        elif study_id_state == "invalid":
            row["preflight_reason"] = "STUDY_ID_INVALID"
        elif study_cell_state == "missing":
            row["preflight_reason"] = "MISSING_STUDY_CELL_ID"
        elif study_cell_state == "invalid":
            row["preflight_reason"] = "STUDY_CELL_ID_INVALID"
        elif not row["readback_verified"]:
            row["preflight_reason"] = "STUDY_CELL_NOT_READBACK_VERIFIED"
        for key in ("source_campaign_id", "source_adset_id"):
            if row["preflight_reason"]:
                continue
            state = _exact_id_state(row.get(key))
            if state == "missing":
                row["preflight_reason"] = "MISSING_META_ID"
            elif state == "invalid":
                row["preflight_reason"] = "META_ID_INVALID"
        by_ad.setdefault(ad_id, []).append(row)
    preflight_reasons = {
        _text(row.get("preflight_reason"))
        for experiments in by_ad.values()
        for row in experiments
        if _text(row.get("preflight_reason"))
    }
    reasons.extend(sorted(preflight_reasons))
    study_ids = [
        _text(row.get("study_id"))
        for experiments in by_ad.values()
        for row in experiments
        if _text(row.get("study_id"))
    ]
    study_cell_ids = [
        _text(row.get("study_cell_id"))
        for experiments in by_ad.values()
        for row in experiments
        if _text(row.get("study_cell_id"))
    ]
    if len(set(study_ids)) > 1:
        reasons.append("STUDY_ID_NOT_SHARED")
    if len(study_cell_ids) != len(set(study_cell_ids)):
        reasons.append("DUPLICATE_STUDY_CELL_ID")
    return by_ad, reasons


def _fact_index(
    conn: sqlite3.Connection,
    audit_input: AuditInput,
    start: datetime,
    end: datetime,
    ad_ids: Sequence[str],
) -> Tuple[Dict[str, set[Tuple[str, str, str, str]]], bool, Dict[str, str]]:
    if not ad_ids:
        return {}, False, {}
    rows = conn.execute(
        f"""
        SELECT account_id,country,campaign_id,adset_id,ad_id
        FROM ad_dashboard_fact_rows
        WHERE date>=? AND date<=? AND account_id=? AND UPPER(country)=?
          AND ad_id IN ({_placeholders(ad_ids)})
        ORDER BY ad_id,campaign_id,adset_id
        LIMIT ?
        """,
        (
            start.date().isoformat(),
            (end - timedelta(microseconds=1)).date().isoformat(),
            audit_input.account_id,
            audit_input.market,
            *ad_ids,
            audit_input.max_events + 1,
        ),
    ).fetchall()
    if len(rows) > audit_input.max_events:
        return {}, True, {}
    result: Dict[str, set[Tuple[str, str, str, str]]] = {}
    fact_reasons: Dict[str, str] = {}
    for row in rows:
        ad_id = _text(row["ad_id"])
        states = {
            _exact_id_state(row[column])
            for column in ("account_id", "campaign_id", "adset_id", "ad_id")
        }
        if "invalid" in states:
            fact_reasons[ad_id] = "META_ID_INVALID"
            continue
        if "missing" in states:
            fact_reasons[ad_id] = "MISSING_META_ID"
            continue
        result.setdefault(ad_id, set()).add(
            (
                _text(row["account_id"]),
                _text(row["campaign_id"]),
                _text(row["adset_id"]),
                ad_id,
            )
        )
    return result, False, fact_reasons


def _candidate_bind_rows(
    conn: sqlite3.Connection,
    audit_input: AuditInput,
    experiments: Sequence[Mapping[str, Any]],
) -> Tuple[List[Dict[str, Any]], bool]:
    meta_tuples = sorted(
        {
            (
                _text(row.get("source_campaign_id")),
                _text(row.get("source_adset_id")),
                _text(row.get("source_ad_id")),
            )
            for row in experiments
            if _text(row.get("source_campaign_id"))
            and _text(row.get("source_adset_id"))
            and _text(row.get("source_ad_id"))
        }
    )
    start_date = datetime.fromisoformat(audit_input.window_start).date().isoformat()
    end_date = (
        datetime.fromisoformat(audit_input.window_end) - timedelta(microseconds=1)
    ).date().isoformat()
    clauses = ["(campaign_id=? AND adset_id=? AND ad_id=?)" for _ in meta_tuples]
    params: List[Any] = [
        audit_input.window_start,
        audit_input.window_end,
        start_date,
        end_date,
        audit_input.project,
        audit_input.market,
    ]
    for campaign_id, adset_id, ad_id in meta_tuples:
        params.extend((campaign_id, adset_id, ad_id))
    if not clauses:
        return [], False
    params.append(audit_input.max_events + 1)
    rows = conn.execute(
        f"""
        SELECT event_id,occurred_at_utc,updated_at_utc,business_date,campaign_id,
               adset_id,ad_id,raw_payload_sha256,raw_payload_json
        FROM tugao_bind_success_raw_events
        WHERE LOWER(COALESCE(bind_status,''))='success'
          AND (
            (COALESCE(NULLIF(occurred_at_utc,''),NULLIF(updated_at_utc,''),'')>=?
             AND COALESCE(NULLIF(occurred_at_utc,''),NULLIF(updated_at_utc,''),'')<?)
            OR (COALESCE(NULLIF(occurred_at_utc,''),NULLIF(updated_at_utc,''),'')=''
                AND COALESCE(business_date,'')>=?
                AND COALESCE(business_date,'')<=?)
          )
          AND UPPER(project)=? AND UPPER(country)=?
          AND ({' OR '.join(clauses)})
        ORDER BY COALESCE(NULLIF(occurred_at_utc,''),NULLIF(updated_at_utc,''),business_date,event_id),event_id
        LIMIT ?
        """,
        params,
    ).fetchall()
    if len(rows) > audit_input.max_events:
        return [], True
    return [dict(row) for row in rows], False


def _canonical_keys(row: Mapping[str, Any]) -> Tuple[str, str, str]:
    payload_text = str(row.get("raw_payload_json") or "")
    if _text(row.get("raw_payload_sha256")) != _sha256_bytes(payload_text.encode("utf-8")):
        return "", "", "RAW_PAYLOAD_HASH_MISMATCH"
    try:
        payload = json.loads(payload_text)
    except (TypeError, ValueError):
        return "", "", "RAW_PAYLOAD_INVALID"
    if not isinstance(payload, dict):
        return "", "", "RAW_PAYLOAD_INVALID"
    lead_value = payload.get("lead_id")
    customer_value = payload.get("customer_id")
    if "lead_id" in payload and _exact_id_state(lead_value) != "valid":
        return "", "", "CANONICAL_IDENTITY_INVALID"
    if "customer_id" in payload and _exact_id_state(customer_value) != "valid":
        return "", "", "CANONICAL_IDENTITY_INVALID"
    lead_id = lead_value if isinstance(lead_value, str) else ""
    customer_id = customer_value if isinstance(customer_value, str) else ""
    if not lead_id and not customer_id:
        return "", "", "MISSING_CANONICAL_IDENTITY"
    return lead_id, customer_id, ""


def _bulk_rows(
    conn: sqlite3.Connection,
    *,
    table: str,
    key: str,
    columns: str,
    values: Sequence[str],
    limit: int,
) -> Dict[str, Dict[str, Any]]:
    unique = sorted(set(values))
    if not unique:
        return {}
    scope_json = canonical_json(unique)
    row_limit = min(limit, len(unique))
    rows = conn.execute(
        f"SELECT {columns} FROM {table} "
        f"JOIN json_each(?) AS scope "
        f"ON {table}.{key}=CAST(scope.value AS TEXT) "
        f"ORDER BY {table}.{key} LIMIT ?",
        (scope_json, row_limit + 1),
    ).fetchall()
    if len(rows) > row_limit:
        raise SourceAuditError("CANONICAL_IDENTITY_SOURCE_AMBIGUOUS")
    return {_text(row[key]): dict(row) for row in rows}


def _reverse_index(
    conn: sqlite3.Connection,
    *,
    table: str,
    key: str,
    value: str,
    keys: Sequence[str],
    limit: int,
) -> Dict[str, set[str]]:
    unique = sorted(set(keys))
    if not unique:
        return {}
    scope_json = canonical_json(unique)
    rows = conn.execute(
        f"SELECT {table}.{key},{table}.{value} FROM {table} "
        f"JOIN json_each(?) AS scope "
        f"ON {table}.{key}=CAST(scope.value AS TEXT) "
        f"ORDER BY {table}.{key},{table}.{value} LIMIT ?",
        (scope_json, limit + 1),
    ).fetchall()
    if len(rows) > limit:
        raise SourceAuditError("CANONICAL_IDENTITY_SOURCE_LIMIT_EXCEEDED")
    result: Dict[str, set[str]] = {}
    for row in rows:
        result.setdefault(_text(row[key]), set()).add(_text(row[value]))
    return result


def _crm_row_id_state(
    row: Optional[Mapping[str, Any]], fields: Sequence[str]
) -> str:
    if row is None:
        return "missing"
    states = {_exact_id_state(row.get(field)) for field in fields}
    if "invalid" in states:
        return "invalid"
    if "missing" in states:
        return "missing"
    return "valid"


def _percentile(values: Sequence[float], percentile: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return round(ordered[0], 3)
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return round(ordered[lower] * (1 - weight) + ordered[upper] * weight, 3)


def _coverage(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 0.0


def _is_complete_utc_day_window(start: datetime, end: datetime) -> bool:
    utc_start = start.astimezone(timezone.utc)
    utc_end = end.astimezone(timezone.utc)
    return (
        utc_start.hour == 0
        and utc_start.minute == 0
        and utc_start.second == 0
        and utc_start.microsecond == 0
        and utc_end.hour == 0
        and utc_end.minute == 0
        and utc_end.second == 0
        and utc_end.microsecond == 0
    )


def _reason_bucket(reason_counts: Mapping[str, int], prefix: str) -> Dict[str, int]:
    if prefix == "missing":
        keys = {
            "MISSING_META_AD_ID",
            "MISSING_META_ID",
            "AD_NOT_IN_FACTS",
            "MISSING_STUDY_ID",
            "MISSING_STUDY_CELL_ID",
            "MISSING_CANONICAL_IDENTITY",
            "CANONICAL_IDENTITY_NOT_IN_CRM",
            "MISSING_CRM_VERIFICATION_TIMESTAMP",
        }
    else:
        keys = {
            "AMBIGUOUS_FACT_LINEAGE",
            "AMBIGUOUS_EXPERIMENT_AD_ID",
            "AMBIGUOUS_CANONICAL_IDENTITY",
            "LEAD_CUSTOMER_LINK_CONFLICT",
        }
    return {key: reason_counts[key] for key in sorted(keys) if reason_counts.get(key)}


def _empty_report(
    audit_input: AuditInput,
    schema_hash: str,
    input_hash: str,
    reasons: Sequence[str],
) -> Dict[str, Any]:
    return _finalize_report(
        {
            "schema_version": AUDIT_SCHEMA_VERSION,
            "status": "BLOCKED",
            "blocking_reasons": sorted(
                set(reasons)
                | {
                    "QUALIFICATION_RULE_UNFROZEN",
                    "READBACK_PROVENANCE_UNAUDITED",
                }
            ),
            "input_contract_hash": input_hash,
            "source_snapshot_sha256": audit_input.expected_db_sha256,
            "source_schema_hash": schema_hash,
            "versions": {
                "attribution": ATTRIBUTION_VERSION,
                "dedupe": DEDUPE_VERSION,
                "qualification_rule": QUALIFICATION_RULE_VERSION,
            },
            "counts": {
                "candidate_event_count": 0,
                "exact_meta_event_count": 0,
                "exact_identity_event_count": 0,
                "deduped_canonical_identity_count": 0,
            },
            "coverage": {"exact_meta": 0.0, "exact_identity": 0.0},
            "reason_counts": {},
            "missing_reason_counts": {},
            "ambiguous_reason_counts": {},
            "crm_verification_latency_seconds": {
                "count": 0,
                "p50": None,
                "p90": None,
                "p95": None,
                "max": None,
            },
            "row_evidence_hash": _sha256_json([]),
        }
    )


def _finalize_report(report: Dict[str, Any]) -> Dict[str, Any]:
    result = dict(report)
    result["report_hash"] = _sha256_json(result)
    return result


def _audit_snapshot(raw_input: AuditInput) -> Dict[str, Any]:
    """Run a deterministic exact-ID audit over one checkpointed SQLite file."""

    audit_input, start, end = _normalized_input(raw_input)
    complete_utc_day_window = _is_complete_utc_day_window(start, end)
    before_stat = audit_input.db_path.stat()
    before_hash = _sha256_file(audit_input.db_path)
    if before_hash != audit_input.expected_db_sha256:
        raise SourceAuditError("SNAPSHOT_SHA256_MISMATCH")
    before_sidecars = _sidecar_state(audit_input.db_path)
    _require_checkpointed_sidecars(before_sidecars)

    input_contract = {
        "account_id": audit_input.account_id,
        "market": audit_input.market,
        "experiment_ids": list(audit_input.experiment_ids),
        "window_start": audit_input.window_start,
        "window_end": audit_input.window_end,
        "project": audit_input.project,
        "max_events": audit_input.max_events,
        "source_snapshot_sha256": audit_input.expected_db_sha256,
    }
    input_hash = _sha256_json(input_contract)

    with open_readonly_snapshot(audit_input.db_path) as conn:
        _, schema_hash = _source_schema(conn)
        experiments, audit_reasons = _experiment_rows(conn, audit_input)
        by_ad, experiment_reasons = _experiment_index(experiments)
        audit_reasons.extend(experiment_reasons)
        ad_ids = sorted(by_ad)
        facts, fact_limit, fact_reasons = _fact_index(
            conn, audit_input, start, end, ad_ids
        )
        candidates, candidate_limit = _candidate_bind_rows(conn, audit_input, experiments)
        if fact_limit or candidate_limit:
            report = _empty_report(
                audit_input,
                schema_hash,
                input_hash,
                [*audit_reasons, "SOURCE_LIMIT_EXCEEDED"],
            )
        else:
            parsed_candidates: List[Dict[str, Any]] = []
            lead_ids: List[str] = []
            customer_ids: List[str] = []
            for row in candidates:
                lead_id, customer_id, identity_reason = _canonical_keys(row)
                parsed_candidates.append(
                    {
                        "row": row,
                        "lead_id": lead_id,
                        "customer_id": customer_id,
                        "identity_reason": identity_reason,
                    }
                )
                if lead_id:
                    lead_ids.append(lead_id)
                if customer_id:
                    customer_ids.append(customer_id)

            leads = _bulk_rows(
                conn,
                table="leads",
                key="lead_id",
                columns="lead_id,matched_customer_id,crm_verified_at,updated_at",
                values=lead_ids,
                limit=audit_input.max_events,
            )
            projections = _bulk_rows(
                conn,
                table="customer_projection",
                key="customer_id",
                columns="customer_id,lead_id,updated_at",
                values=customer_ids,
                limit=audit_input.max_events,
            )
            derived_customer_ids = [
                _text(item.get("matched_customer_id"))
                for item in leads.values()
                if _text(item.get("matched_customer_id"))
                and _text(item.get("matched_customer_id")) not in projections
            ]
            projections.update(
                _bulk_rows(
                    conn,
                    table="customer_projection",
                    key="customer_id",
                    columns="customer_id,lead_id,updated_at",
                    values=derived_customer_ids,
                    limit=audit_input.max_events,
                )
            )
            derived_lead_ids = [
                _text(item.get("lead_id"))
                for item in projections.values()
                if _text(item.get("lead_id")) and _text(item.get("lead_id")) not in leads
            ]
            leads.update(
                _bulk_rows(
                    conn,
                    table="leads",
                    key="lead_id",
                    columns="lead_id,matched_customer_id,crm_verified_at,updated_at",
                    values=derived_lead_ids,
                    limit=audit_input.max_events,
                )
            )
            all_lead_ids = sorted(
                set(lead_ids)
                | {
                    _text(item.get("lead_id"))
                    for item in projections.values()
                    if _text(item.get("lead_id"))
                }
            )
            all_customer_ids = sorted(
                set(customer_ids)
                | {
                    _text(item.get("matched_customer_id"))
                    for item in leads.values()
                    if _text(item.get("matched_customer_id"))
                }
            )
            customers_by_lead = _reverse_index(
                conn,
                table="customer_projection",
                key="lead_id",
                value="customer_id",
                keys=all_lead_ids,
                limit=audit_input.max_events,
            )
            leads_by_customer = _reverse_index(
                conn,
                table="leads",
                key="matched_customer_id",
                value="lead_id",
                keys=all_customer_ids,
                limit=audit_input.max_events,
            )

            reason_counts: Dict[str, int] = {}
            evidence: List[Dict[str, Any]] = []
            exact_meta_count = 0
            exact_identity_count = 0
            canonical_identities: set[str] = set()
            crm_verification_latencies: List[float] = []

            def record_reason(reason: str) -> None:
                reason_counts[reason] = reason_counts.get(reason, 0) + 1

            for item in parsed_candidates:
                row = item["row"]
                event_hash = _sha256_bytes(_text(row.get("event_id")).encode("utf-8"))
                ad_id = _text(row.get("ad_id"))
                meta_reason = ""
                experiments_for_ad = by_ad.get(ad_id, []) if ad_id else []
                if not _text(row.get("occurred_at_utc") or row.get("updated_at_utc")) and not complete_utc_day_window:
                    meta_reason = "EVENT_TIME_PRECISION_INSUFFICIENT"
                elif not ad_id:
                    meta_reason = "MISSING_META_AD_ID"
                elif not experiments_for_ad:
                    meta_reason = "AD_NOT_IN_EXPERIMENT"
                elif len(experiments_for_ad) != 1:
                    meta_reason = "AMBIGUOUS_EXPERIMENT_AD_ID"
                else:
                    experiment = experiments_for_ad[0]
                    meta_reason = _text(experiment.get("preflight_reason"))
                    expected = (
                        audit_input.account_id,
                        _text(experiment.get("source_campaign_id")),
                        _text(experiment.get("source_adset_id")),
                        ad_id,
                    )
                    fact_lineages = facts.get(ad_id, set())
                    if not meta_reason and ad_id in fact_reasons:
                        meta_reason = fact_reasons[ad_id]
                    elif not meta_reason and not fact_lineages:
                        meta_reason = "AD_NOT_IN_FACTS"
                    elif not meta_reason and len(fact_lineages) != 1:
                        meta_reason = "AMBIGUOUS_FACT_LINEAGE"
                    elif not meta_reason and next(iter(fact_lineages)) != expected:
                        meta_reason = "META_ID_CHAIN_MISMATCH"
                    elif not meta_reason and (
                        _text(row.get("campaign_id")) != expected[1]
                        or _text(row.get("adset_id")) != expected[2]
                    ):
                        meta_reason = (
                            "MISSING_META_ID"
                            if not _text(row.get("campaign_id"))
                            or not _text(row.get("adset_id"))
                            else "META_ID_CHAIN_MISMATCH"
                        )
                if meta_reason:
                    record_reason(meta_reason)
                    evidence.append({"event_hash": event_hash, "reason": meta_reason})
                    continue
                exact_meta_count += 1

                identity_reason = item["identity_reason"]
                lead_id = item["lead_id"]
                customer_id = item["customer_id"]
                lead = leads.get(lead_id) if lead_id else None
                projection = projections.get(customer_id) if customer_id else None
                if not identity_reason and lead_id and not lead:
                    identity_reason = "CANONICAL_IDENTITY_NOT_IN_CRM"
                if not identity_reason and customer_id and not projection:
                    identity_reason = "CANONICAL_IDENTITY_NOT_IN_CRM"
                if not identity_reason and lead and _crm_row_id_state(
                    lead, ("lead_id", "matched_customer_id")
                ) == "invalid":
                    identity_reason = "CANONICAL_IDENTITY_INVALID"
                if not identity_reason and projection and _crm_row_id_state(
                    projection, ("customer_id", "lead_id")
                ) == "invalid":
                    identity_reason = "CANONICAL_IDENTITY_INVALID"
                if not identity_reason and lead and not customer_id:
                    customer_id = _text(lead.get("matched_customer_id"))
                    projection = projections.get(customer_id) if customer_id else None
                if not identity_reason and projection and not lead_id:
                    lead_id = _text(projection.get("lead_id"))
                    lead = leads.get(lead_id) if lead_id else None
                if not identity_reason and (
                    _crm_row_id_state(lead, ("lead_id", "matched_customer_id"))
                    == "invalid"
                    or _crm_row_id_state(projection, ("customer_id", "lead_id"))
                    == "invalid"
                ):
                    identity_reason = "CANONICAL_IDENTITY_INVALID"
                if not identity_reason and (
                    not lead_id
                    or not customer_id
                    or _crm_row_id_state(lead, ("lead_id", "matched_customer_id"))
                    != "valid"
                    or _crm_row_id_state(projection, ("customer_id", "lead_id"))
                    != "valid"
                ):
                    identity_reason = "CANONICAL_IDENTITY_NOT_IN_CRM"
                if not identity_reason and (
                    _text(lead.get("matched_customer_id")) != customer_id
                    or _text(projection.get("lead_id")) != lead_id
                ):
                    identity_reason = "LEAD_CUSTOMER_LINK_CONFLICT"
                if not identity_reason and (
                    len(customers_by_lead.get(lead_id, set())) != 1
                    or len(leads_by_customer.get(customer_id, set())) != 1
                ):
                    identity_reason = "AMBIGUOUS_CANONICAL_IDENTITY"
                if identity_reason:
                    record_reason(identity_reason)
                    evidence.append({"event_hash": event_hash, "reason": identity_reason})
                    continue

                exact_identity_count += 1
                identity_key = f"CUSTOMER_ID:{customer_id}"
                canonical_identities.add(identity_key)
                event_time_raw = _text(row.get("occurred_at_utc") or row.get("updated_at_utc"))
                verified_at_raw = _text(lead.get("crm_verified_at"))
                latency: Optional[float] = None
                if event_time_raw and verified_at_raw:
                    try:
                        event_time = _parse_timestamp(event_time_raw, "EVENT_TIME_INVALID")
                        verified_at = _parse_timestamp(
                            verified_at_raw, "CRM_VERIFICATION_TIME_INVALID"
                        )
                        latency = (verified_at - event_time).total_seconds()
                    except AuditContractError:
                        latency = None
                if latency is None or latency < 0:
                    record_reason("MISSING_CRM_VERIFICATION_TIMESTAMP")
                else:
                    crm_verification_latencies.append(latency)
                evidence.append(
                    {
                        "event_hash": event_hash,
                        "identity_hash": _sha256_bytes(identity_key.encode("utf-8")),
                        "reason": "EXACT_IDENTITY_REACHED",
                        "crm_verification_latency_seconds": round(latency, 3)
                        if latency is not None and latency >= 0
                        else None,
                    }
                )

            blocking_reasons = sorted(
                set(audit_reasons)
                | set(reason_counts)
                | ({"NO_CANDIDATE_EVENTS"} if not candidates else set())
                | {
                    "QUALIFICATION_RULE_UNFROZEN",
                    "READBACK_PROVENANCE_UNAUDITED",
                }
            )
            report = _finalize_report(
                {
                    "schema_version": AUDIT_SCHEMA_VERSION,
                    "status": "BLOCKED" if blocking_reasons else "COMPLETE",
                    "blocking_reasons": blocking_reasons,
                    "input_contract_hash": input_hash,
                    "source_snapshot_sha256": audit_input.expected_db_sha256,
                    "source_schema_hash": schema_hash,
                    "versions": {
                        "attribution": ATTRIBUTION_VERSION,
                        "dedupe": DEDUPE_VERSION,
                        "qualification_rule": QUALIFICATION_RULE_VERSION,
                    },
                    "counts": {
                        "candidate_event_count": len(candidates),
                        "exact_meta_event_count": exact_meta_count,
                        "exact_identity_event_count": exact_identity_count,
                        "deduped_canonical_identity_count": len(canonical_identities),
                    },
                    "coverage": {
                        "exact_meta": _coverage(exact_meta_count, len(candidates)),
                        "exact_identity": _coverage(
                            exact_identity_count, exact_meta_count
                        ),
                    },
                    "reason_counts": {
                        key: reason_counts[key] for key in sorted(reason_counts)
                    },
                    "missing_reason_counts": _reason_bucket(
                        reason_counts, "missing"
                    ),
                    "ambiguous_reason_counts": _reason_bucket(
                        reason_counts, "ambiguous"
                    ),
                    "crm_verification_latency_seconds": {
                        "count": len(crm_verification_latencies),
                        "p50": _percentile(crm_verification_latencies, 0.50),
                        "p90": _percentile(crm_verification_latencies, 0.90),
                        "p95": _percentile(crm_verification_latencies, 0.95),
                        "max": round(max(crm_verification_latencies), 3)
                        if crm_verification_latencies
                        else None,
                    },
                    "row_evidence_hash": _sha256_json(
                        sorted(evidence, key=lambda item: canonical_json(item))
                    ),
                }
            )

    after_stat = audit_input.db_path.stat()
    after_hash = _sha256_file(audit_input.db_path)
    after_sidecars = _sidecar_state(audit_input.db_path)
    if after_sidecars != before_sidecars:
        raise SourceAuditError("SOURCE_SIDECAR_DRIFTED")
    if (
        after_hash != before_hash
        or after_stat.st_mtime_ns != before_stat.st_mtime_ns
        or after_stat.st_size != before_stat.st_size
    ):
        raise SourceAuditError("SOURCE_DRIFTED")
    return report


def audit_snapshot(raw_input: AuditInput) -> Dict[str, Any]:
    """Normalize storage and I/O failures without exposing paths or SQL."""

    try:
        return _audit_snapshot(raw_input)
    except (AuditContractError, SourceAuditError):
        raise
    except sqlite3.Error as exc:
        raise SourceAuditError("SOURCE_SQLITE_ERROR") from exc
    except OSError as exc:
        raise SourceAuditError("SOURCE_IO_ERROR") from exc


def exit_code_for_report(report: Mapping[str, Any]) -> int:
    """Exit 0 means audit completeness only; it never represents Gate PASS."""

    return 0 if report.get("status") == "COMPLETE" and not report.get("blocking_reasons") else 2
