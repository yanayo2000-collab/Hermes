from __future__ import annotations

import hashlib
import os
import re
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from app.growth.canonical_evaluation_contracts import canonical_hash, canonical_json, validate_sha256, validate_utc
from app.growth.historical_lineage_candidates import (
    HistoricalLineageCandidateError,
    _canonical_json_document,
    _canonical_ndjson,
    load_validated_audit_directory,
)


TRIAGE_VERSION = "gle-g1-02c-maturing-triage-bundle-v1"
ENGINE_VERSION = "gle-g1-02c-maturing-triage-engine-v1"
MANIFEST_VERSION = "gle-g1-02c-maturing-triage-manifest-v1"
EXACT_FILES = frozenset({"manifest.json", "triage.ndjson", "manual-review.ndjson", "coverage.json"})
MAX_ARTIFACT_FILE_BYTES = 64 * 1024 * 1024
REASON_PRIORITY = (
    "EXTERNAL_MUTATION",
    "DATA_SOURCE_MISSING",
    "STATE_MACHINE_STUCK",
    "SCHEDULER_MISSED",
    "ATTRIBUTION_PENDING",
    "NO_DELIVERY",
    "SPEND_TOO_LOW",
    "EVENTS_TOO_LOW",
    "TIME_NOT_REACHED",
    "UNKNOWN",
)
PROVABLE_EVENT_TYPES = frozenset(REASON_PRIORITY[:-1])
HIGH_RISK_REASONS = frozenset({
    "EXTERNAL_MUTATION", "DATA_SOURCE_MISSING", "STATE_MACHINE_STUCK", "SCHEDULER_MISSED",
})
ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


class HistoricalMaturingTriageError(RuntimeError):
    pass


def _fail(code: str) -> None:
    raise HistoricalMaturingTriageError(code)


def derive_maturing_triage_from_audit_directory(
    audit_dir: str | os.PathLike[str], *, expected_manifest_sha256: str,
    triage_id: str, derived_at: str,
) -> dict[str, Any]:
    bundle, binding = _load_source(audit_dir, expected_manifest_sha256)
    return _derive(bundle, binding, triage_id=triage_id, derived_at=derived_at)


def validate_maturing_triage_bundle(
    candidate: Mapping[str, Any], *, audit_dir: str | os.PathLike[str],
    expected_manifest_sha256: str,
) -> dict[str, Any]:
    bundle, binding = _load_source(audit_dir, expected_manifest_sha256)
    if not isinstance(candidate, Mapping):
        _fail("G102C_BUNDLE_INVALID")
    triage_id = candidate.get("triage_id")
    derived_at = candidate.get("derived_at")
    if not isinstance(triage_id, str) or not isinstance(derived_at, str):
        _fail("G102C_BUNDLE_INVALID")
    expected = _derive(bundle, binding, triage_id=triage_id, derived_at=derived_at)
    if dict(candidate) != expected:
        _fail("G102C_SOURCE_SEMANTICS_MISMATCH")
    return expected


def write_maturing_triage_artifacts(
    candidate: Mapping[str, Any], output_dir: str | os.PathLike[str], *,
    audit_dir: str | os.PathLike[str], expected_manifest_sha256: str,
) -> dict[str, Any]:
    validated = validate_maturing_triage_bundle(
        candidate, audit_dir=audit_dir, expected_manifest_sha256=expected_manifest_sha256,
    )
    root = Path(output_dir).expanduser()
    if not root.name or root.name in {".", ".."}:
        _fail("G102C_OUTPUT_INVALID")
    parent = root.parent
    parent_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        parent_fd = os.open(parent, parent_flags)
    except OSError as exc:
        raise HistoricalMaturingTriageError("G102C_OUTPUT_PARENT_INVALID") from exc
    final_fd: int | None = None
    complete = False
    try:
        try:
            os.mkdir(root.name, mode=0o700, dir_fd=parent_fd)
        except FileExistsError:
            _fail("G102C_OUTPUT_EXISTS")
        final_fd = os.open(
            root.name,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
        os.fchmod(final_fd, 0o700)
        _require_named_directory_identity(parent_fd, root.name, final_fd)
        payloads = {
            "triage.ndjson": _ndjson(validated["items"]),
            "manual-review.ndjson": _ndjson(validated["manual_reviews"]),
            "coverage.json": (canonical_json(validated["coverage"]) + "\n").encode(),
        }
        descriptors = {
            name: {"sha256": hashlib.sha256(raw).hexdigest(), "size_bytes": len(raw)}
            for name, raw in payloads.items()
        }
        manifest = _manifest(validated, descriptors)
        payloads["manifest.json"] = (canonical_json(manifest) + "\n").encode()
        for name in sorted(payloads):
            if len(payloads[name]) > MAX_ARTIFACT_FILE_BYTES:
                _fail("G102C_ARTIFACT_FILE_TOO_LARGE")
            _write_exclusive_at(final_fd, name, payloads[name])
        if set(os.listdir(final_fd)) != EXACT_FILES:
            _fail("G102C_ARTIFACT_DIRECTORY_INVALID")
        os.fsync(final_fd)
        _require_named_directory_identity(parent_fd, root.name, final_fd)
        complete = True
        os.fsync(parent_fd)
    except Exception as exc:
        if complete:
            raise HistoricalMaturingTriageError("G102C_OUTPUT_DURABILITY_UNCERTAIN") from exc
        if final_fd is not None:
            for name in EXACT_FILES:
                try:
                    os.unlink(name, dir_fd=final_fd)
                except FileNotFoundError:
                    pass
            os.fsync(final_fd)
        if final_fd is not None:
            try:
                _require_named_directory_identity(parent_fd, root.name, final_fd)
                os.rmdir(root.name, dir_fd=parent_fd)
            except (FileNotFoundError, OSError):
                pass
        raise
    finally:
        if final_fd is not None:
            os.close(final_fd)
        os.close(parent_fd)
    return manifest


def load_validated_maturing_triage_directory(
    input_dir: str | os.PathLike[str], *, expected_triage_manifest_sha256: str,
    audit_dir: str | os.PathLike[str], expected_audit_manifest_sha256: str,
) -> dict[str, Any]:
    try:
        validate_sha256(expected_triage_manifest_sha256, code="G102C_EXPECTED_MANIFEST_INVALID")
    except ValueError as exc:
        raise HistoricalMaturingTriageError(str(exc)) from exc
    raw = _read_artifact_directory(Path(input_dir).expanduser())
    if hashlib.sha256(raw["manifest.json"]).hexdigest() != expected_triage_manifest_sha256:
        _fail("G102C_MANIFEST_ANCHOR_MISMATCH")
    manifest = _canonical_json_document(raw["manifest.json"], "G102C_MANIFEST_JSON_INVALID")
    triage_items = _canonical_ndjson(raw["triage.ndjson"], "G102C_TRIAGE_NDJSON_INVALID")
    reviews = _canonical_ndjson(raw["manual-review.ndjson"], "G102C_REVIEW_NDJSON_INVALID")
    coverage = _canonical_json_document(raw["coverage.json"], "G102C_COVERAGE_JSON_INVALID")
    if not isinstance(manifest, Mapping):
        _fail("G102C_MANIFEST_INVALID")
    _validate_manifest_files(manifest, raw)
    candidate = {
        "schema_version": manifest.get("bundle_schema_version"),
        "engine_version": manifest.get("engine_version"),
        "triage_id": manifest.get("triage_id"),
        "derived_at": manifest.get("derived_at"),
        "input_binding": manifest.get("input_binding"),
        "classifier_contract": manifest.get("classifier_contract"),
        "items": triage_items,
        "manual_reviews": reviews,
        "coverage": coverage,
        "status": manifest.get("status"),
        "trust_status": manifest.get("trust_status"),
        "evidence_use": manifest.get("evidence_use"),
        "split_assignments": manifest.get("split_assignments"),
        "holdout_status": manifest.get("holdout_status"),
        "replay_eligible": manifest.get("replay_eligible"),
        "golden_eligible": manifest.get("golden_eligible"),
        "gate1_effect": manifest.get("gate1_effect"),
        "not_dataset_receipt": manifest.get("not_dataset_receipt"),
        "not_replay_receipt": manifest.get("not_replay_receipt"),
        "not_gate_receipt": manifest.get("not_gate_receipt"),
        "bundle_hash": manifest.get("bundle_hash"),
    }
    validated = validate_maturing_triage_bundle(
        candidate, audit_dir=audit_dir, expected_manifest_sha256=expected_audit_manifest_sha256,
    )
    if manifest != _manifest(validated, manifest["files"]):
        _fail("G102C_MANIFEST_SEMANTICS_MISMATCH")
    return validated


def _load_source(
    audit_dir: str | os.PathLike[str], expected_manifest_sha256: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        return load_validated_audit_directory(
            audit_dir, expected_manifest_sha256=expected_manifest_sha256,
        )
    except HistoricalLineageCandidateError as exc:
        raise HistoricalMaturingTriageError("G102C_SOURCE_AUDIT_INVALID") from exc


def _derive(
    source: Mapping[str, Any], binding: Mapping[str, Any], *, triage_id: str, derived_at: str,
) -> dict[str, Any]:
    if not isinstance(triage_id, str) or not ID_PATTERN.fullmatch(triage_id):
        _fail("G102C_TRIAGE_ID_INVALID")
    derived_dt = _utc_datetime(derived_at)
    capture_dt = _utc_datetime(source["request"]["captured_at"])
    if derived_dt < capture_dt:
        _fail("G102C_DERIVED_AT_INVALID")
    table_hashes = dict(binding["input_table_manifest_hashes"])
    experiments = sorted(
        (
            record for record in source["records"]
            if record["source_table"] == "ad_experiment"
            and record["projection"]["current_state"] == "MATURING"
        ),
        key=lambda item: item["source_id"],
    )
    if source["coverage"]["cutoff_eligible_experiment_current_context"]["not_asof"] is not True:
        _fail("G102C_SOURCE_CONTEXT_INVALID")
    if len(experiments) != source["coverage"]["cutoff_eligible_experiment_current_context"]["maturing_count"]:
        _fail("G102C_SOURCE_DENOMINATOR_MISMATCH")
    events_by_experiment: dict[str, list[Mapping[str, Any]]] = {}
    for record in source["records"]:
        if record["source_table"] != "ad_experiment_events":
            continue
        experiment_id = record["projection"]["experiment_id"]
        if experiment_id:
            events_by_experiment.setdefault(experiment_id, []).append(record)
    items: list[dict[str, Any]] = []
    reviews: list[dict[str, Any]] = []
    for record in experiments:
        item = _triage_item(record, events_by_experiment.get(record["source_id"], []), table_hashes)
        items.append(item)
        if item["manual_review_required"]:
            observed_high_risk = any(
                candidate["reason_code"] in HIGH_RISK_REASONS
                for candidate in item["observed_candidate_reasons"]
            )
            review = {
                "review_id": "review_" + canonical_hash({"triage_id": triage_id, "item_hash": item["item_hash"]})[:24],
                "experiment_id": item["experiment_id"],
                "triage_item_hash": item["item_hash"],
                "reason_code": item["reason_code"],
                "observed_candidate_reasons": item["observed_candidate_reasons"],
                "blocker_codes": item["blocker_codes"],
                "review_priority": "HIGH" if observed_high_risk else "REQUIRED",
                "review_status": "OPEN",
                "evidence_refs": item["evidence_refs"],
                "guessed_value": None,
            }
            review["review_hash"] = canonical_hash(review)
            reviews.append(review)
    coverage = _coverage(items, reviews, source)
    status = "INCOMPLETE_REVIEW_REQUIRED"
    result = {
        "schema_version": TRIAGE_VERSION,
        "engine_version": ENGINE_VERSION,
        "triage_id": triage_id,
        "derived_at": derived_at,
        "input_binding": dict(binding),
        "classifier_contract": {
            "context": "CUTOFF_ELIGIBLE_EXPERIMENT_CURRENT_CONTEXT",
            "not_asof": True,
            "reason_priority": list(REASON_PRIORITY),
            "proof_rule": "OBSERVED_UNVERIFIED_RETAINED_EVENT_LABEL_ONLY_V1",
            "reason_assertion_contract_status": "MISSING",
            "threshold_contract_status": "UNFROZEN",
            "metric_commitments_are_not_values": True,
        },
        "items": items,
        "manual_reviews": reviews,
        "coverage": coverage,
        "status": status,
        "trust_status": "UNSIGNED_OFFLINE_DERIVATION",
        "evidence_use": "AUDIT_ONLY",
        "split_assignments": [],
        "holdout_status": "LOCKED_NOT_ASSIGNED",
        "replay_eligible": False,
        "golden_eligible": False,
        "gate1_effect": "NONE",
        "not_dataset_receipt": True,
        "not_replay_receipt": True,
        "not_gate_receipt": True,
    }
    result["bundle_hash"] = canonical_hash(result)
    return result


def _triage_item(
    experiment: Mapping[str, Any], events: list[Mapping[str, Any]], table_hashes: Mapping[str, str],
) -> dict[str, Any]:
    by_type: dict[str, list[Mapping[str, Any]]] = {}
    for event in sorted(events, key=lambda value: (value["source_id"], value["record_hash"])):
        event_type = event["projection"]["event_type"]
        if (
            event_type in PROVABLE_EVENT_TYPES
            and event["cutoff_disposition"] == "AT_OR_BEFORE_CUTOFF_RETAINED_HISTORY_ONLY"
        ):
            by_type.setdefault(event_type, []).append(event)
    observed_reasons = [value for value in REASON_PRIORITY if value in by_type]
    evidence = [_evidence_ref(
        experiment, table_hashes["ad_experiment"],
        ["projection.current_state", "projection.experiment_id", "cutoff_disposition"],
    )]
    observed: list[dict[str, Any]] = []
    for reason in observed_reasons:
        refs = [
            _evidence_ref(
                event, table_hashes["ad_experiment_events"],
                ["projection.event_type", "projection.experiment_id", "semantic_at", "cutoff_disposition"],
            )
            for event in by_type[reason]
        ]
        evidence.extend(refs)
        candidate = {
            "reason_code": reason,
            "observation_status": "OBSERVED_UNVERIFIED_EVENT_LABEL",
            "evidence_refs": refs,
            "source_gap_codes": sorted({
                code for event in by_type[reason] for code in event["reason_codes"]
            }),
        }
        candidate["candidate_hash"] = canonical_hash(candidate)
        observed.append(candidate)
    source_gap_codes = set(experiment["reason_codes"])
    for reason in observed_reasons:
        for event in by_type[reason]:
            source_gap_codes.update(event["reason_codes"])
    blocker_codes = {
        "CURRENT_STATE_NOT_ASOF",
        "REASON_ASSERTION_CONTRACT_MISSING",
        "RETENTION_COMPLETENESS_UNKNOWN",
        "THRESHOLD_CONTRACT_UNFROZEN",
    }
    if len(observed) > 1:
        blocker_codes.add("CONFLICTING_REASON_EVENTS")
    item = {
        "experiment_id": experiment["source_id"],
        "experiment_record_hash": experiment["record_hash"],
        "current_state": "MATURING",
        "context_status": "CURRENT_CONTEXT_NOT_ASOF",
        "reason_code": "UNKNOWN",
        "reason_status": "UNKNOWN_INSUFFICIENT_EVIDENCE",
        "observed_candidate_reasons": observed,
        "blocker_codes": sorted(blocker_codes),
        "source_gap_codes": sorted(source_gap_codes),
        "evidence_refs": evidence,
        "manual_review_required": True,
    }
    item["item_hash"] = canonical_hash(item)
    return item


def _evidence_ref(record: Mapping[str, Any], table_hash: str, field_paths: list[str]) -> dict[str, Any]:
    return {
        "source_table": record["source_table"],
        "source_id": record["source_id"],
        "record_hash": record["record_hash"],
        "table_manifest_hash": table_hash,
        "field_paths": sorted(field_paths),
    }


def _coverage(
    items: list[Mapping[str, Any]], reviews: list[Mapping[str, Any]], source: Mapping[str, Any],
) -> dict[str, Any]:
    counts = {reason: 0 for reason in REASON_PRIORITY}
    observed_counts = {reason: 0 for reason in REASON_PRIORITY[:-1]}
    conflict_count = 0
    for item in items:
        counts[item["reason_code"]] += 1
        for candidate in item["observed_candidate_reasons"]:
            observed_counts[candidate["reason_code"]] += 1
        conflict_count += len(item["observed_candidate_reasons"]) > 1
    empty = not items
    return {
        "source_maturing_denominator": source["coverage"]["cutoff_eligible_experiment_current_context"]["maturing_count"],
        "triage_item_count": len(items),
        "reason_counts": counts,
        "observed_candidate_reason_counts": observed_counts,
        "unknown_count": counts["UNKNOWN"],
        "observed_high_risk_count": sum(observed_counts[value] for value in HIGH_RISK_REASONS),
        "candidate_conflict_count": conflict_count,
        "manual_review_count": len(reviews),
        "denominator_status": "EMPTY_INCOMPLETE" if empty else "NONEMPTY_INCOMPLETE",
        "bundle_blocker_codes": (
            ["MATURING_DENOMINATOR_EMPTY", "S02_02_THRESHOLDS_UNFROZEN"]
            if empty else ["S02_02_THRESHOLDS_UNFROZEN"]
        ),
        "s02_02_effect": "NONE",
        "source_current_context_not_asof": True,
    }


def _manifest(candidate: Mapping[str, Any], files: Mapping[str, Any]) -> dict[str, Any]:
    manifest = {
        "schema_version": MANIFEST_VERSION,
        "bundle_schema_version": candidate["schema_version"],
        "engine_version": candidate["engine_version"],
        "triage_id": candidate["triage_id"],
        "derived_at": candidate["derived_at"],
        "input_binding": candidate["input_binding"],
        "classifier_contract": candidate["classifier_contract"],
        "coverage": candidate["coverage"],
        "status": candidate["status"],
        "trust_status": candidate["trust_status"],
        "evidence_use": candidate["evidence_use"],
        "split_assignments": candidate["split_assignments"],
        "holdout_status": candidate["holdout_status"],
        "replay_eligible": candidate["replay_eligible"],
        "golden_eligible": candidate["golden_eligible"],
        "gate1_effect": candidate["gate1_effect"],
        "not_dataset_receipt": candidate["not_dataset_receipt"],
        "not_replay_receipt": candidate["not_replay_receipt"],
        "not_gate_receipt": candidate["not_gate_receipt"],
        "bundle_hash": candidate["bundle_hash"],
        "files": dict(files),
    }
    manifest["manifest_hash"] = canonical_hash(manifest)
    return manifest


def _validate_manifest_files(manifest: Mapping[str, Any], raw: Mapping[str, bytes]) -> None:
    expected_keys = {
        "schema_version", "bundle_schema_version", "engine_version", "triage_id", "derived_at",
        "input_binding", "classifier_contract", "coverage", "status", "trust_status", "evidence_use",
        "split_assignments", "holdout_status", "replay_eligible", "golden_eligible", "gate1_effect",
        "not_dataset_receipt", "not_replay_receipt", "not_gate_receipt", "bundle_hash", "files", "manifest_hash",
    }
    if set(manifest) != expected_keys or manifest["schema_version"] != MANIFEST_VERSION:
        _fail("G102C_MANIFEST_INVALID")
    if manifest["manifest_hash"] != canonical_hash({key: value for key, value in manifest.items() if key != "manifest_hash"}):
        _fail("G102C_MANIFEST_HASH_MISMATCH")
    if not isinstance(manifest["files"], Mapping) or set(manifest["files"]) != EXACT_FILES - {"manifest.json"}:
        _fail("G102C_MANIFEST_FILES_INVALID")
    for name, descriptor in manifest["files"].items():
        if not isinstance(descriptor, Mapping) or set(descriptor) != {"sha256", "size_bytes"}:
            _fail("G102C_MANIFEST_FILES_INVALID")
        try:
            validate_sha256(descriptor["sha256"], code="G102C_MANIFEST_FILE_HASH_INVALID")
        except ValueError as exc:
            raise HistoricalMaturingTriageError(str(exc)) from exc
        if type(descriptor["size_bytes"]) is not int or descriptor["size_bytes"] < 0:
            _fail("G102C_MANIFEST_FILES_INVALID")
        if len(raw[name]) != descriptor["size_bytes"] or hashlib.sha256(raw[name]).hexdigest() != descriptor["sha256"]:
            _fail("G102C_FILE_INTEGRITY_MISMATCH")


def _utc_datetime(value: Any) -> datetime:
    try:
        validate_utc(value, code="G102C_TIMESTAMP_INVALID")
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except (TypeError, ValueError) as exc:
        raise HistoricalMaturingTriageError("G102C_TIMESTAMP_INVALID") from exc


def _ndjson(items: list[Mapping[str, Any]]) -> bytes:
    return ("".join(canonical_json(item) + "\n" for item in items)).encode()


def _write_exclusive_at(directory_fd: int, name: str, raw: bytes) -> None:
    fd = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
        dir_fd=directory_fd,
    )
    try:
        offset = 0
        while offset < len(raw):
            written = os.write(fd, raw[offset:])
            if written <= 0:
                _fail("G102C_ARTIFACT_WRITE_FAILED")
            offset += written
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(name, 0o600, dir_fd=directory_fd, follow_symlinks=False)


def _require_named_directory_identity(parent_fd: int, name: str, opened_fd: int) -> None:
    named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    opened = os.fstat(opened_fd)
    if (
        not stat.S_ISDIR(named.st_mode)
        or (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino)
    ):
        _fail("G102C_OUTPUT_DIRECTORY_CHANGED")


def _read_artifact_directory(root: Path) -> dict[str, bytes]:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        root_fd = os.open(root, flags)
    except OSError as exc:
        raise HistoricalMaturingTriageError("G102C_ARTIFACT_DIRECTORY_INVALID") from exc
    try:
        root_before = os.fstat(root_fd)
        if not stat.S_ISDIR(root_before.st_mode) or stat.S_IMODE(root_before.st_mode) != 0o700:
            _fail("G102C_ARTIFACT_MODE_INVALID")
        if set(os.listdir(root_fd)) != EXACT_FILES:
            _fail("G102C_ARTIFACT_DIRECTORY_INVALID")
        raw: dict[str, bytes] = {}
        file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        for name in sorted(EXACT_FILES):
            try:
                fd = os.open(name, file_flags, dir_fd=root_fd)
            except OSError as exc:
                raise HistoricalMaturingTriageError("G102C_ARTIFACT_FILE_INVALID") from exc
            try:
                before = os.fstat(fd)
                if (
                    not stat.S_ISREG(before.st_mode)
                    or stat.S_IMODE(before.st_mode) != 0o600
                    or before.st_size > MAX_ARTIFACT_FILE_BYTES
                ):
                    _fail("G102C_ARTIFACT_FILE_INVALID")
                chunks: list[bytes] = []
                total = 0
                while True:
                    chunk = os.read(fd, min(1024 * 1024, MAX_ARTIFACT_FILE_BYTES + 1 - total))
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_ARTIFACT_FILE_BYTES:
                        _fail("G102C_ARTIFACT_FILE_INVALID")
                    chunks.append(chunk)
                after = os.fstat(fd)
                if (
                    (before.st_dev, before.st_ino, before.st_mode, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                    != (after.st_dev, after.st_ino, after.st_mode, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                    or total != after.st_size
                ):
                    _fail("G102C_ARTIFACT_CHANGED_DURING_READ")
                raw[name] = b"".join(chunks)
            finally:
                os.close(fd)
        root_after = os.fstat(root_fd)
        if (
            set(os.listdir(root_fd)) != EXACT_FILES
            or (root_before.st_dev, root_before.st_ino, root_before.st_mode, root_before.st_mtime_ns, root_before.st_ctime_ns)
            != (root_after.st_dev, root_after.st_ino, root_after.st_mode, root_after.st_mtime_ns, root_after.st_ctime_ns)
        ):
            _fail("G102C_ARTIFACT_CHANGED_DURING_READ")
        return raw
    finally:
        os.close(root_fd)
