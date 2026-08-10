"""Read-only AppsFlyer + Meta exact Cell-lineage evidence.

The module binds an externally SHA-pinned AppsFlyer aggregate CSV to one
SPLIT_TEST Study and its exact two Cell -> Ad Set -> Ad paths.  It deliberately
does not admit source authority, emit an EvaluationInputSnapshot, or change a
Gate result.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping, Sequence


REQUEST_VERSION = "gle-af-meta-cell-lineage-request-v1"
EVIDENCE_VERSION = "gle-af-meta-cell-lineage-evidence-v1"
GRAPH_API_VERSION = "v25.0"
MAX_CSV_BYTES = 32 * 1024 * 1024
MAX_CSV_ROWS = 100_000
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_GRAPH_RESPONSE_BYTES = 2 * 1024 * 1024
CSV_REQUIRED_FIELDS = (
    "Media source",
    "Ad ID",
    "Impressions",
    "Clicks",
    "Total attributions appsflyer",
    "Installs appsflyer",
    "Re-attributions appsflyer",
    "Re-engagements appsflyer",
)
_SHA_RE = re.compile(r"[0-9a-f]{64}")
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}")
_APP_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{1,255}")

CEILING = {
    "lineage_effect": "REDERIVED_EXACT_CELL_LINEAGE_ONLY",
    "source_content_authority": "NOT_VERIFIED",
    "live_graph_transport_attestation": "NOT_PROVIDED",
    "appsflyer_transport_attestation": "NOT_PROVIDED",
    "objective_authority_effect": "NONE",
    "spec_authority_effect": "NONE",
    "snapshot_effect": "NONE",
    "snapshot_emitted": False,
    "partition_effect": "NONE",
    "holdout_status": "LOCKED_NOT_ASSIGNED",
    "replay_executed": False,
    "replay_eligible": False,
    "golden_eligible": False,
    "gate0_effect": "NONE",
    "gate0_result_effect": "UNCHANGED",
    "gate1_effect": "NONE",
    "not_dataset_receipt": True,
    "not_snapshot_receipt": True,
    "not_replay_receipt": True,
    "not_gate_receipt": True,
}


class CellLineageEvidenceError(ValueError):
    """Stable validation and integrity error contract."""


def _fail(code: str) -> None:
    raise CellLineageEvidenceError(code)


def canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, RecursionError, OverflowError) as exc:
        raise CellLineageEvidenceError("G104B6_JSON_INVALID") from exc


def json_bytes(value: Any) -> bytes:
    return (canonical_json(value) + "\n").encode("utf-8")


def hash_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _pairs(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _fail("G104B6_JSON_DUPLICATE_KEY")
        result[key] = value
    return result


def parse_request(raw: bytes) -> dict[str, Any]:
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_JSON_BYTES:
        _fail("G104B6_REQUEST_SIZE_INVALID")
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("constant")),
        )
        encoded = json_bytes(value)
    except CellLineageEvidenceError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError, RecursionError, OverflowError) as exc:
        raise CellLineageEvidenceError("G104B6_REQUEST_JSON_INVALID") from exc
    if raw != encoded:
        _fail("G104B6_REQUEST_NOT_CANONICAL")
    return validate_request(value)


def _exact(value: Any, keys: set[str], code: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != keys:
        _fail(code)
    return dict(value)


def _identifier(value: Any, code: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        _fail(code)
    return value


def _sha(value: Any, code: str) -> str:
    if not isinstance(value, str) or not _SHA_RE.fullmatch(value):
        _fail(code)
    return value


def _utc(value: Any, code: str) -> datetime:
    if not isinstance(value, str) or not value or value.endswith("Z"):
        _fail(code)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise CellLineageEvidenceError(code) from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(None):
        _fail(code)
    normalized = parsed.astimezone(timezone.utc)
    if value != normalized.isoformat():
        _fail(code)
    return normalized


def _date(value: Any, code: str) -> date:
    if not isinstance(value, str):
        _fail(code)
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise CellLineageEvidenceError(code) from exc


def validate_request(value: Any) -> dict[str, Any]:
    body = _exact(
        value,
        {
            "schema_version",
            "evidence_id",
            "mode",
            "requested_at",
            "appsflyer_raw_sha256",
            "report_window",
            "subject",
        },
        "G104B6_REQUEST_SCHEMA_INVALID",
    )
    if body["schema_version"] != REQUEST_VERSION:
        _fail("G104B6_REQUEST_VERSION_INVALID")
    _identifier(body["evidence_id"], "G104B6_EVIDENCE_ID_INVALID")
    if not isinstance(body["mode"], str) or body["mode"] not in {
        "HISTORICAL_TEST",
        "NATURAL_AUDIT_CANDIDATE",
    }:
        _fail("G104B6_MODE_INVALID")
    requested_at = _utc(body["requested_at"], "G104B6_REQUESTED_AT_INVALID")
    _sha(body["appsflyer_raw_sha256"], "G104B6_APPSFLYER_SHA_INVALID")

    window = _exact(
        body["report_window"],
        {"date_from", "date_to", "reporting_timezone", "data_cutoff_at"},
        "G104B6_WINDOW_SCHEMA_INVALID",
    )
    start = _date(window["date_from"], "G104B6_WINDOW_DATE_INVALID")
    stop = _date(window["date_to"], "G104B6_WINDOW_DATE_INVALID")
    cutoff = _utc(window["data_cutoff_at"], "G104B6_CUTOFF_INVALID")
    if stop < start or (stop - start).days > 31:
        _fail("G104B6_WINDOW_RANGE_INVALID")
    if cutoff > requested_at:
        _fail("G104B6_CUTOFF_AFTER_REQUEST_INVALID")
    if not isinstance(window["reporting_timezone"], str):
        _fail("G104B6_TIMEZONE_INVALID")
    if body["mode"] == "NATURAL_AUDIT_CANDIDATE" and window["reporting_timezone"] != "Asia/Shanghai":
        _fail("G104B6_NATURAL_TIMEZONE_INVALID")
    if body["mode"] == "HISTORICAL_TEST" and window["reporting_timezone"] not in {
        "Asia/Shanghai",
        "Asia/Hong_Kong",
    }:
        _fail("G104B6_HISTORICAL_TIMEZONE_INVALID")

    subject = _exact(
        body["subject"],
        {"app_id", "account_id", "market", "study_id", "campaign_id", "cells"},
        "G104B6_SUBJECT_SCHEMA_INVALID",
    )
    if not isinstance(subject["app_id"], str) or not _APP_ID_RE.fullmatch(subject["app_id"]):
        _fail("G104B6_APP_ID_INVALID")
    for field in ("account_id", "market", "study_id", "campaign_id"):
        _identifier(subject[field], "G104B6_SUBJECT_ID_INVALID")
    if not isinstance(subject["cells"], list) or len(subject["cells"]) != 2:
        _fail("G104B6_CELLS_INVALID")
    cells: list[dict[str, str]] = []
    for raw in subject["cells"]:
        cell = _exact(
            raw,
            {"cell_key", "study_cell_id", "adset_id", "ad_id"},
            "G104B6_CELL_SCHEMA_INVALID",
        )
        if not isinstance(cell["cell_key"], str) or cell["cell_key"] not in {"C1", "C2"}:
            _fail("G104B6_CELL_KEY_INVALID")
        for field in ("study_cell_id", "adset_id", "ad_id"):
            _identifier(cell[field], "G104B6_CELL_ID_INVALID")
        cells.append(cell)
    cells.sort(key=lambda item: item["cell_key"])
    if [item["cell_key"] for item in cells] != ["C1", "C2"]:
        _fail("G104B6_CELL_SET_INVALID")
    for field in ("study_cell_id", "adset_id", "ad_id"):
        values = [item[field] for item in cells]
        if len(set(values)) != 2:
            _fail("G104B6_CELL_ID_DUPLICATE")
    subject["cells"] = cells
    body["subject"] = subject
    body["report_window"] = window
    return body


def _integer(value: Any, code: str, *, nullable: bool = False) -> int | None:
    if value in (None, ""):
        if nullable:
            return None
        _fail(code)
    if isinstance(value, bool):
        _fail(code)
    text = str(value).strip()
    if not re.fullmatch(r"[0-9]+", text):
        _fail(code)
    result = int(text)
    if result < 0:
        _fail(code)
    return result


def parse_appsflyer_csv(
    raw: bytes,
    request: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], int]:
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_CSV_BYTES:
        _fail("G104B6_CSV_SIZE_INVALID")
    expected = _sha(request["appsflyer_raw_sha256"], "G104B6_APPSFLYER_SHA_INVALID")
    if hashlib.sha256(raw).hexdigest() != expected:
        _fail("G104B6_CSV_SHA_MISMATCH")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CellLineageEvidenceError("G104B6_CSV_ENCODING_INVALID") from exc
    if "\x00" in text:
        _fail("G104B6_CSV_ENCODING_INVALID")
    reader = csv.DictReader(io.StringIO(text, newline=""))
    fieldnames = reader.fieldnames
    if not isinstance(fieldnames, list) or len(fieldnames) != len(set(fieldnames)):
        _fail("G104B6_CSV_HEADER_INVALID")
    if not set(CSV_REQUIRED_FIELDS).issubset(fieldnames):
        _fail("G104B6_CSV_HEADER_INVALID")

    target_ids = {cell["ad_id"] for cell in request["subject"]["cells"]}
    matches: dict[str, list[dict[str, Any]]] = {item: [] for item in target_ids}
    count = 0
    for raw_row in reader:
        count += 1
        if count > MAX_CSV_ROWS:
            _fail("G104B6_CSV_ROW_LIMIT_EXCEEDED")
        if None in raw_row:
            _fail("G104B6_CSV_ROW_INVALID")
        ad_id = str(raw_row.get("Ad ID") or "").strip()
        if ad_id not in target_ids:
            continue
        media_source = str(raw_row.get("Media source") or "").strip()
        if media_source != "Facebook Ads":
            _fail("G104B6_MEDIA_SOURCE_INVALID")
        matches[ad_id].append({
            "ad_id": ad_id,
            "media_source": media_source,
            "impressions": _integer(raw_row.get("Impressions"), "G104B6_METRIC_INVALID"),
            "clicks": _integer(raw_row.get("Clicks"), "G104B6_METRIC_INVALID"),
            "total_attributions": _integer(
                raw_row.get("Total attributions appsflyer"), "G104B6_METRIC_INVALID"
            ),
            "installs": _integer(raw_row.get("Installs appsflyer"), "G104B6_METRIC_INVALID"),
            "reattributions": _integer(
                raw_row.get("Re-attributions appsflyer"), "G104B6_METRIC_INVALID", nullable=True
            ),
            "reengagements": _integer(
                raw_row.get("Re-engagements appsflyer"), "G104B6_METRIC_INVALID", nullable=True
            ),
        })
    if count == 0:
        _fail("G104B6_CSV_EMPTY")
    if any(len(rows) != 1 for rows in matches.values()):
        _fail("G104B6_TARGET_AD_GRAIN_INVALID")
    return sorted((rows[0] for rows in matches.values()), key=lambda item: item["ad_id"]), count


def validate_meta_capture(value: Any, request: Mapping[str, Any]) -> dict[str, Any]:
    capture = _exact(
        value,
        {"graph_api_version", "captured_at", "study", "cells", "ads"},
        "G104B6_META_CAPTURE_SCHEMA_INVALID",
    )
    if capture["graph_api_version"] != GRAPH_API_VERSION:
        _fail("G104B6_GRAPH_VERSION_INVALID")
    captured_at = _utc(capture["captured_at"], "G104B6_CAPTURE_TIME_INVALID")
    requested_at = _utc(request["requested_at"], "G104B6_REQUESTED_AT_INVALID")
    if captured_at < requested_at or captured_at - requested_at > timedelta(minutes=5):
        _fail("G104B6_CAPTURE_CLOCK_INVALID")
    study = _exact(
        capture["study"],
        {"id", "type", "start_time", "end_time", "observation_end_time"},
        "G104B6_STUDY_SCHEMA_INVALID",
    )
    if (
        not isinstance(study["id"], str)
        or not isinstance(study["type"], str)
        or study["id"] != request["subject"]["study_id"]
        or study["type"] != "SPLIT_TEST"
    ):
        _fail("G104B6_STUDY_BINDING_INVALID")
    for field in ("start_time", "end_time", "observation_end_time"):
        _utc(study[field], "G104B6_STUDY_TIME_INVALID")
    if _utc(study["end_time"], "G104B6_STUDY_TIME_INVALID") != _utc(
        study["observation_end_time"], "G104B6_STUDY_TIME_INVALID"
    ):
        _fail("G104B6_STUDY_END_INVALID")

    expected_cells = {item["study_cell_id"]: item for item in request["subject"]["cells"]}
    if not isinstance(capture["cells"], list) or len(capture["cells"]) != 2:
        _fail("G104B6_META_CELLS_INVALID")
    normalized_cells: list[dict[str, Any]] = []
    seen_cells: set[str] = set()
    for raw in capture["cells"]:
        cell = _exact(
            raw,
            {"id", "treatment_percentage", "control_percentage", "ad_entities_count", "adsets"},
            "G104B6_META_CELL_SCHEMA_INVALID",
        )
        cell_id = _identifier(cell["id"], "G104B6_META_CELL_ID_INVALID")
        if cell_id not in expected_cells or cell_id in seen_cells:
            _fail("G104B6_META_CELL_SET_INVALID")
        seen_cells.add(cell_id)
        if (
            isinstance(cell["treatment_percentage"], bool)
            or not isinstance(cell["treatment_percentage"], (int, float))
            or cell["treatment_percentage"] != 50
            or isinstance(cell["control_percentage"], bool)
            or not isinstance(cell["control_percentage"], (int, float))
            or cell["control_percentage"] != 0
            or type(cell["ad_entities_count"]) is not int
            or cell["ad_entities_count"] != 1
        ):
            _fail("G104B6_META_CELL_ALLOCATION_INVALID")
        if not isinstance(cell["adsets"], list) or len(cell["adsets"]) != 1:
            _fail("G104B6_META_CELL_ADSET_INVALID")
        adset = _exact(
            cell["adsets"][0],
            {"id", "campaign_id"},
            "G104B6_META_ADSET_SCHEMA_INVALID",
        )
        for field in ("id", "campaign_id"):
            _identifier(adset[field], "G104B6_META_ADSET_ID_INVALID")
        expected = expected_cells[cell_id]
        if adset["id"] != expected["adset_id"] or adset["campaign_id"] != request["subject"]["campaign_id"]:
            _fail("G104B6_META_CELL_ADSET_BINDING_INVALID")
        normalized_cells.append(cell)
    if seen_cells != set(expected_cells):
        _fail("G104B6_META_CELL_SET_INVALID")

    expected_ads = {item["ad_id"]: item for item in request["subject"]["cells"]}
    if not isinstance(capture["ads"], list) or len(capture["ads"]) != 2:
        _fail("G104B6_META_ADS_INVALID")
    normalized_ads: list[dict[str, Any]] = []
    seen_ads: set[str] = set()
    for raw in capture["ads"]:
        ad = _exact(
            raw,
            {"id", "account_id", "campaign_id", "adset_id"},
            "G104B6_META_AD_SCHEMA_INVALID",
        )
        for field in ("id", "account_id", "campaign_id", "adset_id"):
            _identifier(ad[field], "G104B6_META_AD_ID_INVALID")
        if ad["id"] not in expected_ads or ad["id"] in seen_ads:
            _fail("G104B6_META_AD_SET_INVALID")
        seen_ads.add(ad["id"])
        expected = expected_ads[ad["id"]]
        if (
            ad["account_id"] != request["subject"]["account_id"]
            or ad["campaign_id"] != request["subject"]["campaign_id"]
            or ad["adset_id"] != expected["adset_id"]
        ):
            _fail("G104B6_META_AD_BINDING_INVALID")
        normalized_ads.append(ad)
    if seen_ads != set(expected_ads):
        _fail("G104B6_META_AD_SET_INVALID")
    capture["cells"] = sorted(normalized_cells, key=lambda item: item["id"])
    capture["ads"] = sorted(normalized_ads, key=lambda item: item["id"])
    capture["study"] = study
    return capture


def derive_lineage_evidence(
    *,
    request: Mapping[str, Any],
    appsflyer_raw: bytes,
    meta_capture: Mapping[str, Any],
) -> dict[str, Any]:
    normalized_request = validate_request(request)
    appsflyer, appsflyer_row_count = parse_appsflyer_csv(appsflyer_raw, normalized_request)
    capture = validate_meta_capture(meta_capture, normalized_request)
    af_by_ad = {item["ad_id"]: item for item in appsflyer}
    ads_by_id = {item["id"]: item for item in capture["ads"]}
    cells_by_id = {item["id"]: item for item in capture["cells"]}
    rows: list[dict[str, Any]] = []
    for expected in normalized_request["subject"]["cells"]:
        meta_cell = cells_by_id[expected["study_cell_id"]]
        meta_ad = ads_by_id[expected["ad_id"]]
        rows.append({
            "cell_key": expected["cell_key"],
            "study_cell_id": expected["study_cell_id"],
            "adset_id": expected["adset_id"],
            "ad_id": expected["ad_id"],
            "meta_cell": meta_cell,
            "meta_ad": meta_ad,
            "appsflyer": af_by_ad[expected["ad_id"]],
        })
    status = (
        "HISTORICAL_EXACT_CELL_LINEAGE_REDERIVED"
        if normalized_request["mode"] == "HISTORICAL_TEST"
        else "NATURAL_AUDIT_CANDIDATE_EXACT_CELL_LINEAGE_REDERIVED"
    )
    gaps = [
        "SOURCE_CONTENT_AUTHORITY_NOT_VERIFIED",
        "APPSFLYER_TRANSPORT_NOT_EXTERNALLY_ATTESTED",
        "LIVE_GRAPH_TRANSPORT_NOT_EXTERNALLY_ATTESTED",
        "NOT_A_SNAPSHOT_OR_GATE_RECEIPT",
    ]
    if normalized_request["mode"] == "HISTORICAL_TEST":
        gaps.append("HISTORICAL_WINDOW_NOT_ADMISSIBLE_FOR_NATURAL_AUDIT")
    evidence: dict[str, Any] = {
        "schema_version": EVIDENCE_VERSION,
        "evidence_id": normalized_request["evidence_id"],
        "status": status,
        "request_hash": hash_json(normalized_request),
        "appsflyer_raw_sha256": hashlib.sha256(appsflyer_raw).hexdigest(),
        "appsflyer_raw_bytes": len(appsflyer_raw),
        "appsflyer_raw_rows": appsflyer_row_count,
        "meta_capture": capture,
        "meta_capture_hash": hash_json(capture),
        "subject": normalized_request["subject"],
        "report_window": normalized_request["report_window"],
        "captured_at": capture["captured_at"],
        "rows": rows,
        "gaps": sorted(gaps),
        "ceiling": dict(CEILING),
        "evidence_hash": "",
    }
    evidence["evidence_hash"] = hash_json({key: value for key, value in evidence.items() if key != "evidence_hash"})
    return evidence


def validate_lineage_evidence(
    value: Any,
    *,
    request: Mapping[str, Any],
    appsflyer_raw: bytes,
    meta_capture: Mapping[str, Any],
) -> dict[str, Any]:
    expected = derive_lineage_evidence(
        request=request,
        appsflyer_raw=appsflyer_raw,
        meta_capture=meta_capture,
    )
    if value != expected:
        _fail("G104B6_EVIDENCE_REDERIVE_MISMATCH")
    return expected


def capture_meta_graph(
    *,
    session: Any,
    access_token: str,
    request: Mapping[str, Any],
    captured_at: str,
    graph_root: str = "https://graph.facebook.com/v25.0",
) -> dict[str, Any]:
    normalized = validate_request(request)
    if not session or not isinstance(access_token, str) or not access_token.strip():
        _fail("G104B6_META_TRANSPORT_INVALID")
    if graph_root != "https://graph.facebook.com/v25.0":
        _fail("G104B6_GRAPH_ROOT_INVALID")
    _utc(captured_at, "G104B6_CAPTURE_TIME_INVALID")

    def get(path: str, fields: str) -> dict[str, Any]:
        response = None
        try:
            response = session.get(
                f"{graph_root}/{path}",
                params={"fields": fields},
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=20,
                allow_redirects=False,
                stream=True,
            )
            chunks: list[bytes] = []
            consumed = 0
            for chunk in response.iter_content(chunk_size=65536):
                if not isinstance(chunk, bytes):
                    _fail("G104B6_META_GET_FAILED")
                consumed += len(chunk)
                if consumed > MAX_GRAPH_RESPONSE_BYTES:
                    _fail("G104B6_META_RESPONSE_TOO_LARGE")
                chunks.append(chunk)
            raw = b"".join(chunks)
            if not raw:
                _fail("G104B6_META_GET_FAILED")
            payload = json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=_pairs,
                parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("constant")),
            )
        except CellLineageEvidenceError:
            raise
        except Exception as exc:  # requests is an optional runtime boundary here
            raise CellLineageEvidenceError("G104B6_META_GET_FAILED") from exc
        finally:
            if response is not None:
                response.close()
        if (
            getattr(response, "status_code", None) != 200
            or not isinstance(payload, dict)
            or payload.get("error")
        ):
            _fail("G104B6_META_GET_FAILED")
        return payload

    def exact_page(path: str, fields: str) -> list[dict[str, Any]]:
        payload = get(path, fields)
        data = payload.get("data")
        paging = payload.get("paging")
        if (
            not isinstance(data, list)
            or (paging is not None and not isinstance(paging, Mapping))
            or (isinstance(paging, Mapping) and paging.get("next"))
        ):
            _fail("G104B6_META_PAGE_INCOMPLETE")
        return data

    subject = normalized["subject"]
    raw_study = get(
        subject["study_id"],
        "id,type,start_time,end_time,observation_end_time",
    )
    raw_cells = exact_page(
        f"{subject['study_id']}/cells",
        "id,treatment_percentage,control_percentage,ad_entities_count",
    )
    expected_cell_ids = {item["study_cell_id"] for item in subject["cells"]}
    raw_cell_ids = [
        raw.get("id")
        for raw in raw_cells
        if isinstance(raw, Mapping) and isinstance(raw.get("id"), str)
    ]
    if (
        len(raw_cells) != 2
        or len(raw_cell_ids) != 2
        or len(set(raw_cell_ids)) != 2
        or set(raw_cell_ids) != expected_cell_ids
    ):
        _fail("G104B6_META_CELL_SET_INVALID")
    cells: list[dict[str, Any]] = []
    for raw in raw_cells:
        adsets = exact_page(f"{raw['id']}/adsets", "id,campaign_id")
        cells.append({
            "id": str(raw.get("id") or ""),
            "treatment_percentage": raw.get("treatment_percentage"),
            "control_percentage": raw.get("control_percentage"),
            "ad_entities_count": raw.get("ad_entities_count"),
            "adsets": list(adsets or []),
        })
    ads: list[dict[str, Any]] = []
    for expected in subject["cells"]:
        raw = get(expected["ad_id"], "id,account_id,campaign_id,adset_id")
        ads.append({key: raw.get(key) for key in ("id", "account_id", "campaign_id", "adset_id")})
    capture = {
        "graph_api_version": GRAPH_API_VERSION,
        "captured_at": captured_at,
        "study": {
            "id": raw_study.get("id"),
            "type": raw_study.get("type"),
            "start_time": _graph_utc(raw_study.get("start_time")),
            "end_time": _graph_utc(raw_study.get("end_time")),
            "observation_end_time": _graph_utc(raw_study.get("observation_end_time")),
        },
        "cells": cells,
        "ads": ads,
    }
    return validate_meta_capture(capture, normalized)


def _graph_utc(value: Any) -> str:
    if not isinstance(value, str) or not value:
        _fail("G104B6_STUDY_TIME_INVALID")
    candidate = value.replace("Z", "+00:00")
    if re.search(r"[+-][0-9]{4}$", candidate):
        candidate = candidate[:-2] + ":" + candidate[-2:]
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise CellLineageEvidenceError("G104B6_STUDY_TIME_INVALID") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _fail("G104B6_STUDY_TIME_INVALID")
    return parsed.astimezone(timezone.utc).isoformat()
