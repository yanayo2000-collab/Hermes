from __future__ import annotations

import copy
import hashlib
import json
import os
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import pytest

import app.growth.historical_maturing_triage as triage_module

from app.growth.canonical_evaluation_contracts import canonical_hash
from app.growth.historical_asof_audit import build_audit, make_request, open_readonly_snapshot, write_audit_bundle
from app.growth.historical_maturing_triage import (
    HistoricalMaturingTriageError,
    derive_maturing_triage_from_audit_directory,
    load_validated_maturing_triage_directory,
    validate_maturing_triage_bundle,
    write_maturing_triage_artifacts,
)
from tests.test_growth_historical_asof_audit import _fixture


ROOT = Path(__file__).resolve().parents[1]


def _source(tmp_path: Path, *, event_type: str | list[str] | None = None) -> tuple[Path, str]:
    db = tmp_path / "source.db"
    _fixture(db)
    if event_type:
        conn = sqlite3.connect(db)
        values = [event_type] if isinstance(event_type, str) else event_type
        for index, value in enumerate(values):
            conn.execute(
                "INSERT INTO ad_experiment_events VALUES (?,?,?,?,?,?,?,?,?)",
                (f"reason-event-{index}", "exp-1", "MATURING", "MATURING", value, "system", "", "{}", f"2026-08-06T01:00:0{index}Z"),
            )
        conn.commit()
        conn.close()
    conn = open_readonly_snapshot(db)
    request = make_request(
        audit_id="audit-triage", source_logical_id="test-source",
        data_cutoff_at="2026-08-08T00:00:00Z", captured_at="2026-08-08T00:00:01Z",
    )
    bundle = build_audit(conn, request, source_path=db)
    conn.close()
    out = tmp_path / "audit"
    write_audit_bundle(bundle, out)
    return out, hashlib.sha256((out / "manifest.json").read_bytes()).hexdigest()


def _derive(tmp_path: Path, *, event_type: str | list[str] | None = None):
    audit, anchor = _source(tmp_path, event_type=event_type)
    result = derive_maturing_triage_from_audit_directory(
        audit,
        expected_manifest_sha256=anchor,
        triage_id="triage-1",
        derived_at="2026-08-08T00:00:02Z",
    )
    return audit, anchor, result


def test_unknown_denominator_is_conserved_and_requires_review(tmp_path: Path) -> None:
    audit, anchor, result = _derive(tmp_path)
    assert result["status"] == "INCOMPLETE_REVIEW_REQUIRED"
    assert result["coverage"]["source_maturing_denominator"] == 2
    assert result["coverage"]["triage_item_count"] == 2
    assert result["coverage"]["reason_counts"]["UNKNOWN"] == 2
    assert len(result["manual_reviews"]) == 2
    assert all(item["reason_status"] == "UNKNOWN_INSUFFICIENT_EVIDENCE" for item in result["items"])
    assert result["split_assignments"] == []
    assert result["holdout_status"] == "LOCKED_NOT_ASSIGNED"
    assert result["replay_eligible"] is result["golden_eligible"] is False
    assert result["gate1_effect"] == "NONE"
    validate_maturing_triage_bundle(result, audit_dir=audit, expected_manifest_sha256=anchor)


@pytest.mark.parametrize("reason", [
    "EXTERNAL_MUTATION", "DATA_SOURCE_MISSING", "STATE_MACHINE_STUCK", "SCHEDULER_MISSED",
    "ATTRIBUTION_PENDING", "NO_DELIVERY", "SPEND_TOO_LOW", "EVENTS_TOO_LOW", "TIME_NOT_REACHED",
])
def test_exact_event_type_is_only_an_unverified_candidate(tmp_path: Path, reason: str) -> None:
    _audit, _anchor, result = _derive(tmp_path, event_type=reason)
    first = next(item for item in result["items"] if item["experiment_id"] == "exp-1")
    assert first["reason_code"] == "UNKNOWN"
    assert first["reason_status"] == "UNKNOWN_INSUFFICIENT_EVIDENCE"
    assert [item["reason_code"] for item in first["observed_candidate_reasons"]] == [reason]
    assert first["observed_candidate_reasons"][0]["observation_status"] == "OBSERVED_UNVERIFIED_EVENT_LABEL"
    assert first["manual_review_required"] is True
    assert len(first["evidence_refs"]) == 2


def test_multiple_event_labels_remain_visible_and_conflicted(tmp_path: Path) -> None:
    _audit, _anchor, result = _derive(tmp_path, event_type=["TIME_NOT_REACHED", "NO_DELIVERY"])
    first = next(item for item in result["items"] if item["experiment_id"] == "exp-1")
    assert [item["reason_code"] for item in first["observed_candidate_reasons"]] == [
        "NO_DELIVERY", "TIME_NOT_REACHED",
    ]
    assert "CONFLICTING_REASON_EVENTS" in first["blocker_codes"]
    assert result["coverage"]["candidate_conflict_count"] == 1
    assert result["status"] == "INCOMPLETE_REVIEW_REQUIRED"


def test_metric_hashes_and_timestamps_do_not_infer_business_reason(tmp_path: Path) -> None:
    _audit, _anchor, result = _derive(tmp_path)
    first = result["items"][0]
    assert first["reason_code"] == "UNKNOWN"
    assert all("payload_hash" not in ref["field_paths"] for ref in first["evidence_refs"])
    assert result["classifier_contract"]["metric_commitments_are_not_values"] is True
    assert result["classifier_contract"]["threshold_contract_status"] == "UNFROZEN"


def test_invalid_event_timestamp_cannot_prove_reason(tmp_path: Path) -> None:
    db = tmp_path / "source.db"
    _fixture(db)
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO ad_experiment_events VALUES (?,?,?,?,?,?,?,?,?)",
        ("bad-time-event", "exp-1", "MATURING", "MATURING", "NO_DELIVERY", "system", "", "{}", "not-a-time"),
    )
    conn.commit()
    conn.close()
    conn = open_readonly_snapshot(db)
    request = make_request(
        audit_id="audit-invalid-time", source_logical_id="test-source",
        data_cutoff_at="2026-08-08T00:00:00Z", captured_at="2026-08-08T00:00:01Z",
    )
    source = build_audit(conn, request, source_path=db)
    conn.close()
    audit = tmp_path / "audit"
    write_audit_bundle(source, audit)
    anchor = hashlib.sha256((audit / "manifest.json").read_bytes()).hexdigest()
    result = derive_maturing_triage_from_audit_directory(
        audit, expected_manifest_sha256=anchor, triage_id="invalid-time",
        derived_at="2026-08-08T00:00:02Z",
    )
    first = next(item for item in result["items"] if item["experiment_id"] == "exp-1")
    assert first["reason_code"] == "UNKNOWN"


def test_source_aware_validation_rejects_rehashed_promotion(tmp_path: Path) -> None:
    audit, anchor, result = _derive(tmp_path)
    forged = copy.deepcopy(result)
    forged["items"][0]["reason_code"] = "NO_DELIVERY"
    forged["items"][0]["reason_status"] = "PROVEN_BY_EXACT_RETAINED_EVENT_TYPE"
    forged["items"][0]["manual_review_required"] = False
    forged["items"][0]["item_hash"] = canonical_hash({
        key: value for key, value in forged["items"][0].items() if key != "item_hash"
    })
    forged["manual_reviews"] = [
        item for item in forged["manual_reviews"]
        if item["experiment_id"] != forged["items"][0]["experiment_id"]
    ]
    forged["coverage"]["reason_counts"]["UNKNOWN"] -= 1
    forged["coverage"]["reason_counts"]["NO_DELIVERY"] += 1
    forged["coverage"]["unknown_count"] -= 1
    forged["coverage"]["manual_review_count"] -= 1
    forged["status"] = "AUDIT_CLASSIFIED"
    forged["bundle_hash"] = canonical_hash({
        key: value for key, value in forged.items() if key != "bundle_hash"
    })
    with pytest.raises(HistoricalMaturingTriageError, match="G102C_SOURCE_SEMANTICS_MISMATCH"):
        validate_maturing_triage_bundle(forged, audit_dir=audit, expected_manifest_sha256=anchor)


def test_artifact_round_trip_and_external_anchor(tmp_path: Path) -> None:
    audit, anchor, result = _derive(tmp_path)
    output = tmp_path / "triage"
    manifest = write_maturing_triage_artifacts(
        result, output, audit_dir=audit, expected_manifest_sha256=anchor,
    )
    assert set(item.name for item in output.iterdir()) == {
        "manifest.json", "triage.ndjson", "manual-review.ndjson", "coverage.json",
    }
    assert oct(output.stat().st_mode & 0o777) == "0o700"
    assert all((item.stat().st_mode & 0o777) == 0o600 for item in output.iterdir())
    raw_anchor = hashlib.sha256((output / "manifest.json").read_bytes()).hexdigest()
    loaded = load_validated_maturing_triage_directory(
        output,
        expected_triage_manifest_sha256=raw_anchor,
        audit_dir=audit,
        expected_audit_manifest_sha256=anchor,
    )
    assert loaded == result
    assert manifest["bundle_hash"] == result["bundle_hash"]
    with pytest.raises(HistoricalMaturingTriageError, match="G102C_MANIFEST_ANCHOR_MISMATCH"):
        load_validated_maturing_triage_directory(
            output,
            expected_triage_manifest_sha256="0" * 64,
            audit_dir=audit,
            expected_audit_manifest_sha256=anchor,
        )


def test_artifact_loader_rejects_permissions_and_extra_files(tmp_path: Path) -> None:
    audit, anchor, result = _derive(tmp_path)
    output = tmp_path / "triage"
    write_maturing_triage_artifacts(result, output, audit_dir=audit, expected_manifest_sha256=anchor)
    raw_anchor = hashlib.sha256((output / "manifest.json").read_bytes()).hexdigest()
    os.chmod(output / "coverage.json", 0o644)
    with pytest.raises(HistoricalMaturingTriageError, match="G102C_ARTIFACT_FILE_INVALID"):
        load_validated_maturing_triage_directory(
            output, expected_triage_manifest_sha256=raw_anchor,
            audit_dir=audit, expected_audit_manifest_sha256=anchor,
        )
    os.chmod(output / "coverage.json", 0o600)
    (output / "extra").write_text("x")
    with pytest.raises(HistoricalMaturingTriageError, match="G102C_ARTIFACT_DIRECTORY_INVALID"):
        load_validated_maturing_triage_directory(
            output, expected_triage_manifest_sha256=raw_anchor,
            audit_dir=audit, expected_audit_manifest_sha256=anchor,
        )


def test_writer_refuses_existing_output_and_source_manifest_swap(tmp_path: Path) -> None:
    audit, anchor, result = _derive(tmp_path)
    output = tmp_path / "existing"
    output.mkdir()
    with pytest.raises(HistoricalMaturingTriageError, match="G102C_OUTPUT_EXISTS"):
        write_maturing_triage_artifacts(result, output, audit_dir=audit, expected_manifest_sha256=anchor)
    with pytest.raises(HistoricalMaturingTriageError, match="G102C_SOURCE_AUDIT_INVALID"):
        validate_maturing_triage_bundle(result, audit_dir=audit, expected_manifest_sha256="0" * 64)


def test_writer_size_gate_leaves_no_final_or_partial_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    audit, anchor, result = _derive(tmp_path)
    output = tmp_path / "oversize-output"
    monkeypatch.setattr(triage_module, "MAX_ARTIFACT_FILE_BYTES", 1)
    with pytest.raises(HistoricalMaturingTriageError, match="G102C_ARTIFACT_FILE_TOO_LARGE"):
        write_maturing_triage_artifacts(result, output, audit_dir=audit, expected_manifest_sha256=anchor)
    assert not output.exists()
    assert not list(tmp_path.glob(".oversize-output.tmp-*"))


def test_parent_fsync_failure_never_leaves_empty_or_partial_final(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    audit, anchor, result = _derive(tmp_path)
    output = tmp_path / "durability-output"
    real_fsync = os.fsync
    directory_calls = 0

    def fail_second_directory_fsync(fd: int) -> None:
        nonlocal directory_calls
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            directory_calls += 1
            if directory_calls == 2:
                raise OSError("simulated parent fsync failure")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_second_directory_fsync)
    with pytest.raises(HistoricalMaturingTriageError, match="G102C_OUTPUT_DURABILITY_UNCERTAIN"):
        write_maturing_triage_artifacts(result, output, audit_dir=audit, expected_manifest_sha256=anchor)
    assert {item.name for item in output.iterdir()} == {
        "manifest.json", "triage.ndjson", "manual-review.ndjson", "coverage.json",
    }
    raw_anchor = hashlib.sha256((output / "manifest.json").read_bytes()).hexdigest()
    monkeypatch.setattr(os, "fsync", real_fsync)
    assert load_validated_maturing_triage_directory(
        output, expected_triage_manifest_sha256=raw_anchor,
        audit_dir=audit, expected_audit_manifest_sha256=anchor,
    ) == result


def test_cli_exit_two_and_invalid_exit_64(tmp_path: Path) -> None:
    audit, anchor, _result = _derive(tmp_path)
    output = tmp_path / "cli-output"
    command = [
        sys.executable,
        str(ROOT / "scripts/classify_gle_historical_maturing.py"),
        "--audit-dir", str(audit),
        "--expected-audit-manifest-sha256", anchor,
        "--triage-id", "cli-triage",
        "--derived-at", "2026-08-08T00:00:02Z",
        "--output-dir", str(output),
    ]
    env = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONDONTWRITEBYTECODE="1")
    run = subprocess.run(command, cwd=ROOT, env=env, text=True, capture_output=True, check=False)
    assert run.returncode == 2
    assert json.loads(run.stdout)["status"] == "INCOMPLETE_REVIEW_REQUIRED"
    bad_command = command.copy()
    bad_command[bad_command.index("--derived-at") + 1] = "bad-time"
    bad_command[bad_command.index("--output-dir") + 1] = str(tmp_path / "bad")
    bad = subprocess.run(bad_command, cwd=ROOT, env=env, text=True, capture_output=True, check=False)
    assert bad.returncode == 64


def test_derived_at_must_follow_capture(tmp_path: Path) -> None:
    audit, anchor = _source(tmp_path)
    with pytest.raises(HistoricalMaturingTriageError, match="G102C_DERIVED_AT_INVALID"):
        derive_maturing_triage_from_audit_directory(
            audit,
            expected_manifest_sha256=anchor,
            triage_id="triage-early",
            derived_at="2026-08-07T23:59:59Z",
        )


def test_empty_maturing_denominator_remains_incomplete(tmp_path: Path) -> None:
    db = tmp_path / "source.db"
    _fixture(db)
    conn = sqlite3.connect(db)
    conn.execute("UPDATE ad_experiment SET state='COMPLETED'")
    conn.commit()
    conn.close()
    conn = open_readonly_snapshot(db)
    request = make_request(
        audit_id="audit-empty", source_logical_id="test-source",
        data_cutoff_at="2026-08-08T00:00:00Z", captured_at="2026-08-08T00:00:01Z",
    )
    source = build_audit(conn, request, source_path=db)
    conn.close()
    audit = tmp_path / "audit"
    write_audit_bundle(source, audit)
    anchor = hashlib.sha256((audit / "manifest.json").read_bytes()).hexdigest()
    result = derive_maturing_triage_from_audit_directory(
        audit, expected_manifest_sha256=anchor, triage_id="empty",
        derived_at="2026-08-08T00:00:02Z",
    )
    assert result["status"] == "INCOMPLETE_REVIEW_REQUIRED"
    assert result["coverage"]["denominator_status"] == "EMPTY_INCOMPLETE"
    assert "MATURING_DENOMINATOR_EMPTY" in result["coverage"]["bundle_blocker_codes"]
    assert result["coverage"]["s02_02_effect"] == "NONE"
