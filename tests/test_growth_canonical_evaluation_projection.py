from __future__ import annotations

import copy

import pytest

from app.growth.canonical_evaluation_projection import LegacyProjectionError, project_legacy_evaluation, validate_legacy_projection


def single_row():
    return {
        "evaluation_id": "eval-1", "experiment_id": "exp-1", "checkpoint": "D1",
        "baseline_window_json": "{}", "post_window_json": "{}", "baseline_metrics_json": "{}",
        "post_metrics_json": '{"real_bind_count":1}', "data_quality_status": "PASS",
        "dedupe_version": "", "attribution_version": "", "evaluation_status": "EFFECTIVE",
        "evaluated_at": "2026-08-01T00:00:00+00:00",
    }


def test_single_legacy_projection_is_observational_and_nonbinding():
    result = project_legacy_evaluation("SINGLE_EXPERIMENT", single_row())
    assert result["checkpoint_role_hint"] == "SAFETY_CHECK"
    assert result["binding_eligible"] is False
    assert result["causal_classification"] == "OBSERVATIONAL_ONLY"
    assert result["evaluated_at"] == "2026-08-01T00:00:00Z"
    assert validate_legacy_projection(result) == result


def test_missing_is_not_rewritten_to_empty_json():
    row = single_row(); row["baseline_window_json"] = None
    result = project_legacy_evaluation("SINGLE_EXPERIMENT", row)
    assert result["evidence"]["baseline_window"] == {"status": "MISSING", "value": None}
    assert result["missing_fields"] == ["attribution_version", "baseline_window", "dedupe_version"]
    assert "LEGACY_FIELDS_MISSING" in result["reason_codes"]


def test_group_and_pair_winners_remain_nonbinding():
    group = project_legacy_evaluation("CREATIVE_GROUP", {
        "group_evaluation_id": "group-1", "launch_id": "launch-1", "checkpoint": "D7", "window_json": "{}",
        "metrics_by_experiment_json": '{"exp-2":{},"exp-1":{}}', "ranking_json": "[]",
        "winner_experiment_id": "exp-2", "decision_status": "WINNER", "data_quality_status": "PASS",
        "evidence_json": '{"causal_claim":true}', "evaluated_at": "2026-08-01T00:00:00Z",
    })
    pair = project_legacy_evaluation("AUDIENCE_PAIR", {
        "pair_evaluation_id": "pair-1", "launch_id": "launch-1", "checkpoint": "D3",
        "baseline_experiment_id": "exp-1", "challenger_experiment_id": "exp-2", "metrics_json": "{}",
        "winner_experiment_id": "exp-2", "decision_status": "PROVISIONAL", "evidence_json": "{}",
        "evaluated_at": "2026-08-01T00:00:00Z",
    })
    assert group["subject_experiment_ids"] == pair["subject_experiment_ids"] == ["exp-1", "exp-2"]
    assert group["binding_eligible"] is pair["binding_eligible"] is False
    assert group["causal_classification"] == "OBSERVATIONAL_ONLY"


@pytest.mark.parametrize("kind", ["CREATIVE_GROUP", "AUDIENCE_PAIR"])
def test_winner_must_belong_to_projected_subjects(kind):
    if kind == "CREATIVE_GROUP":
        row = {
            "group_evaluation_id": "group-1", "launch_id": "launch-1", "checkpoint": "D7",
            "window_json": "{}", "metrics_by_experiment_json": '{"exp-1":{},"exp-2":{}}',
            "ranking_json": "[]", "winner_experiment_id": "exp-999", "decision_status": "WINNER",
            "data_quality_status": "PASS", "evidence_json": "{}", "evaluated_at": "2026-08-01T00:00:00Z",
        }
    else:
        row = {
            "pair_evaluation_id": "pair-1", "launch_id": "launch-1", "checkpoint": "D3",
            "baseline_experiment_id": "exp-1", "challenger_experiment_id": "exp-2",
            "metrics_json": "{}", "winner_experiment_id": "exp-999", "decision_status": "PROVISIONAL",
            "evidence_json": "{}", "evaluated_at": "2026-08-01T00:00:00Z",
        }
    with pytest.raises(LegacyProjectionError, match="G101_PROJECTION_WINNER_SUBJECT_MISMATCH"):
        project_legacy_evaluation(kind, row)


def test_semantic_tamper_and_duplicate_subjects_fail_even_with_rehashed_projection():
    result = project_legacy_evaluation("SINGLE_EXPERIMENT", single_row())
    result["subject_experiment_ids"] = []
    from app.growth.canonical_evaluation_contracts import canonical_hash
    result["projection_hash"] = canonical_hash({key: value for key, value in result.items() if key != "projection_hash"})
    with pytest.raises(LegacyProjectionError, match="G101_LEGACY_SUBJECT_INVALID"):
        validate_legacy_projection(result)
    result = project_legacy_evaluation("SINGLE_EXPERIMENT", single_row())
    result["evidence"] = {}
    result["projection_hash"] = canonical_hash({key: value for key, value in result.items() if key != "projection_hash"})
    with pytest.raises(LegacyProjectionError, match="G101_PROJECTION_EVIDENCE_SCHEMA_INVALID"):
        validate_legacy_projection(result)


def test_invalid_json_checkpoint_and_non_utc_timestamp_fail_closed():
    row = single_row(); row["post_metrics_json"] = "{"
    with pytest.raises(LegacyProjectionError, match="G101_LEGACY_JSON_INVALID"):
        project_legacy_evaluation("SINGLE_EXPERIMENT", row)
    row = single_row(); row["checkpoint"] = "INFORMATION_LOOK"
    with pytest.raises(LegacyProjectionError, match="G101_LEGACY_CHECKPOINT_INVALID"):
        project_legacy_evaluation("SINGLE_EXPERIMENT", row)
    row = single_row(); row["evaluated_at"] = "2026-08-01T08:00:00+08:00"
    with pytest.raises(LegacyProjectionError, match="G101_LEGACY_TIMESTAMP_INVALID"):
        project_legacy_evaluation("SINGLE_EXPERIMENT", row)
