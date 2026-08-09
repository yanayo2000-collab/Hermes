from __future__ import annotations

import hashlib
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest

from app.growth.canonical_evaluation_contracts import canonical_hash, canonical_json
from app.growth.golden_label_adjudication import (
    ADJUDICATION_VERSION,
    BLINDING_MAP_SIGNATURE_PURPOSE,
    DELIVERY_SIGNATURE_PURPOSE,
    DELIVERY_VERSION,
    LABEL_CONTRACT_VERSION,
    REVIEWER_KEY_REGISTRY_VERSION,
    REVIEW_RESPONSE_VERSION,
    REVIEW_SIGNATURE_PURPOSE,
    GoldenLabelAdjudicationError,
    _load_source,
    build_label_assignment_request,
    evaluate_label_round,
    issue_blinding_map_payload,
    load_validated_label_round_directory,
    load_validated_reviewer_packet_directory,
    signature_message,
    write_label_round_artifact_domains,
)
from app.growth.immutable_lineage_authority import SIGNATURE_ALGORITHM
from app.growth.lineage_devval_registry import (
    evaluate_registry_response,
    write_registry_artifacts,
)
from tests.test_growth_immutable_lineage_authority import _public_key, _sha, _sign
from tests.test_growth_lineage_devval_registry import (
    _authority,
    _devval_keys,
    _request,
    _seed_and_policy,
    _signed_response,
    _source_validation,
)
from scripts.build_gle_golden_label_tasks import main as golden_cli_main


def _registry_source(tmp_path: Path, *, verified: bool) -> dict[str, object]:
    source = _authority(tmp_path / "authority-source", verified=verified)
    if verified:
        (tmp_path / "seed").mkdir(parents=True, exist_ok=True)
        seed, seed_path, seed_sha, policy = _seed_and_policy(tmp_path / "seed")
        request = _request(
            source,
            policy=policy,
            seed_selection_file=seed_path,
            expected_seed_selection_file_sha256=seed_sha,
        )
        devval_keys, private_keys = _devval_keys(tmp_path / "devval-keys")
        response = _signed_response(request, seed, devval_keys, private_keys)
        source_validation = _source_validation(
            source,
            seed_path=seed_path,
            seed_sha=seed_sha,
            devval_key_hash=devval_keys["registry_hash"],
        )
        expected_devval_hash = devval_keys["registry_hash"]
    else:
        request = _request(source)
        response = None
        devval_keys = None
        source_validation = _source_validation(source)
        expected_devval_hash = None
    registry = evaluate_registry_response(
        request,
        response,
        trusted_key_registry=devval_keys,
        expected_devval_key_registry_hash=expected_devval_hash,
        source_validation=source_validation,
    )
    registry_dir = tmp_path / "registry"
    write_registry_artifacts(
        request,
        response,
        devval_keys,
        registry,
        registry_dir,
        expected_devval_key_registry_hash=expected_devval_hash,
        source_validation=source_validation,
    )
    return {
        "registry_dir": registry_dir,
        "expected_registry_manifest_sha256": _sha(registry_dir / "manifest.json"),
        "expected_devval_key_registry_hash": expected_devval_hash,
        "source_validation": source_validation,
        "review_round_id": "blind-round-001",
        "requested_at": "2026-08-07T06:10:00Z",
        "evaluated_at": "2026-08-07T07:00:00Z",
        "label_version": LABEL_CONTRACT_VERSION,
    }


def _reviewer_registry(tmp_path: Path) -> tuple[dict, dict[str, Path]]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    keys = []
    private: dict[str, Path] = {}
    roles = ("BLINDING_CUSTODIAN", "REVIEWER_A", "REVIEWER_B", "ADJUDICATOR_C")
    for index, role in enumerate(roles, start=1):
        path = tmp_path / f"reviewer-{index}.pem"
        subprocess.run(
            ["openssl", "genrsa", "-out", str(path), "2048"],
            check=True,
            capture_output=True,
        )
        key_id = f"review-key-{index}"
        private[key_id] = path
        keys.append({
            "key_id": key_id,
            "signer_id": f"review-principal-{index}",
            "principal_id": f"natural-person-{index}",
            "role": role,
            "purposes": (
                sorted([DELIVERY_SIGNATURE_PURPOSE, BLINDING_MAP_SIGNATURE_PURPOSE])
                if role == "BLINDING_CUSTODIAN"
                else [REVIEW_SIGNATURE_PURPOSE]
            ),
            "algorithm": SIGNATURE_ALGORITHM,
            "status": "ACTIVE",
            "valid_from": "2026-01-01T00:00:00Z",
            "valid_until": "2027-01-01T00:00:00Z",
            "public_key_pem": _public_key(path),
        })
    keys.sort(key=lambda item: item["key_id"])
    registry = {
        "schema_version": REVIEWER_KEY_REGISTRY_VERSION,
        "registry_id": "blind-reviewer-registry-v1",
        "identity_issuer": "growth-governance-identity-registry",
        "identity_registry_manifest_sha256": "d" * 64,
        "keys": keys,
    }
    registry["registry_hash"] = canonical_hash(registry)
    return registry, private


def _reviewer_raw_sha(registry: dict) -> str:
    return hashlib.sha256((canonical_json(registry) + "\n").encode()).hexdigest()


def _ready_context(tmp_path: Path) -> tuple[dict[str, object], dict, dict[str, Path]]:
    context = _registry_source(tmp_path / "source", verified=True)
    registry, private = _reviewer_registry(tmp_path / "reviewers")
    _, _, nodes, _, _, _ = _load_source(
        registry_dir=context["registry_dir"],
        expected_registry_manifest_sha256=context["expected_registry_manifest_sha256"],
        expected_devval_key_registry_hash=context["expected_devval_key_registry_hash"],
        source_validation=context["source_validation"],
    )
    candidate_entry_hashes = sorted({
        ref["entry_hash"]
        for node in nodes.values()
        for ref in node["candidate_entry_refs"]
    })
    blinding_map = issue_blinding_map_payload(
        review_round_id=context["review_round_id"],
        issued_at="2026-08-07T06:05:00Z",
        candidate_entry_hashes=candidate_entry_hashes,
    )
    blinding_map["signature"] = _signature(
        blinding_map["blinding_map_hash"],
        role="BLINDING_CUSTODIAN",
        purpose=BLINDING_MAP_SIGNATURE_PURPOSE,
        signed_at=blinding_map["issued_at"],
        registry=registry,
        private=private,
    )
    context.update({
        "reviewer_key_registry": registry,
        "expected_reviewer_key_registry_hash": registry["registry_hash"],
        "expected_reviewer_key_registry_sha256": _reviewer_raw_sha(registry),
        "blinding_map": blinding_map,
        "expected_blinding_map_sha256": hashlib.sha256(
            (canonical_json(blinding_map) + "\n").encode()
        ).hexdigest(),
    })
    return context, registry, private


def _signature(
    object_hash: str,
    *,
    role: str,
    purpose: str,
    signed_at: str,
    registry: dict,
    private: dict[str, Path],
) -> dict:
    key = next(item for item in registry["keys"] if item["role"] == role)
    return {
        "algorithm": SIGNATURE_ALGORITHM,
        "key_id": key["key_id"],
        "signer_id": key["signer_id"],
        "principal_id": key["principal_id"],
        "role": role,
        "purpose": purpose,
        "object_hash": object_hash,
        "key_registry_hash": registry["registry_hash"],
        "signed_at": signed_at,
        "signature_base64": _sign(
            private[key["key_id"]],
            signature_message(
                object_hash,
                key_registry_hash=registry["registry_hash"],
                key_id=key["key_id"],
                signer_id=key["signer_id"],
                principal_id=key["principal_id"],
                role=role,
                purpose=purpose,
            ),
        ),
    }


def _label(task: dict, *, decision: str = "CONTINUE_WAITING") -> dict:
    data_fix = decision == "CREATE_DATA_FIX_TASK"
    value = {
        "expected_evaluation_result": "DATA_INCOMPLETE" if data_fix else "WAITING_EVIDENCE",
        "expected_decision": decision,
        "action_proposals": ["NONE"],
        "expected_reason_codes": ["DATA_INCOMPLETE" if data_fix else "MORE_EVIDENCE_REQUIRED"],
        "evidence_ref_ids": [next(
            item["fact_id"]
            for item in task["blind_payload"]["evidence_packet"]["facts"]
            if item["fact_kind"] == "PRIMARY_FROZEN_EVALUATION_AUDIT_FACT"
        )],
        "critical_risk_labels": [],
    }
    value["label_hash"] = canonical_hash(value)
    return value


def _delivery(
    request: dict,
    task: dict,
    role: str,
    registry: dict,
    private: dict[str, Path],
    *,
    delivered_at: str,
) -> dict:
    recipient = next(item for item in registry["keys"] if item["role"] == role)
    value = {
        "schema_version": DELIVERY_VERSION,
        "delivery_id": f"delivery-{role.lower()}-{task['task_id']}",
        "request_hash": request["request_hash"],
        "task_id": task["task_id"],
        "blind_payload_hash": task["blind_payload_hash"],
        "recipient_reviewer_id": recipient["principal_id"],
        "recipient_role": role,
        "delivered_at": delivered_at,
        "visible_artifact_hashes": [task["blind_payload_hash"]],
    }
    value["delivery_payload_hash"] = canonical_hash(value)
    value["signature"] = _signature(
        value["delivery_payload_hash"],
        role="BLINDING_CUSTODIAN",
        purpose=DELIVERY_SIGNATURE_PURPOSE,
        signed_at=delivered_at,
        registry=registry,
        private=private,
    )
    return value


def _review(
    request: dict,
    task: dict,
    role: str,
    registry: dict,
    private: dict[str, Path],
    *,
    label: dict,
    submitted_at: str,
    delivered_at: str,
) -> dict:
    reviewer = next(item for item in registry["keys"] if item["role"] == role)
    value = {
        "schema_version": REVIEW_RESPONSE_VERSION,
        "response_id": f"response-{role.lower()}-{task['task_id']}",
        "request_hash": request["request_hash"],
        "task_id": task["task_id"],
        "reviewer_id": reviewer["principal_id"],
        "reviewer_role": role,
        "delivery_receipt": _delivery(
            request, task, role, registry, private, delivered_at=delivered_at,
        ),
        "label": label,
        "submitted_at": submitted_at,
        "blind_attestation": {
            "engine_output_seen": False,
            "peer_label_seen": False,
            "legacy_conclusion_seen": False,
        },
    }
    value["response_payload_hash"] = canonical_hash(value)
    value["signature"] = _signature(
        value["response_payload_hash"],
        role=role,
        purpose=REVIEW_SIGNATURE_PURPOSE,
        signed_at=submitted_at,
        registry=registry,
        private=private,
    )
    return value


def _adjudication(
    request: dict,
    task: dict,
    response_a: dict,
    response_b: dict,
    registry: dict,
    private: dict[str, Path],
    *,
    label: dict,
) -> dict:
    reviewer = next(item for item in registry["keys"] if item["role"] == "ADJUDICATOR_C")
    submitted_at = "2026-08-07T06:50:00Z"
    delivered_at = "2026-08-07T06:40:00Z"
    delivery = {
        "schema_version": DELIVERY_VERSION,
        "delivery_id": f"delivery-adjudicator-{task['task_id']}",
        "request_hash": request["request_hash"],
        "task_id": task["task_id"],
        "blind_payload_hash": task["blind_payload_hash"],
        "recipient_reviewer_id": reviewer["principal_id"],
        "recipient_role": "ADJUDICATOR_C",
        "delivered_at": delivered_at,
        "visible_artifact_hashes": [
            task["blind_payload_hash"],
            response_a["response_payload_hash"],
            response_b["response_payload_hash"],
        ],
    }
    delivery["delivery_payload_hash"] = canonical_hash(delivery)
    delivery["signature"] = _signature(
        delivery["delivery_payload_hash"],
        role="BLINDING_CUSTODIAN",
        purpose=DELIVERY_SIGNATURE_PURPOSE,
        signed_at=delivered_at,
        registry=registry,
        private=private,
    )
    value = {
        "schema_version": ADJUDICATION_VERSION,
        "adjudication_id": f"adjudication-{task['task_id']}",
        "request_hash": request["request_hash"],
        "task_id": task["task_id"],
        "reviewer_id": reviewer["principal_id"],
        "reviewer_role": "ADJUDICATOR_C",
        "reviewer_a_response_hash": response_a["response_payload_hash"],
        "reviewer_b_response_hash": response_b["response_payload_hash"],
        "label": label,
        "submitted_at": submitted_at,
        "blind_attestation": {
            "engine_output_seen": False,
            "reviewer_labels_seen": True,
            "legacy_conclusion_seen": False,
        },
        "delivery_receipt": delivery,
    }
    value["adjudication_payload_hash"] = canonical_hash(value)
    value["signature"] = _signature(
        value["adjudication_payload_hash"],
        role="ADJUDICATOR_C",
        purpose=REVIEW_SIGNATURE_PURPOSE,
        signed_at=submitted_at,
        registry=registry,
        private=private,
    )
    return value


def test_blocked_source_materializes_zero_task_artifact(tmp_path: Path) -> None:
    context = _registry_source(tmp_path / "source", verified=False)
    request, tasks = build_label_assignment_request(**context)
    assert request["status"] == "BLOCKED_SOURCE_PARTITION"
    assert tasks == []
    ledger, round_summary = evaluate_label_round(
        request, tasks, [], [],
        reviewer_key_registry=None,
        expected_reviewer_key_registry_hash=None,
        source_context=context,
    )
    assert ledger == []
    assert round_summary["status"] == "BLOCKED_SOURCE_PARTITION"
    output = tmp_path / "blocked-output"
    write_label_round_artifact_domains(
        request, tasks, [], [], None, ledger, round_summary, output, None,
        expected_reviewer_key_registry_hash=None,
        source_context=context,
    )
    loaded_request, loaded_ledger, loaded_round = load_validated_label_round_directory(
        output,
        expected_label_manifest_sha256=_sha(output / "manifest.json"),
        expected_reviewer_key_registry_hash=None,
        source_context=context,
    )
    assert loaded_request == request
    assert loaded_ledger == []
    assert loaded_round == round_summary


def test_signed_partition_derives_devval_only_blinded_tasks(tmp_path: Path) -> None:
    context, _, _ = _ready_context(tmp_path)
    request, tasks = build_label_assignment_request(**context)
    assert request["status"] == "TASKS_READY_FOR_BLIND_REVIEW"
    assert tasks and {item["dataset_split"] for item in tasks} <= {"DEV", "VALIDATION"}
    raw = canonical_json(tasks).lower()
    for forbidden in ("holdout", "winner", "score_u64", "seed_reveal"):
        assert forbidden not in raw
    for task in tasks:
        assert set(task["blind_payload"]) == {
            "schema_version", "task_id", "evidence_packet", "label_version",
            "label_contract_hash", "blind_policy",
        }
        blind_raw = canonical_json(task["blind_payload"]).lower()
        assert "2026-" not in blind_raw
        assert "evaluated_at\"" not in blind_raw
        assert "created_at" not in blind_raw
        assert "updated_at" not in blind_raw
        assert "_token\"" not in blind_raw
        assert "_hash\"" not in blind_raw.replace('"label_contract_hash"', "")
        assert "dataset_split" not in blind_raw
        assert "lineage_id" not in blind_raw
        assert "canonical_experiment_id" not in blind_raw
        for sensitive_value in (
            task["lineage_id"],
            task["canonical_experiment_id"],
            task["candidate_source_id"],
            task["candidate_entry_hash"],
            task["assignment_hash"],
            task["authority_membership_hash"],
        ):
            assert sensitive_value.lower() not in blind_raw
        assert '"engine_output_visible":false' in blind_raw
        assert '"legacy_conclusion_visible":false' in blind_raw
        assert any(
            fact["fact_kind"] == "PRIMARY_FROZEN_EVALUATION_AUDIT_FACT"
            for fact in task["blind_payload"]["evidence_packet"]["facts"]
        )
    assert all(item["snapshot_id"] is None and item["not_golden_case"] for item in tasks)


def test_custodian_issued_opaque_ids_are_anchored_and_not_round_derived(
    tmp_path: Path,
) -> None:
    context, _, _ = _ready_context(tmp_path)
    request, tasks = build_label_assignment_request(**context)
    expected_ids = {
        item["opaque_task_id"] for item in context["blinding_map"]["assignments"]
    }
    assert {item["task_id"] for item in tasks} == expected_ids
    assert request["blinding_binding"] == {
        "blinding_map_sha256": context["expected_blinding_map_sha256"],
        "blinding_map_hash": context["blinding_map"]["blinding_map_hash"],
        "custodian_principal_id": "natural-person-1",
        "id_algorithm": "CUSTODIAN_ASSERTED_CSPRNG_128BIT",
    }
    enumerable_ids = {
        "review_case_" + canonical_hash({
            "review_round_id": context["review_round_id"],
            "case_index": index,
        })[:24]
        for index in range(1, len(tasks) + 1)
    }
    assert expected_ids.isdisjoint(enumerable_ids)

    missing = dict(context)
    missing["blinding_map"] = None
    missing["expected_blinding_map_sha256"] = None
    with pytest.raises(GoldenLabelAdjudicationError, match="G103A_REVIEWER_TRUST_ROOT_MISSING"):
        build_label_assignment_request(**missing)

    duplicate = deepcopy(context)
    duplicate["blinding_map"]["assignments"].append({
        "candidate_entry_hash": "f" * 64,
        "opaque_task_id": duplicate["blinding_map"]["assignments"][0]["opaque_task_id"],
    })
    duplicate["blinding_map"]["assignments"].sort(
        key=lambda item: item["candidate_entry_hash"]
    )
    duplicate["blinding_map"]["blinding_map_hash"] = canonical_hash({
        key: value for key, value in duplicate["blinding_map"].items()
        if key not in {"blinding_map_hash", "signature"}
    })
    duplicate["expected_blinding_map_sha256"] = hashlib.sha256(
        (canonical_json(duplicate["blinding_map"]) + "\n").encode()
    ).hexdigest()
    with pytest.raises(GoldenLabelAdjudicationError, match="G103A_BLINDING_MAP_INVALID"):
        build_label_assignment_request(**duplicate)

    rehashed = deepcopy(context)
    rehashed["blinding_map"]["assignments"][0]["opaque_task_id"] = (
        "blind_case_" + "f" * 32
    )
    rehashed["blinding_map"]["blinding_map_hash"] = canonical_hash({
        key: value for key, value in rehashed["blinding_map"].items()
        if key not in {"blinding_map_hash", "signature"}
    })
    rehashed["expected_blinding_map_sha256"] = hashlib.sha256(
        (canonical_json(rehashed["blinding_map"]) + "\n").encode()
    ).hexdigest()
    with pytest.raises(GoldenLabelAdjudicationError, match="G103A_SIGNATURE_INVALID"):
        build_label_assignment_request(**rehashed)


def test_reviewer_packet_is_a_separate_identity_free_artifact(tmp_path: Path) -> None:
    context, _, _ = _ready_context(tmp_path)
    request, tasks = build_label_assignment_request(**context)
    reviewer_output = tmp_path / "reviewer-safe"
    coordinator_output = tmp_path / "coordinator-private"
    ledger, round_summary = evaluate_label_round(
        request, tasks, [], [],
        reviewer_key_registry=context["reviewer_key_registry"],
        expected_reviewer_key_registry_hash=context[
            "expected_reviewer_key_registry_hash"
        ],
        source_context=context,
    )
    _, manifest = write_label_round_artifact_domains(
        request, tasks, [], [], context["reviewer_key_registry"],
        ledger, round_summary, coordinator_output, reviewer_output,
        expected_reviewer_key_registry_hash=context[
            "expected_reviewer_key_registry_hash"
        ],
        source_context=context,
    )
    assert manifest is not None
    payloads, loaded_manifest = load_validated_reviewer_packet_directory(
        reviewer_output,
        expected_reviewer_packet_manifest_sha256=_sha(
            reviewer_output / "manifest.json"
        ),
        source_context=context,
    )
    assert loaded_manifest == manifest
    assert payloads == [item["blind_payload"] for item in tasks]
    assert set(path.name for path in reviewer_output.iterdir()) == {
        "manifest.json", "blind-payloads.ndjson",
    }
    raw = (reviewer_output / "blind-payloads.ndjson").read_text().lower()
    for task in tasks:
        for sensitive_value in (
            task["lineage_id"],
            task["canonical_experiment_id"],
            task["candidate_source_id"],
            task["candidate_entry_hash"],
            task["assignment_hash"],
            task["authority_membership_hash"],
        ):
            assert sensitive_value.lower() not in raw
    assert manifest["blindness_scope"] == (
        "REVIEWER_SAFE_PAYLOADS_ONLY_NO_COORDINATOR_MAPPING"
    )
    assert manifest["gate1_effect"] == "NONE"


def test_output_domains_reject_equal_nested_and_symlink_aliases(
    tmp_path: Path,
) -> None:
    context, reviewer_registry, _ = _ready_context(tmp_path / "source")
    request, tasks = build_label_assignment_request(**context)
    ledger, round_summary = evaluate_label_round(
        request, tasks, [], [],
        reviewer_key_registry=reviewer_registry,
        expected_reviewer_key_registry_hash=reviewer_registry["registry_hash"],
        source_context=context,
    )

    same = tmp_path / "same-output"
    with pytest.raises(
        GoldenLabelAdjudicationError,
        match="G103A_OUTPUT_DOMAINS_NOT_DISTINCT_SIBLINGS",
    ):
        write_label_round_artifact_domains(
            request, tasks, [], [], reviewer_registry, ledger, round_summary,
            same, same,
            expected_reviewer_key_registry_hash=reviewer_registry["registry_hash"],
            source_context=context,
        )
    assert not same.exists()

    reviewer_parent = tmp_path / "nested-reviewer"
    with pytest.raises(
        GoldenLabelAdjudicationError,
        match="G103A_OUTPUT_PARENT_INVALID",
    ):
        write_label_round_artifact_domains(
            request, tasks, [], [], reviewer_registry, ledger, round_summary,
            reviewer_parent / "coordinator-private", reviewer_parent,
            expected_reviewer_key_registry_hash=reviewer_registry["registry_hash"],
            source_context=context,
        )
    assert not reviewer_parent.exists()

    physical_parent = tmp_path / "physical-parent"
    physical_parent.mkdir()
    alias_parent = tmp_path / "parent-alias"
    alias_parent.symlink_to(physical_parent, target_is_directory=True)
    with pytest.raises(
        GoldenLabelAdjudicationError,
        match="G103A_OUTPUT_DOMAINS_NOT_DISTINCT_SIBLINGS",
    ):
        write_label_round_artifact_domains(
            request, tasks, [], [], reviewer_registry, ledger, round_summary,
            physical_parent / "same-leaf", alias_parent / "same-leaf",
            expected_reviewer_key_registry_hash=reviewer_registry["registry_hash"],
            source_context=context,
        )
    assert not (physical_parent / "same-leaf").exists()


def test_blind_pair_agreement_round_trips_as_label_candidate_only(tmp_path: Path) -> None:
    context, reviewer_registry, private = _ready_context(tmp_path)
    request, tasks = build_label_assignment_request(**context)
    task = tasks[0]
    label = _label(task)
    responses = [
        _review(
            request, task, "REVIEWER_A", reviewer_registry, private,
            label=label, delivered_at="2026-08-07T06:20:00Z",
            submitted_at="2026-08-07T06:30:00Z",
        ),
        _review(
            request, task, "REVIEWER_B", reviewer_registry, private,
            label=label, delivered_at="2026-08-07T06:21:00Z",
            submitted_at="2026-08-07T06:31:00Z",
        ),
    ]
    responses.sort(key=lambda item: (item["task_id"], item["reviewer_role"]))
    ledger, round_summary = evaluate_label_round(
        request, tasks, responses, [],
        reviewer_key_registry=reviewer_registry,
        expected_reviewer_key_registry_hash=reviewer_registry["registry_hash"],
        source_context=context,
    )
    assert ledger[0]["status"] == "PAIR_AGREED"
    assert round_summary["status"] == "ASSIGNED_EVALUATION_SUBSET_LABELS_RESOLVED"
    assert round_summary["label_effect"] == "ASSIGNED_EVALUATION_SUBSET_PACKET_FOR_ASSEMBLY_ONLY"
    assert round_summary["not_golden_case"] is True
    assert round_summary["golden_eligible"] is False
    assert round_summary["replay_eligible"] is False
    assert round_summary["gate1_effect"] == "NONE"

    output = tmp_path / "resolved-output"
    write_label_round_artifact_domains(
        request, tasks, responses, [], reviewer_registry, ledger, round_summary,
        output, tmp_path / "resolved-reviewer-safe",
        expected_reviewer_key_registry_hash=reviewer_registry["registry_hash"],
        source_context=context,
    )
    _, loaded_ledger, loaded_round = load_validated_label_round_directory(
        output,
        expected_label_manifest_sha256=_sha(output / "manifest.json"),
        expected_reviewer_key_registry_hash=reviewer_registry["registry_hash"],
        source_context=context,
    )
    assert loaded_ledger == ledger
    assert loaded_round == round_summary


def test_disagreement_requires_distinct_third_reviewer_adjudication(tmp_path: Path) -> None:
    context, reviewer_registry, private = _ready_context(tmp_path)
    request, tasks = build_label_assignment_request(**context)
    task = tasks[0]
    label_a = _label(task)
    label_b = _label(task, decision="CREATE_DATA_FIX_TASK")
    responses = [
        _review(
            request, task, "REVIEWER_A", reviewer_registry, private,
            label=label_a, delivered_at="2026-08-07T06:20:00Z",
            submitted_at="2026-08-07T06:30:00Z",
        ),
        _review(
            request, task, "REVIEWER_B", reviewer_registry, private,
            label=label_b, delivered_at="2026-08-07T06:21:00Z",
            submitted_at="2026-08-07T06:31:00Z",
        ),
    ]
    responses.sort(key=lambda item: (item["task_id"], item["reviewer_role"]))
    ledger, pending = evaluate_label_round(
        request, tasks, responses, [],
        reviewer_key_registry=reviewer_registry,
        expected_reviewer_key_registry_hash=reviewer_registry["registry_hash"],
        source_context=context,
    )
    assert ledger[0]["status"] == "CONFLICT_PENDING_ADJUDICATION"
    assert pending["status"] == "ADJUDICATION_PENDING"
    adjudication = _adjudication(
        request, task, responses[0], responses[1], reviewer_registry, private,
        label=label_a,
    )
    ledger, resolved = evaluate_label_round(
        request, tasks, responses, [adjudication],
        reviewer_key_registry=reviewer_registry,
        expected_reviewer_key_registry_hash=reviewer_registry["registry_hash"],
        source_context=context,
    )
    assert ledger[0]["status"] == "ARBITRATED"
    assert resolved["status"] == "ASSIGNED_EVALUATION_SUBSET_LABELS_RESOLVED"


def test_second_blind_packet_must_be_delivered_before_either_submission(tmp_path: Path) -> None:
    context, reviewer_registry, private = _ready_context(tmp_path)
    request, tasks = build_label_assignment_request(**context)
    task = tasks[0]
    label = _label(task)
    responses = [
        _review(
            request, task, "REVIEWER_A", reviewer_registry, private,
            label=label,
            delivered_at="2026-08-07T06:20:00Z",
            submitted_at="2026-08-07T06:30:00Z",
        ),
        _review(
            request, task, "REVIEWER_B", reviewer_registry, private,
            label=label,
            delivered_at="2026-08-07T06:31:00Z",
            submitted_at="2026-08-07T06:40:00Z",
        ),
    ]
    responses.sort(key=lambda item: (item["task_id"], item["reviewer_role"]))
    with pytest.raises(GoldenLabelAdjudicationError, match="G103A_BLIND_DELIVERY_ORDER_INVALID"):
        evaluate_label_round(
            request, tasks, responses, [],
            reviewer_key_registry=reviewer_registry,
            expected_reviewer_key_registry_hash=reviewer_registry["registry_hash"],
            source_context=context,
        )


def test_adjudicator_cannot_override_an_agreed_pair(tmp_path: Path) -> None:
    context, reviewer_registry, private = _ready_context(tmp_path)
    request, tasks = build_label_assignment_request(**context)
    task = tasks[0]
    label = _label(task)
    responses = [
        _review(
            request, task, "REVIEWER_A", reviewer_registry, private,
            label=label, delivered_at="2026-08-07T06:20:00Z",
            submitted_at="2026-08-07T06:30:00Z",
        ),
        _review(
            request, task, "REVIEWER_B", reviewer_registry, private,
            label=label, delivered_at="2026-08-07T06:21:00Z",
            submitted_at="2026-08-07T06:31:00Z",
        ),
    ]
    responses.sort(key=lambda item: (item["task_id"], item["reviewer_role"]))
    adjudication = _adjudication(
        request, task, responses[0], responses[1], reviewer_registry, private,
        label=label,
    )
    with pytest.raises(GoldenLabelAdjudicationError, match="G103A_UNNEEDED_ADJUDICATION"):
        evaluate_label_round(
            request, tasks, responses, [adjudication],
            reviewer_key_registry=reviewer_registry,
            expected_reviewer_key_registry_hash=reviewer_registry["registry_hash"],
            source_context=context,
        )


def test_source_task_and_holdout_tampering_fail_after_full_rehash(tmp_path: Path) -> None:
    context, reviewer_registry, _ = _ready_context(tmp_path)
    request, tasks = build_label_assignment_request(**context)
    forged = deepcopy(tasks)
    forged[0]["dataset_split"] = "HOLDOUT"
    forged[0]["task_hash"] = canonical_hash({
        key: value for key, value in forged[0].items() if key != "task_hash"
    })
    with pytest.raises(GoldenLabelAdjudicationError, match="G103A_TASKS_INVALID"):
        evaluate_label_round(
            request, forged, [], [],
            reviewer_key_registry=reviewer_registry,
            expected_reviewer_key_registry_hash=reviewer_registry["registry_hash"],
            source_context=context,
        )


def test_label_contract_and_primary_frozen_fact_are_mandatory(tmp_path: Path) -> None:
    context, reviewer_registry, private = _ready_context(tmp_path)
    request, tasks = build_label_assignment_request(**context)
    task = tasks[0]
    invalid = _label(task)
    invalid["action_proposals"] = ["NONE", "PAUSE_LOSER"]
    invalid["label_hash"] = canonical_hash({
        key: value for key, value in invalid.items() if key != "label_hash"
    })
    response = _review(
        request, task, "REVIEWER_A", reviewer_registry, private,
        label=invalid, delivered_at="2026-08-07T06:20:00Z",
        submitted_at="2026-08-07T06:30:00Z",
    )
    with pytest.raises(GoldenLabelAdjudicationError, match="G103A_LABEL_INVALID"):
        evaluate_label_round(
            request, tasks, [response], [],
            reviewer_key_registry=reviewer_registry,
            expected_reviewer_key_registry_hash=reviewer_registry["registry_hash"],
            source_context=context,
        )

    supporting = next(
        item["fact_id"]
        for item in task["blind_payload"]["evidence_packet"]["facts"]
        if item["fact_kind"] == "SUPPORTING_FROZEN_EXPERIMENT_AUDIT_FACT"
    )
    invalid = _label(task)
    invalid["evidence_ref_ids"] = [supporting]
    invalid["label_hash"] = canonical_hash({
        key: value for key, value in invalid.items() if key != "label_hash"
    })
    response = _review(
        request, task, "REVIEWER_A", reviewer_registry, private,
        label=invalid, delivered_at="2026-08-07T06:20:00Z",
        submitted_at="2026-08-07T06:30:00Z",
    )
    with pytest.raises(GoldenLabelAdjudicationError, match="G103A_LABEL_EVIDENCE_INVALID"):
        evaluate_label_round(
            request, tasks, [response], [],
            reviewer_key_registry=reviewer_registry,
            expected_reviewer_key_registry_hash=reviewer_registry["registry_hash"],
            source_context=context,
        )


def test_reviewer_registry_is_frozen_before_assignment_and_principals_are_distinct(
    tmp_path: Path,
) -> None:
    context, reviewer_registry, _ = _ready_context(tmp_path)
    request, tasks = build_label_assignment_request(**context)
    alternate_registry, _ = _reviewer_registry(tmp_path / "alternate-reviewers")
    with pytest.raises(GoldenLabelAdjudicationError, match="G103A_REVIEWER_REGISTRY_INVALID"):
        evaluate_label_round(
            request, tasks, [], [],
            reviewer_key_registry=alternate_registry,
            expected_reviewer_key_registry_hash=alternate_registry["registry_hash"],
            source_context=context,
        )

    duplicate_principal = deepcopy(reviewer_registry)
    duplicate_principal["keys"][1]["principal_id"] = (
        duplicate_principal["keys"][0]["principal_id"]
    )
    duplicate_principal["registry_hash"] = canonical_hash({
        key: value for key, value in duplicate_principal.items() if key != "registry_hash"
    })
    forged_context = dict(context)
    forged_context.update({
        "reviewer_key_registry": duplicate_principal,
        "expected_reviewer_key_registry_hash": duplicate_principal["registry_hash"],
        "expected_reviewer_key_registry_sha256": _reviewer_raw_sha(duplicate_principal),
    })
    with pytest.raises(GoldenLabelAdjudicationError, match="G103A_REVIEWER_REGISTRY_INVALID"):
        build_label_assignment_request(**forged_context)


def test_same_spki_or_existing_output_fails_closed(tmp_path: Path) -> None:
    context, reviewer_registry, private = _ready_context(tmp_path)
    request, tasks = build_label_assignment_request(**context)
    forged_registry = deepcopy(reviewer_registry)
    forged_registry["keys"][1]["public_key_pem"] = forged_registry["keys"][0]["public_key_pem"]
    forged_registry["registry_hash"] = canonical_hash({
        key: value for key, value in forged_registry.items() if key != "registry_hash"
    })
    with pytest.raises(GoldenLabelAdjudicationError, match="G103A_REVIEWER_REGISTRY_INVALID"):
        evaluate_label_round(
            request, tasks, [], [],
            reviewer_key_registry=forged_registry,
            expected_reviewer_key_registry_hash=forged_registry["registry_hash"],
            source_context=context,
        )

    ledger, round_summary = evaluate_label_round(
        request, tasks, [], [],
        reviewer_key_registry=reviewer_registry,
        expected_reviewer_key_registry_hash=reviewer_registry["registry_hash"],
        source_context=context,
    )
    output = tmp_path / "existing"
    output.mkdir()
    with pytest.raises(GoldenLabelAdjudicationError, match="G103A_OUTPUT_EXISTS"):
        write_label_round_artifact_domains(
            request, tasks, [], [], reviewer_registry, ledger, round_summary,
            output, tmp_path / "existing-reviewer-safe",
            expected_reviewer_key_registry_hash=reviewer_registry["registry_hash"],
            source_context=context,
        )


def test_noncanonical_and_extra_artifact_files_are_rejected(tmp_path: Path) -> None:
    context = _registry_source(tmp_path / "source", verified=False)
    request, tasks = build_label_assignment_request(**context)
    ledger, round_summary = evaluate_label_round(
        request, tasks, [], [],
        reviewer_key_registry=None,
        expected_reviewer_key_registry_hash=None,
        source_context=context,
    )
    output = tmp_path / "artifact"
    write_label_round_artifact_domains(
        request, tasks, [], [], None, ledger, round_summary, output, None,
        expected_reviewer_key_registry_hash=None,
        source_context=context,
    )
    (output / "extra.json").write_text("{}\n")
    with pytest.raises(GoldenLabelAdjudicationError, match="G103A_ARTIFACT_FILE_SET_INVALID"):
        load_validated_label_round_directory(
            output,
            expected_label_manifest_sha256=_sha(output / "manifest.json"),
            expected_reviewer_key_registry_hash=None,
            source_context=context,
        )


def test_cli_exits_distinguish_blocked_and_review_pending(tmp_path: Path) -> None:
    blocked = _registry_source(tmp_path / "blocked", verified=False)

    def base_args(context: dict[str, object], output: Path) -> list[str]:
        source = context["source_validation"]
        args = [
            "--audit-dir", str(source["audit_dir"]),
            "--expected-audit-manifest-sha256", str(source["expected_audit_manifest_sha256"]),
            "--candidate-dir", str(source["candidate_dir"]),
            "--expected-candidate-manifest-sha256", str(source["expected_candidate_manifest_sha256"]),
            "--authority-dir", str(source["authority_dir"]),
            "--expected-authority-manifest-sha256", str(source["expected_authority_manifest_sha256"]),
            "--registry-dir", str(context["registry_dir"]),
            "--expected-registry-manifest-sha256", str(context["expected_registry_manifest_sha256"]),
            "--review-round-id", str(context["review_round_id"]),
            "--requested-at", str(context["requested_at"]),
            "--evaluated-at", str(context["evaluated_at"]),
            "--label-version", str(context["label_version"]),
            "--output-dir", str(output),
        ]
        if source["expected_authority_key_registry_hash"] is not None:
            args += [
                "--expected-authority-key-registry-hash",
                str(source["expected_authority_key_registry_hash"]),
            ]
        if context["expected_devval_key_registry_hash"] is not None:
            args += [
                "--expected-devval-key-registry-hash",
                str(context["expected_devval_key_registry_hash"]),
            ]
        if source["seed_selection_file"] is not None:
            args += [
                "--seed-selection-file", str(source["seed_selection_file"]),
                "--expected-seed-selection-file-sha256",
                str(source["expected_seed_selection_file_sha256"]),
            ]
        return args

    assert golden_cli_main(base_args(blocked, tmp_path / "blocked-cli")) == 4

    signed, reviewer_registry, _ = _ready_context(tmp_path / "signed")
    reviewer_path = tmp_path / "reviewer-registry.json"
    reviewer_path.write_text(canonical_json(reviewer_registry) + "\n")
    blinding_path = tmp_path / "blinding-map.json"
    blinding_path.write_text(canonical_json(signed["blinding_map"]) + "\n")
    pending_args = base_args(signed, tmp_path / "pending-cli") + [
        "--reviewer-key-registry", str(reviewer_path),
        "--expected-reviewer-key-registry-hash", reviewer_registry["registry_hash"],
        "--expected-reviewer-key-registry-sha256", _reviewer_raw_sha(reviewer_registry),
        "--blinding-map", str(blinding_path),
        "--expected-blinding-map-sha256", str(signed["expected_blinding_map_sha256"]),
        "--reviewer-output-dir", str(tmp_path / "pending-reviewer-packets"),
    ]
    assert golden_cli_main(pending_args) == 2
    assert set(path.name for path in (tmp_path / "pending-reviewer-packets").iterdir()) == {
        "manifest.json", "blind-payloads.ndjson",
    }
