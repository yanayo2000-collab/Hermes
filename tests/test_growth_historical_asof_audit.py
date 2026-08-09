from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import app.growth.historical_asof_audit as historical

from app.growth.historical_asof_audit import (
    HistoricalAsOfAuditError,
    build_audit,
    make_request,
    open_readonly_snapshot,
    validate_audit_bundle,
    write_audit_bundle,
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fixture(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE ad_experiment (
          experiment_id TEXT PRIMARY KEY, account_id TEXT, country TEXT, platform TEXT,
          source_report_id TEXT, source_campaign_id TEXT, source_adset_id TEXT,
          source_ad_id TEXT, source_creative_id TEXT, hypothesis_json TEXT,
          control_definition_json TEXT, state TEXT, created_at TEXT, updated_at TEXT
        );
        CREATE TABLE ad_experiment_evaluation (
          evaluation_id TEXT PRIMARY KEY, experiment_id TEXT, episode_id TEXT, checkpoint TEXT,
          baseline_window_json TEXT, post_window_json TEXT, baseline_metrics_json TEXT,
          post_metrics_json TEXT, data_quality_status TEXT, dedupe_version TEXT,
          attribution_version TEXT, evaluation_status TEXT, evaluated_at TEXT
        );
        CREATE TABLE ad_creative_group_evaluation (
          group_evaluation_id TEXT PRIMARY KEY, launch_id TEXT, checkpoint TEXT,
          window_json TEXT, metrics_by_experiment_json TEXT, ranking_json TEXT,
          winner_experiment_id TEXT, decision_status TEXT, actual_days INTEGER,
          data_quality_status TEXT, evidence_json TEXT, evaluated_at TEXT
        );
        CREATE TABLE ad_audience_pair_evaluation (
          pair_evaluation_id TEXT PRIMARY KEY, launch_id TEXT, checkpoint TEXT,
          baseline_experiment_id TEXT, challenger_experiment_id TEXT, metrics_json TEXT,
          winner_experiment_id TEXT, decision_status TEXT, evidence_json TEXT, evaluated_at TEXT
        );
        CREATE TABLE ad_experiment_events (
          event_id TEXT PRIMARY KEY, experiment_id TEXT, from_state TEXT, to_state TEXT,
          event_type TEXT, actor TEXT, reason TEXT, evidence_json TEXT, created_at TEXT
        );
        CREATE TABLE ad_daily_report (
          report_id TEXT PRIMARY KEY, report_date TEXT, data_mode TEXT, snapshot_version TEXT,
          rule_version TEXT, window_start_utc TEXT, window_end_utc TEXT,
          generated_at_utc TEXT, payload_json TEXT
        );
        CREATE TABLE ad_creative_group_evaluation_history (
          history_id TEXT PRIMARY KEY, group_evaluation_id TEXT, launch_id TEXT,
          checkpoint TEXT, snapshot_json TEXT, archived_reason TEXT, archived_at TEXT
        );
        """
    )
    experiments = [
        ("exp-1", "act-secret", "MX", "meta", "launch-1", "camp-1", "set-1", "ad-1", "creative-1", '{"mode":"copy_variant"}', '{"role":"CHAMPION"}', "MATURING", "2026-08-01T00:00:00Z", "2026-08-09T00:00:00Z"),
        ("exp-2", "act-secret", "MX", "meta", "launch-1", "camp-1", "set-2", "ad-2", "creative-2", '{"mode":"copy_variant"}', '{"role":"CHALLENGER"}', "MATURING", "2026-08-01T00:00:00Z", "2026-08-09T00:00:00Z"),
    ]
    conn.executemany("INSERT INTO ad_experiment VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", experiments)
    conn.executemany(
        "INSERT INTO ad_experiment_evaluation VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("eval-1", "exp-1", "", "D1", None, "{}", "{}", '{"qualified_joins":0}', "PASS", "", "", "PENDING", "2026-08-05T01:00:00Z"),
            ("eval-future", "exp-2", "", "D3", "{}", "{}", "{}", "{}", "PASS", "d-v1", "a-v1", "PENDING", "2026-08-11T01:00:00Z"),
        ],
    )
    conn.execute(
        "INSERT INTO ad_experiment_events VALUES (?,?,?,?,?,?,?,?,?)",
        ("event-1", "exp-1", "RUNNING", "MATURING", "PERFORMANCE_EVALUATED", "system", "D1", '{"evaluation_id":"eval-1"}', "2026-08-05T01:00:01Z"),
    )
    conn.execute(
        "INSERT INTO ad_daily_report VALUES (?,?,?,?,?,?,?,?,?)",
        ("report-1", "2026-08-05", "real", "snapshot-v1", "rule-v1", "2026-08-05T00:00:00Z", "2026-08-05T23:59:59Z", "2026-08-06T01:00:00Z", '{"private":"must-not-export"}'),
    )
    conn.commit()
    conn.close()


def _request():
    return make_request(
        audit_id="audit-1", data_cutoff_at="2026-08-10T00:00:00Z",
        captured_at="2026-08-10T01:00:00Z", source_logical_id="production-growth-snapshot",
    )


def test_capture_is_readonly_deterministic_cutoff_bounded_and_gap_explicit(tmp_path: Path) -> None:
    database = tmp_path / "source.db"; _fixture(database); before = _sha(database)
    conn = open_readonly_snapshot(database)
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("CREATE TEMP TABLE forbidden(id INTEGER)")
    first = build_audit(conn, _request(), source_path=database); conn.close()
    assert _sha(database) == before
    conn = open_readonly_snapshot(database)
    second = build_audit(conn, _request(), source_path=database); conn.close()
    assert first == second
    assert validate_audit_bundle(first) == first
    assert first["status"] == "INCOMPLETE"
    assert first["replay_eligibility"] == "AUDIT_ONLY"
    assert first["coverage"]["legacy_evaluations_by_table"]["ad_experiment_evaluation"] == {
        "captured": 1, "post_cutoff": 1, "invalid_timestamp": 0,
    }
    manifests = {item["table"]: item for item in first["table_manifests"]}
    assert manifests["ad_daily_report"]["schema_columns"] == [
        "report_id", "report_date", "data_mode", "snapshot_version", "rule_version",
        "window_start_utc", "window_end_utc", "generated_at_utc", "payload_json",
    ]
    assert manifests["ad_experiment"]["schema_columns"][0] == "experiment_id"
    assert manifests["ad_experiment"]["schema_columns"] != manifests["ad_daily_report"]["schema_columns"]
    assert {item["code"] for item in first["gaps"]} >= {
        "LEGACY_INPUT_SNAPSHOT_MISSING", "EPISODE_ID_MISSING", "LINEAGE_UNRESOLVED",
        "OBJECTIVE_INCOMPATIBLE", "MUTABLE_CURRENT_STATE_NO_PREIMAGE",
    }
    record = next(item for item in first["records"] if item["source_table"] == "ad_experiment_evaluation")
    assert record["projection"]["evidence_summary"]["baseline_window"] == {
        "status": "MISSING", "value_hash": None, "value_type": None,
    }
    serialized = json.dumps(first, sort_keys=True)
    assert "act-secret" not in serialized and "must-not-export" not in serialized


def test_artifacts_are_content_addressed_and_refuse_overwrite(tmp_path: Path) -> None:
    database = tmp_path / "source.db"; _fixture(database)
    conn = open_readonly_snapshot(database); bundle = build_audit(conn, _request(), source_path=database); conn.close()
    output = tmp_path / "audit"
    manifest = write_audit_bundle(bundle, output)
    assert set(path.name for path in output.iterdir()) == {"manifest.json", "records.ndjson", "gaps.ndjson", "coverage.json"}
    assert manifest["schema_version"] == historical.MANIFEST_VERSION == "gle-g1-02a-asof-audit-manifest-v2"
    assert manifest["request"]["schema_version"] == "gle-g1-02a-asof-audit-request-v2"
    assert manifest["files"]["records.ndjson"]["sha256"] == _sha(output / "records.ndjson")
    with pytest.raises(HistoricalAsOfAuditError, match="G102_OUTPUT_EXISTS"):
        write_audit_bundle(bundle, output)


def test_missing_table_and_hash_tamper_fail_closed(tmp_path: Path) -> None:
    database = tmp_path / "source.db"; sqlite3.connect(database).close()
    conn = open_readonly_snapshot(database)
    with pytest.raises(HistoricalAsOfAuditError, match="G102_REQUIRED_TABLE_MISSING"):
        build_audit(conn, _request(), source_path=database)
    conn.close()
    _fixture(database)
    conn = open_readonly_snapshot(database); bundle = build_audit(conn, _request(), source_path=database); conn.close()
    bundle["records"][0]["source_row_hash"] = "0" * 64
    with pytest.raises(HistoricalAsOfAuditError, match="G102_RECORD_HASH_MISMATCH"):
        validate_audit_bundle(bundle)


def test_declared_primary_key_is_required_for_bounded_ordering(tmp_path: Path) -> None:
    database = tmp_path / "source.db"; _fixture(database)
    conn = sqlite3.connect(database)
    conn.execute("ALTER TABLE ad_daily_report RENAME TO old_ad_daily_report")
    conn.execute(
        "CREATE TABLE ad_daily_report AS SELECT * FROM old_ad_daily_report"
    )
    conn.execute("DROP TABLE old_ad_daily_report")
    conn.commit(); conn.close()
    readonly = open_readonly_snapshot(database)
    with pytest.raises(HistoricalAsOfAuditError, match="G102_PRIMARY_KEY_MISMATCH:ad_daily_report"):
        build_audit(readonly, _request(), source_path=database)
    readonly.close()


def test_post_cutoff_event_and_report_never_enter_records(tmp_path: Path) -> None:
    database = tmp_path / "source.db"; _fixture(database)
    readonly = open_readonly_snapshot(database); before_bundle = build_audit(readonly, _request(), source_path=database); readonly.close()
    conn = sqlite3.connect(database)
    conn.execute("INSERT INTO ad_experiment_events VALUES (?,?,?,?,?,?,?,?,?)", ("event-future", "exp-1", "MATURING", "EFFECTIVE", "LATE", "system", "", "{}", "2026-08-12T00:00:00Z"))
    conn.execute("INSERT INTO ad_daily_report VALUES (?,?,?,?,?,?,?,?,?)", ("report-future", "2026-08-12", "real", "v", "v", "", "", "2026-08-12T01:00:00Z", "{}"))
    conn.commit(); conn.close()
    readonly = open_readonly_snapshot(database); bundle = build_audit(readonly, _request(), source_path=database); readonly.close()
    ids = {item["source_id"] for item in bundle["records"]}
    assert "event-future" not in ids and "report-future" not in ids
    tables = {item["table"]: item for item in bundle["table_manifests"]}
    assert tables["ad_experiment_events"]["post_cutoff_count"] == 1
    assert tables["ad_daily_report"]["post_cutoff_count"] == 1
    assert bundle["source_snapshot"]["authoritative_asof_hash"] == before_bundle["source_snapshot"]["authoritative_asof_hash"]
    assert bundle["source_snapshot"]["source_snapshot_hash"] != before_bundle["source_snapshot"]["source_snapshot_hash"]


def test_cli_writes_one_new_audit_directory_and_returns_audit_only(tmp_path: Path) -> None:
    database = tmp_path / "source.db"; _fixture(database); output = tmp_path / "bundle"
    completed = subprocess.run(
        [
            sys.executable, "scripts/audit_gle_historical_asof.py", "--database", str(database),
            "--output-dir", str(output), "--audit-id", "audit-cli", "--data-cutoff-at",
            "2026-08-10T00:00:00Z", "--captured-at", "2026-08-10T01:00:00Z",
            "--source-logical-id", "fixture-source",
        ],
        cwd=Path(__file__).resolve().parents[1], text=True, capture_output=True, check=False,
    )
    assert completed.returncode == 0 and completed.stderr == ""
    manifest = json.loads(completed.stdout)
    assert manifest["status"] == "INCOMPLETE" and manifest["replay_eligibility"] == "AUDIT_ONLY"
    assert output.is_dir()


def _rehash_bundle(bundle: dict) -> None:
    for record in bundle["records"]:
        record["record_hash"] = historical.canonical_hash({key: value for key, value in record.items() if key != "record_hash"})
    for gap in bundle["gaps"]:
        if gap["scope"] == "RECORD":
            record = next(item for item in bundle["records"] if (item["source_table"], item["source_id"]) == (gap["source_table"], gap["source_id"]))
            gap["record_hash"] = record["record_hash"]
        gap["gap_id"] = historical.canonical_hash({key: value for key, value in gap.items() if key != "gap_id"})
    for manifest in bundle["table_manifests"]:
        rows = sorted((item for item in bundle["records"] if item["source_table"] == manifest["table"]), key=lambda item: item["source_id"])
        manifest["row_chain_hash"] = historical.canonical_hash([item["source_row_hash"] for item in rows])
        manifest["projection_chain_hash"] = historical.canonical_hash([item["record_hash"] for item in rows])
        manifest["table_manifest_hash"] = historical.canonical_hash({key: value for key, value in manifest.items() if key != "table_manifest_hash"})
    bundle["source_snapshot"]["table_chain_hash"] = historical.canonical_hash([item["table_manifest_hash"] for item in bundle["table_manifests"]])
    bundle["source_snapshot"]["source_snapshot_hash"] = historical.canonical_hash({key: value for key, value in bundle["source_snapshot"].items() if key != "source_snapshot_hash"})
    bundle["bundle_hash"] = historical.canonical_hash({key: value for key, value in bundle.items() if key != "bundle_hash"})


def test_rehashed_cross_binding_tamper_fails_closed(tmp_path: Path) -> None:
    database = tmp_path / "source.db"; _fixture(database)
    conn = open_readonly_snapshot(database); original = build_audit(conn, _request(), source_path=database); conn.close()
    for mutate, code in (
        (lambda value: value["source_snapshot"].update(source_logical_id="borrowed"), "G102_SOURCE_REQUEST_BINDING_MISMATCH"),
        (lambda value: value["coverage"].update(record_count=999), "G102_COVERAGE_MISMATCH"),
    ):
        value = json.loads(json.dumps(original)); mutate(value); _rehash_bundle(value)
        with pytest.raises(HistoricalAsOfAuditError, match=code):
            validate_audit_bundle(value)
    value = json.loads(json.dumps(original)); _rehash_bundle(value)
    value["table_manifests"][0]["row_chain_hash"] = "1" * 64
    value["table_manifests"][0]["table_manifest_hash"] = historical.canonical_hash({key: item for key, item in value["table_manifests"][0].items() if key != "table_manifest_hash"})
    value["source_snapshot"]["table_chain_hash"] = historical.canonical_hash([item["table_manifest_hash"] for item in value["table_manifests"]])
    value["source_snapshot"]["source_snapshot_hash"] = historical.canonical_hash({key: item for key, item in value["source_snapshot"].items() if key != "source_snapshot_hash"})
    value["bundle_hash"] = historical.canonical_hash({key: item for key, item in value.items() if key != "bundle_hash"})
    with pytest.raises(HistoricalAsOfAuditError, match="G102_TABLE_ROW_CHAIN_MISMATCH"):
        validate_audit_bundle(value)
    value = json.loads(json.dumps(original))
    value["gaps"] = [item for item in value["gaps"] if item["code"] != "EPISODE_ID_MISSING"]
    value["coverage"]["gap_count"] = len(value["gaps"]); _rehash_bundle(value)
    with pytest.raises(HistoricalAsOfAuditError, match="G102_RECORD_GAP_CLOSURE_MISMATCH"):
        validate_audit_bundle(value)
    value = json.loads(json.dumps(original))
    target = next(item for item in value["records"] if item["source_id"] == "eval-1")
    target["reason_codes"].remove("EPISODE_ID_MISSING")
    value["gaps"] = [item for item in value["gaps"] if item["code"] != "EPISODE_ID_MISSING"]
    value["coverage"]["gap_count"] = len(value["gaps"]); _rehash_bundle(value)
    with pytest.raises(HistoricalAsOfAuditError, match="G102_RECORD_REASONS_INCONSISTENT"):
        validate_audit_bundle(value)
    value = json.loads(json.dumps(original))
    value["trust_status"] = "TRUSTED"; _rehash_bundle(value)
    with pytest.raises(HistoricalAsOfAuditError, match="G102_STATUS_LATTICE_INVALID"):
        validate_audit_bundle(value)
    for mutate, code in (
        (lambda value: value["records"][0].update(cutoff_disposition="TRUSTED_ASOF"), "G102_RECORD_CUTOFF_DISPOSITION_INVALID"),
        (lambda value: next(item for item in value["records"] if item["source_id"] == "eval-1")["projection"].update(split="HOLDOUT"), "G102_PROJECTION_BINDING_INVALID"),
        (lambda value: next(item for item in value["gaps"] if item["scope"] == "RECORD").update(severity="PASS"), "G102_GAP_SEMANTICS_INVALID"),
    ):
        value = json.loads(json.dumps(original)); mutate(value); _rehash_bundle(value)
        with pytest.raises(HistoricalAsOfAuditError, match=code):
            validate_audit_bundle(value)
    value = json.loads(json.dumps(original))
    value["table_manifests"][0]["invalid_timestamp_count"] = 1
    value["table_manifests"][0]["table_manifest_hash"] = historical.canonical_hash({key: item for key, item in value["table_manifests"][0].items() if key != "table_manifest_hash"})
    value["source_snapshot"]["table_chain_hash"] = historical.canonical_hash([item["table_manifest_hash"] for item in value["table_manifests"]])
    value["source_snapshot"]["source_snapshot_hash"] = historical.canonical_hash({key: item for key, item in value["source_snapshot"].items() if key != "source_snapshot_hash"})
    value["coverage"] = historical._coverage(value["table_manifests"], value["records"], value["gaps"])
    value["bundle_hash"] = historical.canonical_hash({key: item for key, item in value.items() if key != "bundle_hash"})
    with pytest.raises(HistoricalAsOfAuditError, match="G102_TABLE_COUNT_MISMATCH"):
        validate_audit_bundle(value)
    value = json.loads(json.dumps(original))
    evaluation = next(item for item in value["records"] if item["source_id"] == "eval-1")
    wrapper = evaluation["projection"]["evidence_summary"]["post_metrics"]
    wrapper["safe_value"] = "private@example.com"; wrapper["value_type"] = "secret_email"
    _rehash_bundle(value)
    with pytest.raises(HistoricalAsOfAuditError, match="G102_PROJECTION_SCHEMA_INVALID"):
        validate_audit_bundle(value)


def test_timestamp_classification_is_instant_based_and_total(tmp_path: Path) -> None:
    database = tmp_path / "source.db"; _fixture(database)
    conn = sqlite3.connect(database)
    conn.execute(
        "UPDATE ad_experiment_evaluation SET evaluated_at=? WHERE evaluation_id='eval-1'",
        ("2026-08-05T01:00:00.123456+00:00",),
    )
    conn.execute(
        "UPDATE ad_experiment_events SET created_at=? WHERE event_id='event-1'",
        ("2026-08-05T01:00:00.123457+00:00",),
    )
    conn.executemany(
        "INSERT INTO ad_experiment_events VALUES (?,?,?,?,?,?,?,?,?)",
        [
            ("event-after", "exp-1", "A", "B", "OTHER", "system", "", "{}", "2026-08-10T00:00:00.500000Z"),
            ("event-offset", "exp-1", "A", "B", "OTHER", "system", "", "{}", "2026-08-09T23:00:00+00:00"),
            ("event-invalid", "exp-1", "A", "B", "OTHER", "system", "", "{}", None),
        ],
    )
    conn.commit(); conn.close()
    readonly = open_readonly_snapshot(database); bundle = build_audit(readonly, _request(), source_path=database); readonly.close()
    manifest = next(item for item in bundle["table_manifests"] if item["table"] == "ad_experiment_events")
    assert manifest["physical_count"] == 4
    assert manifest["captured_count"] == 3 and manifest["post_cutoff_count"] == 1
    assert manifest["invalid_timestamp_count"] == 1
    invalid = next(item for item in bundle["records"] if item["source_id"] == "event-invalid")
    assert invalid["cutoff_disposition"] == "SOURCE_TIMESTAMP_INVALID"
    assert "SOURCE_TIMESTAMP_INVALID" in invalid["reason_codes"]


def test_event_reference_requires_same_experiment_valid_json_and_time_order(tmp_path: Path) -> None:
    database = tmp_path / "source.db"; _fixture(database)
    conn = sqlite3.connect(database)
    conn.execute("DELETE FROM ad_experiment_events")
    conn.executemany(
        "INSERT INTO ad_experiment_events VALUES (?,?,?,?,?,?,?,?,?)",
        [
            ("event-wrong-exp", "exp-2", "A", "B", "PERFORMANCE_EVALUATED", "system", "", '{"evaluation_id":"eval-1"}', "2026-08-05T02:00:00Z"),
            ("event-bad-json", "exp-1", "A", "B", "PERFORMANCE_EVALUATED", "system", "", "{bad", "2026-08-05T02:00:00Z"),
            ("event-too-early", "exp-1", "A", "B", "PERFORMANCE_EVALUATED", "system", "", '{"evaluation_id":"eval-1"}', "2026-08-05T00:59:59Z"),
            ("event-empty", "exp-1", "A", "B", "PERFORMANCE_EVALUATED", "system", "", "{}", "2026-08-05T02:00:00Z"),
        ],
    )
    conn.commit(); conn.close()
    readonly = open_readonly_snapshot(database); bundle = build_audit(readonly, _request(), source_path=database); readonly.close()
    evaluation = next(item for item in bundle["records"] if item["source_id"] == "eval-1")
    assert "EXACT_EVALUATION_EVENT_REF_MISSING" in evaluation["reason_codes"]
    invalid = next(item for item in bundle["records"] if item["source_id"] == "event-bad-json")
    assert "EVENT_EVIDENCE_INVALID" in invalid["reason_codes"]
    empty = next(item for item in bundle["records"] if item["source_id"] == "event-empty")
    assert "EVENT_EVALUATION_ID_MISSING" in empty["reason_codes"]


@pytest.mark.parametrize("table,source_id", [
    ("ad_experiment_evaluation", "eval-1"),
    ("ad_creative_group_evaluation", "group-invalid"),
    ("ad_audience_pair_evaluation", "pair-invalid"),
])
def test_invalid_legacy_timestamp_is_kept_as_gap(
    tmp_path: Path, table: str, source_id: str
) -> None:
    database = tmp_path / "source.db"; _fixture(database)
    conn = sqlite3.connect(database)
    if table == "ad_experiment_evaluation":
        conn.execute("UPDATE ad_experiment_evaluation SET evaluated_at=NULL WHERE evaluation_id='eval-1'")
    elif table == "ad_creative_group_evaluation":
        conn.execute(
            "INSERT INTO ad_creative_group_evaluation VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (source_id, "launch", "D3", "{}", '{"exp-1":{},"exp-2":{}}', "[]", "exp-1", "PENDING", 3, "PASS", "{}", None),
        )
    else:
        conn.execute(
            "INSERT INTO ad_audience_pair_evaluation VALUES (?,?,?,?,?,?,?,?,?,?)",
            (source_id, "launch", "D3", "exp-1", "exp-2", "{}", "exp-1", "PENDING", "{}", "not-a-time"),
        )
    conn.commit(); conn.close()
    readonly = open_readonly_snapshot(database); bundle = build_audit(readonly, _request(), source_path=database); readonly.close()
    record = next(item for item in bundle["records"] if item["source_table"] == table and item["source_id"] == source_id)
    assert record["cutoff_disposition"] == "SOURCE_TIMESTAMP_INVALID"
    assert {"SOURCE_TIMESTAMP_INVALID", "LEGACY_PROJECTION_INVALID"} <= set(record["reason_codes"])


def test_legacy_payloads_are_hashed_not_exported(tmp_path: Path) -> None:
    database = tmp_path / "source.db"; _fixture(database)
    secret = "private@example.com"
    conn = sqlite3.connect(database)
    conn.execute("UPDATE ad_experiment_evaluation SET baseline_metrics_json=? WHERE evaluation_id='eval-1'", (json.dumps({"email": secret}),))
    conn.executemany(
        "INSERT INTO ad_creative_group_evaluation VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [("group-1", "launch-secret", "D3", "{}", '{"exp-1":{"email":"private@example.com"},"exp-2":{}}', "[]", "exp-1", "PENDING", 3, "PASS", '{"account_id":"secret"}', "2026-08-05T02:00:00Z")],
    )
    conn.execute(
        "INSERT INTO ad_audience_pair_evaluation VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("pair-1", "launch-secret", "D3", "exp-1", "exp-2", '{"email":"private@example.com"}', "exp-1", "PENDING", '{"account_id":"secret"}', "2026-08-05T02:00:00Z"),
    )
    conn.commit(); conn.close()
    readonly = open_readonly_snapshot(database); bundle = build_audit(readonly, _request(), source_path=database); readonly.close()
    serialized = json.dumps(bundle, sort_keys=True)
    assert secret not in serialized and "launch-secret" not in serialized


def test_report_payload_is_bounded_committed_without_aggregate_materialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "source.db"; _fixture(database)
    payload = '{"large":"' + ("x" * 65_536) + '"}'
    conn = sqlite3.connect(database)
    conn.execute("UPDATE ad_daily_report SET payload_json=? WHERE report_id='report-1'", (payload,))
    conn.commit(); conn.close()
    monkeypatch.setattr(historical, "MAX_TOTAL_CANONICAL_BYTES", 16 * 1024)
    readonly = open_readonly_snapshot(database)
    bundle = build_audit(readonly, _request(), source_path=database)
    readonly.close()
    report = next(item for item in bundle["records"] if item["source_table"] == "ad_daily_report")
    assert report["projection"]["payload_hash"] == hashlib.sha256(payload.encode()).hexdigest()
    assert payload not in json.dumps(bundle, sort_keys=True)
    assert bundle["request"]["query_contract_version"].endswith("-v2")
    manifest = next(item for item in bundle["table_manifests"] if item["table"] == "ad_daily_report")
    assert "payload_json" in manifest["source_columns"]
    assert "payload_json" not in manifest["materialized_columns"]
    assert manifest["materialized_columns"][-3:] == [
        "payload_sha256", "payload_size_bytes", "payload_storage_class",
    ]
    assert manifest["large_field_summary"] == {
        "source_field": "payload_json",
        "source_row_count": 1,
        "total_source_bytes": len(payload.encode()),
        "maximum_source_row_bytes": len(payload.encode()),
        "maximum_allowed_total_source_bytes": historical.MAX_TOTAL_REPORT_SOURCE_BYTES,
        "maximum_allowed_source_row_bytes": historical.MAX_REPORT_PAYLOAD_BYTES,
        "required_storage_class": "text",
        "payload_commitment_algorithm": historical.REPORT_PAYLOAD_COMMITMENT_ALGORITHM,
        "source_row_commitment_algorithm": historical.REPORT_SOURCE_ROW_COMMITMENT_ALGORITHM,
    }


def test_total_report_payload_bound_fails_before_hash_callback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "source.db"; _fixture(database)
    payload = "x" * 64
    conn = sqlite3.connect(database)
    conn.execute("UPDATE ad_daily_report SET payload_json=? WHERE report_id='report-1'", (payload,))
    conn.commit(); conn.close()
    monkeypatch.setattr(historical, "MAX_TOTAL_REPORT_SOURCE_BYTES", 32)
    readonly = open_readonly_snapshot(database)
    hash_calls = 0

    def _count_hash(value: object) -> str:
        nonlocal hash_calls
        hash_calls += 1
        return hashlib.sha256(str(value).encode()).hexdigest()

    readonly.create_function("gle_sha256_text", 1, _count_hash, deterministic=True)
    with pytest.raises(
        HistoricalAsOfAuditError,
        match="G102_SOURCE_BOUND_EXCEEDED:ad_daily_report:total_payload",
    ):
        build_audit(readonly, _request(), source_path=database)
    readonly.close()
    assert hash_calls == 0


def test_report_row_bound_fails_before_hash_callback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "source.db"; _fixture(database)
    conn = sqlite3.connect(database)
    conn.executemany(
        "INSERT INTO ad_daily_report VALUES (?,?,?,?,?,?,?,?,?)",
        [
            ("report-2", "2026-08-06", "real", "v", "v", "", "", "2026-08-06T01:00:00Z", "{}"),
            ("report-3", "2026-08-07", "real", "v", "v", "", "", "2026-08-07T01:00:00Z", "{}"),
        ],
    )
    conn.commit(); conn.close()
    monkeypatch.setattr(historical, "MAX_ROWS_PER_TABLE", 2)
    readonly = open_readonly_snapshot(database)
    hash_calls = 0

    def _count_hash(value: object) -> str:
        nonlocal hash_calls
        hash_calls += 1
        return hashlib.sha256(str(value).encode()).hexdigest()

    readonly.create_function("gle_sha256_text", 1, _count_hash, deterministic=True)
    with pytest.raises(HistoricalAsOfAuditError, match="G102_SOURCE_BOUND_EXCEEDED:ad_daily_report"):
        build_audit(readonly, _request(), source_path=database)
    readonly.close()
    assert hash_calls == 0


def test_single_report_payload_limit_remains_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "source.db"; _fixture(database)
    conn = sqlite3.connect(database)
    conn.execute("UPDATE ad_daily_report SET payload_json=? WHERE report_id='report-1'", ("x" * 64,))
    conn.commit(); conn.close()
    monkeypatch.setattr(historical, "MAX_REPORT_PAYLOAD_BYTES", 32)
    readonly = open_readonly_snapshot(database)
    with pytest.raises(HistoricalAsOfAuditError, match="G102_SOURCE_BOUND_EXCEEDED:ad_daily_report:payload"):
        build_audit(readonly, _request(), source_path=database)
    readonly.close()


@pytest.mark.parametrize("payload", [None, sqlite3.Binary(b"{}")])
def test_report_payload_requires_text_storage(
    tmp_path: Path, payload: object,
) -> None:
    database = tmp_path / "source.db"; _fixture(database)
    conn = sqlite3.connect(database)
    conn.execute("UPDATE ad_daily_report SET payload_json=? WHERE report_id='report-1'", (payload,))
    conn.commit(); conn.close()
    readonly = open_readonly_snapshot(database)
    with pytest.raises(HistoricalAsOfAuditError, match="G102_SOURCE_BOUND_EXCEEDED:ad_daily_report:payload"):
        build_audit(readonly, _request(), source_path=database)
    readonly.close()


def test_archived_history_and_current_maturing_context_are_explicitly_non_asof(tmp_path: Path) -> None:
    database = tmp_path / "source.db"; _fixture(database)
    conn = sqlite3.connect(database)
    conn.execute(
        "INSERT INTO ad_creative_group_evaluation_history VALUES (?,?,?,?,?,?,?)",
        ("history-1", "group-old", "launch-secret", "D3", '{"email":"private@example.com"}', "replacement", "2026-08-06T03:00:00Z"),
    )
    conn.commit(); conn.close()
    readonly = open_readonly_snapshot(database); bundle = build_audit(readonly, _request(), source_path=database); readonly.close()
    context = bundle["coverage"]["cutoff_eligible_experiment_current_context"]
    assert context == {
        "not_asof": True, "state_counts": {"MATURING": 2}, "maturing_count": 2,
        "maturing_with_single_evaluation": 1, "maturing_without_single_evaluation": 1,
    }
    history = next(item for item in bundle["records"] if item["source_table"] == "ad_creative_group_evaluation_history")
    assert history["projection"]["snapshot_status"] == "PRESENT"
    assert "ARCHIVED_PREIMAGE_PARTIAL" in history["reason_codes"]
    assert "private@example.com" not in json.dumps(bundle)


def test_row_bound_applies_before_unbounded_materialization(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database = tmp_path / "source.db"; _fixture(database)
    monkeypatch.setattr(historical, "MAX_ROWS_PER_TABLE", 1)
    readonly = open_readonly_snapshot(database)
    with pytest.raises(HistoricalAsOfAuditError, match="G102_SOURCE_BOUND_EXCEEDED:ad_experiment_evaluation"):
        build_audit(readonly, _request(), source_path=database)
    readonly.close()
