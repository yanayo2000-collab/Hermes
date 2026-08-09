from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote

from app.growth.canonical_evaluation_contracts import canonical_hash, canonical_json, validate_sha256, validate_utc
from app.growth.canonical_evaluation_projection import LegacyProjectionError, project_legacy_evaluation


REQUEST_VERSION = "gle-g1-02a-asof-audit-request-v2"
BUNDLE_VERSION = "gle-g1-02a-asof-audit-bundle-v2"
MANIFEST_VERSION = "gle-g1-02a-asof-audit-manifest-v2"
GENERATOR_VERSION = "gle-g1-02a-asof-audit-engine-v2"
QUERY_CONTRACT_VERSION = "gle-g1-02a-fixed-seven-table-query-v2"
TOKEN_VERSION = "gle-g1-02a-technical-id-token-v1"
MAX_ROWS_PER_TABLE = 50_000
MAX_TOTAL_CANONICAL_BYTES = 64 * 1024 * 1024
MAX_REPORT_PAYLOAD_BYTES = 8 * 1024 * 1024
MAX_TOTAL_REPORT_SOURCE_BYTES = 512 * 1024 * 1024
REPORT_PAYLOAD_COMMITMENT_ALGORITHM = "SHA256_UTF8_TEXT_V1"
REPORT_SOURCE_ROW_COMMITMENT_ALGORITHM = "CANONICAL_SAFE_FIELDS_PLUS_PAYLOAD_COMMITMENT_V1"
DEFAULT_SOURCE_ROW_COMMITMENT_ALGORITHM = "CANONICAL_SELECTED_SOURCE_ROW_V1"
AUDIT_SCOPE = "AVAILABLE_CURRENT_AND_ARCHIVED_GLE_LEGACY"
ALLOWED_EVALUATION_EVENT_TYPES = frozenset({"PERFORMANCE_EVALUATED"})
LEGACY_CHECKPOINT_ROLES = {"D1": "SAFETY_CHECK", "D3": "TREND_ONLY", "D7": "BINDING_EFFECT_DECISION"}
LEGACY_EVIDENCE_FIELDS = {
    "SINGLE_EXPERIMENT": {"baseline_window", "post_window", "baseline_metrics", "post_metrics", "data_quality_status", "dedupe_version", "attribution_version"},
    "CREATIVE_GROUP": {"launch_id", "window", "metrics_by_experiment", "ranking", "winner_experiment_id", "data_quality_status", "legacy_evidence"},
    "AUDIENCE_PAIR": {"launch_id", "metrics", "winner_experiment_id", "legacy_evidence"},
}
LEGACY_EVIDENCE_TYPES = {
    "SINGLE_EXPERIMENT": {
        "baseline_window": "dict", "post_window": "dict", "baseline_metrics": "dict",
        "post_metrics": "dict", "data_quality_status": "str", "dedupe_version": "str",
        "attribution_version": "str",
    },
    "CREATIVE_GROUP": {
        "launch_id": "str", "window": "dict", "metrics_by_experiment": "dict", "ranking": "list",
        "winner_experiment_id": "str", "data_quality_status": "str", "legacy_evidence": "dict",
    },
    "AUDIENCE_PAIR": {
        "launch_id": "str", "metrics": "dict", "winner_experiment_id": "str", "legacy_evidence": "dict",
    },
}


TABLES = (
    {
        "table": "ad_experiment_evaluation", "pk": "evaluation_id", "semantic_at": "evaluated_at", "cutoff_at": "evaluated_at",
        "class": "MUTABLE_CURRENT_ONLY", "projection_kind": "SINGLE_EXPERIMENT",
        "columns": (
            "evaluation_id", "experiment_id", "episode_id", "checkpoint", "baseline_window_json",
            "post_window_json", "baseline_metrics_json", "post_metrics_json", "data_quality_status",
            "dedupe_version", "attribution_version", "evaluation_status", "evaluated_at",
        ),
    },
    {
        "table": "ad_creative_group_evaluation", "pk": "group_evaluation_id", "semantic_at": "evaluated_at", "cutoff_at": "evaluated_at",
        "class": "MUTABLE_CURRENT_ONLY", "projection_kind": "CREATIVE_GROUP",
        "columns": (
            "group_evaluation_id", "launch_id", "checkpoint", "window_json",
            "metrics_by_experiment_json", "ranking_json", "winner_experiment_id", "decision_status",
            "actual_days", "data_quality_status", "evidence_json", "evaluated_at",
        ),
    },
    {
        "table": "ad_audience_pair_evaluation", "pk": "pair_evaluation_id", "semantic_at": "evaluated_at", "cutoff_at": "evaluated_at",
        "class": "MUTABLE_CURRENT_ONLY", "projection_kind": "AUDIENCE_PAIR",
        "columns": (
            "pair_evaluation_id", "launch_id", "checkpoint", "baseline_experiment_id",
            "challenger_experiment_id", "metrics_json", "winner_experiment_id", "decision_status",
            "evidence_json", "evaluated_at",
        ),
    },
    {
        "table": "ad_experiment_events", "pk": "event_id", "semantic_at": "created_at", "cutoff_at": "created_at",
        "class": "APPEND_ONLY_WITH_RETENTION", "columns": (
            "event_id", "experiment_id", "from_state", "to_state", "event_type", "actor", "reason",
            "evidence_json", "created_at",
        ),
    },
    {
        "table": "ad_daily_report", "pk": "report_id", "semantic_at": "generated_at_utc", "cutoff_at": "generated_at_utc",
        "class": "REPLACEABLE_DAILY_FACT", "columns": (
            "report_id", "report_date", "data_mode", "snapshot_version", "rule_version",
            "window_start_utc", "window_end_utc", "generated_at_utc", "payload_json",
        ),
    },
    {
        "table": "ad_creative_group_evaluation_history", "pk": "history_id", "semantic_at": "archived_at", "cutoff_at": "archived_at",
        "class": "ARCHIVED_PREIMAGE_PARTIAL", "columns": (
            "history_id", "group_evaluation_id", "launch_id", "checkpoint", "snapshot_json",
            "archived_reason", "archived_at",
        ),
    },
    {
        "table": "ad_experiment", "pk": "experiment_id", "semantic_at": None, "cutoff_at": "created_at",
        "class": "MUTABLE_CURRENT_ONLY", "columns": (
            "experiment_id", "account_id", "country", "platform", "source_report_id",
            "source_campaign_id", "source_adset_id", "source_ad_id", "source_creative_id",
            "hypothesis_json", "control_definition_json", "state", "created_at", "updated_at",
        ),
    },
)


class HistoricalAsOfAuditError(RuntimeError):
    pass


def _fail(code: str) -> None:
    raise HistoricalAsOfAuditError(code)


def make_request(*, audit_id: str, data_cutoff_at: str, captured_at: str, source_logical_id: str) -> dict[str, Any]:
    for value, code in ((audit_id, "G102_AUDIT_ID_INVALID"), (source_logical_id, "G102_SOURCE_ID_INVALID")):
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value):
            _fail(code)
    try:
        cutoff = validate_utc(data_cutoff_at)
        capture = validate_utc(captured_at)
    except ValueError:
        _fail("G102_REQUEST_TIMESTAMP_INVALID")
    request = {
        "schema_version": REQUEST_VERSION,
        "audit_id": audit_id,
        "scope": AUDIT_SCOPE,
        "data_cutoff_at": cutoff,
        "captured_at": capture,
        "source_logical_id": source_logical_id,
        "query_contract_version": QUERY_CONTRACT_VERSION,
        "generator_version": GENERATOR_VERSION,
    }
    if _utc_instant(request["captured_at"]) < _utc_instant(request["data_cutoff_at"]):
        _fail("G102_CAPTURE_BEFORE_CUTOFF")
    request["request_hash"] = canonical_hash(request)
    return request


def open_readonly_snapshot(database_path: str | os.PathLike[str]) -> sqlite3.Connection:
    path = Path(database_path).expanduser().resolve()
    if not path.is_file():
        _fail("G102_SOURCE_NOT_FOUND")
    conn = sqlite3.connect(f"file:{quote(str(path), safe='/')}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA trusted_schema=OFF")
    conn.create_function(
        "gle_sha256_text", 1,
        lambda value: hashlib.sha256(str(value or "").encode("utf-8")).hexdigest(),
        deterministic=True,
    )
    if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
        conn.close()
        _fail("G102_QUERY_ONLY_NOT_ENFORCED")
    denied = {
        sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE,
        sqlite3.SQLITE_CREATE_INDEX, sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_CREATE_TEMP_INDEX,
        sqlite3.SQLITE_CREATE_TEMP_TABLE, sqlite3.SQLITE_CREATE_TEMP_TRIGGER, sqlite3.SQLITE_CREATE_TEMP_VIEW,
        sqlite3.SQLITE_CREATE_TRIGGER, sqlite3.SQLITE_CREATE_VIEW, sqlite3.SQLITE_DROP_INDEX,
        sqlite3.SQLITE_DROP_TABLE, sqlite3.SQLITE_DROP_TEMP_INDEX, sqlite3.SQLITE_DROP_TEMP_TABLE,
        sqlite3.SQLITE_DROP_TEMP_TRIGGER, sqlite3.SQLITE_DROP_TEMP_VIEW, sqlite3.SQLITE_DROP_TRIGGER,
        sqlite3.SQLITE_DROP_VIEW, sqlite3.SQLITE_ALTER_TABLE, sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH,
    }
    conn.set_authorizer(lambda action, _a, _b, _c, _d: sqlite3.SQLITE_DENY if action in denied else sqlite3.SQLITE_OK)
    return conn


def build_audit(conn: sqlite3.Connection, request: Mapping[str, Any], *, source_path: str | os.PathLike[str]) -> dict[str, Any]:
    request = _validate_request(request)
    if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
        _fail("G102_QUERY_ONLY_NOT_ENFORCED")
    source_resolved = Path(source_path).expanduser().resolve()
    database_list = list(conn.execute("PRAGMA database_list"))
    database_files = [Path(str(row[2])).resolve() for row in database_list if str(row[1]) == "main"]
    if database_files != [source_resolved] or {str(row[1]) for row in database_list} != {"main"}:
        _fail("G102_SOURCE_PATH_MISMATCH")
    before = _source_stat(source_path)
    total_changes = conn.total_changes
    records: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    manifests: list[dict[str, Any]] = []
    total_bytes = 0
    event_evaluation_refs: set[tuple[str, str, str]] = set()
    conn.execute("BEGIN")
    try:
        schema_rows = list(conn.execute("SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name LIMIT 1001"))
        if len(schema_rows) > 1000:
            _fail("G102_SOURCE_BOUND_EXCEEDED:schema")
        available = {str(row["name"]) for row in schema_rows}
        schema_columns_by_table: dict[str, list[str]] = {}
        for descriptor in TABLES:
            if descriptor["table"] not in available:
                _fail(f"G102_REQUIRED_TABLE_MISSING:{descriptor['table']}")
            table_info = list(conn.execute(f"PRAGMA table_info({descriptor['table']})"))
            actual_columns = [str(row[1]) for row in table_info]
            missing = sorted(set(descriptor["columns"]) - set(actual_columns))
            if missing:
                _fail(f"G102_REQUIRED_COLUMN_MISSING:{descriptor['table']}:{','.join(missing)}")
            primary_key_columns = [
                str(row[1]) for row in sorted(table_info, key=lambda row: int(row[5])) if int(row[5]) > 0
            ]
            if primary_key_columns != [descriptor["pk"]]:
                _fail(f"G102_PRIMARY_KEY_MISMATCH:{descriptor['table']}")
            schema_columns_by_table[descriptor["table"]] = actual_columns
        schema_fingerprint = canonical_hash([{"name": row["name"], "sql": row["sql"]} for row in schema_rows])
        data_version_before = int(conn.execute("PRAGMA data_version").fetchone()[0])
        for descriptor in TABLES:
            actual_columns = schema_columns_by_table[descriptor["table"]]
            rows, post_cutoff, invalid_timestamps, physical_count, read_bytes, large_field_summary = _read_table(
                conn, descriptor, request["data_cutoff_at"]
            )
            total_bytes += read_bytes
            if total_bytes > MAX_TOTAL_CANONICAL_BYTES:
                _fail("G102_SOURCE_BOUND_EXCEEDED:total_bytes")
            row_hashes: list[str] = []
            projected_hashes: list[str] = []
            for row, cutoff_disposition in rows:
                raw = dict(row)
                row_hash = canonical_hash(raw)
                row_hashes.append(row_hash)
                envelope, row_gaps = _project_row(descriptor, raw, row_hash, cutoff_disposition)
                records.append(envelope)
                projected_hashes.append(envelope["record_hash"])
                gaps.extend(row_gaps)
                if descriptor["table"] == "ad_experiment_events":
                    projection = envelope["projection"]
                    if (
                        projection["evidence_status"] == "PRESENT"
                        and projection["event_type"] in ALLOWED_EVALUATION_EVENT_TYPES
                        and projection["evaluation_id"]
                        and envelope["cutoff_disposition"] != "SOURCE_TIMESTAMP_INVALID"
                    ):
                        event_evaluation_refs.add((
                            projection["evaluation_id"], projection["experiment_id"],
                            str(envelope["semantic_at"] or ""),
                        ))
            query_contract = _query_contract(descriptor, request["data_cutoff_at"])
            manifest = {
                "table": descriptor["table"], "semantic_class": descriptor["class"],
                "primary_key": descriptor["pk"], "semantic_time_field": descriptor["semantic_at"],
                "source_columns": list(descriptor["columns"]),
                "materialized_columns": _materialized_columns(descriptor),
                "source_row_commitment_algorithm": _source_row_commitment_algorithm(descriptor),
                "large_field_summary": large_field_summary,
                "schema_columns": actual_columns,
                "query_contract_hash": canonical_hash(query_contract),
                "schema_hash": canonical_hash(actual_columns), "physical_count": physical_count,
                "captured_count": len(rows), "post_cutoff_count": post_cutoff,
                "invalid_timestamp_count": invalid_timestamps,
                "first_key": str(rows[0][0][descriptor["pk"]]) if rows else None,
                "last_key": str(rows[-1][0][descriptor["pk"]]) if rows else None,
                "row_chain_hash": canonical_hash(row_hashes), "projection_chain_hash": canonical_hash(projected_hashes),
            }
            manifest["table_manifest_hash"] = canonical_hash(manifest)
            manifests.append(manifest)
        for record in records:
            if record["source_table"] == "ad_experiment_evaluation":
                if record["projection"].get("status") == "INVALID_LEGACY_PROJECTION":
                    continue
                subjects = record["projection"].get("subject_experiment_ids") or []
                exact_refs = [item for item in event_evaluation_refs if item[:2] == (
                    record["source_id"], subjects[0] if len(subjects) == 1 else ""
                )]
                if (
                    record["cutoff_disposition"] == "SOURCE_TIMESTAMP_INVALID"
                    or not exact_refs
                    or all(_utc_instant(item[2]) < _utc_instant(str(record["semantic_at"])) for item in exact_refs)
                ):
                    _add_record_reason(record, "EXACT_EVALUATION_EVENT_REF_MISSING")
        gaps = []
        for record in records:
            gaps.extend(_gap(code, "INCOMPLETE", record) for code in record["reason_codes"])
        gaps.extend(_source_gap(_structural_gap_code(descriptor), descriptor["table"]) for descriptor in TABLES)
        grouped_records = {
            descriptor["table"]: sorted(
                (item for item in records if item["source_table"] == descriptor["table"]),
                key=lambda item: item["source_id"],
            )
            for descriptor in TABLES
        }
        for manifest in manifests:
            table_records = grouped_records[manifest["table"]]
            manifest["first_key"] = table_records[0]["source_id"] if table_records else None
            manifest["last_key"] = table_records[-1]["source_id"] if table_records else None
            manifest["row_chain_hash"] = canonical_hash([item["source_row_hash"] for item in table_records])
            manifest["projection_chain_hash"] = canonical_hash([item["record_hash"] for item in table_records])
            manifest["table_manifest_hash"] = canonical_hash({
                key: value for key, value in manifest.items() if key != "table_manifest_hash"
            })
        data_version_after = int(conn.execute("PRAGMA data_version").fetchone()[0])
    finally:
        conn.rollback()
    if conn.total_changes != total_changes:
        _fail("G102_SOURCE_WRITE_DETECTED")
    after = _source_stat(source_path)
    records.sort(key=lambda item: (item["source_table"], item["source_id"]))
    gaps = _dedupe_gaps(gaps)
    source_snapshot = {
        "source_logical_id": request["source_logical_id"], "schema_fingerprint": schema_fingerprint,
        "query_contract_version": QUERY_CONTRACT_VERSION, "data_cutoff_at": request["data_cutoff_at"],
        "data_version_before": data_version_before, "data_version_after": data_version_after,
        "stat_before": before, "stat_after": after, "source_drifted": before != after,
        "table_chain_hash": canonical_hash([item["table_manifest_hash"] for item in manifests]),
        "authoritative_asof_hash": _authoritative_asof_hash(request, schema_fingerprint, manifests),
        "trust_status": "UNSIGNED_LOCAL_CAPTURE",
    }
    source_snapshot["source_snapshot_hash"] = canonical_hash(source_snapshot)
    coverage = _coverage(manifests, records, gaps)
    bundle = {
        "schema_version": BUNDLE_VERSION, "request": request, "source_snapshot": source_snapshot,
        "table_manifests": manifests, "records": records, "gaps": gaps, "coverage": coverage,
        "status": "INCOMPLETE", "replay_eligibility": "AUDIT_ONLY",
        "not_replay_receipt": True, "trust_status": "UNSIGNED_LOCAL_CAPTURE",
    }
    bundle["bundle_hash"] = canonical_hash(bundle)
    return validate_audit_bundle(bundle)


def validate_audit_bundle(value: Mapping[str, Any]) -> dict[str, Any]:
    expected_keys = {
        "schema_version", "request", "source_snapshot", "table_manifests", "records", "gaps",
        "coverage", "status", "replay_eligibility", "not_replay_receipt", "trust_status", "bundle_hash",
    }
    if not isinstance(value, Mapping) or set(value) != expected_keys or value["schema_version"] != BUNDLE_VERSION:
        _fail("G102_BUNDLE_SCHEMA_INVALID")
    body = dict(value)
    request = _validate_request(body["request"])
    source = body["source_snapshot"]
    source_keys = {
        "source_logical_id", "schema_fingerprint", "query_contract_version", "data_cutoff_at",
        "data_version_before", "data_version_after", "stat_before", "stat_after", "source_drifted",
        "table_chain_hash", "trust_status", "source_snapshot_hash",
        "authoritative_asof_hash",
    }
    if not isinstance(source, Mapping) or set(source) != source_keys:
        _fail("G102_SOURCE_SCHEMA_INVALID")
    validate_sha256(source.get("source_snapshot_hash"), code="G102_SOURCE_HASH_INVALID")
    if source["source_snapshot_hash"] != canonical_hash({key: val for key, val in source.items() if key != "source_snapshot_hash"}):
        _fail("G102_SOURCE_HASH_MISMATCH")
    if (
        source["source_logical_id"] != request["source_logical_id"]
        or source["query_contract_version"] != request["query_contract_version"]
        or source["data_cutoff_at"] != request["data_cutoff_at"]
        or source["trust_status"] != "UNSIGNED_LOCAL_CAPTURE"
    ):
        _fail("G102_SOURCE_REQUEST_BINDING_MISMATCH")
    expected_tables = [item["table"] for item in TABLES]
    if [item.get("table") for item in body["table_manifests"]] != expected_tables:
        _fail("G102_TABLE_MANIFEST_SET_INVALID")
    record_groups: dict[str, list[dict[str, Any]]] = {table: [] for table in expected_tables}
    identities: set[tuple[str, str]] = set()
    for item in body["records"]:
        _validate_record(item)
        identity = (str(item.get("source_table") or ""), str(item.get("source_id") or ""))
        if identity in identities or identity[0] not in record_groups or not identity[1]:
            _fail("G102_RECORD_IDENTITY_INVALID")
        identities.add(identity)
        record_groups[identity[0]].append(item)
    for item in body["records"]:
        if set(item["reason_codes"]) != _required_record_reasons(item, body["records"]):
            _fail("G102_RECORD_REASONS_INCONSISTENT")
    for descriptor, item in zip(TABLES, body["table_manifests"]):
        _validate_table_manifest_shape(item)
        if item.get("table_manifest_hash") != canonical_hash({key: val for key, val in item.items() if key != "table_manifest_hash"}):
            _fail("G102_TABLE_MANIFEST_HASH_MISMATCH")
        rows = sorted(record_groups[item["table"]], key=lambda row: row["source_id"])
        if item["captured_count"] != len(rows) or item["physical_count"] != item["captured_count"] + item["post_cutoff_count"]:
            _fail("G102_TABLE_COUNT_MISMATCH")
        if item["invalid_timestamp_count"] > item["captured_count"]:
            _fail("G102_TABLE_COUNT_MISMATCH")
        if item["invalid_timestamp_count"] != sum(row["cutoff_disposition"] == "SOURCE_TIMESTAMP_INVALID" for row in rows):
            _fail("G102_TABLE_COUNT_MISMATCH")
        if item["semantic_class"] != descriptor["class"] or item["primary_key"] != descriptor["pk"]:
            _fail("G102_TABLE_DESCRIPTOR_MISMATCH")
        if (
            item["semantic_time_field"] != descriptor["semantic_at"]
            or item["source_columns"] != list(descriptor["columns"])
            or item["materialized_columns"] != _materialized_columns(descriptor)
            or item["source_row_commitment_algorithm"] != _source_row_commitment_algorithm(descriptor)
        ):
            _fail("G102_TABLE_DESCRIPTOR_MISMATCH")
        _validate_large_field_summary(item, descriptor)
        if item["schema_hash"] != canonical_hash(item["schema_columns"]):
            _fail("G102_TABLE_SCHEMA_HASH_MISMATCH")
        if item["query_contract_hash"] != canonical_hash(_query_contract(descriptor, request["data_cutoff_at"])):
            _fail("G102_QUERY_CONTRACT_HASH_MISMATCH")
        expected_first = rows[0]["source_id"] if rows else None
        expected_last = rows[-1]["source_id"] if rows else None
        if item["first_key"] != expected_first or item["last_key"] != expected_last:
            _fail("G102_TABLE_KEY_RANGE_MISMATCH")
        if item["row_chain_hash"] != canonical_hash([row["source_row_hash"] for row in rows]):
            _fail("G102_TABLE_ROW_CHAIN_MISMATCH")
        if item["projection_chain_hash"] != canonical_hash([row["record_hash"] for row in rows]):
            _fail("G102_TABLE_PROJECTION_CHAIN_MISMATCH")
    if source["table_chain_hash"] != canonical_hash([item["table_manifest_hash"] for item in body["table_manifests"]]):
        _fail("G102_SOURCE_TABLE_CHAIN_MISMATCH")
    if source["authoritative_asof_hash"] != _authoritative_asof_hash(request, source["schema_fingerprint"], body["table_manifests"]):
        _fail("G102_AUTHORITATIVE_ASOF_HASH_MISMATCH")
    record_gap_codes: dict[tuple[str, str], set[str]] = {identity: set() for identity in identities}
    source_gap_codes: dict[str, set[str]] = {table: set() for table in expected_tables}
    gap_ids: set[str] = set()
    for item in body["gaps"]:
        _validate_gap_shape(item)
        if item.get("gap_id") != canonical_hash({key: val for key, val in item.items() if key != "gap_id"}):
            _fail("G102_GAP_HASH_MISMATCH")
        if item["gap_id"] in gap_ids:
            _fail("G102_GAP_DUPLICATE")
        gap_ids.add(item["gap_id"])
        if item["scope"] == "RECORD":
            identity = (item["source_table"], item["source_id"])
            if identity not in identities:
                _fail("G102_GAP_RECORD_MISMATCH")
            record_gap_codes[identity].add(item["code"])
        else:
            if item["source_table"] not in expected_tables:
                _fail("G102_GAP_SOURCE_MISMATCH")
            source_gap_codes[item["source_table"]].add(item["code"])
    for item in body["records"]:
        if set(item["reason_codes"]) != record_gap_codes[(item["source_table"], item["source_id"])]:
            _fail("G102_RECORD_GAP_CLOSURE_MISMATCH")
    for descriptor in TABLES:
        if source_gap_codes[descriptor["table"]] != {_structural_gap_code(descriptor)}:
            _fail("G102_SOURCE_GAP_CLOSURE_MISMATCH")
    expected_coverage = _coverage(body["table_manifests"], body["records"], body["gaps"])
    if body["coverage"] != expected_coverage:
        _fail("G102_COVERAGE_MISMATCH")
    if (
        body["status"] != "INCOMPLETE" or body["replay_eligibility"] != "AUDIT_ONLY"
        or body["not_replay_receipt"] is not True or body["trust_status"] != "UNSIGNED_LOCAL_CAPTURE"
    ):
        _fail("G102_STATUS_LATTICE_INVALID")
    expected = canonical_hash({key: val for key, val in body.items() if key != "bundle_hash"})
    if body["bundle_hash"] != expected:
        _fail("G102_BUNDLE_HASH_MISMATCH")
    return body


def write_audit_bundle(bundle: Mapping[str, Any], output_dir: str | os.PathLike[str]) -> dict[str, Any]:
    bundle = validate_audit_bundle(bundle)
    output = Path(output_dir)
    if output.exists():
        _fail("G102_OUTPUT_EXISTS")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=output.parent))
    try:
        payloads = {
            "records.ndjson": "".join(canonical_json(item) + "\n" for item in bundle["records"]).encode(),
            "gaps.ndjson": "".join(canonical_json(item) + "\n" for item in bundle["gaps"]).encode(),
            "coverage.json": (canonical_json(bundle["coverage"]) + "\n").encode(),
        }
        files: dict[str, Any] = {}
        for name, payload in payloads.items():
            (temporary / name).write_bytes(payload)
            files[name] = {"sha256": hashlib.sha256(payload).hexdigest(), "size_bytes": len(payload)}
        manifest = {
            "schema_version": MANIFEST_VERSION, "audit_id": bundle["request"]["audit_id"],
            "data_cutoff_at": bundle["request"]["data_cutoff_at"], "bundle_hash": bundle["bundle_hash"],
            "source_snapshot_hash": bundle["source_snapshot"]["source_snapshot_hash"],
            "request": bundle["request"], "source_snapshot": bundle["source_snapshot"],
            "table_manifests": bundle["table_manifests"], "coverage": bundle["coverage"],
            "status": bundle["status"], "replay_eligibility": bundle["replay_eligibility"],
            "not_replay_receipt": bundle["not_replay_receipt"], "trust_status": bundle["trust_status"],
            "files": files,
        }
        manifest["manifest_hash"] = canonical_hash(manifest)
        (temporary / "manifest.json").write_text(canonical_json(manifest) + "\n", encoding="utf-8")
        temporary.replace(output)
        return manifest
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def _validate_request(value: Mapping[str, Any]) -> dict[str, Any]:
    expected = {
        "schema_version", "audit_id", "scope", "data_cutoff_at", "captured_at", "source_logical_id",
        "query_contract_version", "generator_version", "request_hash",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        _fail("G102_REQUEST_SCHEMA_INVALID")
    body = dict(value)
    if body["schema_version"] != REQUEST_VERSION or body["scope"] != AUDIT_SCOPE:
        _fail("G102_REQUEST_VERSION_INVALID")
    if body["query_contract_version"] != QUERY_CONTRACT_VERSION or body["generator_version"] != GENERATOR_VERSION:
        _fail("G102_REQUEST_VERSION_INVALID")
    for field in ("audit_id", "source_logical_id"):
        if not isinstance(body[field], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", body[field]):
            _fail("G102_REQUEST_ID_INVALID")
    try:
        validate_utc(body["data_cutoff_at"]); validate_utc(body["captured_at"])
    except ValueError:
        _fail("G102_REQUEST_TIMESTAMP_INVALID")
    if _utc_instant(body["captured_at"]) < _utc_instant(body["data_cutoff_at"]):
        _fail("G102_CAPTURE_BEFORE_CUTOFF")
    expected_hash = canonical_hash({key: val for key, val in body.items() if key != "request_hash"})
    if body["request_hash"] != expected_hash:
        _fail("G102_REQUEST_HASH_MISMATCH")
    return body


def _read_table(
    conn: sqlite3.Connection, descriptor: Mapping[str, Any], cutoff: str
) -> tuple[list[tuple[sqlite3.Row, str]], int, int, int, int, dict[str, Any] | None]:
    columns = ",".join(descriptor["columns"])
    table = descriptor["table"]
    pk = descriptor["pk"]
    cutoff_at = descriptor["cutoff_at"]
    parameters: tuple[Any, ...] = (MAX_ROWS_PER_TABLE + 1,)
    large_field_summary: dict[str, Any] | None = None
    if table == "ad_daily_report":
        source_count = 0
        total_source_bytes = 0
        maximum_source_row_bytes = 0
        preflight_rows = conn.execute(
            "SELECT typeof(payload_json), length(CAST(payload_json AS BLOB)) "
            "FROM ad_daily_report ORDER BY report_id LIMIT ?",
            (MAX_ROWS_PER_TABLE + 1,),
        )
        for storage_class, source_row_bytes in preflight_rows:
            source_count += 1
            if source_count > MAX_ROWS_PER_TABLE:
                _fail(f"G102_SOURCE_BOUND_EXCEEDED:{table}")
            if storage_class != "text" or type(source_row_bytes) is not int or source_row_bytes < 0:
                _fail(f"G102_SOURCE_BOUND_EXCEEDED:{table}:payload_storage")
            if source_row_bytes > MAX_REPORT_PAYLOAD_BYTES:
                _fail(f"G102_SOURCE_BOUND_EXCEEDED:{table}:payload")
            total_source_bytes += source_row_bytes
            maximum_source_row_bytes = max(maximum_source_row_bytes, source_row_bytes)
            if total_source_bytes > MAX_TOTAL_REPORT_SOURCE_BYTES:
                _fail(f"G102_SOURCE_BOUND_EXCEEDED:{table}:total_payload")
        large_field_summary = {
            "source_field": "payload_json",
            "source_row_count": source_count,
            "total_source_bytes": total_source_bytes,
            "maximum_source_row_bytes": maximum_source_row_bytes,
            "maximum_allowed_total_source_bytes": MAX_TOTAL_REPORT_SOURCE_BYTES,
            "maximum_allowed_source_row_bytes": MAX_REPORT_PAYLOAD_BYTES,
            "required_storage_class": "text",
            "payload_commitment_algorithm": REPORT_PAYLOAD_COMMITMENT_ALGORITHM,
            "source_row_commitment_algorithm": REPORT_SOURCE_ROW_COMMITMENT_ALGORITHM,
        }
        safe_columns = ",".join(item for item in descriptor["columns"] if item != "payload_json")
        columns = (
            f"{safe_columns},"
            "CASE WHEN typeof(payload_json) = 'text' AND length(CAST(payload_json AS BLOB)) <= ? "
            "THEN gle_sha256_text(payload_json) ELSE NULL END AS payload_sha256,"
            "length(CAST(payload_json AS BLOB)) AS payload_size_bytes,"
            "typeof(payload_json) AS payload_storage_class"
        )
        parameters = (MAX_REPORT_PAYLOAD_BYTES, MAX_ROWS_PER_TABLE + 1)
    rows = list(conn.execute(
        f"SELECT {columns} FROM {table} ORDER BY {pk} LIMIT ?", parameters
    ))
    if len(rows) > MAX_ROWS_PER_TABLE:
        _fail(f"G102_SOURCE_BOUND_EXCEEDED:{table}")
    cutoff_instant = _utc_instant(cutoff)
    captured: list[tuple[sqlite3.Row, str]] = []
    post = 0
    invalid = 0
    read_bytes = 0
    for row in rows:
        if table == "ad_daily_report":
            payload_size = row["payload_size_bytes"]
            payload_sha = row["payload_sha256"]
            if (
                row["payload_storage_class"] != "text"
                or type(payload_size) is not int or payload_size < 0
                or payload_size > MAX_REPORT_PAYLOAD_BYTES
                or not isinstance(payload_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", payload_sha)
            ):
                _fail(f"G102_SOURCE_BOUND_EXCEEDED:{table}:payload")
        read_bytes += len(canonical_json(dict(row)).encode("utf-8"))
        if read_bytes > MAX_TOTAL_CANONICAL_BYTES:
            _fail(f"G102_SOURCE_BOUND_EXCEEDED:{table}:bytes")
        try:
            instant = _utc_instant(row[cutoff_at])
        except HistoricalAsOfAuditError:
            captured.append((row, "SOURCE_TIMESTAMP_INVALID"))
            invalid += 1
            continue
        if instant <= cutoff_instant:
            captured.append((row, "AT_OR_BEFORE_CUTOFF"))
        else:
            post += 1
    return captured, post, invalid, len(rows), read_bytes, large_field_summary


def _project_row(
    descriptor: Mapping[str, Any], raw: Mapping[str, Any], row_hash: str, cutoff_disposition: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    table = descriptor["table"]
    source_id = str(raw[descriptor["pk"]])
    reason_codes: list[str] = []
    if descriptor.get("projection_kind"):
        try:
            projection = _safe_legacy_projection(
                project_legacy_evaluation(descriptor["projection_kind"], raw),
                episode_present=bool(str(raw.get("episode_id") or "")) if table == "ad_experiment_evaluation" else None,
            )
            reason_codes.extend(projection["reason_codes"])
        except LegacyProjectionError as exc:
            projection = {"status": "INVALID_LEGACY_PROJECTION", "error_code": str(exc).split(":", 1)[0]}
            reason_codes.append("LEGACY_PROJECTION_INVALID")
        reason_codes.extend(["OBJECTIVE_INCOMPATIBLE", "MUTATION_PROVENANCE_MISSING"])
        reason_codes.append("MUTABLE_ROW_PREIMAGE_UNAVAILABLE")
        if table == "ad_experiment_evaluation" and projection.get("status") != "INVALID_LEGACY_PROJECTION" and not str(raw.get("episode_id") or ""):
            reason_codes.append("EPISODE_ID_MISSING")
    elif table == "ad_experiment_events":
        evidence, evidence_status = _json_object_status(raw.get("evidence_json"))
        projection = {
            "experiment_id": str(raw.get("experiment_id") or ""), "from_state": str(raw.get("from_state") or ""),
            "to_state": str(raw.get("to_state") or ""), "event_type": str(raw.get("event_type") or ""),
            "evaluation_id": str(evidence.get("evaluation_id") or ""), "evidence_status": evidence_status,
            "evidence_hash": canonical_hash(evidence) if evidence_status == "PRESENT" else None,
        }
        if evidence_status != "PRESENT":
            reason_codes.append(f"EVENT_EVIDENCE_{evidence_status}")
        if projection["event_type"] in ALLOWED_EVALUATION_EVENT_TYPES and not projection["evaluation_id"]:
            reason_codes.append("EVENT_EVALUATION_ID_MISSING")
        if not projection["experiment_id"]:
            reason_codes.append("EVENT_EXPERIMENT_ID_MISSING")
    elif table == "ad_daily_report":
        payload_hash = str(raw.get("payload_sha256") or "")
        try:
            validate_sha256(payload_hash, code="G102_REPORT_PAYLOAD_COMMITMENT_INVALID")
        except ValueError as exc:
            raise HistoricalAsOfAuditError(str(exc)) from exc
        projection = {
            "report_date": str(raw.get("report_date") or ""), "data_mode": str(raw.get("data_mode") or ""),
            "snapshot_version": str(raw.get("snapshot_version") or ""), "rule_version": str(raw.get("rule_version") or ""),
            "window_start_utc": str(raw.get("window_start_utc") or ""), "window_end_utc": str(raw.get("window_end_utc") or ""),
            "payload_hash": payload_hash,
        }
        reason_codes.append("REPLACEABLE_REPORT_PREIMAGE_UNAVAILABLE")
    elif table == "ad_creative_group_evaluation_history":
        snapshot, snapshot_status = _json_object_status(raw.get("snapshot_json"))
        projection = {
            "group_evaluation_id": str(raw.get("group_evaluation_id") or ""),
            "launch_token": _token("launch", raw.get("launch_id")),
            "checkpoint": str(raw.get("checkpoint") or ""),
            "snapshot_status": snapshot_status,
            "snapshot_hash": canonical_hash(snapshot) if snapshot_status == "PRESENT" else None,
            "archived_reason_hash": hashlib.sha256(str(raw.get("archived_reason") or "").encode()).hexdigest(),
        }
        reason_codes.append("ARCHIVED_PREIMAGE_PARTIAL")
        if snapshot_status != "PRESENT":
            reason_codes.append(f"ARCHIVED_SNAPSHOT_{snapshot_status}")
    else:
        projection = {
            "experiment_id": source_id, "account_token": _token("account", raw.get("account_id")),
            "market": str(raw.get("country") or ""), "platform": str(raw.get("platform") or ""),
            "launch_token": _token("launch", raw.get("source_report_id")),
            "campaign_token": _token("campaign", raw.get("source_campaign_id")),
            "adset_token": _token("adset", raw.get("source_adset_id")),
            "ad_token": _token("ad", raw.get("source_ad_id")), "creative_token": _token("creative", raw.get("source_creative_id")),
            "current_state": str(raw.get("state") or ""), "created_at": str(raw.get("created_at") or ""),
            "updated_at": str(raw.get("updated_at") or ""),
            "hypothesis_hash": hashlib.sha256(str(raw.get("hypothesis_json") or "").encode()).hexdigest(),
            "control_hash": hashlib.sha256(str(raw.get("control_definition_json") or "").encode()).hexdigest(),
        }
        reason_codes.append("MUTABLE_CURRENT_STATE_NO_PREIMAGE")
    if cutoff_disposition == "SOURCE_TIMESTAMP_INVALID":
        disposition = cutoff_disposition
        reason_codes.append("SOURCE_TIMESTAMP_INVALID")
    elif descriptor["semantic_at"] is None:
        disposition = "CURRENT_ONLY_NOT_ASOF"
    elif descriptor["class"] == "APPEND_ONLY_WITH_RETENTION":
        disposition = "AT_OR_BEFORE_CUTOFF_RETAINED_HISTORY_ONLY"
        reason_codes.append("RETENTION_COMPLETENESS_UNKNOWN")
    else:
        disposition = "TIMESTAMP_ELIGIBLE_CURRENT_CONTENT_UNVERIFIED"
    envelope = {
        "source_table": table, "source_id": source_id, "source_row_hash": row_hash,
        "semantic_at": str(raw.get(descriptor["semantic_at"]) or "") if descriptor["semantic_at"] else None,
        "cutoff_disposition": disposition,
        "reconstruction_status": "INCOMPLETE", "projection": projection,
        "reason_codes": sorted(set(reason_codes)),
    }
    envelope["record_hash"] = canonical_hash(envelope)
    row_gaps = [_gap(code, "INCOMPLETE", envelope) for code in envelope["reason_codes"]]
    return envelope, row_gaps


def _gap(code: str, severity: str, record: Mapping[str, Any]) -> dict[str, Any]:
    gap = {
        "scope": "RECORD", "code": code, "severity": severity, "source_table": record["source_table"],
        "source_id": record["source_id"], "record_hash": record["record_hash"],
        "reconstructibility": "UNKNOWN", "guessed_value": None,
    }
    gap["gap_id"] = canonical_hash(gap)
    return gap


def _source_gap(code: str, table: str) -> dict[str, Any]:
    gap = {
        "scope": "SOURCE", "code": code, "severity": "STRUCTURAL",
        "source_table": table, "source_id": None, "record_hash": None,
        "reconstructibility": "UNKNOWN", "guessed_value": None,
    }
    gap["gap_id"] = canonical_hash(gap)
    return gap


def _dedupe_gaps(gaps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = {item["gap_id"]: item for item in gaps}
    return [result[key] for key in sorted(result)]


def _utc_instant(value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        _fail("G102_SOURCE_TIMESTAMP_INVALID")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError:
        _fail("G102_SOURCE_TIMESTAMP_INVALID")
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        _fail("G102_SOURCE_TIMESTAMP_INVALID")
    return parsed.astimezone(timezone.utc)


def _safe_legacy_projection(projection: Mapping[str, Any], *, episode_present: bool | None) -> dict[str, Any]:
    evidence_summary: dict[str, Any] = {}
    for field, wrapper in projection["evidence"].items():
        if wrapper["status"] == "MISSING":
            evidence_summary[field] = {"status": "MISSING", "value_hash": None, "value_type": None}
            continue
        value = wrapper["value"]
        if field == "launch_id":
            safe_value: Any = _token("launch", value)
        else:
            safe_value = None
        evidence_summary[field] = {
            "status": "PRESENT", "value_hash": canonical_hash(value),
            "value_type": type(value).__name__, "safe_value": safe_value,
        }
    return {
        "schema_version": "gle-g1-02a-redacted-legacy-projection-v1",
        "source_kind": projection["source_kind"], "source_id": projection["source_id"],
        "subject_experiment_ids": projection["subject_experiment_ids"],
        "legacy_checkpoint": projection["legacy_checkpoint"],
        "checkpoint_role_hint": projection["checkpoint_role_hint"],
        "legacy_status": projection["legacy_status"], "evidence_summary": evidence_summary,
        "episode_status": "PRESENT" if episode_present is True else ("MISSING" if episode_present is False else "NOT_APPLICABLE"),
        "missing_fields": projection["missing_fields"], "evaluated_at": projection["evaluated_at"],
        "lineage_status": projection["lineage_status"], "split": projection["split"],
        "binding_eligible": False, "causal_classification": "OBSERVATIONAL_ONLY",
        "reason_codes": projection["reason_codes"], "source_projection_hash": projection["projection_hash"],
    }


def _add_record_reason(record: dict[str, Any], code: str) -> None:
    record["reason_codes"] = sorted(set(record["reason_codes"] + [code]))
    record["record_hash"] = canonical_hash({key: value for key, value in record.items() if key != "record_hash"})


def _structural_gap_code(descriptor: Mapping[str, Any]) -> str:
    return {
        "MUTABLE_CURRENT_ONLY": "MUTABLE_SOURCE_PREIMAGE_COMPLETENESS_UNKNOWN",
        "APPEND_ONLY_WITH_RETENTION": "RETENTION_COMPLETENESS_UNKNOWN",
        "REPLACEABLE_DAILY_FACT": "REPLACEABLE_SOURCE_NO_PREIMAGE",
        "ARCHIVED_PREIMAGE_PARTIAL": "ARCHIVED_PREIMAGE_COVERAGE_UNKNOWN",
    }[descriptor["class"]]


def _materialized_columns(descriptor: Mapping[str, Any]) -> list[str]:
    if descriptor["table"] != "ad_daily_report":
        return list(descriptor["columns"])
    return [
        *(item for item in descriptor["columns"] if item != "payload_json"),
        "payload_sha256", "payload_size_bytes", "payload_storage_class",
    ]


def _source_row_commitment_algorithm(descriptor: Mapping[str, Any]) -> str:
    return (
        REPORT_SOURCE_ROW_COMMITMENT_ALGORITHM
        if descriptor["table"] == "ad_daily_report"
        else DEFAULT_SOURCE_ROW_COMMITMENT_ALGORITHM
    )


def _query_contract(descriptor: Mapping[str, Any], cutoff: str) -> dict[str, Any]:
    contract = {
        "table": descriptor["table"],
        "source_columns": list(descriptor["columns"]),
        "materialized_columns": _materialized_columns(descriptor),
        "source_row_commitment_algorithm": _source_row_commitment_algorithm(descriptor),
        "pk": descriptor["pk"], "semantic_at": descriptor["semantic_at"],
        "cutoff_at": descriptor["cutoff_at"], "cutoff": cutoff,
        "ordering": [descriptor["pk"]], "limit": MAX_ROWS_PER_TABLE + 1,
        "timestamp_classification": "PYTHON_UTC_INSTANT_V1",
    }
    if descriptor["table"] == "ad_daily_report":
        contract["large_field_commitment"] = {
            "source_field": "payload_json",
            "algorithm": REPORT_PAYLOAD_COMMITMENT_ALGORITHM,
            "size_algorithm": "SQLITE_CAST_BLOB_LENGTH_V1",
            "preflight_algorithm": "ORDERED_LAZY_TYPE_LENGTH_ACCUMULATION_BEFORE_HASH_V1",
            "preflight_observed_fields": [
                "source_row_count", "total_source_bytes", "maximum_source_row_bytes",
            ],
            "maximum_source_row_bytes": MAX_REPORT_PAYLOAD_BYTES,
            "maximum_total_source_bytes": MAX_TOTAL_REPORT_SOURCE_BYTES,
            "required_storage_class": "text",
            "materialized_fields": ["payload_sha256", "payload_size_bytes", "payload_storage_class"],
        }
    return contract


def _authoritative_asof_hash(
    request: Mapping[str, Any], schema_fingerprint: str, manifests: list[Mapping[str, Any]]
) -> str:
    return canonical_hash({
        "request_hash": request["request_hash"], "schema_fingerprint": schema_fingerprint,
        "tables": [
            {
                "table": item["table"], "query_contract_hash": item["query_contract_hash"],
                "schema_hash": item["schema_hash"], "captured_count": item["captured_count"],
                "invalid_timestamp_count": item["invalid_timestamp_count"],
                "source_row_commitment_algorithm": item["source_row_commitment_algorithm"],
                "row_chain_hash": item["row_chain_hash"],
                "projection_chain_hash": item["projection_chain_hash"],
            }
            for item in manifests
        ],
    })


def _coverage(
    manifests: list[Mapping[str, Any]], records: list[Mapping[str, Any]], gaps: list[Mapping[str, Any]]
) -> dict[str, Any]:
    legacy_tables = {item["table"] for item in TABLES if item.get("projection_kind")}
    current_experiments = [item for item in records if item["source_table"] == "ad_experiment"]
    maturing_ids = {
        item["projection"]["experiment_id"] for item in current_experiments
        if item["projection"]["current_state"] == "MATURING"
    }
    single_evaluated_ids = {
        item["projection"]["subject_experiment_ids"][0]
        for item in records
        if item["source_table"] == "ad_experiment_evaluation"
        and item["projection"].get("schema_version") == "gle-g1-02a-redacted-legacy-projection-v1"
        and len(item["projection"].get("subject_experiment_ids") or []) == 1
    }
    state_counts: dict[str, int] = {}
    for item in current_experiments:
        state = item["projection"]["current_state"] or "MISSING"
        state_counts[state] = state_counts.get(state, 0) + 1
    return {
        "legacy_evaluations_by_table": {
            item["table"]: {
                "captured": item["captured_count"], "post_cutoff": item["post_cutoff_count"],
                "invalid_timestamp": item["invalid_timestamp_count"],
            }
            for item in manifests if item["table"] in legacy_tables
        },
        "record_count": len(records), "gap_count": len(gaps),
        "table_counts": {item["table"]: item["captured_count"] for item in manifests},
        "physical_table_counts": {item["table"]: item["physical_count"] for item in manifests},
        "cutoff_eligible_experiment_current_context": {
            "not_asof": True, "state_counts": dict(sorted(state_counts.items())),
            "maturing_count": len(maturing_ids),
            "maturing_with_single_evaluation": len(maturing_ids & single_evaluated_ids),
            "maturing_without_single_evaluation": len(maturing_ids - single_evaluated_ids),
        },
        "reconstruction": {"complete": 0, "incomplete": len(records), "unrecoverable": 0},
    }


def _validate_record(item: Any) -> None:
    expected = {
        "source_table", "source_id", "source_row_hash", "semantic_at", "cutoff_disposition",
        "reconstruction_status", "projection", "reason_codes", "record_hash",
    }
    if not isinstance(item, Mapping) or set(item) != expected:
        _fail("G102_RECORD_SCHEMA_INVALID")
    validate_sha256(item["source_row_hash"], code="G102_SOURCE_ROW_HASH_INVALID")
    if item["reconstruction_status"] != "INCOMPLETE":
        _fail("G102_RECORD_STATUS_INVALID")
    descriptor = next((candidate for candidate in TABLES if candidate["table"] == item["source_table"]), None)
    if descriptor is None:
        _fail("G102_RECORD_SOURCE_INVALID")
    normal_disposition = (
        "CURRENT_ONLY_NOT_ASOF" if descriptor["semantic_at"] is None
        else "AT_OR_BEFORE_CUTOFF_RETAINED_HISTORY_ONLY" if descriptor["class"] == "APPEND_ONLY_WITH_RETENTION"
        else "TIMESTAMP_ELIGIBLE_CURRENT_CONTENT_UNVERIFIED"
    )
    if item["cutoff_disposition"] not in {normal_disposition, "SOURCE_TIMESTAMP_INVALID"}:
        _fail("G102_RECORD_CUTOFF_DISPOSITION_INVALID")
    if not isinstance(item["reason_codes"], list) or item["reason_codes"] != sorted(set(item["reason_codes"])):
        _fail("G102_RECORD_REASONS_INVALID")
    if any(not isinstance(code, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]*", code) for code in item["reason_codes"]):
        _fail("G102_RECORD_REASONS_INVALID")
    _validate_projection(item["source_table"], item["projection"])
    if item["source_table"] in {"ad_experiment_evaluation", "ad_creative_group_evaluation", "ad_audience_pair_evaluation"}:
        projection = item["projection"]
        if projection.get("status") != "INVALID_LEGACY_PROJECTION" and (
            projection["source_id"] != item["source_id"]
            or _utc_instant(projection["evaluated_at"]) != _utc_instant(item["semantic_at"])
        ):
            _fail("G102_PROJECTION_RECORD_BINDING_MISMATCH")
    if item.get("record_hash") != canonical_hash({key: value for key, value in item.items() if key != "record_hash"}):
        _fail("G102_RECORD_HASH_MISMATCH")


def _validate_table_manifest_shape(item: Any) -> None:
    expected = {
        "table", "semantic_class", "primary_key", "semantic_time_field", "source_columns",
        "materialized_columns", "source_row_commitment_algorithm", "large_field_summary",
        "schema_columns", "query_contract_hash", "schema_hash", "physical_count", "captured_count",
        "post_cutoff_count", "invalid_timestamp_count", "first_key", "last_key", "row_chain_hash",
        "projection_chain_hash", "table_manifest_hash",
    }
    if not isinstance(item, Mapping) or set(item) != expected:
        _fail("G102_TABLE_MANIFEST_SCHEMA_INVALID")
    for field in ("query_contract_hash", "schema_hash", "row_chain_hash", "projection_chain_hash", "table_manifest_hash"):
        validate_sha256(item[field], code="G102_TABLE_MANIFEST_HASH_INVALID")
    for field in ("physical_count", "captured_count", "post_cutoff_count", "invalid_timestamp_count"):
        if type(item[field]) is not int or item[field] < 0:
            _fail("G102_TABLE_COUNT_INVALID")


def _validate_large_field_summary(item: Mapping[str, Any], descriptor: Mapping[str, Any]) -> None:
    summary = item["large_field_summary"]
    if descriptor["table"] != "ad_daily_report":
        if summary is not None:
            _fail("G102_LARGE_FIELD_SUMMARY_INVALID")
        return
    expected = {
        "source_field", "source_row_count", "total_source_bytes", "maximum_source_row_bytes",
        "maximum_allowed_total_source_bytes", "maximum_allowed_source_row_bytes",
        "required_storage_class", "payload_commitment_algorithm", "source_row_commitment_algorithm",
    }
    if not isinstance(summary, Mapping) or set(summary) != expected:
        _fail("G102_LARGE_FIELD_SUMMARY_INVALID")
    for field in (
        "source_row_count", "total_source_bytes", "maximum_source_row_bytes",
        "maximum_allowed_total_source_bytes", "maximum_allowed_source_row_bytes",
    ):
        if type(summary[field]) is not int or summary[field] < 0:
            _fail("G102_LARGE_FIELD_SUMMARY_INVALID")
    if (
        summary["source_field"] != "payload_json"
        or summary["source_row_count"] != item["physical_count"]
        or summary["total_source_bytes"] > summary["maximum_allowed_total_source_bytes"]
        or summary["maximum_source_row_bytes"] > summary["maximum_allowed_source_row_bytes"]
        or summary["maximum_allowed_total_source_bytes"] != MAX_TOTAL_REPORT_SOURCE_BYTES
        or summary["maximum_allowed_source_row_bytes"] != MAX_REPORT_PAYLOAD_BYTES
        or summary["required_storage_class"] != "text"
        or summary["payload_commitment_algorithm"] != REPORT_PAYLOAD_COMMITMENT_ALGORITHM
        or summary["source_row_commitment_algorithm"] != REPORT_SOURCE_ROW_COMMITMENT_ALGORITHM
    ):
        _fail("G102_LARGE_FIELD_SUMMARY_INVALID")


def _validate_gap_shape(item: Any) -> None:
    expected = {
        "scope", "code", "severity", "source_table", "source_id", "record_hash",
        "reconstructibility", "guessed_value", "gap_id",
    }
    if not isinstance(item, Mapping) or set(item) != expected or item["scope"] not in {"RECORD", "SOURCE"}:
        _fail("G102_GAP_SCHEMA_INVALID")
    if item["guessed_value"] is not None or item["reconstructibility"] != "UNKNOWN":
        _fail("G102_GAP_SEMANTICS_INVALID")
    if item["scope"] == "RECORD":
        if item["severity"] != "INCOMPLETE":
            _fail("G102_GAP_SEMANTICS_INVALID")
        if not isinstance(item["source_id"], str) or not item["source_id"]:
            _fail("G102_GAP_RECORD_INVALID")
        validate_sha256(item["record_hash"], code="G102_GAP_RECORD_HASH_INVALID")
    elif item["source_id"] is not None or item["record_hash"] is not None or item["severity"] != "STRUCTURAL":
        _fail("G102_GAP_SOURCE_INVALID")


def _validate_projection(table: str, projection: Any) -> None:
    if not isinstance(projection, Mapping):
        _fail("G102_PROJECTION_SCHEMA_INVALID")
    if table in {"ad_experiment_evaluation", "ad_creative_group_evaluation", "ad_audience_pair_evaluation"}:
        expected = {
            "schema_version", "source_kind", "source_id", "subject_experiment_ids", "legacy_checkpoint",
            "checkpoint_role_hint", "legacy_status", "evidence_summary", "episode_status", "missing_fields", "evaluated_at",
            "lineage_status", "split", "binding_eligible", "causal_classification", "reason_codes",
            "source_projection_hash",
        }
        if set(projection) == {"status", "error_code"}:
            if projection["status"] != "INVALID_LEGACY_PROJECTION":
                _fail("G102_PROJECTION_SCHEMA_INVALID")
            return
        if set(projection) != expected or projection["schema_version"] != "gle-g1-02a-redacted-legacy-projection-v1":
            _fail("G102_PROJECTION_SCHEMA_INVALID")
        if projection["binding_eligible"] is not False or projection["causal_classification"] != "OBSERVATIONAL_ONLY":
            _fail("G102_PROJECTION_BINDING_INVALID")
        expected_kind = {
            "ad_experiment_evaluation": "SINGLE_EXPERIMENT",
            "ad_creative_group_evaluation": "CREATIVE_GROUP",
            "ad_audience_pair_evaluation": "AUDIENCE_PAIR",
        }[table]
        if projection["source_kind"] != expected_kind:
            _fail("G102_PROJECTION_KIND_INVALID")
        expected_episode = {"SINGLE_EXPERIMENT": {"PRESENT", "MISSING"}}.get(expected_kind, {"NOT_APPLICABLE"})
        if projection["episode_status"] not in expected_episode:
            _fail("G102_PROJECTION_SCHEMA_INVALID")
        if projection["lineage_status"] != "UNRESOLVED" or projection["split"] != "UNASSIGNED":
            _fail("G102_PROJECTION_BINDING_INVALID")
        if projection["legacy_checkpoint"] not in LEGACY_CHECKPOINT_ROLES or projection["checkpoint_role_hint"] != LEGACY_CHECKPOINT_ROLES[projection["legacy_checkpoint"]]:
            _fail("G102_PROJECTION_CHECKPOINT_INVALID")
        if not isinstance(projection["legacy_status"], str) or not projection["legacy_status"]:
            _fail("G102_PROJECTION_STATUS_INVALID")
        if not isinstance(projection["missing_fields"], list) or projection["missing_fields"] != sorted(set(projection["missing_fields"])):
            _fail("G102_PROJECTION_MISSING_INVALID")
        projection_reasons = {"LEGACY_CALENDAR_CHECKPOINT", "LEGACY_INPUT_SNAPSHOT_MISSING", "LINEAGE_UNRESOLVED"}
        if projection["missing_fields"]:
            projection_reasons.add("LEGACY_FIELDS_MISSING")
        if set(projection["reason_codes"]) != projection_reasons:
            _fail("G102_PROJECTION_REASONS_INVALID")
        validate_sha256(projection["source_projection_hash"], code="G102_PROJECTION_HASH_INVALID")
        subjects = projection["subject_experiment_ids"]
        if not isinstance(subjects, list) or subjects != sorted(set(subjects)) or not subjects:
            _fail("G102_PROJECTION_SUBJECT_INVALID")
        summary = projection["evidence_summary"]
        if not isinstance(summary, Mapping) or set(summary) != LEGACY_EVIDENCE_FIELDS[expected_kind]:
            _fail("G102_PROJECTION_SCHEMA_INVALID")
        for field, wrapper in summary.items():
            if not isinstance(wrapper, Mapping):
                _fail("G102_PROJECTION_SCHEMA_INVALID")
            if wrapper.get("status") == "MISSING":
                if set(wrapper) != {"status", "value_hash", "value_type"} or wrapper["value_hash"] is not None or wrapper["value_type"] is not None:
                    _fail("G102_PROJECTION_SCHEMA_INVALID")
            elif wrapper.get("status") == "PRESENT":
                if set(wrapper) != {"status", "value_hash", "value_type", "safe_value"}:
                    _fail("G102_PROJECTION_SCHEMA_INVALID")
                validate_sha256(wrapper["value_hash"], code="G102_PROJECTION_HASH_INVALID")
                if wrapper["value_type"] != LEGACY_EVIDENCE_TYPES[expected_kind][field]:
                    _fail("G102_PROJECTION_SCHEMA_INVALID")
                if field == "launch_id":
                    if not isinstance(wrapper["safe_value"], str) or not re.fullmatch(r"launch_[0-9a-f]{24}", wrapper["safe_value"]):
                        _fail("G102_PROJECTION_PRIVACY_INVALID")
                elif wrapper["safe_value"] is not None:
                    _fail("G102_PROJECTION_PRIVACY_INVALID")
            else:
                _fail("G102_PROJECTION_SCHEMA_INVALID")
    elif table == "ad_experiment_events":
        if set(projection) != {"experiment_id", "from_state", "to_state", "event_type", "evaluation_id", "evidence_status", "evidence_hash"}:
            _fail("G102_PROJECTION_SCHEMA_INVALID")
        if projection["evidence_status"] not in {"PRESENT", "MISSING", "INVALID"}:
            _fail("G102_PROJECTION_SCHEMA_INVALID")
        if projection["evidence_status"] == "PRESENT":
            validate_sha256(projection["evidence_hash"], code="G102_PROJECTION_HASH_INVALID")
        elif projection["evidence_hash"] is not None:
            _fail("G102_PROJECTION_SCHEMA_INVALID")
    elif table == "ad_daily_report":
        if set(projection) != {"report_date", "data_mode", "snapshot_version", "rule_version", "window_start_utc", "window_end_utc", "payload_hash"}:
            _fail("G102_PROJECTION_SCHEMA_INVALID")
        validate_sha256(projection["payload_hash"], code="G102_PROJECTION_HASH_INVALID")
    elif table == "ad_creative_group_evaluation_history":
        if set(projection) != {"group_evaluation_id", "launch_token", "checkpoint", "snapshot_status", "snapshot_hash", "archived_reason_hash"}:
            _fail("G102_PROJECTION_SCHEMA_INVALID")
        validate_sha256(projection["archived_reason_hash"], code="G102_PROJECTION_HASH_INVALID")
        if projection["snapshot_status"] not in {"PRESENT", "MISSING", "INVALID"}:
            _fail("G102_PROJECTION_SCHEMA_INVALID")
        if projection["snapshot_status"] == "PRESENT":
            validate_sha256(projection["snapshot_hash"], code="G102_PROJECTION_HASH_INVALID")
        elif projection["snapshot_hash"] is not None:
            _fail("G102_PROJECTION_SCHEMA_INVALID")
    elif table == "ad_experiment":
        expected = {
            "experiment_id", "account_token", "market", "platform", "launch_token", "campaign_token",
            "adset_token", "ad_token", "creative_token", "current_state", "created_at", "updated_at",
            "hypothesis_hash", "control_hash",
        }
        if set(projection) != expected:
            _fail("G102_PROJECTION_SCHEMA_INVALID")
        validate_sha256(projection["hypothesis_hash"], code="G102_PROJECTION_HASH_INVALID")
        validate_sha256(projection["control_hash"], code="G102_PROJECTION_HASH_INVALID")
    else:
        _fail("G102_PROJECTION_SCHEMA_INVALID")


def _required_record_reasons(item: Mapping[str, Any], records: list[Mapping[str, Any]]) -> set[str]:
    table = item["source_table"]
    projection = item["projection"]
    reasons: set[str] = set()
    if table in {"ad_experiment_evaluation", "ad_creative_group_evaluation", "ad_audience_pair_evaluation"}:
        if projection.get("status") == "INVALID_LEGACY_PROJECTION":
            reasons.add("LEGACY_PROJECTION_INVALID")
        else:
            reasons.update(projection["reason_codes"])
            if projection["episode_status"] == "MISSING":
                reasons.add("EPISODE_ID_MISSING")
        reasons.update({"OBJECTIVE_INCOMPATIBLE", "MUTATION_PROVENANCE_MISSING", "MUTABLE_ROW_PREIMAGE_UNAVAILABLE"})
        if table == "ad_experiment_evaluation" and projection.get("status") != "INVALID_LEGACY_PROJECTION":
            subject = projection["subject_experiment_ids"][0]
            evaluated_at = _utc_instant(projection["evaluated_at"])
            exact = False
            for candidate in records:
                if candidate["source_table"] != "ad_experiment_events" or candidate["cutoff_disposition"] == "SOURCE_TIMESTAMP_INVALID":
                    continue
                event = candidate["projection"]
                if (
                    event["evidence_status"] == "PRESENT"
                    and event["event_type"] in ALLOWED_EVALUATION_EVENT_TYPES
                    and event["evaluation_id"] == item["source_id"]
                    and event["experiment_id"] == subject
                    and _utc_instant(candidate["semantic_at"]) >= evaluated_at
                ):
                    exact = True
                    break
            if not exact:
                reasons.add("EXACT_EVALUATION_EVENT_REF_MISSING")
    elif table == "ad_experiment_events":
        if projection["evidence_status"] != "PRESENT":
            reasons.add(f"EVENT_EVIDENCE_{projection['evidence_status']}")
        if projection["event_type"] in ALLOWED_EVALUATION_EVENT_TYPES and not projection["evaluation_id"]:
            reasons.add("EVENT_EVALUATION_ID_MISSING")
        if not projection["experiment_id"]:
            reasons.add("EVENT_EXPERIMENT_ID_MISSING")
        if item["cutoff_disposition"] != "SOURCE_TIMESTAMP_INVALID":
            reasons.add("RETENTION_COMPLETENESS_UNKNOWN")
    elif table == "ad_daily_report":
        reasons.add("REPLACEABLE_REPORT_PREIMAGE_UNAVAILABLE")
    elif table == "ad_creative_group_evaluation_history":
        reasons.add("ARCHIVED_PREIMAGE_PARTIAL")
        if projection["snapshot_status"] != "PRESENT":
            reasons.add(f"ARCHIVED_SNAPSHOT_{projection['snapshot_status']}")
    elif table == "ad_experiment":
        reasons.add("MUTABLE_CURRENT_STATE_NO_PREIMAGE")
    if item["cutoff_disposition"] == "SOURCE_TIMESTAMP_INVALID":
        reasons.add("SOURCE_TIMESTAMP_INVALID")
    return reasons


def _json_object_status(raw: Any) -> tuple[dict[str, Any], str]:
    if raw is None or raw == "":
        return {}, "MISSING"
    try:
        value = json.loads(str(raw))
    except (TypeError, ValueError):
        return {}, "INVALID"
    if not isinstance(value, dict):
        return {}, "INVALID"
    return value, "PRESENT"


def _token(kind: str, value: Any) -> str:
    raw = str(value or "")
    if not raw:
        return ""
    return f"{kind}_{hashlib.sha256(f'{TOKEN_VERSION}|{kind}|{raw}'.encode()).hexdigest()[:24]}"


def _source_stat(source_path: str | os.PathLike[str]) -> dict[str, Any]:
    path = Path(source_path).expanduser().resolve()
    result: dict[str, Any] = {}
    for suffix, target in (("db", path), ("wal", Path(str(path) + "-wal")), ("shm", Path(str(path) + "-shm")), ("journal", Path(str(path) + "-journal"))):
        if target.exists():
            stat = target.stat()
            result[suffix] = {
                "exists": True, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
                "device": stat.st_dev, "inode": stat.st_ino,
            }
        else:
            result[suffix] = {"exists": False, "size": 0, "mtime_ns": 0, "device": 0, "inode": 0}
    return result
