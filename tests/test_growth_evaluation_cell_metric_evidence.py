from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from copy import deepcopy
from pathlib import Path

import pytest

import app.growth.evaluation_cell_metric_evidence as metric_evidence
from app.growth.common import canonical_json
from app.growth.evaluation_cell_metric_evidence import (
    CEILING,
    EXACT_ARTIFACT_FILES,
    load_validated_cell_metric_evidence_directory,
    write_cell_metric_evidence_artifact,
)
from app.growth.gate0_feasibility_assessment import G005ContractError, hash_json
from scripts.build_gle_evaluation_cell_metric_evidence import main as cli_main
from scripts.assess_gle_gate0_feasibility import _collect_observations as legacy_collect
from tests.test_growth_gate0_feasibility_assessment import _policy, _snapshot, _subject


def _governed(value: dict) -> dict:
    result = dict(value)
    result["integrity"] = {
        "algorithm": "sha256",
        "payload_sha256": hashlib.sha256(json.dumps(
            result, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest(),
    }
    return result


def _transport(root: Path) -> tuple[Path, Path, dict]:
    runtime_paths = (
        "app/tugao_funnel_api.py", "app/ad_dashboard_repository.py", "app/main_shared.py",
        "app/schema_migrations.py", "app/sqlite_write_queue.py",
        "scripts/backfill_ad_dashboard_fact_rows.py",
    )
    runtime = [
        {"path": path, "sha256": hashlib.sha256(path.encode()).hexdigest(),
         "size_bytes": 10, "mode": 0o644, "mtime_ns": 1}
        for path in runtime_paths
    ]
    manifest_value = _governed({
        "schema_version": 1, "record_type": "mcn_release_manifest",
        "release_id": "gle-g0-02b-r1", "created_at_utc": "2026-07-17T00:00:00+00:00",
        "environment": {"host": "test", "user": "codex", "repository_root": "/opt/mcn-ai-automation"},
        "change_source": {
            "kind": "codex_task",
            "reference": "c2bdc06bb4926bb22de573e7967d4f4f5effa719",
            "base_revision": "production-baseline",
        },
        "plan_sha256": "1" * 64, "artifacts": {"files": runtime},
        "systemd": {"units": [{"name": "mcn-backend.service"}]},
        "databases": [{"name": "automation", "path": "/var/lib/mcn/automation.db"}],
        "backup": {"required": True, "status": "verified", "artifacts": []},
        "verification": {"tests": [{"status": "passed"}], "smokes": [{"status": "passed"}]},
        "rollback": {"status": "ready", "strategy": "restore preimage"},
    })
    manifest_path = root / "transport-manifest.json"
    manifest_path.write_text(json.dumps(manifest_value))
    receipt_value = _governed({
        "schema_version": 1, "record_type": "mcn_controlled_restart_receipt",
        "receipt_id": "gle-g0-02b-r1-1", "receipt_path": "/var/lib/receipts/r1.json",
        "release_id": "gle-g0-02b-r1", "status": "passed", "unit": "mcn-backend.service",
        "started_at_utc": "2026-07-16T23:59:00+00:00", "error": None,
        "finished_at_utc": "2026-07-17T00:00:00+00:00",
        "manifest": {"path": str(manifest_path), "payload_sha256": manifest_value["integrity"]["payload_sha256"]},
        "before": {"state": {"InvocationID": "invocation-1"}},
        "after": {"state": {"InvocationID": "invocation-2", "ActiveState": "active"}},
        "validation": {"ok": True, "phase": "restart", "release_id": "gle-g0-02b-r1"},
        "command": {"result": {"returncode": 0, "timed_out": False}},
        "smokes": [{"kind": "systemd", "target": "mcn-backend.service", "status": "passed"}],
    })
    receipt_path = root / "transport-receipt.json"
    receipt_path.write_text(json.dumps(receipt_value))
    evidence = {
        "schema_version": "gle-g0-02b-qualified-transport-deployment-v1",
        "source_commit": "c2bdc06bb4926bb22de573e7967d4f4f5effa719",
        "manifest_sha256": _sha(manifest_path),
        "receipt_sha256": _sha(receipt_path),
        "deployed_artifact_sha256": hash_json([
            {"path": item["path"], "sha256": item["sha256"], "size_bytes": item["size_bytes"]}
            for item in runtime
        ]),
        "backend_invocation_id": "invocation-2",
        "receipt_status": "passed",
        "deployed_at": "2026-07-17T00:00:00+00:00",
        "release_id": "gle-g0-02b-r1",
        "natural_evidence_not_before_date": "2026-07-18",
    }
    evidence["evidence_hash"] = hash_json(evidence)
    return manifest_path, receipt_path, evidence


def _sources(root: Path) -> tuple[Path, str, Path, str, Path, str, dict]:
    database = root / "snapshot.db"
    _snapshot(database)
    conn = sqlite3.connect(database)
    conn.executescript(
        """
        ALTER TABLE ad_dashboard_fact_rows RENAME TO ad_dashboard_fact_rows_legacy;
        CREATE TABLE ad_dashboard_fact_rows(
          row_id TEXT PRIMARY KEY,
          date TEXT,data_source TEXT,platform TEXT,account_id TEXT,country TEXT,
          media_source TEXT,campaign_id TEXT,adset_id TEXT,ad_id TEXT,impressions REAL,cost REAL,
          tugao_join_success_users REAL,payload_json TEXT,updated_at TEXT
        );
        INSERT INTO ad_dashboard_fact_rows
        SELECT printf('row-%08d', rowid), date,data_source,platform,account_id,country,
               media_source,campaign_id,adset_id,ad_id,impressions,cost,
               tugao_join_success_users,payload_json,updated_at
        FROM ad_dashboard_fact_rows_legacy;
        DROP TABLE ad_dashboard_fact_rows_legacy;
        """
    )
    conn.commit()
    conn.close()
    database_sha = _sha(database)
    manifest, receipt, transport_evidence = _transport(root)
    request = {
        "schema_version": "gle-g0-05-run-request-v1",
        "assessment_id": "metric-evidence-source-1",
        "requested_at": "2026-08-07T11:00:00+00:00",
        "data_cutoff_at": "2026-08-07T10:00:00+00:00",
        "subject": _subject(), "policy": _policy(),
        "windows": {
            "allocation_start": "2026-07-29", "allocation_end": "2026-07-31",
            "baseline_start": "2026-07-18", "baseline_end": "2026-07-31",
        },
        "qualified_transport_evidence": transport_evidence,
    }
    request_path = root / "request.json"
    request_path.write_text(canonical_json(request) + "\n")
    request_path.chmod(0o600)
    return database, database_sha, manifest, _sha(manifest), receipt, _sha(receipt), request


def _write(root: Path) -> tuple[Path, dict, tuple]:
    sources = _sources(root)
    database, database_sha, manifest, manifest_sha, receipt, receipt_sha, request = sources
    output = root / "artifact"
    request_raw = (canonical_json(request) + "\n").encode()
    value = write_cell_metric_evidence_artifact(
        output, request_raw,
        evidence_id="cell-metric-evidence-1",
        source_request_sha256=hashlib.sha256(request_raw).hexdigest(),
        source_snapshot_path=database, source_snapshot_sha256=database_sha,
        transport_manifest_path=manifest, transport_manifest_sha256=manifest_sha,
        transport_receipt_path=receipt, transport_receipt_sha256=receipt_sha,
    )
    return output, value, sources


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_round_trip_rederives_verified_subset_and_preserves_gaps(tmp_path: Path) -> None:
    output, manifest, sources = _write(tmp_path)
    database, _, transport_manifest, _, transport_receipt, _, _ = sources
    loaded = load_validated_cell_metric_evidence_directory(
        output, expected_manifest_sha256=_sha(output / "manifest.json"),
        source_snapshot_path=database,
        transport_manifest_path=transport_manifest,
        transport_receipt_path=transport_receipt,
    )
    assert loaded["manifest"] == manifest
    assert set(path.name for path in output.iterdir()) == EXACT_ARTIFACT_FILES
    assert loaded["evidence"]["status"] == "REDERIVED_METRIC_SUBSET_FROM_PINNED_BYTES"
    assert loaded["evidence"]["trust_status"] == "SOURCE_CONTENT_AUTHORITY_NOT_VERIFIED"
    assert sum(item["impressions"] for item in loaded["evidence"]["cells"]) == 600
    assert sum(item["qualified_joins"] for item in loaded["evidence"]["cells"]) == 12
    assert {item["allocation_share"] for item in loaded["evidence"]["cells"]} == {"0.5"}
    assert all(item["clicks"] is None and item["installs"] is None and item["invalid_users"] is None
               for item in loaded["evidence"]["cells"])
    assert "INVALID_USER_DEFINITION_UNFROZEN" in loaded["evidence"]["reason_codes"]
    assert loaded["evidence"]["ceiling"] == CEILING
    assert loaded["coverage"]["verified_fields"] == []
    assert "cell_metrics.spend" in loaded["coverage"]["rederived_fields"]
    assert oct(output.stat().st_mode & 0o777) == "0o700"
    assert all(oct(path.stat().st_mode & 0o777) == "0o600" for path in output.iterdir())


def test_source_hash_sidecar_and_request_anchor_fail_closed(tmp_path: Path) -> None:
    database, database_sha, manifest, manifest_sha, receipt, receipt_sha, request = _sources(tmp_path)
    kwargs = dict(
        evidence_id="cell-metric-evidence-1",
        source_request_sha256="0" * 64,
        source_snapshot_path=database, source_snapshot_sha256=database_sha,
        transport_manifest_path=manifest, transport_manifest_sha256=manifest_sha,
        transport_receipt_path=receipt, transport_receipt_sha256=receipt_sha,
    )
    request_raw = (canonical_json(request) + "\n").encode()
    with pytest.raises(G005ContractError, match="G104B2_REQUEST_ANCHOR_MISMATCH"):
        write_cell_metric_evidence_artifact(tmp_path / "bad-request", request_raw, **kwargs)
    kwargs["source_request_sha256"] = hashlib.sha256((canonical_json(request) + "\n").encode()).hexdigest()
    kwargs["source_snapshot_sha256"] = "0" * 64
    with pytest.raises(G005ContractError, match="G005_SOURCE_HASH_MISMATCH"):
        write_cell_metric_evidence_artifact(tmp_path / "bad-db", request_raw, **kwargs)
    kwargs["source_snapshot_sha256"] = database_sha
    Path(str(database) + "-wal").write_bytes(b"pending")
    with pytest.raises(G005ContractError, match="G005_SOURCE_SIDECAR_PRESENT"):
        write_cell_metric_evidence_artifact(tmp_path / "sidecar", request_raw, **kwargs)


def test_full_rehash_cannot_promote_snapshot_replay_or_gate(tmp_path: Path) -> None:
    output, _, sources = _write(tmp_path)
    evidence_path = output / "cell-metric-evidence.json"
    manifest_path = output / "manifest.json"
    evidence = json.loads(evidence_path.read_text())
    evidence["ceiling"].update({"snapshot_emitted": True, "replay_eligible": True, "gate1_effect": "PASS"})
    evidence["evidence_hash"] = hash_json({key: value for key, value in evidence.items() if key != "evidence_hash"})
    evidence_raw = (canonical_json(evidence) + "\n").encode()
    evidence_path.write_bytes(evidence_raw)
    manifest = json.loads(manifest_path.read_text())
    manifest["ceiling"] = deepcopy(evidence["ceiling"])
    manifest["evidence_hash"] = evidence["evidence_hash"]
    manifest["files"]["cell-metric-evidence.json"] = {
        "sha256": hashlib.sha256(evidence_raw).hexdigest(), "size_bytes": len(evidence_raw),
    }
    manifest["manifest_hash"] = hash_json({key: value for key, value in manifest.items() if key != "manifest_hash"})
    manifest_path.write_text(canonical_json(manifest) + "\n")
    database, _, transport_manifest, _, transport_receipt, _, _ = sources
    with pytest.raises(G005ContractError, match="G104B2_ARTIFACT_DERIVATION_MISMATCH"):
        load_validated_cell_metric_evidence_directory(
            output, expected_manifest_sha256=_sha(manifest_path),
            source_snapshot_path=database,
            transport_manifest_path=transport_manifest,
            transport_receipt_path=transport_receipt,
        )


def test_cli_returns_two_and_never_claims_snapshot_or_gate(tmp_path: Path, capsys) -> None:
    database, database_sha, manifest, manifest_sha, receipt, receipt_sha, request = _sources(tmp_path)
    request_path = tmp_path / "request.json"
    rc = cli_main([
        "--request", str(request_path),
        "--expected-request-sha256", _sha(request_path),
        "--database", str(database), "--database-sha256", database_sha,
        "--qualified-transport-manifest", str(manifest),
        "--expected-qualified-transport-manifest-sha256", manifest_sha,
        "--qualified-transport-receipt", str(receipt),
        "--expected-qualified-transport-receipt-sha256", receipt_sha,
        "--evidence-id", "cell-metric-evidence-1",
        "--output-dir", str(tmp_path / "cli-artifact"),
    ])
    assert rc == 2
    result = json.loads(capsys.readouterr().out)
    assert result["snapshot_emitted"] is False
    assert result["replay_executed"] is False
    assert result["golden_eligible"] is False
    assert result["gate0_result_effect"] == "UNCHANGED"
    assert result["gate1_effect"] == "NONE"


def test_subject_binding_full_rehash_cannot_change_study(tmp_path: Path) -> None:
    database, database_sha, manifest, manifest_sha, receipt, receipt_sha, request = _sources(tmp_path)
    forged = deepcopy(request)
    forged["subject"]["study_id"] = "forged-study"
    forged_raw = (canonical_json(forged) + "\n").encode()
    with pytest.raises(G005ContractError, match="G005_EXPERIMENT_BINDING_MISMATCH"):
        write_cell_metric_evidence_artifact(
            tmp_path / "forged-study-artifact",
            forged_raw,
            evidence_id="cell-metric-evidence-forged",
            source_request_sha256=hashlib.sha256(forged_raw).hexdigest(),
            source_snapshot_path=database,
            source_snapshot_sha256=database_sha,
            transport_manifest_path=manifest,
            transport_manifest_sha256=manifest_sha,
            transport_receipt_path=receipt,
            transport_receipt_sha256=receipt_sha,
        )


def test_missing_experiment_binding_cannot_produce_subset(tmp_path: Path) -> None:
    database, _, manifest, manifest_sha, receipt, receipt_sha, request = _sources(tmp_path)
    conn = sqlite3.connect(database)
    conn.execute("DELETE FROM ad_experiment")
    conn.commit()
    conn.close()
    database_sha = _sha(database)
    request_raw = (canonical_json(request) + "\n").encode()
    with pytest.raises(G005ContractError, match="G005_EXPERIMENT_BINDING_INVALID"):
        write_cell_metric_evidence_artifact(
            tmp_path / "missing-binding-artifact",
            request_raw,
            evidence_id="cell-metric-evidence-missing-binding",
            source_request_sha256=hashlib.sha256(request_raw).hexdigest(),
            source_snapshot_path=database,
            source_snapshot_sha256=database_sha,
            transport_manifest_path=manifest,
            transport_manifest_sha256=manifest_sha,
            transport_receipt_path=receipt,
            transport_receipt_sha256=receipt_sha,
        )


def test_noncanonical_request_raw_cannot_borrow_canonical_anchor(tmp_path: Path, capsys) -> None:
    database, database_sha, manifest, manifest_sha, receipt, receipt_sha, request = _sources(tmp_path)
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(request, indent=2) + "\n")
    request_path.chmod(0o600)
    rc = cli_main([
        "--request", str(request_path),
        "--expected-request-sha256", _sha(request_path),
        "--database", str(database), "--database-sha256", database_sha,
        "--qualified-transport-manifest", str(manifest),
        "--expected-qualified-transport-manifest-sha256", manifest_sha,
        "--qualified-transport-receipt", str(receipt),
        "--expected-qualified-transport-receipt-sha256", receipt_sha,
        "--evidence-id", "cell-metric-evidence-noncanonical",
        "--output-dir", str(tmp_path / "noncanonical-artifact"),
    ])
    assert rc == 64
    assert "Traceback" not in capsys.readouterr().err
    assert not (tmp_path / "noncanonical-artifact").exists()


def test_request_permissions_and_single_link_are_required(tmp_path: Path) -> None:
    *_, request = _sources(tmp_path)
    request_path = tmp_path / "request.json"
    request_path.chmod(0o644)
    with pytest.raises(G005ContractError, match="G104B2_REQUEST_ARTIFACT_INVALID"):
        metric_evidence.read_external_canonical_json(request_path, _sha(request_path))
    request_path.chmod(0o600)
    os.link(request_path, tmp_path / "request-hardlink.json")
    with pytest.raises(G005ContractError, match="G104B2_REQUEST_ARTIFACT_INVALID"):
        metric_evidence.read_external_canonical_json(request_path, _sha(request_path))


def test_incomplete_attribution_never_enters_rederived_fields(tmp_path: Path) -> None:
    database, _, manifest, manifest_sha, receipt, receipt_sha, request = _sources(tmp_path)
    conn = sqlite3.connect(database)
    row = conn.execute(
        "SELECT row_id,payload_json FROM ad_dashboard_fact_rows "
        "WHERE data_source='TugaoFunnel' AND date='2026-07-30' AND ad_id='ad-2'"
    ).fetchone()
    payload = json.loads(row[1])
    payload["qualified_join_exact_attribution"] = False
    payload["qualified_join_attribution_status"] = "unattributed"
    conn.execute(
        "UPDATE ad_dashboard_fact_rows SET payload_json=? WHERE row_id=?",
        (json.dumps(payload), row[0]),
    )
    conn.commit()
    conn.close()
    database_sha = _sha(database)
    request_raw = (canonical_json(request) + "\n").encode()
    manifest_value = write_cell_metric_evidence_artifact(
        tmp_path / "incomplete-artifact",
        request_raw,
        evidence_id="cell-metric-evidence-incomplete",
        source_request_sha256=hashlib.sha256(request_raw).hexdigest(),
        source_snapshot_path=database,
        source_snapshot_sha256=database_sha,
        transport_manifest_path=manifest,
        transport_manifest_sha256=manifest_sha,
        transport_receipt_path=receipt,
        transport_receipt_sha256=receipt_sha,
    )
    assert manifest_value["status"] == "INCOMPLETE_METRIC_SUBSET"
    coverage = json.loads((tmp_path / "incomplete-artifact" / "coverage.json").read_text())
    assert coverage["verified_fields"] == []
    assert coverage["rederived_fields"] == []


def test_payload_limit_fails_before_payload_parser(tmp_path: Path, monkeypatch) -> None:
    database, _, manifest, manifest_sha, receipt, receipt_sha, request = _sources(tmp_path)
    conn = sqlite3.connect(database)
    conn.execute(
        "UPDATE ad_dashboard_fact_rows SET payload_json=? WHERE row_id=(SELECT row_id FROM ad_dashboard_fact_rows LIMIT 1)",
        ("x" * 64,),
    )
    conn.commit()
    conn.close()
    database_sha = _sha(database)
    request_raw = (canonical_json(request) + "\n").encode()
    monkeypatch.setattr(metric_evidence, "MAX_PAYLOAD_BYTES", 32)
    monkeypatch.setattr(
        metric_evidence,
        "_payload",
        lambda _row: (_ for _ in ()).throw(AssertionError("payload parser called")),
    )
    with pytest.raises(G005ContractError, match="G104B2_SOURCE_PAYLOAD_LIMIT_EXCEEDED"):
        write_cell_metric_evidence_artifact(
            tmp_path / "oversize-artifact",
            request_raw,
            evidence_id="cell-metric-evidence-oversize",
            source_request_sha256=hashlib.sha256(request_raw).hexdigest(),
            source_snapshot_path=database,
            source_snapshot_sha256=database_sha,
            transport_manifest_path=manifest,
            transport_manifest_sha256=manifest_sha,
            transport_receipt_path=receipt,
            transport_receipt_sha256=receipt_sha,
        )


def test_total_payload_limit_fails_before_payload_parser(tmp_path: Path, monkeypatch) -> None:
    database, _, manifest, manifest_sha, receipt, receipt_sha, request = _sources(tmp_path)
    database_sha = _sha(database)
    request_raw = (canonical_json(request) + "\n").encode()
    monkeypatch.setattr(metric_evidence, "MAX_PAYLOAD_BYTES", 1024 * 1024)
    monkeypatch.setattr(metric_evidence, "MAX_TOTAL_PAYLOAD_BYTES", 1)
    monkeypatch.setattr(
        metric_evidence,
        "_payload",
        lambda _row: (_ for _ in ()).throw(AssertionError("payload parser called")),
    )
    with pytest.raises(G005ContractError, match="G104B2_SOURCE_PAYLOAD_LIMIT_EXCEEDED"):
        write_cell_metric_evidence_artifact(
            tmp_path / "aggregate-oversize-artifact",
            request_raw,
            evidence_id="cell-metric-evidence-aggregate-oversize",
            source_request_sha256=hashlib.sha256(request_raw).hexdigest(),
            source_snapshot_path=database,
            source_snapshot_sha256=database_sha,
            transport_manifest_path=manifest,
            transport_manifest_sha256=manifest_sha,
            transport_receipt_path=receipt,
            transport_receipt_sha256=receipt_sha,
        )


def test_total_materialized_limit_fails_before_fact_materialization(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database, _, manifest, manifest_sha, receipt, receipt_sha, request = _sources(tmp_path)
    database_sha = _sha(database)
    request_raw = (canonical_json(request) + "\n").encode()
    monkeypatch.setattr(metric_evidence, "MAX_TOTAL_MATERIALIZED_BYTES", 1)
    monkeypatch.setattr(
        metric_evidence,
        "_materialize_fact_rows",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("fact materializer called")
        ),
    )
    with pytest.raises(
        G005ContractError,
        match="G104B2_SOURCE_MATERIALIZED_BYTES_LIMIT_EXCEEDED",
    ):
        write_cell_metric_evidence_artifact(
            tmp_path / "aggregate-materialized-oversize-artifact",
            request_raw,
            evidence_id="cell-metric-evidence-aggregate-materialized-oversize",
            source_request_sha256=hashlib.sha256(request_raw).hexdigest(),
            source_snapshot_path=database,
            source_snapshot_sha256=database_sha,
            transport_manifest_path=manifest,
            transport_manifest_sha256=manifest_sha,
            transport_receipt_path=receipt,
            transport_receipt_sha256=receipt_sha,
        )


def test_row_limit_fails_before_payload_parser(tmp_path: Path, monkeypatch) -> None:
    database, _, manifest, manifest_sha, receipt, receipt_sha, request = _sources(tmp_path)
    database_sha = _sha(database)
    request_raw = (canonical_json(request) + "\n").encode()
    monkeypatch.setattr(metric_evidence, "MAX_FACT_ROWS", 1)
    monkeypatch.setattr(
        metric_evidence,
        "_payload",
        lambda _row: (_ for _ in ()).throw(AssertionError("payload parser called")),
    )
    with pytest.raises(G005ContractError, match="G005_SOURCE_ROW_LIMIT_EXCEEDED"):
        write_cell_metric_evidence_artifact(
            tmp_path / "row-oversize-artifact",
            request_raw,
            evidence_id="cell-metric-evidence-row-oversize",
            source_request_sha256=hashlib.sha256(request_raw).hexdigest(),
            source_snapshot_path=database,
            source_snapshot_sha256=database_sha,
            transport_manifest_path=manifest,
            transport_manifest_sha256=manifest_sha,
            transport_receipt_path=receipt,
            transport_receipt_sha256=receipt_sha,
        )


@pytest.mark.parametrize("field", ["row_id", "campaign_id", "updated_at"])
def test_nonpayload_field_limit_fails_before_fact_materialization(
    tmp_path: Path,
    monkeypatch,
    field: str,
) -> None:
    database, _, manifest, manifest_sha, receipt, receipt_sha, request = _sources(tmp_path)
    conn = sqlite3.connect(database)
    conn.execute(
        f'UPDATE ad_dashboard_fact_rows SET "{field}"=? '
        "WHERE row_id=(SELECT row_id FROM ad_dashboard_fact_rows LIMIT 1)",
        ("x" * 64,),
    )
    conn.commit()
    conn.close()
    database_sha = _sha(database)
    request_raw = (canonical_json(request) + "\n").encode()
    monkeypatch.setattr(metric_evidence, "MAX_VARIABLE_FIELD_BYTES", 32)
    monkeypatch.setattr(
        metric_evidence,
        "_materialize_fact_rows",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("fact materializer called")
        ),
    )
    monkeypatch.setattr(
        metric_evidence,
        "_payload",
        lambda _row: (_ for _ in ()).throw(AssertionError("payload parser called")),
    )
    with pytest.raises(G005ContractError, match="G104B2_SOURCE_FIELD_LIMIT_EXCEEDED"):
        write_cell_metric_evidence_artifact(
            tmp_path / f"oversize-{field}-artifact",
            request_raw,
            evidence_id=f"cell-metric-evidence-oversize-{field}",
            source_request_sha256=hashlib.sha256(request_raw).hexdigest(),
            source_snapshot_path=database,
            source_snapshot_sha256=database_sha,
            transport_manifest_path=manifest,
            transport_manifest_sha256=manifest_sha,
            transport_receipt_path=receipt,
            transport_receipt_sha256=receipt_sha,
        )


def _assert_ancillary_preflight_failure(
    tmp_path: Path,
    monkeypatch,
    *,
    database: Path,
    manifest: Path,
    manifest_sha: str,
    receipt: Path,
    receipt_sha: str,
    request: dict,
    error: str,
) -> None:
    request_raw = (canonical_json(request) + "\n").encode()
    for name in (
        "_materialize_fact_rows",
        "_materialize_sync_rows",
        "_materialize_experiment_rows",
        "_parse_control_definition",
        "_payload",
    ):
        monkeypatch.setattr(
            metric_evidence,
            name,
            lambda *_args, _name=name, **_kwargs: (_ for _ in ()).throw(
                AssertionError(f"{_name} called")
            ),
        )
    with pytest.raises(G005ContractError, match=error):
        write_cell_metric_evidence_artifact(
            tmp_path / f"{error.lower()}-artifact",
            request_raw,
            evidence_id=f"cell-metric-evidence-{error.lower()}",
            source_request_sha256=hashlib.sha256(request_raw).hexdigest(),
            source_snapshot_path=database,
            source_snapshot_sha256=_sha(database),
            transport_manifest_path=manifest,
            transport_manifest_sha256=manifest_sha,
            transport_receipt_path=receipt,
            transport_receipt_sha256=receipt_sha,
        )


def test_excessive_duplicate_sync_rows_fail_before_materialization(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database, _, manifest, manifest_sha, receipt, receipt_sha, request = _sources(tmp_path)
    conn = sqlite3.connect(database)
    conn.executemany(
        "INSERT INTO ad_dashboard_sync_state VALUES('all','2026-07-29','ok')",
        [()] * 32,
    )
    conn.commit()
    conn.close()
    _assert_ancillary_preflight_failure(
        tmp_path, monkeypatch, database=database, manifest=manifest,
        manifest_sha=manifest_sha, receipt=receipt, receipt_sha=receipt_sha,
        request=request, error="G104B2_SYNC_ROW_LIMIT_EXCEEDED",
    )


def test_duplicate_experiment_rows_fail_before_materialization(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database, _, manifest, manifest_sha, receipt, receipt_sha, request = _sources(tmp_path)
    conn = sqlite3.connect(database)
    conn.execute(
        "INSERT INTO ad_experiment SELECT * FROM ad_experiment LIMIT 1"
    )
    conn.commit()
    conn.close()
    _assert_ancillary_preflight_failure(
        tmp_path, monkeypatch, database=database, manifest=manifest,
        manifest_sha=manifest_sha, receipt=receipt, receipt_sha=receipt_sha,
        request=request, error="G104B2_EXPERIMENT_ROW_LIMIT_EXCEEDED",
    )


def test_oversize_control_definition_fails_before_materialization_or_json_parse(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database, _, manifest, manifest_sha, receipt, receipt_sha, request = _sources(tmp_path)
    conn = sqlite3.connect(database)
    conn.execute(
        "UPDATE ad_experiment SET control_definition_json=? "
        "WHERE experiment_id=(SELECT experiment_id FROM ad_experiment LIMIT 1)",
        ("x" * 64,),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(metric_evidence, "MAX_EXPERIMENT_FIELD_BYTES", 32)
    _assert_ancillary_preflight_failure(
        tmp_path, monkeypatch, database=database, manifest=manifest,
        manifest_sha=manifest_sha, receipt=receipt, receipt_sha=receipt_sha,
        request=request, error="G104B2_EXPERIMENT_FIELD_INVALID",
    )


def test_ancillary_preflights_return_only_metadata_or_scalar_aggregates(
    tmp_path: Path,
) -> None:
    database, _, _, _, _, _, request = _sources(tmp_path)
    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    try:
        metric_evidence._preflight_sync_rows(
            conn, "2026-07-18", "2026-07-31", "all",
        )
        metric_evidence._preflight_experiment_rows(
            conn,
            [str(cell["experiment_id"]) for cell in request["subject"]["cells"]],
        )
    finally:
        conn.close()
    ancillary = [
        statement.lstrip().upper()
        for statement in statements
        if "AD_DASHBOARD_SYNC_STATE" in statement.upper()
        or "AD_EXPERIMENT" in statement.upper()
    ]
    assert ancillary
    assert all(
        statement.startswith("SELECT TYPEOF(")
        or statement.startswith("SELECT COUNT(*),COUNT(DISTINCT")
        for statement in ancillary
    )


def test_missing_admitted_timestamp_fails_closed(tmp_path: Path) -> None:
    database, _, manifest, manifest_sha, receipt, receipt_sha, request = _sources(tmp_path)
    conn = sqlite3.connect(database)
    conn.execute(
        "UPDATE ad_dashboard_fact_rows SET updated_at=NULL "
        "WHERE row_id=(SELECT row_id FROM ad_dashboard_fact_rows "
        "WHERE data_source='Meta' AND date='2026-07-29' LIMIT 1)"
    )
    conn.commit()
    conn.close()
    database_sha = _sha(database)
    request_raw = (canonical_json(request) + "\n").encode()
    with pytest.raises(G005ContractError, match="G104B2_SOURCE_TIMESTAMP_MISSING"):
        write_cell_metric_evidence_artifact(
            tmp_path / "missing-timestamp-artifact",
            request_raw,
            evidence_id="cell-metric-evidence-missing-timestamp",
            source_request_sha256=hashlib.sha256(request_raw).hexdigest(),
            source_snapshot_path=database,
            source_snapshot_sha256=database_sha,
            transport_manifest_path=manifest,
            transport_manifest_sha256=manifest_sha,
            transport_receipt_path=receipt,
            transport_receipt_sha256=receipt_sha,
        )


def test_duplicate_normalized_source_grain_fails_closed(tmp_path: Path) -> None:
    database, _, manifest, manifest_sha, receipt, receipt_sha, request = _sources(tmp_path)
    conn = sqlite3.connect(database)
    conn.execute(
        "INSERT INTO ad_dashboard_fact_rows "
        "SELECT 'duplicate-row',date,'META',platform,account_id,'MX',media_source,"
        "campaign_id,adset_id,ad_id,impressions,cost,tugao_join_success_users,payload_json,updated_at "
        "FROM ad_dashboard_fact_rows WHERE data_source='Meta' AND date='2026-07-29' LIMIT 1"
    )
    conn.commit()
    conn.close()
    database_sha = _sha(database)
    request_raw = (canonical_json(request) + "\n").encode()
    with pytest.raises(G005ContractError, match="G104B2_SOURCE_GRAIN_DUPLICATE"):
        write_cell_metric_evidence_artifact(
            tmp_path / "duplicate-grain-artifact",
            request_raw,
            evidence_id="cell-metric-evidence-duplicate-grain",
            source_request_sha256=hashlib.sha256(request_raw).hexdigest(),
            source_snapshot_path=database,
            source_snapshot_sha256=database_sha,
            transport_manifest_path=manifest,
            transport_manifest_sha256=manifest_sha,
            transport_receipt_path=receipt,
            transport_receipt_sha256=receipt_sha,
        )


@pytest.mark.parametrize("field,value", [
    ("source_metric", "caller-defined-joins"),
    ("qualification_version", "caller-defined-qualification"),
])
def test_policy_full_rehash_cannot_change_source_contract(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    database, database_sha, manifest, manifest_sha, receipt, receipt_sha, request = _sources(tmp_path)
    forged = deepcopy(request)
    forged["policy"][field] = value
    forged_raw = (canonical_json(forged) + "\n").encode()
    with pytest.raises(G005ContractError, match="G005_POLICY_VERSION_MISMATCH"):
        write_cell_metric_evidence_artifact(
            tmp_path / f"forged-{field}-artifact",
            forged_raw,
            evidence_id=f"cell-metric-evidence-forged-{field}",
            source_request_sha256=hashlib.sha256(forged_raw).hexdigest(),
            source_snapshot_path=database,
            source_snapshot_sha256=database_sha,
            transport_manifest_path=manifest,
            transport_manifest_sha256=manifest_sha,
            transport_receipt_path=receipt,
            transport_receipt_sha256=receipt_sha,
        )


def test_stable_named_file_rejects_name_replacement(tmp_path: Path) -> None:
    source = tmp_path / "source.json"
    source.write_bytes(b"{}\n")
    source.chmod(0o600)
    moved = tmp_path / "moved.json"
    with pytest.raises(G005ContractError, match="G104B2_CHANGED"):
        with metric_evidence._open_stable_named_file(
            source,
            maximum=1024,
            code="G104B2_CHANGED",
            required_mode=0o600,
        ):
            source.rename(moved)
            source.write_bytes(b"{}\n")
            source.chmod(0o600)


def test_evidence_row_bound_does_not_change_legacy_g005_collector(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database = tmp_path / "legacy-snapshot.db"
    database_sha = _snapshot(database)
    request = {
        "data_cutoff_at": "2026-08-07T10:00:00+00:00",
        "natural_evidence_not_before_date": "2026-07-29",
        "subject": _subject(),
        "policy": _policy(),
        "windows": {
            "allocation_start": "2026-07-29",
            "allocation_end": "2026-07-31",
            "baseline_start": "2026-07-18",
            "baseline_end": "2026-07-31",
        },
    }
    monkeypatch.setattr(metric_evidence, "MAX_FACT_ROWS", 1)
    allocation, qualified, baseline, binding = legacy_collect(
        database, request, database_sha,
    )
    assert allocation["complete_days"] == 3
    assert qualified["exact_attributed_qualified_joins"] == 12
    assert baseline["complete_days"] == 14
    assert len(binding["bindings"]) == 2
