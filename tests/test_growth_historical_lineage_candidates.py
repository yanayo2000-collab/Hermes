from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from app.growth.canonical_evaluation_contracts import canonical_hash
from app.growth.historical_asof_audit import build_audit, make_request, open_readonly_snapshot, write_audit_bundle
from app.growth.historical_lineage_candidates import (
    HistoricalLineageCandidateError,
    derive_lineage_candidates_from_audit_directory,
    load_validated_audit_directory,
    validate_lineage_candidate_bundle,
    write_lineage_candidate_bundle,
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _database(path: Path) -> None:
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
    rows = [
        ("exp-1", "act-private", "MX", "meta", "launch-1", "campaign-1", "set-1", "ad-1", "creative-1", "{}", "{}", "MATURING", "2026-08-01T00:00:00Z", "2026-08-06T00:00:00Z"),
        ("exp-2", "act-private", "MX", "meta", "launch-1", "campaign-1", "set-2", "ad-2", "creative-2", "{}", "{}", "MATURING", "2026-08-01T00:00:00Z", "2026-08-06T00:00:00Z"),
        ("exp-3", "act-private", "MX", "meta", "launch-2", "campaign-2", "set-3", "ad-3", "creative-3", "{}", "{}", "MATURING", "2026-08-01T00:00:00Z", "2026-08-06T00:00:00Z"),
    ]
    conn.executemany("INSERT INTO ad_experiment VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.execute(
        "INSERT INTO ad_experiment_evaluation VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("eval-1", "exp-1", "", "D1", "{}", "{}", "{}", "{}", "PASS", "d-v1", "a-v1", "PENDING", "2026-08-05T01:00:00Z"),
    )
    conn.execute(
        "INSERT INTO ad_creative_group_evaluation VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("group-1", "launch-1", "D3", "{}", '{"exp-1":{},"exp-2":{}}', '["exp-1","exp-2"]', "exp-1", "PROVISIONAL", 3, "PASS", "{}", "2026-08-05T02:00:00Z"),
    )
    conn.execute(
        "INSERT INTO ad_audience_pair_evaluation VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("pair-1", "launch-1", "D3", "exp-1", "exp-2", "{}", "exp-1", "PROVISIONAL", "{}", "2026-08-05T03:00:00Z"),
    )
    conn.execute(
        "INSERT INTO ad_experiment_events VALUES (?,?,?,?,?,?,?,?,?)",
        ("event-1", "exp-1", "RUNNING", "MATURING", "PERFORMANCE_EVALUATED", "system", "D1", '{"evaluation_id":"eval-1"}', "2026-08-05T01:00:01Z"),
    )
    conn.execute(
        "INSERT INTO ad_daily_report VALUES (?,?,?,?,?,?,?,?,?)",
        ("report-1", "2026-08-05", "real", "v1", "v1", "2026-08-05T00:00:00Z", "2026-08-05T23:59:59Z", "2026-08-06T01:00:00Z", "{}"),
    )
    conn.execute(
        "INSERT INTO ad_creative_group_evaluation_history VALUES (?,?,?,?,?,?,?)",
        ("history-1", "old-group", "launch-1", "D1", "{}", "CREATIVE_REPLACED", "2026-08-06T02:00:00Z"),
    )
    conn.commit()
    conn.close()


def _audit_dir(tmp_path: Path, *, cutoff: str = "2026-08-07T00:00:00Z") -> tuple[Path, str]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    database = tmp_path / "source.db"
    _database(database)
    request = make_request(
        audit_id="audit-lineage", data_cutoff_at=cutoff,
        captured_at="2026-08-07T01:00:00Z", source_logical_id="fixture-growth",
    )
    conn = open_readonly_snapshot(database)
    bundle = build_audit(conn, request, source_path=database)
    conn.close()
    output = tmp_path / "audit"
    write_audit_bundle(bundle, output)
    return output, _sha(output / "manifest.json")


def _candidate(tmp_path: Path) -> tuple[dict, Path, str]:
    audit, expected = _audit_dir(tmp_path)
    candidate = derive_lineage_candidates_from_audit_directory(
        audit, expected_manifest_sha256=expected,
        derivation_id="derive-1", derived_at="2026-08-07T02:00:00Z",
    )
    return candidate, audit, expected


def _refresh_coverage_and_hash(candidate: dict) -> None:
    entries = candidate["lineage_candidates"]
    candidate["coverage"] = {
        "legacy_evaluation_count": len(entries),
        "component_candidate_count": len(candidate["components"]),
        "component_resolved_parent_unresolved_count": sum(
            item["lineage_status"] == "COMPONENT_RESOLVED_PARENT_UNRESOLVED" for item in entries
        ),
        "unresolved_count": sum(item["lineage_status"] == "UNRESOLVED_INSUFFICIENT_EVIDENCE" for item in entries),
        "conflict_count": sum(item["lineage_status"] == "CONFLICT" for item in entries),
        "dev_assignment_count": 0,
        "validation_assignment_count": 0,
        "holdout_assignment_count": 0,
    }
    candidate["candidate_hash"] = canonical_hash({
        key: value for key, value in candidate.items() if key != "candidate_hash"
    })


def test_validated_artifact_consumer_cross_binds_exact_bundle(tmp_path: Path) -> None:
    audit, expected = _audit_dir(tmp_path)
    bundle, binding = load_validated_audit_directory(audit, expected_manifest_sha256=expected)
    assert bundle["trust_status"] == "UNSIGNED_LOCAL_CAPTURE"
    assert binding["input_manifest_sha256"] == expected
    assert binding["input_bundle_hash"] == bundle["bundle_hash"]
    with pytest.raises(HistoricalLineageCandidateError, match="G102B_MANIFEST_ANCHOR_MISMATCH"):
        load_validated_audit_directory(audit, expected_manifest_sha256="0" * 64)
    (audit / "extra.json").write_text("{}")
    with pytest.raises(HistoricalLineageCandidateError, match="G102B_INPUT_FILE_SET_INVALID"):
        load_validated_audit_directory(audit, expected_manifest_sha256=expected)


def test_consumer_rejects_directory_and_file_symlinks(tmp_path: Path) -> None:
    audit, expected = _audit_dir(tmp_path)
    linked_directory = tmp_path / "audit-link"
    linked_directory.symlink_to(audit, target_is_directory=True)
    with pytest.raises(HistoricalLineageCandidateError, match="G102B_INPUT_DIRECTORY_INVALID"):
        load_validated_audit_directory(linked_directory, expected_manifest_sha256=expected)

    coverage = audit / "coverage.json"
    real_coverage = tmp_path / "coverage-real.json"
    coverage.replace(real_coverage)
    coverage.symlink_to(real_coverage)
    with pytest.raises(HistoricalLineageCandidateError, match="G102B_INPUT_FILE_INVALID"):
        load_validated_audit_directory(audit, expected_manifest_sha256=expected)

    fifo_root = tmp_path / "fifo"
    fifo_audit, fifo_expected = _audit_dir(fifo_root)
    fifo_coverage = fifo_audit / "coverage.json"
    fifo_coverage.unlink()
    os.mkfifo(fifo_coverage)
    with pytest.raises(HistoricalLineageCandidateError, match="G102B_INPUT_FILE_INVALID"):
        load_validated_audit_directory(fifo_audit, expected_manifest_sha256=fifo_expected)


def test_components_are_exact_but_parent_and_all_splits_remain_blocked(tmp_path: Path) -> None:
    candidate, _audit, _expected = _candidate(tmp_path)
    entries = {item["source_id"]: item for item in candidate["lineage_candidates"]}
    assert entries["eval-1"]["lineage_status"] == "UNRESOLVED_INSUFFICIENT_EVIDENCE"
    assert entries["group-1"]["lineage_status"] == "COMPONENT_RESOLVED_PARENT_UNRESOLVED"
    assert entries["pair-1"]["lineage_status"] == "COMPONENT_RESOLVED_PARENT_UNRESOLVED"
    assert entries["group-1"]["component_id"] == entries["pair-1"]["component_id"]
    assert {item["split"] for item in entries.values()} == {"UNASSIGNED"}
    assert all(item["lineage_id"] is None for item in entries.values())
    assert candidate["split_registry"]["assignments"] == []
    assert candidate["split_registry"]["allowed_splits"] == ["DEV", "VALIDATION"]
    assert candidate["coverage"]["holdout_assignment_count"] == 0
    assert candidate["trust_status"] == "UNSIGNED_LOCAL_DERIVATION"
    assert candidate["gate1_effect"] == "NONE" and candidate["not_dataset_receipt"] is True
    single_ref = next(
        ref for ref in entries["eval-1"]["evidence_refs"]
        if ref["source_table"] == "ad_experiment_evaluation"
    )
    assert single_ref["field_paths"] == ["projection.subject_experiment_ids"]


def test_launch_and_membership_drift_quarantine_instead_of_guessing(tmp_path: Path) -> None:
    audit, expected = _audit_dir(tmp_path)
    manifest = json.loads((audit / "manifest.json").read_text())
    assert manifest["trust_status"] == "UNSIGNED_LOCAL_CAPTURE"
    database = tmp_path / "source.db"
    conn = sqlite3.connect(database)
    conn.execute(
        "INSERT INTO ad_creative_group_evaluation VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("group-2", "launch-1", "D7", "{}", '{"exp-1":{},"exp-3":{}}', '["exp-1","exp-3"]', "exp-1", "WINNER", 7, "PASS", "{}", "2026-08-06T03:00:00Z"),
    )
    conn.commit(); conn.close()
    request = make_request(
        audit_id="audit-conflict", data_cutoff_at="2026-08-07T00:00:00Z",
        captured_at="2026-08-07T01:00:00Z", source_logical_id="fixture-growth",
    )
    conn = open_readonly_snapshot(database); bundle = build_audit(conn, request, source_path=database); conn.close()
    output = tmp_path / "audit-conflict"; write_audit_bundle(bundle, output)
    candidate = derive_lineage_candidates_from_audit_directory(
        output, expected_manifest_sha256=_sha(output / "manifest.json"),
        derivation_id="conflict", derived_at="2026-08-07T02:00:00Z",
    )
    entries = {item["source_id"]: item for item in candidate["lineage_candidates"]}
    assert entries["group-1"]["lineage_status"] == "CONFLICT"
    assert entries["group-2"]["lineage_status"] == "CONFLICT"
    assert "COMPONENT_MEMBERSHIP_CONFLICT" in entries["group-1"]["reason_codes"]
    assert "LAUNCH_TOKEN_CONFLICT" in entries["group-2"]["reason_codes"]
    assert candidate["coverage"]["dev_assignment_count"] == 0


def test_invalid_projection_and_dangling_subject_stay_in_denominator(tmp_path: Path) -> None:
    database = tmp_path / "source.db"
    _database(database)
    conn = sqlite3.connect(database)
    conn.execute(
        "INSERT INTO ad_experiment_evaluation VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("eval-invalid", "exp-2", "", "D1", "{}", "{}", "{}", "{}", "PASS", "d-v1", "a-v1", "PENDING", None),
    )
    conn.execute(
        "INSERT INTO ad_audience_pair_evaluation VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("pair-missing", "launch-1", "D3", "exp-1", "exp-missing", "{}", "exp-1", "PROVISIONAL", "{}", "2026-08-05T04:00:00Z"),
    )
    conn.commit()
    conn.close()
    request = make_request(
        audit_id="audit-invalid", data_cutoff_at="2026-08-07T00:00:00Z",
        captured_at="2026-08-07T01:00:00Z", source_logical_id="fixture-growth",
    )
    conn = open_readonly_snapshot(database)
    audit_bundle = build_audit(conn, request, source_path=database)
    conn.close()
    audit = tmp_path / "audit"
    write_audit_bundle(audit_bundle, audit)
    candidate = derive_lineage_candidates_from_audit_directory(
        audit, expected_manifest_sha256=_sha(audit / "manifest.json"),
        derivation_id="invalid", derived_at="2026-08-07T02:00:00Z",
    )
    entries = {item["source_id"]: item for item in candidate["lineage_candidates"]}
    assert entries["eval-invalid"]["source_kind"] == "INVALID"
    assert entries["eval-invalid"]["split"] == "UNASSIGNED"
    assert entries["pair-missing"]["lineage_status"] == "CONFLICT"
    assert "SUBJECT_EXPERIMENT_MISSING" in entries["pair-missing"]["reason_codes"]
    assert candidate["coverage"]["legacy_evaluation_count"] == 5


def test_missing_subject_launch_or_metadata_cannot_form_component(tmp_path: Path) -> None:
    database = tmp_path / "source.db"
    _database(database)
    conn = sqlite3.connect(database)
    conn.execute("UPDATE ad_experiment SET source_report_id=NULL, account_id=NULL WHERE experiment_id='exp-2'")
    conn.commit()
    conn.close()
    request = make_request(
        audit_id="audit-missing-binding", data_cutoff_at="2026-08-07T00:00:00Z",
        captured_at="2026-08-07T01:00:00Z", source_logical_id="fixture-growth",
    )
    conn = open_readonly_snapshot(database)
    audit_bundle = build_audit(conn, request, source_path=database)
    conn.close()
    audit = tmp_path / "audit"
    write_audit_bundle(audit_bundle, audit)
    candidate = derive_lineage_candidates_from_audit_directory(
        audit, expected_manifest_sha256=_sha(audit / "manifest.json"),
        derivation_id="missing-binding", derived_at="2026-08-07T02:00:00Z",
    )
    entries = {item["source_id"]: item for item in candidate["lineage_candidates"]}
    for source_id in ("group-1", "pair-1"):
        assert entries[source_id]["component_id"] is None
        assert entries[source_id]["lineage_status"] == "CONFLICT"
        assert "LAUNCH_TOKEN_MISSING" in entries[source_id]["reason_codes"]
        assert "SUBJECT_METADATA_MISSING" in entries[source_id]["reason_codes"]
    assert candidate["components"] == []


def test_extra_experiment_with_same_launch_quarantines_component(tmp_path: Path) -> None:
    database = tmp_path / "source.db"
    _database(database)
    conn = sqlite3.connect(database)
    conn.execute(
        "INSERT INTO ad_experiment VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("exp-extra", "act-private", "MX", "meta", "launch-1", "campaign-1", "set-extra", "ad-extra", "creative-extra", "{}", "{}", "MATURING", "2026-08-01T00:00:00Z", "2026-08-06T00:00:00Z"),
    )
    conn.commit()
    conn.close()
    request = make_request(
        audit_id="audit-extra-member", data_cutoff_at="2026-08-07T00:00:00Z",
        captured_at="2026-08-07T01:00:00Z", source_logical_id="fixture-growth",
    )
    conn = open_readonly_snapshot(database)
    audit_bundle = build_audit(conn, request, source_path=database)
    conn.close()
    audit = tmp_path / "audit"
    write_audit_bundle(audit_bundle, audit)
    candidate = derive_lineage_candidates_from_audit_directory(
        audit, expected_manifest_sha256=_sha(audit / "manifest.json"),
        derivation_id="extra-member", derived_at="2026-08-07T02:00:00Z",
    )
    entries = {item["source_id"]: item for item in candidate["lineage_candidates"]}
    for source_id in ("group-1", "pair-1"):
        assert entries[source_id]["lineage_status"] == "CONFLICT"
        assert "COMPONENT_MEMBERSHIP_CONFLICT" in entries[source_id]["reason_codes"]
    assert candidate["components"] == []


def test_rehashed_holdout_or_trust_promotion_is_rejected(tmp_path: Path) -> None:
    candidate, audit, expected = _candidate(tmp_path)
    candidate["split_registry"]["allowed_splits"].append("HOLDOUT")
    candidate["candidate_hash"] = canonical_hash({key: value for key, value in candidate.items() if key != "candidate_hash"})
    with pytest.raises(HistoricalLineageCandidateError, match="G102B_SPLIT_REGISTRY_INVALID"):
        validate_lineage_candidate_bundle(candidate, audit_dir=audit, expected_manifest_sha256=expected)
    candidate, audit, expected = _candidate(tmp_path / "second")
    candidate["trust_status"] = "SIGNED"
    candidate["candidate_hash"] = canonical_hash({key: value for key, value in candidate.items() if key != "candidate_hash"})
    with pytest.raises(HistoricalLineageCandidateError, match="G102B_TRUST_CEILING_INVALID"):
        validate_lineage_candidate_bundle(candidate, audit_dir=audit, expected_manifest_sha256=expected)

    candidate, audit, expected = _candidate(tmp_path / "third")
    candidate["input_binding"]["input_manifest_sha256"] = "1" * 64
    candidate["input_binding"]["input_manifest_hash"] = "2" * 64
    candidate["candidate_hash"] = canonical_hash({key: value for key, value in candidate.items() if key != "candidate_hash"})
    with pytest.raises(HistoricalLineageCandidateError, match="G102B_INPUT_MANIFEST_BINDING_MISMATCH"):
        validate_lineage_candidate_bundle(candidate, audit_dir=audit, expected_manifest_sha256=expected)


def test_rehashed_component_or_entry_semantic_tamper_is_rejected(tmp_path: Path) -> None:
    candidate, audit, expected = _candidate(tmp_path)
    tampered = deepcopy(candidate)
    tampered["components"][0]["reason_codes"].append("LAUNCH_TOKEN_MISSING")
    tampered["components"][0]["reason_codes"].sort()
    tampered["components"][0]["component_hash"] = canonical_hash({
        key: value for key, value in tampered["components"][0].items() if key != "component_hash"
    })
    tampered["candidate_hash"] = canonical_hash({key: value for key, value in tampered.items() if key != "candidate_hash"})
    with pytest.raises(HistoricalLineageCandidateError, match="G102B_COMPONENT_ENTRY_CLOSURE_INVALID"):
        validate_lineage_candidate_bundle(tampered, audit_dir=audit, expected_manifest_sha256=expected)

    tampered = deepcopy(candidate)
    single = next(item for item in tampered["lineage_candidates"] if item["source_kind"] == "SINGLE_EXPERIMENT")
    single["reason_codes"].remove("SINGLE_EXPERIMENT_COMPONENT_INSUFFICIENT")
    single["entry_hash"] = canonical_hash({key: value for key, value in single.items() if key != "entry_hash"})
    tampered["candidate_hash"] = canonical_hash({key: value for key, value in tampered.items() if key != "candidate_hash"})
    with pytest.raises(HistoricalLineageCandidateError, match="G102B_ENTRY_STATE_INVALID"):
        validate_lineage_candidate_bundle(tampered, audit_dir=audit, expected_manifest_sha256=expected)

    tampered = deepcopy(candidate)
    single = next(item for item in tampered["lineage_candidates"] if item["source_kind"] == "SINGLE_EXPERIMENT")
    single["subject_experiment_ids"] = ["exp-forged"]
    experiment_ref = next(ref for ref in single["evidence_refs"] if ref["source_table"] == "ad_experiment")
    experiment_ref["source_id"] = "exp-forged"
    experiment_ref["record_hash"] = "f" * 64
    single["entry_hash"] = canonical_hash({key: value for key, value in single.items() if key != "entry_hash"})
    tampered["candidate_hash"] = canonical_hash({key: value for key, value in tampered.items() if key != "candidate_hash"})
    with pytest.raises(HistoricalLineageCandidateError, match="G102B_ENTRY_SOURCE_BINDING_INVALID"):
        validate_lineage_candidate_bundle(tampered, audit_dir=audit, expected_manifest_sha256=expected)


def test_source_rederivation_rejects_false_conflict_or_missing_evidence(tmp_path: Path) -> None:
    candidate, audit, expected = _candidate(tmp_path)
    tampered = deepcopy(candidate)
    group = next(item for item in tampered["lineage_candidates"] if item["source_id"] == "group-1")
    group["reason_codes"].append("LAUNCH_TOKEN_CONFLICT")
    group["reason_codes"].sort()
    group["lineage_status"] = "CONFLICT"
    group["entry_hash"] = canonical_hash({key: value for key, value in group.items() if key != "entry_hash"})
    component = tampered["components"][0]
    component["status"] = "CONFLICT"
    component["reason_codes"] = sorted({
        reason for item in tampered["lineage_candidates"]
        if item["component_id"] == component["component_id"]
        for reason in item["reason_codes"]
    })
    component["component_hash"] = canonical_hash({
        key: value for key, value in component.items() if key != "component_hash"
    })
    _refresh_coverage_and_hash(tampered)
    with pytest.raises(HistoricalLineageCandidateError, match="G102B_DERIVATION_SEMANTICS_MISMATCH"):
        validate_lineage_candidate_bundle(tampered, audit_dir=audit, expected_manifest_sha256=expected)

    tampered = deepcopy(candidate)
    single = next(item for item in tampered["lineage_candidates"] if item["source_id"] == "eval-1")
    single["evidence_refs"] = [
        ref for ref in single["evidence_refs"] if ref["source_table"] != "ad_experiment"
    ]
    single["reason_codes"].append("SUBJECT_EXPERIMENT_MISSING")
    single["reason_codes"].sort()
    single["lineage_status"] = "CONFLICT"
    single["entry_hash"] = canonical_hash({key: value for key, value in single.items() if key != "entry_hash"})
    _refresh_coverage_and_hash(tampered)
    with pytest.raises(HistoricalLineageCandidateError, match="G102B_DERIVATION_SEMANTICS_MISMATCH"):
        validate_lineage_candidate_bundle(tampered, audit_dir=audit, expected_manifest_sha256=expected)


def test_derived_clock_and_determinism_are_frozen(tmp_path: Path) -> None:
    audit, expected = _audit_dir(tmp_path)
    with pytest.raises(HistoricalLineageCandidateError, match="G102B_DERIVED_BEFORE_CUTOFF"):
        derive_lineage_candidates_from_audit_directory(
            audit, expected_manifest_sha256=expected,
            derivation_id="too-early", derived_at="2026-08-06T23:59:59Z",
        )
    first = derive_lineage_candidates_from_audit_directory(
        audit, expected_manifest_sha256=expected,
        derivation_id="stable", derived_at="2026-08-07T02:00:00Z",
    )
    second = derive_lineage_candidates_from_audit_directory(
        audit, expected_manifest_sha256=expected,
        derivation_id="stable", derived_at="2026-08-07T02:00:00Z",
    )
    assert first == second



def test_output_is_content_addressed_refuses_overwrite_and_cli_is_audit_only(tmp_path: Path) -> None:
    candidate, audit, expected = _candidate(tmp_path)
    output = tmp_path / "derived"
    manifest = write_lineage_candidate_bundle(
        candidate, output, audit_dir=audit, expected_manifest_sha256=expected,
    )
    assert {item.name for item in output.iterdir()} == {
        "manifest.json", "lineage_candidates.ndjson", "components.ndjson", "coverage.json",
    }
    assert manifest["not_dataset_receipt"] is True and manifest["gate1_effect"] == "NONE"
    with pytest.raises(HistoricalLineageCandidateError, match="G102B_OUTPUT_EXISTS"):
        write_lineage_candidate_bundle(
            candidate, output, audit_dir=audit, expected_manifest_sha256=expected,
        )

    cli_root = tmp_path / "cli"
    audit, expected = _audit_dir(cli_root)
    completed = subprocess.run(
        [
            sys.executable, "scripts/derive_gle_lineage_devval_candidates.py",
            "--audit-dir", str(audit), "--expected-manifest-sha256", expected,
            "--output-dir", str(cli_root / "derived"), "--derivation-id", "cli-derive",
            "--derived-at", "2026-08-07T02:00:00Z",
        ],
        cwd=Path(__file__).resolve().parents[1], text=True, capture_output=True, check=False,
    )
    assert completed.returncode == 0 and completed.stderr == ""
    cli_manifest = json.loads(completed.stdout)
    assert cli_manifest["trust_status"] == "UNSIGNED_LOCAL_DERIVATION"
    assert cli_manifest["split_registry"]["holdout_status"] == "LOCKED_NOT_ASSIGNED"
