from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.growth.common import canonical_json, payload_hash
from app.growth.evaluation_mutation_provenance import (
    CEILING,
    EXACT_ARTIFACT_FILES,
    MutationProvenanceError,
    derive_mutation_provenance,
    load_validated_mutation_provenance_directory,
    read_external_request,
    write_mutation_provenance_artifact,
)
from app.growth.schema import ensure_growth_schema


NOW = datetime(2026, 8, 9, 8, 0, tzinfo=timezone.utc)


def _json_bytes(value: object) -> bytes:
    return (canonical_json(value) + "\n").encode()


def _subject() -> dict:
    return {
        "account_id": "account-1",
        "study_id": "study-1",
        "launch_id": "launch-1",
        "campaign_id": "campaign-1",
        "cells": [
            {
                "cell_id": "C1", "experiment_id": "experiment-C1",
                "study_cell_id": "study-cell-1", "adset_id": "adset-1", "ad_id": "ad-1",
            },
            {
                "cell_id": "C2", "experiment_id": "experiment-C2",
                "study_cell_id": "study-cell-2", "adset_id": "adset-2", "ad_id": "ad-2",
            },
        ],
    }


def _request(snapshot_sha: str, **updates: object) -> tuple[dict, bytes, str]:
    subject = _subject()
    fields = [
        {"object_type": "AD", "object_id": cell["ad_id"], "field": "status"}
        for cell in subject["cells"]
    ] + [
        {"object_type": "ADSET", "object_id": cell["adset_id"], "field": "status"}
        for cell in subject["cells"]
    ] + [{"object_type": "CAMPAIGN", "object_id": "campaign-1", "field": "status"}]
    fields.sort(key=lambda item: (item["object_type"], item["object_id"], item["field"]))
    request = {
        "schema_version": "gle-e04-s04-01b3-mutation-provenance-request-v1",
        "evidence_id": "mutation-evidence-1",
        "requested_at": NOW.isoformat(),
        "window_start": (NOW - timedelta(days=2)).isoformat(),
        "data_cutoff_at": (NOW - timedelta(minutes=1)).isoformat(),
        "subject": subject,
        "relevant_fields": fields,
        "source_snapshot": {"logical_source_id": "automation-db-snapshot-1", "sha256": snapshot_sha},
        "request_hash": "",
    }
    request.update(updates)
    request["request_hash"] = payload_hash({key: value for key, value in request.items() if key != "request_hash"})
    raw = _json_bytes(request)
    return request, raw, hashlib.sha256(raw).hexdigest()


def _plan(action_type: str = "REACTIVATE_AD") -> dict:
    return {
        "plan_id": "action-activate",
        "plan_version": "NEW_ACCOUNT_DELIVERY_BATCH_V1",
        "action_type": action_type,
        "target_account_id": "account-1",
        "target_object_type": "LAUNCH",
        "target_object_id": "launch-1",
        "launch_id": "launch-1",
        "experiment_ids": ["experiment-C1", "experiment-C2"],
        "steps": {
            "CAMPAIGN_STATUS_UPDATE": {
                "target_id": "campaign-1", "object_key": "campaign_id",
                "before_status": "PAUSED", "status": "ACTIVE",
            },
        },
        "cells": [
            {
                "cell_key": f"C{index}", "experiment_id": f"experiment-C{index}",
                "steps": {
                    "ADSET_STATUS_UPDATE": {
                        "target_id": f"adset-{index}", "object_key": f"c{index}_adset_id",
                        "before_status": "PAUSED", "status": "ACTIVE",
                    },
                    "AD_STATUS_UPDATE": {
                        "target_id": f"ad-{index}", "object_key": f"c{index}_ad_id",
                        "before_status": "PAUSED", "status": "ACTIVE",
                    },
                },
            }
            for index in (1, 2)
        ],
        "max_write_requests": 5,
        "expires_at": (NOW + timedelta(minutes=10)).isoformat(),
    }


def _database(
    path: Path,
    *,
    include_chain: bool = True,
    include_retained: bool = True,
    invalid_verify: bool = False,
    action_type: str = "REACTIVATE_AD",
) -> str:
    with sqlite3.connect(path) as conn:
        ensure_growth_schema(conn)
        if include_chain:
            plan = _plan(action_type)
            created = (NOW - timedelta(hours=2)).isoformat()
            approved = (NOW - timedelta(minutes=100)).isoformat()
            consumed = (NOW - timedelta(minutes=90)).isoformat()
            task_created = (NOW - timedelta(minutes=80)).isoformat()
            task_finished = (NOW - timedelta(minutes=69)).isoformat()
            action_updated = (NOW - timedelta(minutes=68)).isoformat()
            approval = {
                "approval_id": "approval-activate", "status": "APPROVED",
                "approved_by": "operator:reviewer", "approved_at": approved,
                "expires_at": plan["expires_at"], "consumed_at": consumed,
            }
            object_ids = {
                "campaign_id": "campaign-1", "study_id": "study-1",
                "c1_study_cell_id": "study-cell-1", "c1_adset_id": "adset-1", "c1_ad_id": "ad-1",
                "c2_study_cell_id": "study-cell-2", "c2_adset_id": "adset-2", "c2_ad_id": "ad-2",
            }
            conn.execute(
                """INSERT INTO growth_operation_action
                (operation_action_id,decision_id,action_type,action_scope,target_type,target_id,
                 payload_json,status,created_by,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                ("action-activate", "decision-1", action_type, "EXPERIMENT", "LAUNCH", "launch-1",
                 canonical_json({"plan": plan}), "VERIFIED", "operator:planner", created, action_updated),
            )
            conn.execute(
                """INSERT INTO growth_operation_approval
                (approval_id,operation_action_id,plan_hash,plan_json,status,proposed_by,approved_by,
                 approved_at,expires_at,consumed_at,idempotency_key,request_hash,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("approval-activate", "action-activate", payload_hash(plan), canonical_json(plan), "APPROVED",
                 "operator:planner", "operator:reviewer", approved, plan["expires_at"], consumed,
                 "approval-key", payload_hash({"operation_action_id": "action-activate", "plan": plan}),
                 created, consumed),
            )
            task_payload = {
                "plan": plan, "approval": approval, "account_id": "account-1",
                "execution_mode": "live", "action_type": "REACTIVATE_AD",
            }
            conn.execute(
                """INSERT INTO meta_execution_task
                (execution_task_id,operation_action_id,idempotency_key,request_hash,status,current_step,
                 payload_json,meta_object_ids_json,created_at,updated_at,finished_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                ("task-activate", "action-activate", "task-key", payload_hash({
                    "operation_action_id": "action-activate", "payload": task_payload,
                }), "SUCCESS", "RECEIPT", canonical_json(task_payload), canonical_json(object_ids),
                 task_created, task_finished, task_finished),
            )
            steps = [
                "CAMPAIGN_STATUS_UPDATE", "C1_ADSET_STATUS_UPDATE", "C1_AD_STATUS_UPDATE",
                "C2_ADSET_STATUS_UPDATE", "C2_AD_STATUS_UPDATE", "VERIFY", "RECEIPT",
            ]
            step_objects = {
                "CAMPAIGN_STATUS_UPDATE": ("campaign_id", "campaign-1"),
                "C1_ADSET_STATUS_UPDATE": ("c1_adset_id", "adset-1"),
                "C1_AD_STATUS_UPDATE": ("c1_ad_id", "ad-1"),
                "C2_ADSET_STATUS_UPDATE": ("c2_adset_id", "adset-2"),
                "C2_AD_STATUS_UPDATE": ("c2_ad_id", "ad-2"),
            }
            final_statuses = {key: "ACTIVE" for key, _object_id in step_objects.values()}
            for index, step in enumerate(steps):
                result = {"status": "SUCCESS"}
                if step in step_objects:
                    object_key, object_id = step_objects[step]
                    result = {
                        "status": "SUCCESS", "meta_object_ids": {object_key: object_id},
                        "result": {"success": True},
                    }
                    verification = {
                        "status": "SUCCESS", "meta_object_ids": {object_key: object_id},
                        "object_statuses": {object_key: "ACTIVE"},
                    }
                else:
                    verification = {
                        "status": "SUCCESS", "meta_object_ids": object_ids,
                        "object_statuses": final_statuses,
                    }
                status = "VERIFIED"
                if step == "VERIFY":
                    result = {}
                    if invalid_verify:
                        verification = {"status": "UNKNOWN"}
                elif step == "RECEIPT":
                    result = {"final_status": "SUCCESS"}
                    status = "SUCCESS"
                conn.execute(
                    """INSERT INTO meta_execution_task_receipt
                    (receipt_id,execution_task_id,step_name,step_status,step_result_json,
                     meta_object_ids_json,verification_result_json,created_at)
                    VALUES (?,?,?,?,?,?,?,?)""",
                    (f"receipt-{index:02d}", "task-activate", step, status, canonical_json(result),
                     canonical_json(object_ids), canonical_json(verification),
                     (NOW - timedelta(minutes=70) + timedelta(seconds=index)).isoformat()),
                )
        if include_retained:
            conn.execute(
                """INSERT INTO ad_experiment_events
                (event_id,experiment_id,from_state,to_state,event_type,actor,reason,evidence_json,created_at)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                ("event-1", "experiment-C1", "RUNNING", "MATURING", "OBSERVATION_WINDOW_REACHED",
                 "system", "window", "{}", (NOW - timedelta(minutes=60)).isoformat()),
            )
        conn.commit()
    os.chmod(path, 0o600)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _derive(tmp_path: Path, **database_kwargs: object):
    database = tmp_path / "snapshot.db"
    database_sha = _database(database, **database_kwargs)
    _request_value, request_raw, request_sha = _request(database_sha)
    return database, database_sha, request_raw, request_sha, derive_mutation_provenance(
        request_raw,
        expected_request_sha256=request_sha,
        source_snapshot_path=database,
        expected_source_snapshot_sha256=database_sha,
    )


def test_receipt_backed_status_subset_and_retained_event_are_derived(tmp_path: Path):
    _database_path, _database_sha, _raw, _raw_sha, (_request_value, events, coverage, assessment) = _derive(tmp_path)
    assert assessment["status"] == "RECONCILED_GLE_RECEIPT_OBSERVATION_SUBSET"
    assert coverage["normalized_events"] == {
        "gle_receipt_chain_observations": 5, "local_retained_context_rows": 1,
    }
    assert len(events["events"]) == 5
    gle = [item for item in events["events"] if item["source_class"] == "GLE_RECEIPT_CHAIN_OBSERVATION"]
    assert {item["object_id"] for item in gle} == {"campaign-1", "adset-1", "adset-2", "ad-1", "ad-2"}
    assert all(item["receipt_chain_status"] == "PLAN_APPROVAL_TASK_VERIFY_RECEIPT_CLOSED" for item in gle)
    assert all(item["changed_at"] is None for item in gle)
    assert all(item["plan_claim"] == "APPROVED_INTENT_ONLY" for item in gle)
    assert all(item["after_claim"] == "GET_READBACK_OBSERVED" for item in gle)
    assert "ACTUAL_BEFORE_NOT_OBSERVED" in coverage["reason_codes"]
    assert "MUTATION_TIME_NOT_OBSERVED" in coverage["reason_codes"]
    assert assessment["ceiling"] == CEILING
    assert assessment["ceiling"]["complete_event_journal"] is False


def test_invalid_final_verify_cannot_emit_gle_events(tmp_path: Path):
    _database_path, _database_sha, _raw, _raw_sha, (_request_value, events, coverage, assessment) = _derive(
        tmp_path, invalid_verify=True,
    )
    assert coverage["normalized_events"] == {
        "gle_receipt_chain_observations": 0, "local_retained_context_rows": 1,
    }
    assert "GLE_FINAL_VERIFY_INVALID" in coverage["reason_codes"]
    assert assessment["status"] == "INCOMPLETE_MUTATION_PROVENANCE"
    assert all(item["source_class"] != "GLE_RECEIPT_CHAIN_OBSERVATION" for item in events["events"])


def test_empty_step_readback_cannot_emit_receipt_observations(tmp_path: Path):
    database = tmp_path / "snapshot.db"
    _database(database)
    with sqlite3.connect(database) as conn:
        conn.execute(
            "UPDATE meta_execution_task_receipt SET verification_result_json=? "
            "WHERE step_name='CAMPAIGN_STATUS_UPDATE'",
            (canonical_json({"status": "SUCCESS"}),),
        )
        conn.commit()
    os.chmod(database, 0o600)
    database_sha = hashlib.sha256(database.read_bytes()).hexdigest()
    _request_value, request_raw, request_sha = _request(database_sha)
    _request_value, events, coverage, _assessment = derive_mutation_provenance(
        request_raw, expected_request_sha256=request_sha,
        source_snapshot_path=database, expected_source_snapshot_sha256=database_sha,
    )
    assert events["events"] == []
    assert "GLE_RECEIPT_OBJECT_OR_VALUE_INVALID" in coverage["reason_codes"]


def test_idempotent_already_active_step_is_not_a_mutation_observation(tmp_path: Path):
    database = tmp_path / "snapshot.db"
    _database(database)
    with sqlite3.connect(database) as conn:
        conn.execute(
            "UPDATE meta_execution_task_receipt SET step_result_json=? "
            "WHERE step_name='CAMPAIGN_STATUS_UPDATE'",
            (canonical_json({
                "status": "SUCCESS", "meta_object_ids": {"campaign_id": "campaign-1"},
                "result": {"id": "campaign-1", "status": "ACTIVE", "already_target_status": True},
            }),),
        )
        conn.commit()
    os.chmod(database, 0o600)
    database_sha = hashlib.sha256(database.read_bytes()).hexdigest()
    _request_value, request_raw, request_sha = _request(database_sha)
    _request_value, events, coverage, _assessment = derive_mutation_provenance(
        request_raw, expected_request_sha256=request_sha,
        source_snapshot_path=database, expected_source_snapshot_sha256=database_sha,
    )
    assert events["events"] == []
    assert "GLE_RECEIPT_OBJECT_OR_VALUE_INVALID" in coverage["reason_codes"]


def test_forged_task_request_hash_and_expired_chain_cannot_emit(tmp_path: Path):
    database = tmp_path / "snapshot.db"
    _database(database)
    with sqlite3.connect(database) as conn:
        conn.execute("UPDATE meta_execution_task SET request_hash='forged'")
        conn.commit()
    os.chmod(database, 0o600)
    database_sha = hashlib.sha256(database.read_bytes()).hexdigest()
    _request_value, request_raw, request_sha = _request(database_sha)
    _request_value, events, coverage, _assessment = derive_mutation_provenance(
        request_raw, expected_request_sha256=request_sha,
        source_snapshot_path=database, expected_source_snapshot_sha256=database_sha,
    )
    assert events["events"] == []
    assert "GLE_PLAN_APPROVAL_TASK_BINDING_INVALID" in coverage["reason_codes"]


def test_noncanonical_offset_and_post_cutoff_state_fail_closed(tmp_path: Path):
    database = tmp_path / "snapshot.db"
    _database(database)
    with sqlite3.connect(database) as conn:
        conn.execute(
            "UPDATE ad_experiment_events SET created_at='2026-08-09T03:59:30-04:00'"
        )
        conn.commit()
    os.chmod(database, 0o600)
    database_sha = hashlib.sha256(database.read_bytes()).hexdigest()
    _request_value, request_raw, request_sha = _request(database_sha)
    with pytest.raises(MutationProvenanceError, match="TIME_INVALID"):
        derive_mutation_provenance(
            request_raw, expected_request_sha256=request_sha,
            source_snapshot_path=database, expected_source_snapshot_sha256=database_sha,
        )


def test_post_cutoff_action_or_task_state_cannot_emit(tmp_path: Path):
    database = tmp_path / "snapshot.db"
    _database(database)
    with sqlite3.connect(database) as conn:
        conn.execute(
            "UPDATE growth_operation_action SET updated_at='2026-08-09T09:00:00+00:00'"
        )
        conn.execute(
            "UPDATE meta_execution_task SET updated_at='2026-08-09T09:00:00+00:00', "
            "finished_at='2026-08-09T09:00:00+00:00'"
        )
        conn.commit()
    os.chmod(database, 0o600)
    database_sha = hashlib.sha256(database.read_bytes()).hexdigest()
    _request_value, request_raw, request_sha = _request(database_sha)
    _request_value, events, coverage, _assessment = derive_mutation_provenance(
        request_raw, expected_request_sha256=request_sha,
        source_snapshot_path=database, expected_source_snapshot_sha256=database_sha,
    )
    assert events["events"] == []
    assert "GLE_PLAN_APPROVAL_TASK_BINDING_INVALID" in coverage["reason_codes"]


def test_task_object_denominator_mismatch_cannot_emit_gle_events(tmp_path: Path):
    database = tmp_path / "snapshot.db"
    _database(database)
    with sqlite3.connect(database) as conn:
        value = json.loads(conn.execute(
            "SELECT meta_object_ids_json FROM meta_execution_task WHERE execution_task_id='task-activate'"
        ).fetchone()[0])
        value["study_id"] = "forged-study"
        conn.execute(
            "UPDATE meta_execution_task SET meta_object_ids_json=? WHERE execution_task_id='task-activate'",
            (canonical_json(value),),
        )
        conn.execute(
            "UPDATE meta_execution_task_receipt SET meta_object_ids_json=? WHERE execution_task_id='task-activate'",
            (canonical_json(value),),
        )
        conn.commit()
    os.chmod(database, 0o600)
    database_sha = hashlib.sha256(database.read_bytes()).hexdigest()
    _request_value, request_raw, request_sha = _request(database_sha)
    _request_value, events, coverage, _assessment = derive_mutation_provenance(
        request_raw, expected_request_sha256=request_sha,
        source_snapshot_path=database, expected_source_snapshot_sha256=database_sha,
    )
    assert not [item for item in events["events"] if item["source_class"] == "GLE_RECEIPT_CHAIN_OBSERVATION"]
    assert "GLE_TASK_OBJECT_DENOMINATOR_INVALID" in coverage["reason_codes"]


def test_empty_retained_surface_is_not_no_mutation_proof(tmp_path: Path):
    _database_path, _database_sha, _raw, _raw_sha, (_request_value, events, coverage, assessment) = _derive(
        tmp_path, include_chain=False, include_retained=False,
    )
    assert events["events"] == []
    assert assessment["status"] == "NO_MUTATIONS_OBSERVED_WITH_INCOMPLETE_COVERAGE"
    assert "NO_RETAINED_ROWS_OBSERVED" in coverage["reason_codes"]
    assert coverage["complete_event_journal"] is False


def test_non_admitted_action_type_stays_a_gap(tmp_path: Path):
    _database_path, _database_sha, _raw, _raw_sha, (_request_value, events, coverage, _assessment) = _derive(
        tmp_path, action_type="PAUSE_AD",
    )
    assert not [item for item in events["events"] if item["source_class"] == "GLE_RECEIPT_CHAIN_OBSERVATION"]
    assert "GLE_ACTION_TYPE_NOT_ADMITTED_FOR_EXACT_BEFORE_AFTER" in coverage["reason_codes"]


def test_writer_loader_exact_files_modes_and_no_replace(tmp_path: Path):
    database = tmp_path / "snapshot.db"
    database_sha = _database(database)
    _request_value, request_raw, request_sha = _request(database_sha)
    output = tmp_path / "artifact"
    manifest = write_mutation_provenance_artifact(
        output, request_raw, expected_request_sha256=request_sha,
        source_snapshot_path=database, expected_source_snapshot_sha256=database_sha,
    )
    assert set(item.name for item in output.iterdir()) == EXACT_ARTIFACT_FILES
    assert stat_mode(output) == 0o700
    assert all(stat_mode(item) == 0o600 for item in output.iterdir())
    manifest_sha = hashlib.sha256((output / "manifest.json").read_bytes()).hexdigest()
    loaded = load_validated_mutation_provenance_directory(
        output, expected_manifest_sha256=manifest_sha, source_snapshot_path=database,
    )
    assert loaded["manifest"] == manifest
    with pytest.raises(MutationProvenanceError, match="G104B3_OUTPUT_EXISTS"):
        write_mutation_provenance_artifact(
            output, request_raw, expected_request_sha256=request_sha,
            source_snapshot_path=database, expected_source_snapshot_sha256=database_sha,
        )


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def test_full_rehash_cannot_promote_ceiling_or_status(tmp_path: Path):
    database = tmp_path / "snapshot.db"
    database_sha = _database(database)
    _request_value, request_raw, request_sha = _request(database_sha)
    output = tmp_path / "artifact"
    write_mutation_provenance_artifact(
        output, request_raw, expected_request_sha256=request_sha,
        source_snapshot_path=database, expected_source_snapshot_sha256=database_sha,
    )
    assessment_path = output / "provenance-assessment.json"
    assessment = json.loads(assessment_path.read_text())
    assessment["status"] = "COMPLETE"
    assessment["ceiling"]["snapshot_emitted"] = True
    assessment["assessment_hash"] = payload_hash({key: value for key, value in assessment.items() if key != "assessment_hash"})
    assessment_path.write_bytes(_json_bytes(assessment))
    manifest_path = output / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["status"] = "COMPLETE"
    manifest["ceiling"]["snapshot_emitted"] = True
    manifest["assessment_hash"] = assessment["assessment_hash"]
    manifest["files"]["provenance-assessment.json"] = {
        "sha256": hashlib.sha256(assessment_path.read_bytes()).hexdigest(),
        "size_bytes": assessment_path.stat().st_size,
    }
    manifest["manifest_hash"] = payload_hash({key: value for key, value in manifest.items() if key != "manifest_hash"})
    manifest_path.write_bytes(_json_bytes(manifest))
    new_sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    with pytest.raises(MutationProvenanceError, match="DERIVATION_MISMATCH"):
        load_validated_mutation_provenance_directory(
            output, expected_manifest_sha256=new_sha, source_snapshot_path=database,
        )


def test_request_and_source_raw_anchors_fail_closed(tmp_path: Path):
    database = tmp_path / "snapshot.db"
    database_sha = _database(database)
    _request_value, request_raw, request_sha = _request(database_sha)
    with pytest.raises(MutationProvenanceError, match="REQUEST_ANCHOR_MISMATCH"):
        derive_mutation_provenance(
            request_raw, expected_request_sha256="0" * 64,
            source_snapshot_path=database, expected_source_snapshot_sha256=database_sha,
        )
    with pytest.raises(MutationProvenanceError, match="SOURCE_BINDING_MISMATCH"):
        derive_mutation_provenance(
            request_raw, expected_request_sha256=request_sha,
            source_snapshot_path=database, expected_source_snapshot_sha256="0" * 64,
        )


def test_request_reader_rejects_noncanonical_and_wrong_mode(tmp_path: Path):
    database = tmp_path / "snapshot.db"
    database_sha = _database(database)
    request, _raw, _sha = _request(database_sha)
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request, indent=2))
    os.chmod(path, 0o600)
    raw_sha = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(MutationProvenanceError, match="JSON_INVALID"):
        read_external_request(path, raw_sha)
    path.write_bytes(_json_bytes(request))
    os.chmod(path, 0o644)
    with pytest.raises(MutationProvenanceError, match="REQUEST_ARTIFACT_INVALID"):
        read_external_request(path, hashlib.sha256(path.read_bytes()).hexdigest())


def test_sidecar_is_rejected(tmp_path: Path):
    database = tmp_path / "snapshot.db"
    database_sha = _database(database)
    _request_value, request_raw, request_sha = _request(database_sha)
    Path(str(database) + "-wal").write_bytes(b"not-empty")
    with pytest.raises(MutationProvenanceError, match="SOURCE_SIDECAR_PRESENT"):
        derive_mutation_provenance(
            request_raw, expected_request_sha256=request_sha,
            source_snapshot_path=database, expected_source_snapshot_sha256=database_sha,
        )


def test_oversized_action_payload_rejected_before_json_decode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    database = tmp_path / "snapshot.db"
    database_sha = _database(database, include_retained=False)
    with sqlite3.connect(database) as conn:
        conn.execute("UPDATE growth_operation_action SET payload_json=?", ("x" * (2 * 1024 * 1024 + 1),))
        conn.commit()
    os.chmod(database, 0o600)
    database_sha = hashlib.sha256(database.read_bytes()).hexdigest()
    _request_value, request_raw, request_sha = _request(database_sha)
    called = False

    def forbidden(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("JSON decoder should not run before preflight")

    monkeypatch.setattr("app.growth.evaluation_mutation_provenance._decode_object", forbidden)
    with pytest.raises(MutationProvenanceError, match="SOURCE_FIELD_INVALID"):
        derive_mutation_provenance(
            request_raw, expected_request_sha256=request_sha,
            source_snapshot_path=database, expected_source_snapshot_sha256=database_sha,
        )
    assert called is False


def test_cross_table_materialization_budget_fails_before_later_materialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    database = tmp_path / "snapshot.db"
    database_sha = _database(database)
    _request_value, request_raw, request_sha = _request(database_sha)
    monkeypatch.setattr(
        "app.growth.evaluation_mutation_provenance.MAX_TOTAL_SOURCE_MATERIALIZED_BYTES", 4_000,
    )
    materialized: list[str] = []
    from app.growth import evaluation_mutation_provenance as module

    original = module._materialize_rows

    def tracked(*args, **kwargs):
        materialized.append(str(kwargs["table"]))
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "_materialize_rows", tracked)
    with pytest.raises(MutationProvenanceError, match="SOURCE_GLOBAL_TOTAL_TOO_LARGE"):
        derive_mutation_provenance(
            request_raw, expected_request_sha256=request_sha,
            source_snapshot_path=database, expected_source_snapshot_sha256=database_sha,
        )
    assert "meta_execution_task_receipt" not in materialized
    assert "ad_experiment_events" not in materialized


def test_subject_and_field_denominator_cannot_be_forged(tmp_path: Path):
    database = tmp_path / "snapshot.db"
    database_sha = _database(database)
    request, _raw, _request_sha = _request(database_sha)
    forged = deepcopy(request)
    forged["relevant_fields"] = forged["relevant_fields"][:-1]
    forged["request_hash"] = payload_hash({key: value for key, value in forged.items() if key != "request_hash"})
    raw = _json_bytes(forged)
    with pytest.raises(MutationProvenanceError, match="FIELD_DENOMINATOR_INVALID"):
        derive_mutation_provenance(
            raw, expected_request_sha256=hashlib.sha256(raw).hexdigest(),
            source_snapshot_path=database, expected_source_snapshot_sha256=database_sha,
        )


def test_window_is_capped_at_exactly_31_days(tmp_path: Path):
    database = tmp_path / "snapshot.db"
    database_sha = _database(database, include_chain=False, include_retained=False)
    exact_start = (NOW - timedelta(minutes=1) - timedelta(days=31)).isoformat()
    _request_value, raw, raw_sha = _request(database_sha, window_start=exact_start)
    derive_mutation_provenance(
        raw, expected_request_sha256=raw_sha,
        source_snapshot_path=database, expected_source_snapshot_sha256=database_sha,
    )
    too_early = (
        NOW - timedelta(minutes=1) - timedelta(days=31) - timedelta(microseconds=1)
    ).isoformat()
    _request_value, raw, raw_sha = _request(database_sha, window_start=too_early)
    with pytest.raises(MutationProvenanceError, match="TIME_ORDER_INVALID"):
        derive_mutation_provenance(
            raw, expected_request_sha256=raw_sha,
            source_snapshot_path=database, expected_source_snapshot_sha256=database_sha,
        )


def test_cli_outputs_non_promoting_status_and_exit_two(tmp_path: Path):
    database = tmp_path / "snapshot.db"
    database_sha = _database(database)
    _request_value, request_raw, request_sha = _request(database_sha)
    request_path = tmp_path / "request.json"
    request_path.write_bytes(request_raw)
    os.chmod(request_path, 0o600)
    output = tmp_path / "artifact"
    result = subprocess.run(
        [
            sys.executable, "scripts/build_gle_evaluation_mutation_provenance.py",
            "--request", str(request_path), "--expected-request-sha256", request_sha,
            "--database", str(database), "--database-sha256", database_sha,
            "--output-dir", str(output),
        ],
        cwd=Path(__file__).resolve().parents[1], text=True, capture_output=True, check=False,
    )
    assert result.returncode == 2, result.stderr
    value = json.loads(result.stdout)
    assert value["status"] == "RECONCILED_GLE_RECEIPT_OBSERVATION_SUBSET"
    assert value["complete_event_journal"] is False
    assert value["snapshot_emitted"] is False
    assert value["gate1_effect"] == "NONE"


def test_cli_invalid_input_is_64_without_traceback(tmp_path: Path):
    request = tmp_path / "request.json"
    request.write_bytes(b"{}\n")
    os.chmod(request, 0o600)
    database = tmp_path / "snapshot.db"
    database.write_bytes(b"not sqlite")
    os.chmod(database, 0o600)
    result = subprocess.run(
        [
            sys.executable, "scripts/build_gle_evaluation_mutation_provenance.py",
            "--request", str(request), "--expected-request-sha256", hashlib.sha256(request.read_bytes()).hexdigest(),
            "--database", str(database), "--database-sha256", hashlib.sha256(database.read_bytes()).hexdigest(),
            "--output-dir", str(tmp_path / "output"),
        ],
        cwd=Path(__file__).resolve().parents[1], text=True, capture_output=True, check=False,
    )
    assert result.returncode == 64
    assert "Traceback" not in result.stderr


def test_cli_malformed_nested_plan_is_64_without_traceback(tmp_path: Path):
    database = tmp_path / "snapshot.db"
    _database(database)
    with sqlite3.connect(database) as conn:
        payload = json.loads(conn.execute(
            "SELECT payload_json FROM growth_operation_action WHERE operation_action_id='action-activate'"
        ).fetchone()[0])
        payload["plan"]["cells"] = "not-a-list"
        conn.execute(
            "UPDATE growth_operation_action SET payload_json=? WHERE operation_action_id='action-activate'",
            (canonical_json(payload),),
        )
        conn.commit()
    os.chmod(database, 0o600)
    database_sha = hashlib.sha256(database.read_bytes()).hexdigest()
    _request_value, request_raw, request_sha = _request(database_sha)
    request_path = tmp_path / "request.json"
    request_path.write_bytes(request_raw)
    os.chmod(request_path, 0o600)
    result = subprocess.run(
        [
            sys.executable, "scripts/build_gle_evaluation_mutation_provenance.py",
            "--request", str(request_path), "--expected-request-sha256", request_sha,
            "--database", str(database), "--database-sha256", database_sha,
            "--output-dir", str(tmp_path / "output"),
        ],
        cwd=Path(__file__).resolve().parents[1], text=True, capture_output=True, check=False,
    )
    assert result.returncode == 64
    assert "Traceback" not in result.stderr
