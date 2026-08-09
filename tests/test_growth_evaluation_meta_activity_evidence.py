from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.growth.evaluation_meta_activity_evidence import (
    CEILING,
    MetaActivityEvidenceError,
    canonical_json,
    capture_meta_activity_evidence,
    hash_json,
    load_validated_meta_activity_evidence_directory,
    validate_request,
    write_meta_activity_evidence_artifact,
)


NOW = datetime(2026, 8, 9, 10, 0, tzinfo=timezone.utc)


class _Response:
    def __init__(self, value: dict, status: int = 200) -> None:
        self._value = value
        self.status_code = status
        self.history: list[object] = []
        self.headers = {"Content-Length": str(len(canonical_json(value).encode("utf-8")))}

    def json(self) -> dict:
        return deepcopy(self._value)


class _Session:
    def __init__(
        self,
        *,
        activities: list[dict] | None = None,
        drift_object: str = "",
        paging: dict | None = None,
        foreign_topology: bool = False,
        study_type: str = "SPLIT_TEST",
        study_start: str = "2026-07-01T00:00:00+0000",
        study_end: str = "2026-08-31T00:00:00+0000",
        effective_status_override: str = "",
        duplicate_cell_row: bool = False,
        duplicate_cell_ad_id: bool = False,
    ) -> None:
        self.activities = deepcopy(activities or [])
        self.drift_object = drift_object
        self.paging = paging
        self.foreign_topology = foreign_topology
        self.study_type = study_type
        self.study_start = study_start
        self.study_end = study_end
        self.effective_status_override = effective_status_override
        self.duplicate_cell_row = duplicate_cell_row
        self.duplicate_cell_ad_id = duplicate_cell_ad_id
        self.calls: list[tuple[str, dict]] = []
        self.object_reads: dict[str, int] = {}
        self.active_objects = {str(item.get("object_id") or "") for item in self.activities}

    def get(self, url: str, *, params: dict, timeout: int, allow_redirects: bool) -> _Response:
        assert timeout == 25
        assert allow_redirects is False
        assert params["access_token"] == "token-only-in-memory"
        path = url.split("/v25.0/", 1)[1]
        self.calls.append((path, deepcopy(params)))
        if path == "act_account-1/activities":
            body = {"data": deepcopy(self.activities)}
            if self.paging is not None:
                body["paging"] = deepcopy(self.paging)
            return _Response(body)
        if path == "study-1":
            return _Response({
                "id": "study-1",
                "type": self.study_type,
                "start_time": self.study_start,
                "end_time": self.study_end,
            })
        if path == "study-1/cells":
            rows = [
                {"id": "study-cell-1", "ad_ids": ["ad-1"]},
                {"id": "study-cell-2", "ad_ids": ["ad-2"]},
            ]
            if self.duplicate_cell_row:
                rows.insert(1, {"id": "study-cell-1", "ad_ids": ["ad-1"]})
            if self.duplicate_cell_ad_id:
                rows[0]["ad_ids"].append("ad-1")
            return _Response({"data": rows})
        if path.endswith("/adsets"):
            index = path.split("study-cell-", 1)[1].split("/", 1)[0]
            return _Response({"data": [{
                "id": f"adset-{index}",
                "campaign_id": "foreign-campaign" if self.foreign_topology else "campaign-1",
                "account_id": "account-1",
            }]})
        if path.endswith("/campaigns"):
            return _Response({"data": [{"id": "campaign-1", "account_id": "account-1"}]})
        self.object_reads[path] = self.object_reads.get(path, 0) + 1
        status = "ACTIVE" if path in self.active_objects else "PAUSED"
        if path == self.drift_object and self.object_reads[path] > 1:
            status = "ACTIVE" if status == "PAUSED" else "PAUSED"
        value = {
            "id": path,
            "account_id": "account-1",
            "status": status,
            "effective_status": self.effective_status_override or status,
            "updated_time": "2026-08-09T09:59:00+0000",
        }
        if path != "campaign-1":
            value["campaign_id"] = "campaign-1"
        if path.startswith("ad-"):
            value["adset_id"] = path.replace("ad-", "adset-", 1)
        return _Response(value)


def _registry() -> dict:
    return {
        "schema_version": "gle-g0-04-actor-binding-registry-v1",
        "principals": [{
            "actor_id": "operator-1",
            "application_id": "app-system",
            "roles": ["ACTIVATE"],
        }],
    }


def _raw(value: dict) -> bytes:
    return (canonical_json(value) + "\n").encode("utf-8")


def _request(registry_raw: bytes, *, requested_at: datetime = NOW) -> dict:
    registry = _registry()
    value = {
        "schema_version": "gle-e04-s04-01b4-meta-activity-request-v1",
        "capture_id": "capture-1",
        "requested_at": requested_at.isoformat(),
        "window_start_at": (requested_at - timedelta(days=7)).isoformat(),
        "data_cutoff_at": requested_at.isoformat(),
        "graph_api_version": "v25.0",
        "subject": {
            "account_id": "account-1",
            "market": "MX",
            "study_id": "study-1",
            "campaign_id": "campaign-1",
            "cells": [
                {"cell_id": "C1", "study_cell_id": "study-cell-1", "adset_id": "adset-1", "ad_id": "ad-1"},
                {"cell_id": "C2", "study_cell_id": "study-cell-2", "adset_id": "adset-2", "ad_id": "ad-2"},
            ],
        },
        "study_contract": {
            "study_type": "SPLIT_TEST",
            "start_at": "2026-07-01T00:00:00+00:00",
            "end_at": "2026-08-31T00:00:00+00:00",
        },
        "activity_contract": {"allowed_event_types": ["STATUS_UPDATE"]},
        "relevant_fields": [
            {"object_type": "AD", "object_id": "ad-1", "field": "status"},
            {"object_type": "AD", "object_id": "ad-2", "field": "status"},
            {"object_type": "ADSET", "object_id": "adset-1", "field": "status"},
            {"object_type": "ADSET", "object_id": "adset-2", "field": "status"},
            {"object_type": "CAMPAIGN", "object_id": "campaign-1", "field": "status"},
        ],
        "actor_registry": {
            "raw_sha256": hashlib.sha256(registry_raw).hexdigest(),
            "semantic_hash": hash_json(registry),
        },
        "transport_policy": {
            "allowed_methods": ["GET"],
            "max_pages": 5,
            "max_events": 100,
            "clock_skew_seconds": 60,
        },
        "ceiling": deepcopy(CEILING),
        "request_hash": "",
    }
    value["request_hash"] = hash_json({key: item for key, item in value.items() if key != "request_hash"})
    return value


def _activity(
    *,
    activity_id: str = "activity-1",
    object_id: str = "campaign-1",
    actor_id: str = "operator-1",
    application_id: str = "app-system",
    event_time: str = "2026-08-09T05:00:00-04:00",
    object_type: str = "",
    event_type: str = "STATUS_UPDATE",
) -> dict:
    if not object_type:
        object_type = "CAMPAIGN" if object_id.startswith("campaign-") else (
            "ADSET" if object_id.startswith("adset-") else "AD"
        )
    return {
        "id": activity_id,
        "event_time": event_time,
        "event_type": event_type,
        "object_id": object_id,
        "object_type": object_type,
        "changed_data": [{"field": "status", "old_value": "PAUSED", "new_value": "ACTIVE"}],
        "extra_data": {},
        "actor_id": actor_id,
        "actor_name": "not retained",
        "application_id": application_id,
        "application_name": "not retained",
    }


def _inputs(*, activities: list[dict] | None = None, drift_object: str = "") -> tuple:
    registry_raw = _raw(_registry())
    request = _request(registry_raw)
    request_raw = _raw(request)
    return (
        request_raw,
        hashlib.sha256(request_raw).hexdigest(),
        registry_raw,
        hashlib.sha256(registry_raw).hexdigest(),
        _Session(activities=activities, drift_object=drift_object),
    )


def _write_external(path: Path, raw: bytes) -> None:
    path.write_bytes(raw)
    path.chmod(0o600)


def test_empty_capture_is_observed_but_never_complete() -> None:
    request_raw, request_sha, registry_raw, registry_sha, session = _inputs()
    request, capture, activity, readbacks, coverage = capture_meta_activity_evidence(
        request_raw,
        expected_request_sha256=request_sha,
        actor_registry_raw=registry_raw,
        expected_actor_registry_sha256=registry_sha,
        session=session,
        access_token="token-only-in-memory",
        now=NOW,
    )
    assert request["subject"]["study_id"] == "study-1"
    assert coverage["status"] == "CALLER_ANCHORED_GET_CAPTURE_CLAIM_REDERIVED"
    assert coverage["complete_event_journal"] is False
    assert coverage["retention_coverage"] == "UNKNOWN"
    assert activity["observations"] == []
    assert len(readbacks["readbacks"]) == 5
    assert all(item["stable_during_capture"] for item in readbacks["readbacks"])
    assert capture["get_only_call_claim"]["allowed_methods"] == ["GET"]
    assert coverage["live_graph_transport_attested"] is False
    activity_query = next(
        item for item in capture["query_claim_journal"]
        if item["endpoint"].endswith("/act_account-1/activities")
    )
    assert activity_query["params"] == {
        "fields": "id,event_time,date_time_in_timezone,event_type,object_id,object_type,changed_data,extra_data,actor_id,actor_name,application_id,application_name",
        "limit": 100,
        "since": request["window_start_at"],
        "until": request["data_cutoff_at"],
    }
    assert "LIVE_GRAPH_TRANSPORT_NOT_EXTERNALLY_ATTESTED" in coverage["reason_codes"]
    assert len(session.calls) == 17
    assert all(call[0] != "POST" for call in session.calls)


def test_registry_matched_activity_is_only_observation() -> None:
    request_raw, request_sha, registry_raw, registry_sha, session = _inputs(activities=[_activity()])
    _, _, activity, _, coverage = capture_meta_activity_evidence(
        request_raw,
        expected_request_sha256=request_sha,
        actor_registry_raw=registry_raw,
        expected_actor_registry_sha256=registry_sha,
        session=session,
        access_token="token-only-in-memory",
        now=NOW,
    )
    row = activity["observations"][0]
    assert row["changed_at"] == "2026-08-09T09:00:00+00:00"
    assert row["source_class"] == "ACTOR_REGISTRY_MATCHED_OBSERVATION"
    assert row["transition_status"] == "EXACT_STATUS_TRANSITION_OBSERVED"
    assert coverage["status"] == "CALLER_ANCHORED_GET_CAPTURE_CLAIM_REDERIVED"
    assert coverage["ceiling"]["snapshot_effect"] == "NONE"


def test_external_actor_marks_pollution() -> None:
    request_raw, request_sha, registry_raw, registry_sha, session = _inputs(
        activities=[_activity(actor_id="outside", application_id="outside-app")],
    )
    *_, coverage = capture_meta_activity_evidence(
        request_raw,
        expected_request_sha256=request_sha,
        actor_registry_raw=registry_raw,
        expected_actor_registry_sha256=registry_sha,
        session=session,
        access_token="token-only-in-memory",
        now=NOW,
    )
    assert coverage["status"] == "POLLUTED_EXTERNAL_OR_UNGOVERNED_ACTIVITY_CLAIM"
    assert "EXTERNAL_OR_UNGOVERNED_ACTIVITY_OBSERVED" in coverage["reason_codes"]


def test_current_state_drift_is_incomplete() -> None:
    request_raw, request_sha, registry_raw, registry_sha, session = _inputs(drift_object="ad-1")
    *_, coverage = capture_meta_activity_evidence(
        request_raw,
        expected_request_sha256=request_sha,
        actor_registry_raw=registry_raw,
        expected_actor_registry_sha256=registry_sha,
        session=session,
        access_token="token-only-in-memory",
        now=NOW,
    )
    assert coverage["status"] == "INCOMPLETE_CALLER_ANCHORED_ACTIVITY_OR_STATE_CLAIM"
    assert "CURRENT_STATE_DRIFT_OR_UNKNOWN" in coverage["reason_codes"]


@pytest.mark.parametrize("field,value", [
    ("graph_api_version", "v24.0"),
    ("ceiling", {**CEILING, "snapshot_effect": "EMIT"}),
    ("transport_policy", {"allowed_methods": ["GET", "POST"], "max_pages": 5, "max_events": 100, "clock_skew_seconds": 60}),
])
def test_request_cannot_weaken_contract(field: str, value: object) -> None:
    registry_raw = _raw(_registry())
    request = _request(registry_raw)
    request[field] = value
    request["request_hash"] = hash_json({key: item for key, item in request.items() if key != "request_hash"})
    raw = _raw(request)
    with pytest.raises(MetaActivityEvidenceError):
        validate_request(raw, hashlib.sha256(raw).hexdigest())


def test_window_limit_is_exact() -> None:
    registry_raw = _raw(_registry())
    request = _request(registry_raw)
    request["window_start_at"] = (NOW - timedelta(days=31, microseconds=1)).isoformat()
    request["request_hash"] = hash_json({key: item for key, item in request.items() if key != "request_hash"})
    raw = _raw(request)
    with pytest.raises(MetaActivityEvidenceError, match="TIME_ORDER"):
        validate_request(raw, hashlib.sha256(raw).hexdigest())

    request = _request(registry_raw)
    request["window_start_at"] = (NOW - timedelta(days=31)).isoformat()
    request["request_hash"] = hash_json({key: item for key, item in request.items() if key != "request_hash"})
    raw = _raw(request)
    assert validate_request(raw, hashlib.sha256(raw).hexdigest())["window_start_at"] == request["window_start_at"]


def test_request_requires_bare_account_and_frozen_study_window() -> None:
    registry_raw = _raw(_registry())
    request = _request(registry_raw)
    request["subject"]["account_id"] = "act_account-1"
    request["request_hash"] = hash_json({key: item for key, item in request.items() if key != "request_hash"})
    raw = _raw(request)
    with pytest.raises(MetaActivityEvidenceError, match="SUBJECT_INVALID"):
        validate_request(raw, hashlib.sha256(raw).hexdigest())

    request = _request(registry_raw)
    request["study_contract"]["end_at"] = (NOW - timedelta(seconds=1)).isoformat()
    request["request_hash"] = hash_json({key: item for key, item in request.items() if key != "request_hash"})
    raw = _raw(request)
    with pytest.raises(MetaActivityEvidenceError, match="STUDY_WINDOW_INVALID"):
        validate_request(raw, hashlib.sha256(raw).hexdigest())

    request = _request(registry_raw)
    request["subject"]["campaign_id"] = request["subject"]["cells"][0]["ad_id"]
    request["relevant_fields"] = [
        {"object_type": "AD", "object_id": "ad-1", "field": "status"},
        {"object_type": "AD", "object_id": "ad-2", "field": "status"},
        {"object_type": "ADSET", "object_id": "adset-1", "field": "status"},
        {"object_type": "ADSET", "object_id": "adset-2", "field": "status"},
        {"object_type": "CAMPAIGN", "object_id": "ad-1", "field": "status"},
    ]
    request["request_hash"] = hash_json({key: item for key, item in request.items() if key != "request_hash"})
    raw = _raw(request)
    with pytest.raises(MetaActivityEvidenceError, match="SUBJECT_INVALID"):
        validate_request(raw, hashlib.sha256(raw).hexdigest())


def test_duplicate_activity_and_out_of_scope_activity_fail_closed() -> None:
    for activities in (
        [_activity(), _activity()],
        [_activity(object_id="foreign-object")],
    ):
        request_raw, request_sha, registry_raw, registry_sha, session = _inputs(activities=activities)
        with pytest.raises(MetaActivityEvidenceError):
            capture_meta_activity_evidence(
                request_raw,
                expected_request_sha256=request_sha,
                actor_registry_raw=registry_raw,
                expected_actor_registry_sha256=registry_sha,
                session=session,
                access_token="token-only-in-memory",
                now=NOW,
            )


def test_pagination_cursor_loop_fails_closed() -> None:
    request_raw, request_sha, registry_raw, registry_sha, _ = _inputs()
    session = _Session(paging={"next": "https://example.invalid", "cursors": {"after": "same"}})
    with pytest.raises(MetaActivityEvidenceError, match="GRAPH_CAPTURE_FAILED"):
        capture_meta_activity_evidence(
            request_raw,
            expected_request_sha256=request_sha,
            actor_registry_raw=registry_raw,
            expected_actor_registry_sha256=registry_sha,
            session=session,
            access_token="token-only-in-memory",
            now=NOW,
        )


def test_subject_topology_is_mechanically_opened() -> None:
    request_raw, request_sha, registry_raw, registry_sha, _ = _inputs()
    with pytest.raises(MetaActivityEvidenceError, match="TOPOLOGY_INVALID"):
        capture_meta_activity_evidence(
            request_raw,
            expected_request_sha256=request_sha,
            actor_registry_raw=registry_raw,
            expected_actor_registry_sha256=registry_sha,
            session=_Session(foreign_topology=True),
            access_token="token-only-in-memory",
            now=NOW,
        )

    for session in (
        _Session(duplicate_cell_row=True),
        _Session(duplicate_cell_ad_id=True),
    ):
        with pytest.raises(MetaActivityEvidenceError, match="TOPOLOGY_INVALID"):
            capture_meta_activity_evidence(
                request_raw,
                expected_request_sha256=request_sha,
                actor_registry_raw=registry_raw,
                expected_actor_registry_sha256=registry_sha,
                session=session,
                access_token="token-only-in-memory",
                now=NOW,
            )

    with pytest.raises(MetaActivityEvidenceError, match="TOPOLOGY_INVALID"):
        capture_meta_activity_evidence(
            request_raw,
            expected_request_sha256=request_sha,
            actor_registry_raw=registry_raw,
            expected_actor_registry_sha256=registry_sha,
            session=_Session(study_type="LIFT"),
            access_token="token-only-in-memory",
            now=NOW,
        )


def test_configured_status_is_not_replaced_by_effective_status() -> None:
    request_raw, request_sha, registry_raw, registry_sha, _ = _inputs()
    _, capture, _, readbacks, coverage = capture_meta_activity_evidence(
        request_raw,
        expected_request_sha256=request_sha,
        actor_registry_raw=registry_raw,
        expected_actor_registry_sha256=registry_sha,
        session=_Session(effective_status_override="WITH_ISSUES"),
        access_token="token-only-in-memory",
        now=NOW,
    )
    assert {item["status"] for item in capture["first_states"]} == {"PAUSED"}
    assert all(item["stable_during_capture"] for item in readbacks["readbacks"])
    assert coverage["status"] == "CALLER_ANCHORED_GET_CAPTURE_CLAIM_REDERIVED"


def test_activity_requires_explicit_status_field_matching_type_and_event_contract() -> None:
    missing_field = _activity()
    missing_field["changed_data"] = [{"from": "PAUSED", "to": "ACTIVE"}]
    wrong_event = _activity(activity_id="activity-2", event_type="BUDGET_UPDATE")
    request_raw, request_sha, registry_raw, registry_sha, session = _inputs(
        activities=[missing_field, wrong_event],
    )
    _, _, activity, _, coverage = capture_meta_activity_evidence(
        request_raw,
        expected_request_sha256=request_sha,
        actor_registry_raw=registry_raw,
        expected_actor_registry_sha256=registry_sha,
        session=session,
        access_token="token-only-in-memory",
        now=NOW,
    )
    assert {item["transition_status"] for item in activity["observations"]} == {
        "UNCLASSIFIED_OR_CONFLICTING_TRANSITION",
    }
    assert coverage["status"] == "INCOMPLETE_CALLER_ANCHORED_ACTIVITY_OR_STATE_CLAIM"

    wrong_type = _activity(object_type="AD")
    request_raw, request_sha, registry_raw, registry_sha, session = _inputs(activities=[wrong_type])
    with pytest.raises(MetaActivityEvidenceError, match="OBJECT_TYPE_INVALID"):
        capture_meta_activity_evidence(
            request_raw,
            expected_request_sha256=request_sha,
            actor_registry_raw=registry_raw,
            expected_actor_registry_sha256=registry_sha,
            session=session,
            access_token="token-only-in-memory",
            now=NOW,
        )


def test_activity_dual_timestamps_must_name_the_same_instant() -> None:
    row = _activity()
    row["date_time_in_timezone"] = "2020-01-01T00:00:00+00:00"
    request_raw, request_sha, registry_raw, registry_sha, session = _inputs(activities=[row])
    with pytest.raises(MetaActivityEvidenceError, match="ACTIVITY_TIME_CONFLICT"):
        capture_meta_activity_evidence(
            request_raw,
            expected_request_sha256=request_sha,
            actor_registry_raw=registry_raw,
            expected_actor_registry_sha256=registry_sha,
            session=session,
            access_token="token-only-in-memory",
            now=NOW,
        )


def test_transition_sequence_and_capture_clock_are_fail_closed() -> None:
    repeated = [
        _activity(activity_id="activity-1", event_time="2026-08-09T08:00:00+00:00"),
        _activity(activity_id="activity-2", event_time="2026-08-09T09:00:00+00:00"),
    ]
    request_raw, request_sha, registry_raw, registry_sha, session = _inputs(activities=repeated)
    *_, coverage = capture_meta_activity_evidence(
        request_raw,
        expected_request_sha256=request_sha,
        actor_registry_raw=registry_raw,
        expected_actor_registry_sha256=registry_sha,
        session=session,
        access_token="token-only-in-memory",
        now=NOW,
    )
    assert "ACTIVITY_TRANSITION_SEQUENCE_CONFLICT" in coverage["reason_codes"]
    assert coverage["status"] == "INCOMPLETE_CALLER_ANCHORED_ACTIVITY_OR_STATE_CLAIM"

    request = json.loads(request_raw)
    request["data_cutoff_at"] = NOW.isoformat()
    request["requested_at"] = (NOW + timedelta(seconds=30)).isoformat()
    request["request_hash"] = hash_json({key: item for key, item in request.items() if key != "request_hash"})
    raw = _raw(request)
    with pytest.raises(MetaActivityEvidenceError, match="CAPTURE_CLOCK_INVALID"):
        capture_meta_activity_evidence(
            raw,
            expected_request_sha256=hashlib.sha256(raw).hexdigest(),
            actor_registry_raw=registry_raw,
            expected_actor_registry_sha256=registry_sha,
            session=_Session(),
            access_token="token-only-in-memory",
            now=NOW - timedelta(seconds=1),
        )


def test_activity_current_state_conflict_remains_incomplete() -> None:
    request_raw, request_sha, registry_raw, registry_sha, session = _inputs(activities=[_activity()])
    session.active_objects.clear()
    *_, coverage = capture_meta_activity_evidence(
        request_raw,
        expected_request_sha256=request_sha,
        actor_registry_raw=registry_raw,
        expected_actor_registry_sha256=registry_sha,
        session=session,
        access_token="token-only-in-memory",
        now=NOW,
    )
    assert coverage["status"] == "INCOMPLETE_CALLER_ANCHORED_ACTIVITY_OR_STATE_CLAIM"
    assert "ACTIVITY_CURRENT_STATE_CONFLICT" in coverage["reason_codes"]


def test_noop_activity_never_becomes_exact_mutation() -> None:
    row = _activity()
    row["changed_data"] = [{"field": "status", "old_value": "PAUSED", "new_value": "PAUSED"}]
    request_raw, request_sha, registry_raw, registry_sha, session = _inputs(activities=[row])
    session.active_objects.clear()
    _, _, activity, _, coverage = capture_meta_activity_evidence(
        request_raw,
        expected_request_sha256=request_sha,
        actor_registry_raw=registry_raw,
        expected_actor_registry_sha256=registry_sha,
        session=session,
        access_token="token-only-in-memory",
        now=NOW,
    )
    assert activity["observations"][0]["transition_status"] == "NO_OP_TRANSITION_OBSERVED"
    assert coverage["status"] == "INCOMPLETE_CALLER_ANCHORED_ACTIVITY_OR_STATE_CLAIM"


def test_write_load_and_full_rederive(tmp_path: Path) -> None:
    request_raw, request_sha, registry_raw, registry_sha, session = _inputs(activities=[_activity()])
    output = tmp_path / "artifact"
    result = write_meta_activity_evidence_artifact(
        output,
        request_raw=request_raw,
        expected_request_sha256=request_sha,
        actor_registry_raw=registry_raw,
        expected_actor_registry_sha256=registry_sha,
        session=session,
        access_token="token-only-in-memory",
        now=NOW,
    )
    loaded = load_validated_meta_activity_evidence_directory(
        output,
        expected_manifest_sha256=result["manifest_sha256"],
        expected_request_sha256=request_sha,
        actor_registry_raw=registry_raw,
        expected_actor_registry_sha256=registry_sha,
    )
    assert loaded["manifest"]["status"] == "CALLER_ANCHORED_GET_CAPTURE_CLAIM_REDERIVED"
    assert set(path.name for path in output.iterdir()) == {
        "source-request.json", "graph-capture.json", "activity-observations.json",
        "current-state-readbacks.json", "coverage.json", "manifest.json",
    }
    assert all((path.stat().st_mode & 0o777) == 0o600 for path in output.iterdir())


def test_full_rehash_cannot_promote_ceiling(tmp_path: Path) -> None:
    request_raw, request_sha, registry_raw, registry_sha, session = _inputs()
    output = tmp_path / "artifact"
    result = write_meta_activity_evidence_artifact(
        output,
        request_raw=request_raw,
        expected_request_sha256=request_sha,
        actor_registry_raw=registry_raw,
        expected_actor_registry_sha256=registry_sha,
        session=session,
        access_token="token-only-in-memory",
        now=NOW,
    )
    coverage_path = output / "coverage.json"
    manifest_path = output / "manifest.json"
    coverage = json.loads(coverage_path.read_text())
    coverage["ceiling"]["snapshot_effect"] = "EMIT"
    coverage["coverage_hash"] = hash_json({key: item for key, item in coverage.items() if key != "coverage_hash"})
    coverage_raw = _raw(coverage)
    coverage_path.write_bytes(coverage_raw)
    coverage_path.chmod(0o600)
    manifest = json.loads(manifest_path.read_text())
    manifest["coverage_hash"] = coverage["coverage_hash"]
    manifest["files"]["coverage.json"] = {
        "sha256": hashlib.sha256(coverage_raw).hexdigest(), "size": len(coverage_raw),
    }
    manifest["manifest_hash"] = hash_json({key: item for key, item in manifest.items() if key != "manifest_hash"})
    manifest_raw = _raw(manifest)
    manifest_path.write_bytes(manifest_raw)
    manifest_path.chmod(0o600)
    with pytest.raises(MetaActivityEvidenceError, match="REDERIVATION"):
        load_validated_meta_activity_evidence_directory(
            output,
            expected_manifest_sha256=hashlib.sha256(manifest_raw).hexdigest(),
            expected_request_sha256=request_sha,
            actor_registry_raw=registry_raw,
            expected_actor_registry_sha256=registry_sha,
        )
    assert result["manifest"]["ceiling"]["snapshot_effect"] == "NONE"


@pytest.mark.parametrize("mutation", [
    "capture_clock", "query_window", "response_status", "event_contract", "actor_identity",
])
def test_full_rehash_cannot_forge_capture_clock_or_query_contract(
    tmp_path: Path, mutation: str,
) -> None:
    request_raw, request_sha, registry_raw, registry_sha, session = _inputs(
        activities=[_activity()] if mutation in {"event_contract", "actor_identity"} else None,
    )
    output = tmp_path / mutation
    result = write_meta_activity_evidence_artifact(
        output,
        request_raw=request_raw,
        expected_request_sha256=request_sha,
        actor_registry_raw=registry_raw,
        expected_actor_registry_sha256=registry_sha,
        session=session,
        access_token="token-only-in-memory",
        now=NOW,
    )
    capture_path = output / "graph-capture.json"
    manifest_path = output / "manifest.json"
    capture = json.loads(capture_path.read_text())
    if mutation == "capture_clock":
        capture["captured_at"] = (NOW - timedelta(seconds=1)).isoformat()
    else:
        if mutation == "query_window":
            query = next(
                item for item in capture["query_claim_journal"]
                if item["endpoint"].endswith("/act_account-1/activities")
            )
            query["params"]["since"] = (NOW - timedelta(days=1)).isoformat()
        elif mutation == "response_status":
            capture["response_journal_claim"][0]["http_status"] = 201
            capture["get_only_call_claim"]["request_journal_hash"] = hash_json(
                capture["response_journal_claim"],
            )
        elif mutation == "event_contract":
            capture["activities"][0]["event_type"] = "BUDGET_UPDATE"
            capture["activities"][0]["observation_hash"] = hash_json({
                key: item for key, item in capture["activities"][0].items()
                if key != "observation_hash"
            })
        else:
            capture["activities"][0]["actor_id"] = {"malformed": True}
            capture["activities"][0]["observation_hash"] = hash_json({
                key: item for key, item in capture["activities"][0].items()
                if key != "observation_hash"
            })
    capture["capture_hash"] = hash_json({
        key: item for key, item in capture.items() if key != "capture_hash"
    })
    capture_raw = _raw(capture)
    capture_path.write_bytes(capture_raw)
    capture_path.chmod(0o600)
    manifest = json.loads(manifest_path.read_text())
    manifest["capture_hash"] = capture["capture_hash"]
    manifest["files"]["graph-capture.json"] = {
        "sha256": hashlib.sha256(capture_raw).hexdigest(), "size": len(capture_raw),
    }
    manifest["manifest_hash"] = hash_json({
        key: item for key, item in manifest.items() if key != "manifest_hash"
    })
    manifest_raw = _raw(manifest)
    manifest_path.write_bytes(manifest_raw)
    manifest_path.chmod(0o600)
    with pytest.raises(MetaActivityEvidenceError):
        load_validated_meta_activity_evidence_directory(
            output,
            expected_manifest_sha256=hashlib.sha256(manifest_raw).hexdigest(),
            expected_request_sha256=request_sha,
            actor_registry_raw=registry_raw,
            expected_actor_registry_sha256=registry_sha,
        )
    assert result["manifest"]["status"] == "CALLER_ANCHORED_GET_CAPTURE_CLAIM_REDERIVED"


def test_output_is_new_only_and_reader_rejects_extra_file(tmp_path: Path) -> None:
    request_raw, request_sha, registry_raw, registry_sha, session = _inputs()
    output = tmp_path / "artifact"
    result = write_meta_activity_evidence_artifact(
        output,
        request_raw=request_raw,
        expected_request_sha256=request_sha,
        actor_registry_raw=registry_raw,
        expected_actor_registry_sha256=registry_sha,
        session=session,
        access_token="token-only-in-memory",
        now=NOW,
    )
    with pytest.raises(MetaActivityEvidenceError, match="OUTPUT_EXISTS"):
        write_meta_activity_evidence_artifact(
            output,
            request_raw=request_raw,
            expected_request_sha256=request_sha,
            actor_registry_raw=registry_raw,
            expected_actor_registry_sha256=registry_sha,
            session=_Session(),
            access_token="token-only-in-memory",
            now=NOW,
        )
    extra = output / "extra.json"
    extra.write_text("{}\n")
    extra.chmod(0o600)
    with pytest.raises(MetaActivityEvidenceError, match="FILE_SET"):
        load_validated_meta_activity_evidence_directory(
            output,
            expected_manifest_sha256=result["manifest_sha256"],
            expected_request_sha256=request_sha,
            actor_registry_raw=registry_raw,
            expected_actor_registry_sha256=registry_sha,
        )


def test_reader_rejects_hardlinked_artifact_file(tmp_path: Path) -> None:
    request_raw, request_sha, registry_raw, registry_sha, session = _inputs()
    output = tmp_path / "artifact"
    result = write_meta_activity_evidence_artifact(
        output,
        request_raw=request_raw,
        expected_request_sha256=request_sha,
        actor_registry_raw=registry_raw,
        expected_actor_registry_sha256=registry_sha,
        session=session,
        access_token="token-only-in-memory",
        now=NOW,
    )
    os.link(output / "coverage.json", tmp_path / "coverage-hardlink.json")
    with pytest.raises(MetaActivityEvidenceError, match="ARTIFACT_FILE_INVALID"):
        load_validated_meta_activity_evidence_directory(
            output,
            expected_manifest_sha256=result["manifest_sha256"],
            expected_request_sha256=request_sha,
            actor_registry_raw=registry_raw,
            expected_actor_registry_sha256=registry_sha,
        )


def test_request_duplicate_key_and_noncanonical_bytes_rejected() -> None:
    registry_raw = _raw(_registry())
    request = _request(registry_raw)
    pretty = json.dumps(request, indent=2).encode()
    with pytest.raises(MetaActivityEvidenceError):
        validate_request(pretty, hashlib.sha256(pretty).hexdigest())
    duplicate = b'{"schema_version":"a","schema_version":"b"}\n'
    with pytest.raises(MetaActivityEvidenceError):
        validate_request(duplicate, hashlib.sha256(duplicate).hexdigest())


def test_direct_api_rejects_oversize_registry_before_json_parse() -> None:
    request_raw, request_sha, _registry_raw, _registry_sha, session = _inputs()
    oversized = b"x" * (2 * 1024 * 1024 + 1)
    with pytest.raises(MetaActivityEvidenceError, match="REGISTRY_ANCHOR_MISMATCH"):
        capture_meta_activity_evidence(
            request_raw,
            expected_request_sha256=request_sha,
            actor_registry_raw=oversized,
            expected_actor_registry_sha256=hashlib.sha256(oversized).hexdigest(),
            session=session,
            access_token="token-only-in-memory",
            now=NOW,
        )


def test_cli_requires_explicit_execution_and_positive_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location(
        "g104b4_cli",
        Path(__file__).parents[1] / "scripts" / "build_gle_evaluation_meta_activity_evidence.py",
    )
    assert spec and spec.loader
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    request_time = datetime.now(timezone.utc)
    registry_raw = _raw(_registry())
    request_raw = _raw(_request(registry_raw, requested_at=request_time))
    request_path = tmp_path / "request.json"
    registry_path = tmp_path / "registry.json"
    _write_external(request_path, request_raw)
    _write_external(registry_path, registry_raw)
    args = [
        "--request", str(request_path),
        "--expected-request-sha256", hashlib.sha256(request_raw).hexdigest(),
        "--actor-registry", str(registry_path),
        "--expected-actor-registry-sha256", hashlib.sha256(registry_raw).hexdigest(),
        "--output-dir", str(tmp_path / "output"),
    ]
    assert cli.main(args) == 64
    import requests
    monkeypatch.setattr(requests, "Session", lambda: _Session())
    monkeypatch.setenv("META_ACCESS_TOKEN", "token-only-in-memory")
    assert cli.main([*args, "--execute-read-only"]) == 2

    class _TimeoutSession:
        def get(self, *_args: object, **_kwargs: object) -> object:
            raise TimeoutError("bounded transport timeout")

    monkeypatch.setattr(requests, "Session", _TimeoutSession)
    timeout_args = [
        *args[:-1], str(tmp_path / "timeout-output"), "--execute-read-only",
    ]
    assert cli.main(timeout_args) == 66


def test_external_files_require_0600(tmp_path: Path) -> None:
    registry_raw = _raw(_registry())
    request_raw = _raw(_request(registry_raw))
    path = tmp_path / "request.json"
    path.write_bytes(request_raw)
    path.chmod(0o644)
    from app.growth.evaluation_meta_activity_evidence import read_external_json
    with pytest.raises(MetaActivityEvidenceError):
        read_external_json(
            path,
            hashlib.sha256(request_raw).hexdigest(),
            maximum=2 * 1024 * 1024,
            code="G104B4_REQUEST_ARTIFACT_INVALID",
        )
