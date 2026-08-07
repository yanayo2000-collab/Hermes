"""Bounded GET-only Gate-0 permission, topology, and activation audit.

G0-04 emits an evidence fragment.  It never grants ``CONTROLLED_FEASIBLE``
and never creates a Gate receipt.  G0-05 must combine this fragment with
attribution, actual allocation, power, thresholds, and named attestations.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from copy import deepcopy
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Tuple
from urllib.parse import quote

from app.growth.common import payload_hash
from app.growth.meta_execution_worker import execution_steps_for, is_delivery_status_step
from app.growth.meta_readback import COPY_ONLY_READ_FIELDS, MetaCopyOnlyReadback
from app.growth.primary_text_only_compiler import verify_compiler_receipt


ENGINE_VERSION = "gle-g0-04-get-only-topology-audit-v1"
REQUEST_VERSION = "gle-g0-04-audit-request-v1"
RECEIPT_VERSION = "gle-g0-04-audit-receipt-v1"
TOPOLOGY_VERSION = "gle-g0-04-topology-v1"
CANONICAL_VERSION = "gle-canonical-json-v1"
GRAPH_API_VERSION = "v25.0"
SDK_CONTRACT_VERSION = "gle-meta-sdk-v1"
MAX_DB_BYTES = 32 * 1024 * 1024 * 1024
MAX_GRAPH_PAGES = 20
MAX_GRAPH_ITEMS = 2000
MAX_GRAPH_RESPONSE_BYTES = 2 * 1024 * 1024
MIN_REQUIRED_PERMISSIONS = frozenset({
    "ads_management", "ads_read", "business_management", "pages_manage_metadata",
    "pages_read_engagement", "pages_show_list",
})
MIN_REQUIRED_ACCOUNT_TASKS = frozenset({"ADVERTISE", "MANAGE"})
MIN_REQUIRED_PAGE_TASKS = frozenset({"ADVERTISE"})
ALLOWED_APP_ROLES = frozenset({"ADMINISTRATOR", "DEVELOPER"})
ALLOWED_STUDY_OBJECTIVE_TYPES = frozenset({"COST_PER_ACTION"})
FROZEN_FRESHNESS_POLICY = {
    "max_run_seconds": 120,
    "receipt_ttl_seconds": 300,
    "activity_settlement_seconds": 300,
    "clock_skew_seconds": 60,
    "max_pages": 5,
    "max_events": 100,
}

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9._:-]+$")
_SECRET_KEY_RE = re.compile(r"(?:access[_-]?token|token|secret|password|authorization)", re.I)
_TOKEN_TEXT_RE = re.compile(r"(?i)(access_token|authorization|bearer)(\s*[:=]\s*|\s+)[^\s&]+")
_ALLOWED_STATUSES = {"PASS", "FAIL", "INCOMPLETE", "POLLUTED"}
_REQUEST_KEYS = {
    "schema_version", "audit_id", "requested_at", "request_nonce",
    "graph_api_version", "sdk_contract_version", "topology_contract_version",
    "create_operation_action_id", "activation_operation_action_id",
    "actor_binding_registry_hash", "required_permissions", "required_account_tasks",
    "freshness_policy",
}
_FRESHNESS_KEYS = {
    "max_run_seconds", "receipt_ttl_seconds", "activity_settlement_seconds",
    "clock_skew_seconds", "max_pages", "max_events",
}
_ACTOR_REGISTRY_KEYS = {"schema_version", "principals"}
_ACTOR_KEYS = {"actor_id", "application_id", "roles"}


class G004ContractError(ValueError):
    pass


class G004SourceError(RuntimeError):
    pass


class G004GraphError(RuntimeError):
    pass


def canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise G004ContractError("G004_INPUT_SCHEMA_INVALID") from exc


def hash_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise G004SourceError("G004_SOURCE_UNREADABLE") from exc
    return digest.hexdigest()


def _redact(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): "[REDACTED]" if _SECRET_KEY_RE.search(str(key)) else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        return _TOKEN_TEXT_RE.sub(r"\1\2[REDACTED]", value)
    return value


def _evidence_safe(value: Any, *, key: str = "") -> Any:
    """Preserve audit structure while hashing copy, names, links, and free text."""

    if isinstance(value, Mapping):
        return {
            str(item_key): _evidence_safe(item, key=str(item_key))
            for item_key, item in value.items()
            if not _SECRET_KEY_RE.search(str(item_key))
        }
    if isinstance(value, (list, tuple)):
        return [_evidence_safe(item, key=key) for item in value]
    if isinstance(value, str):
        normalized = key.lower()
        safe_keys = {
            "schema_version", "request_hash", "source_snapshot_sha256", "local_evidence_hash",
            "evidence_bundle_hash", "id", "account_id", "business_id", "page_id",
            "application_id", "app_id", "user_id", "study_id", "campaign_id", "adset_id",
            "ad_id", "creative_id", "actor_id", "object_id", "object_type", "event_type",
            "status", "effective_status", "old_value", "new_value", "from", "to", "field",
            "permission", "tasks", "role", "type", "source", "country", "market", "endpoint",
            "fields", "response_hash", "observed_at", "checked_at", "expires_at", "start_time",
            "end_time", "updated_time", "created_time", "event_time", "date_time_in_timezone",
        }
        if normalized not in safe_keys and not normalized.endswith(("_id", "_ids", "_at", "_time", "_hash")):
            encoded = value.encode("utf-8")
            return {"sha256": hashlib.sha256(encoded).hexdigest(), "size": len(encoded)}
    return _redact(value)


def _utc(value: Any, code: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError as exc:
        raise G004ContractError(code) from exc
    if parsed.tzinfo is None:
        raise G004ContractError(code)
    return parsed.astimezone(timezone.utc)


def _exact_keys(value: Any, keys: set[str], code: str) -> Dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise G004ContractError(code)
    return dict(value)


def _identifier(value: Any, code: str, *, optional: bool = False) -> str:
    text = value if isinstance(value, str) else ""
    if optional and text == "":
        return ""
    if not text or text != text.strip() or not _ID_RE.fullmatch(text):
        raise G004ContractError(code)
    return text


def _bounded_int(value: Any, low: int, high: int, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise G004ContractError(code)
    return value


def normalize_request(raw: Mapping[str, Any]) -> Dict[str, Any]:
    request = _exact_keys(raw, _REQUEST_KEYS, "G004_INPUT_SCHEMA_INVALID")
    if request["schema_version"] != REQUEST_VERSION:
        raise G004ContractError("G004_INPUT_SCHEMA_INVALID")
    _identifier(request["audit_id"], "G004_INPUT_SCHEMA_INVALID")
    _identifier(request["request_nonce"], "G004_INPUT_SCHEMA_INVALID")
    _utc(request["requested_at"], "G004_INPUT_SCHEMA_INVALID")
    if request["graph_api_version"] != GRAPH_API_VERSION:
        raise G004ContractError("GRAPH_VERSION_MISMATCH")
    if request["sdk_contract_version"] != SDK_CONTRACT_VERSION:
        raise G004ContractError("G004_INPUT_SCHEMA_INVALID")
    if request["topology_contract_version"] != TOPOLOGY_VERSION:
        raise G004ContractError("G004_INPUT_SCHEMA_INVALID")
    _identifier(request["create_operation_action_id"], "G004_INPUT_SCHEMA_INVALID")
    _identifier(
        request["activation_operation_action_id"], "G004_INPUT_SCHEMA_INVALID", optional=True,
    )
    if not _SHA256_RE.fullmatch(str(request["actor_binding_registry_hash"] or "")):
        raise G004ContractError("G004_INPUT_SCHEMA_INVALID")
    for key in ("required_permissions", "required_account_tasks"):
        values = request[key]
        if (
            not isinstance(values, list)
            or any(not isinstance(item, str) or not item or item != item.strip() for item in values)
            or values != sorted(set(values))
        ):
            raise G004ContractError("G004_INPUT_SCHEMA_INVALID")
    if not MIN_REQUIRED_PERMISSIONS.issubset(set(request["required_permissions"])):
        raise G004ContractError("G004_INPUT_SCHEMA_INVALID")
    if not MIN_REQUIRED_ACCOUNT_TASKS.issubset({
        str(item).upper() for item in request["required_account_tasks"]
    }):
        raise G004ContractError("G004_INPUT_SCHEMA_INVALID")
    policy = _exact_keys(
        request["freshness_policy"], _FRESHNESS_KEYS, "G004_INPUT_SCHEMA_INVALID",
    )
    _bounded_int(policy["max_run_seconds"], 1, 300, "G004_INPUT_SCHEMA_INVALID")
    _bounded_int(policy["receipt_ttl_seconds"], 60, 3600, "G004_INPUT_SCHEMA_INVALID")
    _bounded_int(policy["activity_settlement_seconds"], 60, 86400, "G004_INPUT_SCHEMA_INVALID")
    _bounded_int(policy["clock_skew_seconds"], 0, 300, "G004_INPUT_SCHEMA_INVALID")
    _bounded_int(policy["max_pages"], 1, MAX_GRAPH_PAGES, "G004_INPUT_SCHEMA_INVALID")
    _bounded_int(policy["max_events"], 1, MAX_GRAPH_ITEMS, "G004_INPUT_SCHEMA_INVALID")
    if policy != FROZEN_FRESHNESS_POLICY:
        raise G004ContractError("G004_INPUT_SCHEMA_INVALID")
    return json.loads(canonical_json(request))


def normalize_actor_registry(raw: Mapping[str, Any], expected_hash: str) -> Dict[str, Any]:
    registry = _exact_keys(raw, _ACTOR_REGISTRY_KEYS, "G004_INPUT_SCHEMA_INVALID")
    if registry["schema_version"] != "gle-g0-04-actor-binding-registry-v1":
        raise G004ContractError("G004_INPUT_SCHEMA_INVALID")
    principals = registry["principals"]
    if not isinstance(principals, list) or not principals:
        raise G004ContractError("G004_INPUT_SCHEMA_INVALID")
    normalized = []
    identities = set()
    for raw_actor in principals:
        actor = _exact_keys(raw_actor, _ACTOR_KEYS, "G004_INPUT_SCHEMA_INVALID")
        actor_id = _identifier(actor["actor_id"], "G004_INPUT_SCHEMA_INVALID")
        app_id = _identifier(actor["application_id"], "G004_INPUT_SCHEMA_INVALID")
        roles = actor["roles"]
        if (
            not isinstance(roles, list) or not roles
            or roles != sorted(set(roles))
            or any(not isinstance(item, str) or not item for item in roles)
        ):
            raise G004ContractError("G004_INPUT_SCHEMA_INVALID")
        if (actor_id, app_id) in identities:
            raise G004ContractError("G004_INPUT_SCHEMA_INVALID")
        identities.add((actor_id, app_id))
        normalized.append({"actor_id": actor_id, "application_id": app_id, "roles": roles})
    result = {"schema_version": registry["schema_version"], "principals": sorted(
        normalized, key=lambda item: (item["actor_id"], item["application_id"]),
    )}
    if hash_json(result) != expected_hash:
        raise G004ContractError("REQUEST_HASH_MISMATCH")
    return result


@contextmanager
def open_readonly_snapshot(path: Path) -> Iterator[sqlite3.Connection]:
    resolved = Path(path).resolve()
    if not resolved.is_file() or resolved.stat().st_size > MAX_DB_BYTES:
        raise G004SourceError("G004_SOURCE_UNREADABLE")
    for suffix in ("-wal", "-journal", "-shm"):
        sidecar = Path(f"{resolved}{suffix}")
        if sidecar.exists() and sidecar.stat().st_size:
            raise G004SourceError(f"G004_SOURCE_SIDECAR_PRESENT:{suffix}")
    uri = f"file:{quote(str(resolved), safe='/')}?mode=ro&immutable=1"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
            raise G004SourceError("LOCAL_DB_NOT_QUERY_ONLY")
        yield conn
    except sqlite3.Error as exc:
        raise G004SourceError("G004_SOURCE_SQLITE_ERROR") from exc
    finally:
        if "conn" in locals():
            conn.close()


def _decode_object(value: Any) -> Dict[str, Any]:
    try:
        decoded = json.loads(str(value or "{}"))
    except (TypeError, ValueError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _decode_list(value: Any) -> List[Any]:
    try:
        decoded = json.loads(str(value or "[]"))
    except (TypeError, ValueError):
        return []
    return decoded if isinstance(decoded, list) else []


def _required_columns(conn: sqlite3.Connection, table: str, columns: Iterable[str]) -> None:
    actual = {str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")')}
    missing = set(columns) - actual
    if missing:
        raise G004SourceError(f"G004_SOURCE_SCHEMA_MISSING:{table}:{','.join(sorted(missing))}")


def _required_object_keys_after_step(step: str, plan: Mapping[str, Any]) -> set[str]:
    normalized = str(step or "").upper()
    if normalized in {"CAMPAIGN_CREATE", "CAMPAIGN_STATUS_UPDATE"}:
        return {"campaign_id"}
    match = re.fullmatch(r"(C\d+)_(IMAGE_UPLOAD|CREATIVE_CREATE|ADSET_CREATE|AD_CREATE|ADSET_STATUS_UPDATE|AD_STATUS_UPDATE)", normalized)
    if match:
        prefix = match.group(1).lower()
        suffix = match.group(2)
        field = {
            "IMAGE_UPLOAD": "image_hash", "CREATIVE_CREATE": "creative_id",
            "ADSET_CREATE": "adset_id", "AD_CREATE": "ad_id",
            "ADSET_STATUS_UPDATE": "adset_id", "AD_STATUS_UPDATE": "ad_id",
        }[suffix]
        return {f"{prefix}_{field}"}
    if normalized == "STUDY_CREATE":
        result = {"study_id"}
        for index, raw_cell in enumerate(list(plan.get("cells") or []), start=1):
            key = str(dict(raw_cell or {}).get("cell_key") or f"C{index}").lower()
            result.add(f"{key}_study_cell_id")
        return result
    if normalized in {"ADSET_STATUS_UPDATE", "AD_STATUS_UPDATE"}:
        return {"adset_id" if normalized.startswith("ADSET") else "ad_id"}
    return set()


def _load_action_chain(
    conn: sqlite3.Connection, action_id: str, expected_type: str, *, audit_cutoff: str,
) -> Dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM growth_operation_action WHERE operation_action_id=?", (action_id,),
    ).fetchone()
    if not row:
        raise G004SourceError("G004_LOCAL_ACTION_MISSING")
    action = dict(row)
    payload = _decode_object(action.get("payload_json"))
    plan = dict(payload.get("plan") or {})
    reasons = []
    if (
        str(action.get("action_type") or "").upper() != expected_type
        or str(action.get("status") or "").upper() != "VERIFIED"
        or str(action.get("action_scope") or "").upper() != "EXPERIMENT"
        or str(action.get("target_type") or "").upper()
        != str(plan.get("target_object_type") or "").upper()
        or str(action.get("target_id") or "") != str(plan.get("target_object_id") or "")
    ):
        reasons.append("CREATE_ACTIVATION_NOT_SEPARATE")
    if not plan or str(plan.get("action_type") or "").upper() != expected_type:
        reasons.append("STUDY_PLAN_UNBOUND")
    if expected_type == "CREATE_PAUSED_AD":
        try:
            verify_compiler_receipt(plan)
        except Exception:
            reasons.append("LEGACY_STUDY_INADMISSIBLE")
    approval_rows = conn.execute(
        "SELECT * FROM growth_operation_approval WHERE operation_action_id=? ORDER BY created_at",
        (action_id,),
    ).fetchall()
    approval = dict(approval_rows[-1]) if len(approval_rows) == 1 else {}
    plan_digest = payload_hash(plan)
    if (
        len(approval_rows) != 1
        or approval.get("status") != "APPROVED"
        or str(approval.get("plan_hash") or "") != plan_digest
        or _decode_object(approval.get("plan_json")) != plan
        or not str(approval.get("approved_at") or "")
        or not str(approval.get("approved_by") or "").startswith("operator:")
        or not str(approval.get("consumed_at") or "")
    ):
        reasons.append("APPROVAL_HASH_MISMATCH")
    try:
        approved_at = _utc(approval.get("approved_at"), "APPROVAL_HASH_MISMATCH")
        expires_at = _utc(approval.get("expires_at"), "APPROVAL_HASH_MISMATCH")
        consumed_at = _utc(approval.get("consumed_at"), "APPROVAL_HASH_MISMATCH")
        if not approved_at <= consumed_at <= expires_at:
            raise G004ContractError("APPROVAL_HASH_MISMATCH")
    except G004ContractError:
        reasons.append("APPROVAL_HASH_MISMATCH")
    task_rows = conn.execute(
        "SELECT * FROM meta_execution_task WHERE operation_action_id=? ORDER BY created_at",
        (action_id,),
    ).fetchall()
    task = dict(task_rows[-1]) if len(task_rows) == 1 else {}
    task_payload = _decode_object(task.get("payload_json"))
    if (
        len(task_rows) != 1
        or str(task.get("status") or "") not in {"SUCCESS", "VERIFIED"}
        or payload_hash(dict(task_payload.get("plan") or {})) != plan_digest
        or str(dict(task_payload.get("approval") or {}).get("approval_id") or "")
        != str(approval.get("approval_id") or "")
    ):
        reasons.append("STUDY_PLAN_UNBOUND")
    receipts = [dict(item) for item in conn.execute(
        "SELECT * FROM meta_execution_task_receipt WHERE execution_task_id=? ORDER BY created_at,receipt_id",
        (str(task.get("execution_task_id") or ""),),
    ).fetchall()]
    receipt_steps = {str(item.get("step_name") or "").upper(): item for item in receipts}
    if not receipts or any(
        str(receipt_steps.get(name, {}).get("step_status") or "").upper() not in {"SUCCESS", "VERIFIED"}
        for name in ("VERIFY", "RECEIPT")
    ):
        reasons.append("STUDY_PLAN_UNBOUND")
    dry_rows = conn.execute(
        """SELECT response_json,created_at FROM growth_idempotency_record
        WHERE route_key='ad_experiment.plan_dry_run' AND json_valid(response_json)
          AND json_extract(response_json,'$.plan_id')=? ORDER BY created_at DESC LIMIT 2""",
        (action_id,),
    ).fetchall()
    dry = _decode_object(dry_rows[0]["response_json"]) if len(dry_rows) == 1 else {}
    if (
        len(dry_rows) != 1
        or dry.get("status") != "DRY_RUN_VERIFIED"
        or str(dry.get("execution_mode") or "").lower() != "dry_run"
        or str(dry.get("plan_hash") or "") != plan_digest
        or str(dry.get("approval_id") or "") != str(approval.get("approval_id") or "")
        or str(dry.get("approved_by") or "") != str(approval.get("approved_by") or "")
    ):
        reasons.append("STUDY_PLAN_UNBOUND")
    object_ids = _decode_object(task.get("meta_object_ids_json"))
    expected_steps = list(execution_steps_for(expected_type, task_payload))
    expected_receipt_steps = expected_steps + ["VERIFY", "RECEIPT"]
    if not expected_steps or [str(item.get("step_name") or "").upper() for item in receipts] != expected_receipt_steps:
        reasons.append("STUDY_PLAN_UNBOUND")
    for index, receipt in enumerate(receipts):
        step = str(receipt.get("step_name") or "").upper()
        expected_status = (
            "VERIFIED" if step == "VERIFY" or is_delivery_status_step(step) else "SUCCESS"
        )
        if step == "RECEIPT":
            expected_status = "SUCCESS"
        receipt_ids = _decode_object(receipt.get("meta_object_ids_json"))
        if (
            str(receipt.get("step_status") or "").upper() != expected_status
            or any(str(object_ids.get(key) or "") != str(value or "") for key, value in receipt_ids.items())
            or not _required_object_keys_after_step(step, plan).issubset({
                key for key, value in receipt_ids.items() if str(value or "")
            })
        ):
            reasons.append("STUDY_PLAN_UNBOUND")
        result = _decode_object(receipt.get("step_result_json"))
        verification = _decode_object(receipt.get("verification_result_json"))
        if step in expected_steps and str(result.get("status") or "").upper() != "SUCCESS":
            reasons.append("STUDY_PLAN_UNBOUND")
        if (step == "VERIFY" or is_delivery_status_step(step)) and str(
            verification.get("status") or "",
        ).upper() != "SUCCESS":
            reasons.append("STUDY_PLAN_UNBOUND")
        if step == "VERIFY" and receipt_ids != object_ids:
            reasons.append("STUDY_PLAN_UNBOUND")
        if step == "RECEIPT" and (
            str(result.get("final_status") or "").upper() != "SUCCESS"
            or str(verification.get("status") or "").upper() != "SUCCESS"
            or receipt_ids != object_ids
        ):
            reasons.append("STUDY_PLAN_UNBOUND")
        if index and str(receipts[index - 1].get("created_at") or "") > str(receipt.get("created_at") or ""):
            reasons.append("STUDY_PLAN_UNBOUND")
    try:
        timeline = [
            _utc(action.get("created_at"), "STUDY_PLAN_UNBOUND"),
            _utc(approval.get("created_at"), "STUDY_PLAN_UNBOUND"),
            _utc(approval.get("approved_at"), "STUDY_PLAN_UNBOUND"),
            _utc(dry_rows[0]["created_at"] if len(dry_rows) == 1 else "", "STUDY_PLAN_UNBOUND"),
            _utc(approval.get("consumed_at"), "STUDY_PLAN_UNBOUND"),
            _utc(task.get("created_at"), "STUDY_PLAN_UNBOUND"),
        ] + [_utc(item.get("created_at"), "STUDY_PLAN_UNBOUND") for item in receipts] + [
            _utc(audit_cutoff, "STUDY_PLAN_UNBOUND"),
        ]
        if timeline != sorted(timeline):
            raise G004ContractError("STUDY_PLAN_UNBOUND")
    except G004ContractError:
        reasons.append("STUDY_PLAN_UNBOUND")
    return {
        "action": action, "payload": payload, "plan": plan, "approval": approval,
        "task": task, "receipts": receipts, "dry_run": dry, "object_ids": object_ids,
        "expected_steps": expected_steps,
        "status": "PASS" if not reasons else "FAIL", "reason_codes": sorted(set(reasons)),
        "binding_hash": hash_json({
            "action_id": action_id, "plan_hash": plan_digest,
            "approval_id": approval.get("approval_id"), "task_id": task.get("execution_task_id"),
            "receipt_ids": [item.get("receipt_id") for item in receipts],
        }),
    }


def load_local_evidence(conn: sqlite3.Connection, request: Mapping[str, Any]) -> Dict[str, Any]:
    required = {
        "growth_operation_action": {
            "operation_action_id", "action_type", "action_scope", "target_type", "target_id",
            "payload_json", "status", "created_at",
        },
        "growth_operation_approval": {
            "approval_id", "operation_action_id", "plan_hash", "plan_json", "status",
            "approved_at", "expires_at", "consumed_at", "created_at",
        },
        "meta_execution_task": {"execution_task_id", "operation_action_id", "status", "payload_json", "meta_object_ids_json"},
        "meta_execution_task_receipt": {"receipt_id", "execution_task_id", "step_name", "step_status"},
        "growth_idempotency_record": {"route_key", "response_json", "created_at"},
        "ad_audience_preflight": {
            "preflight_id", "launch_id", "account_id", "business_id", "country",
            "strategy_keys_json", "evidence_json", "evidence_hash", "status",
            "checked_at", "expires_at",
        },
    }
    for table, columns in required.items():
        _required_columns(conn, table, columns)
    create = _load_action_chain(
        conn, request["create_operation_action_id"], "CREATE_PAUSED_AD",
        audit_cutoff=request["requested_at"],
    )
    activation_id = str(request.get("activation_operation_action_id") or "")
    activation = _load_action_chain(
        conn, activation_id, "REACTIVATE_AD", audit_cutoff=request["requested_at"],
    ) if activation_id else {}
    plan = dict(create.get("plan") or {})
    embedded_preflight = dict(plan.get("audience_preflight") or {})
    preflight_row = conn.execute(
        "SELECT * FROM ad_audience_preflight WHERE preflight_id=?",
        (str(embedded_preflight.get("preflight_id") or ""),),
    ).fetchone()
    preflight = dict(preflight_row or {})
    preflight_evidence = _decode_object(preflight.get("evidence_json"))
    preflight_reasons = []
    if (
        not preflight
        or preflight.get("status") != "VERIFIED"
        or preflight_evidence != embedded_preflight
        or str(preflight.get("evidence_hash") or "") != payload_hash(embedded_preflight)
        or str(preflight.get("launch_id") or "") != str(embedded_preflight.get("launch_id") or "")
        or str(preflight.get("account_id") or "").removeprefix("act_")
        != str(embedded_preflight.get("account_id") or "").removeprefix("act_")
        or str(preflight.get("business_id") or "") != str(embedded_preflight.get("business_id") or "")
        or str(preflight.get("country") or "").upper()
        != str(embedded_preflight.get("country") or "").upper()
        or str(preflight.get("checked_at") or "") != str(embedded_preflight.get("checked_at") or "")
        or str(preflight.get("expires_at") or "") != str(embedded_preflight.get("expires_at") or "")
        or _decode_list(preflight.get("strategy_keys_json"))
        != embedded_preflight.get("strategy_keys")
    ):
        preflight_reasons.append("EVIDENCE_HASH_MISMATCH")
    try:
        requested_at = _utc(request["requested_at"], "EVIDENCE_HASH_MISMATCH")
        if _utc(preflight.get("checked_at"), "EVIDENCE_HASH_MISMATCH") > requested_at:
            preflight_reasons.append("EVIDENCE_HASH_MISMATCH")
        if _utc(preflight.get("expires_at"), "RECEIPT_EXPIRED") <= requested_at + timedelta(
            seconds=request["freshness_policy"]["receipt_ttl_seconds"],
        ):
            preflight_reasons.append("RECEIPT_EXPIRED")
    except G004ContractError as exc:
        preflight_reasons.append(str(exc))
    if preflight_reasons:
        create["status"] = "FAIL"
        create["reason_codes"] = sorted(set(create["reason_codes"] + preflight_reasons))
    cells = list(plan.get("cells") or [])
    object_ids = dict(create.get("object_ids") or {})
    topology_cells = []
    for index, raw_cell in enumerate(cells, start=1):
        cell = dict(raw_cell or {})
        key = str(cell.get("cell_key") or f"C{index}").upper()
        prefix = key.lower()
        creative = dict(dict(cell.get("steps") or {}).get("CREATIVE_CREATE") or {})
        link = dict(dict(creative.get("object_story_spec") or {}).get("link_data") or {})
        adset = dict(dict(cell.get("steps") or {}).get("ADSET_CREATE") or {})
        topology_cells.append({
            "cell_key": key,
            "role": str(cell.get("role") or ""),
            "study_cell_id": str(object_ids.get(f"{prefix}_study_cell_id") or ""),
            "adset_id": str(object_ids.get(f"{prefix}_adset_id") or ""),
            "ad_id": str(object_ids.get(f"{prefix}_ad_id") or ""),
            "creative_id": str(object_ids.get(f"{prefix}_creative_id") or ""),
            "image_hash": str(object_ids.get(f"{prefix}_image_hash") or ""),
            "target_allocation": int(cell.get("allocation_percent") or 0),
            "page_id": str(dict(creative.get("object_story_spec") or {}).get("page_id") or ""),
            "application_id": str(dict(adset.get("promoted_object") or {}).get("application_id") or ""),
            "expected_creative": {
                "message": str(link.get("message") or ""),
                "headline": str(link.get("name") or ""),
                "description": str(link.get("description") or ""),
                "cta": str(dict(link.get("call_to_action") or {}).get("type") or ""),
            },
        })
    target = {
        "ad_account_id": str(plan.get("target_account_id") or "").removeprefix("act_"),
        "business_id": str(dict(plan.get("study") or {}).get("business_id") or ""),
        "page_id": next(iter({item["page_id"] for item in topology_cells if item["page_id"]}), ""),
        "application_id": next(iter({item["application_id"] for item in topology_cells if item["application_id"]}), ""),
        "study_id": str(object_ids.get("study_id") or ""),
        "campaign_id": str(object_ids.get("campaign_id") or ""),
        "market": str(dict(dict(plan.get("invariants") or {}).get("base_conditions") or {}).get("country") or "").upper(),
    }
    if not all(target.values()) or len(topology_cells) != 2 or any(
        not all(str(item.get(key) or "") for key in ("study_cell_id", "adset_id", "ad_id", "creative_id"))
        for item in topology_cells
    ):
        create["status"] = "FAIL"
        create["reason_codes"] = sorted(set(create["reason_codes"] + ["STUDY_PLAN_UNBOUND"]))
    activity_window_start = str(create.get("action", {}).get("created_at") or "")
    try:
        if _utc(activity_window_start, "STUDY_PLAN_UNBOUND") > _utc(
            request["requested_at"], "STUDY_PLAN_UNBOUND",
        ):
            raise G004ContractError("STUDY_PLAN_UNBOUND")
    except G004ContractError:
        create["status"] = "FAIL"
        create["reason_codes"] = sorted(set(create["reason_codes"] + ["STUDY_PLAN_UNBOUND"]))
    return {
        "create": create, "activation": activation, "target": target,
        "cells": topology_cells, "preflight": {
            "status": "PASS" if not preflight_reasons else "FAIL",
            "reason_codes": preflight_reasons,
            "preflight_id": preflight.get("preflight_id"),
            "evidence_hash": preflight.get("evidence_hash"),
        },
        "activity_window_start": activity_window_start,
        "local_evidence_hash": hash_json({
            "create": create.get("binding_hash"),
            "activation": activation.get("binding_hash"),
            "preflight_hash": preflight.get("evidence_hash"),
            "target": target, "cells": topology_cells,
        }),
    }


@dataclass
class GraphJournalEntry:
    endpoint: str
    fields: str
    page: int
    http_status: int
    response_hash: str
    response_size: int
    observed_at: str


class GetOnlyGraphClient:
    """Exact GET-only transport.  It has no mutation method."""

    def __init__(
        self, *, session: Any, access_token: str, now: datetime,
        allowed_paths: Iterable[str], api_version: str = GRAPH_API_VERSION,
        max_pages: int = 10, max_items: int = 1000,
    ) -> None:
        if api_version != GRAPH_API_VERSION or not session or not str(access_token or ""):
            raise G004ContractError("GRAPH_VERSION_MISMATCH")
        self.session = session
        self.access_token = str(access_token)
        self.now = now.astimezone(timezone.utc)
        self.max_pages = max_pages
        self.max_items = max_items
        self.allowed_paths = {self._safe_path(path) for path in allowed_paths}
        if not self.allowed_paths:
            raise G004ContractError("G004_INPUT_SCHEMA_INVALID")
        self.journal: List[GraphJournalEntry] = []

    @staticmethod
    def _safe_path(path: str) -> str:
        candidate = str(path or "").strip().strip("/")
        if not candidate or "?" in candidate or "#" in candidate or ".." in candidate:
            raise G004GraphError("ENDPOINT_NOT_ALLOWLISTED")
        if not re.fullmatch(r"[A-Za-z0-9._:-]+(?:/[A-Za-z0-9._:-]+)?", candidate):
            raise G004GraphError("ENDPOINT_NOT_ALLOWLISTED")
        return candidate

    def get(self, path: str, *, fields: str, params: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        safe_path = self._safe_path(path)
        if safe_path not in self.allowed_paths:
            raise G004GraphError("ENDPOINT_NOT_ALLOWLISTED")
        query = {"access_token": self.access_token, "fields": fields}
        query.update(dict(params or {}))
        response = self.session.get(
            f"https://graph.facebook.com/{GRAPH_API_VERSION}/{safe_path}",
            params=query, timeout=25, allow_redirects=False,
        )
        return self._decode(response, safe_path, fields, 1)

    def get_edge(
        self, path: str, *, fields: str, params: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        safe_path = self._safe_path(path)
        if safe_path not in self.allowed_paths:
            raise G004GraphError("ENDPOINT_NOT_ALLOWLISTED")
        after = ""
        seen = set()
        data: List[Any] = []
        for page in range(1, self.max_pages + 1):
            query = {"access_token": self.access_token, "fields": fields, "limit": min(100, self.max_items)}
            query.update(dict(params or {}))
            if after:
                query["after"] = after
            response = self.session.get(
                f"https://graph.facebook.com/{GRAPH_API_VERSION}/{safe_path}",
                params=query, timeout=25, allow_redirects=False,
            )
            body = self._decode(response, safe_path, fields, page)
            rows = body.get("data")
            if not isinstance(rows, list):
                raise G004GraphError("GRAPH_READ_FAILED")
            data.extend(rows)
            if len(data) > self.max_items:
                raise G004GraphError("PAGINATION_INCOMPLETE")
            paging = dict(body.get("paging") or {})
            if not paging.get("next"):
                return {"data": data, "pagination_complete": True, "page_count": page}
            next_after = str(dict(paging.get("cursors") or {}).get("after") or "")
            if not next_after or len(next_after) > 2048:
                raise G004GraphError("PAGINATION_INCOMPLETE")
            if next_after in seen:
                raise G004GraphError("CURSOR_LOOP")
            seen.add(next_after)
            after = next_after
        raise G004GraphError("PAGINATION_INCOMPLETE")

    def _decode(self, response: Any, path: str, fields: str, page: int) -> Dict[str, Any]:
        if getattr(response, "history", None) or 300 <= int(getattr(response, "status_code", 0) or 0) < 400:
            raise G004GraphError("REDIRECT_FORBIDDEN")
        headers = getattr(response, "headers", {}) or {}
        try:
            content_length = int(headers.get("Content-Length", 0) or 0)
        except (TypeError, ValueError):
            raise G004GraphError("GRAPH_READ_FAILED")
        if content_length > MAX_GRAPH_RESPONSE_BYTES:
            raise G004GraphError("GRAPH_READ_FAILED")
        try:
            body = response.json()
        except Exception as exc:
            raise G004GraphError("GRAPH_READ_FAILED") from exc
        redacted = _redact(body)
        serialized = canonical_json(redacted)
        if len(serialized.encode("utf-8")) > MAX_GRAPH_RESPONSE_BYTES:
            raise G004GraphError("GRAPH_READ_FAILED")
        self.journal.append(GraphJournalEntry(
            endpoint=f"/{GRAPH_API_VERSION}/{path}", fields=fields, page=page,
            http_status=int(getattr(response, "status_code", 0) or 0),
            response_hash=hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
            response_size=len(serialized.encode("utf-8")), observed_at=self.now.isoformat(),
        ))
        if not isinstance(body, dict) or body.get("error") or int(getattr(response, "status_code", 0) or 0) >= 400:
            raise G004GraphError("GRAPH_READ_FAILED")
        return dict(body)

    def proof(self) -> Dict[str, Any]:
        rows = [entry.__dict__ for entry in self.journal]
        return {
            "allowed_methods": ["GET"], "get_count": len(rows),
            "post_count": 0, "put_count": 0, "patch_count": 0, "delete_count": 0,
            "redirect_count": 0, "batch_count": 0, "async_job_count": 0,
            "meta_object_writes": 0, "request_journal_hash": hash_json(rows),
        }


def _graph_call(
    client: GetOnlyGraphClient, store: Dict[str, Any], key: str, *, edge: bool,
    path: str, fields: str, params: Optional[Mapping[str, Any]] = None,
) -> None:
    try:
        store[key] = (client.get_edge if edge else client.get)(path, fields=fields, params=params)
    except G004GraphError as exc:
        store[key] = {"error": str(exc), "pagination_complete": False}


def collect_graph_evidence(
    client: GetOnlyGraphClient, local: Mapping[str, Any], request: Mapping[str, Any],
) -> Dict[str, Any]:
    target = dict(local["target"])
    cells = list(local["cells"])
    evidence: Dict[str, Any] = {}
    _graph_call(client, evidence, "debug_token", edge=False, path="debug_token", fields="", params={"input_token": client.access_token})
    _graph_call(client, evidence, "me", edge=False, path="me", fields="id,name")
    _graph_call(client, evidence, "permissions", edge=True, path="me/permissions", fields="permission,status")
    account = f"act_{target['ad_account_id']}"
    _graph_call(client, evidence, "account", edge=False, path=account, fields="id,account_id,name,account_status,business,user_tasks,capabilities,ad_account_promotable_objects,updated_time")
    _graph_call(client, evidence, "account_assigned_users", edge=True, path=f"{account}/assigned_users", fields="id,name,tasks", params={"business": target["business_id"]})
    business = target["business_id"]
    _graph_call(client, evidence, "business", edge=False, path=business, fields="id,name,created_time,updated_time")
    for edge_name in ("owned_ad_accounts", "client_ad_accounts", "owned_pages", "client_pages", "owned_apps", "client_apps", "system_users"):
        _graph_call(client, evidence, f"business_{edge_name}", edge=True, path=f"{business}/{edge_name}", fields="id,name")
    _graph_call(client, evidence, "page", edge=False, path=target["page_id"], fields="id,name,is_published,verification_status")
    _graph_call(client, evidence, "page_assigned_users", edge=True, path=f"{target['page_id']}/assigned_users", fields="id,name,tasks", params={"business": business})
    _graph_call(client, evidence, "app", edge=False, path=target["application_id"], fields="id,name,app_domains,link")
    _graph_call(client, evidence, "app_roles", edge=True, path=f"{target['application_id']}/roles", fields="user,role")
    _graph_call(client, evidence, "study", edge=False, path=target["study_id"], fields=COPY_ONLY_READ_FIELDS["study"] + ",created_time,updated_time")
    _graph_call(client, evidence, "study_cells", edge=True, path=f"{target['study_id']}/cells", fields=COPY_ONLY_READ_FIELDS["study_cells"] + ",ad_entities_count,ad_ids")
    _graph_call(client, evidence, "study_objectives", edge=True, path=f"{target['study_id']}/objectives", fields="id,name,type")
    for cell in cells:
        cell_id = cell["study_cell_id"]
        for edge_name in ("adsets", "campaigns", "adaccounts"):
            _graph_call(client, evidence, f"cell_{cell['cell_key']}_{edge_name}", edge=True, path=f"{cell_id}/{edge_name}", fields="id,name,account_id,campaign_id,status,effective_status")
    object_specs = [("campaign", target["campaign_id"], COPY_ONLY_READ_FIELDS["campaign"] + ",account_id,updated_time")]
    for cell in cells:
        object_specs.extend([
            (f"adset_{cell['cell_key']}", cell["adset_id"], COPY_ONLY_READ_FIELDS["adset"] + ",account_id,updated_time"),
            (f"ad_{cell['cell_key']}", cell["ad_id"], COPY_ONLY_READ_FIELDS["ad"] + ",account_id,updated_time"),
            (f"creative_{cell['cell_key']}", cell["creative_id"], COPY_ONLY_READ_FIELDS["creative"] + ",account_id,updated_time"),
        ])
    for prefix in ("first", "last"):
        for key, object_id, fields in object_specs:
            _graph_call(client, evidence, f"{prefix}_{key}", edge=False, path=object_id, fields=fields)
    _graph_call(
        client, evidence, "activities", edge=True, path=f"{account}/activities",
        fields="id,event_time,date_time_in_timezone,event_type,object_id,object_type,changed_data,extra_data,actor_id,actor_name,application_id,application_name",
        params={"since": local["activity_window_start"], "until": request["requested_at"]},
    )
    evidence["evidence_hash"] = hash_json(_redact(evidence))
    return evidence


def allowed_graph_paths(local: Mapping[str, Any]) -> set[str]:
    target = dict(local["target"])
    cells = list(local["cells"])
    account = f"act_{target['ad_account_id']}"
    paths = {
        "debug_token", "me", "me/permissions", account, f"{account}/assigned_users",
        f"{account}/activities", target["business_id"], target["page_id"],
        f"{target['page_id']}/assigned_users", target["application_id"],
        f"{target['application_id']}/roles", target["study_id"],
        f"{target['study_id']}/cells", f"{target['study_id']}/objectives",
        target["campaign_id"],
    }
    for edge in (
        "owned_ad_accounts", "client_ad_accounts", "owned_pages", "client_pages",
        "owned_apps", "client_apps", "system_users",
    ):
        paths.add(f"{target['business_id']}/{edge}")
    for cell in cells:
        paths.update({
            cell["adset_id"], cell["ad_id"], cell["creative_id"],
            f"{cell['study_cell_id']}/adsets", f"{cell['study_cell_id']}/campaigns",
            f"{cell['study_cell_id']}/adaccounts",
        })
    return paths


def _ids(rows: Any) -> set[str]:
    return {str(item.get("id") or "") for item in list(dict(rows or {}).get("data") or []) if isinstance(item, dict)}


def _role_user_ids(rows: Any) -> set[str]:
    result = set()
    for item in list(dict(rows or {}).get("data") or []):
        if not isinstance(item, dict):
            continue
        value = item.get("user")
        if isinstance(value, dict):
            value = value.get("id")
        if value:
            result.add(str(value))
    return result


def _role_for_user(rows: Any, principal_id: str) -> set[str]:
    result = set()
    for item in list(dict(rows or {}).get("data") or []):
        if not isinstance(item, dict):
            continue
        value = item.get("user")
        user_id = str(value.get("id") or "") if isinstance(value, dict) else str(value or "")
        if user_id == principal_id:
            result.add(str(item.get("role") or "").upper())
    return result


def _contains_application_id(value: Any, expected: str) -> bool:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).lower() in {"application_id", "app_id", "id", "object_id"} and str(item) == expected:
                return True
            if _contains_application_id(item, expected):
                return True
    elif isinstance(value, (list, tuple)):
        return any(_contains_application_id(item, expected) for item in value)
    return False


def _check(status: str, reasons: Iterable[str], refs: Iterable[str]) -> Dict[str, Any]:
    if status not in _ALLOWED_STATUSES:
        raise G004ContractError("G004_INPUT_SCHEMA_INVALID")
    return {"status": status, "reason_codes": sorted(set(reasons)), "evidence_refs": sorted(set(refs))}


def _permission_check(request: Mapping[str, Any], local: Mapping[str, Any], graph: Mapping[str, Any]) -> Dict[str, Any]:
    reasons = []
    debug = dict(dict(graph.get("debug_token") or {}).get("data") or {})
    principal = dict(graph.get("me") or {})
    if (
        debug.get("is_valid") is not True
        or not str(debug.get("app_id") or "")
        or not str(debug.get("user_id") or "")
        or str(debug.get("user_id") or "") != str(principal.get("id") or "")
        or str(debug.get("type") or "").upper() not in {"USER", "SYSTEM_USER"}
    ):
        reasons.append("TOKEN_PRINCIPAL_MISMATCH")
    for key in ("expires_at", "data_access_expires_at"):
        expiry = debug.get(key)
        if expiry not in (None, 0, "0", ""):
            try:
                if datetime.fromtimestamp(int(expiry), tz=timezone.utc) <= _utc(
                    request["requested_at"], "G004_INPUT_SCHEMA_INVALID",
                ) + timedelta(seconds=request["freshness_policy"]["receipt_ttl_seconds"]):
                    reasons.append("TOKEN_EXPIRED")
            except (TypeError, ValueError, OSError):
                reasons.append("TOKEN_EXPIRED")
    scopes = {
        str(item.get("permission") or "")
        for item in list(dict(graph.get("permissions") or {}).get("data") or [])
        if str(item.get("status") or "").lower() == "granted"
    }
    if not set(request["required_permissions"]).issubset(scopes):
        reasons.append("TOKEN_SCOPE_MISSING")
    debug_scopes = {str(item) for item in list(debug.get("scopes") or [])}
    if not set(request["required_permissions"]).issubset(debug_scopes):
        reasons.append("TOKEN_SCOPE_MISSING")
    account = dict(graph.get("account") or {})
    tasks = {str(item).upper() for item in list(account.get("user_tasks") or [])}
    if int(account.get("account_status") or 0) != 1:
        reasons.append("BUSINESS_ACCESS_MISSING")
    if not set(request["required_account_tasks"]).issubset(tasks):
        reasons.append("ACCOUNT_TASK_MISSING")
    target = local["target"]
    if str(account.get("account_id") or account.get("id") or "").removeprefix("act_") != target["ad_account_id"]:
        reasons.append("PRINCIPAL_MISMATCH")
    if any(dict(graph.get(key) or {}).get("error") for key in ("debug_token", "me", "permissions", "account")):
        reasons.append("GRAPH_READ_FAILED")
    return _check("PASS" if not reasons else "INCOMPLETE", reasons, ["debug_token", "me", "permissions", "account"])


def _assigned_tasks(rows: Any, principal_id: str) -> set[str]:
    for item in list(dict(rows or {}).get("data") or []):
        if isinstance(item, dict) and str(item.get("id") or "") == principal_id:
            return {str(value).upper() for value in list(item.get("tasks") or [])}
    return set()


def _ownership_check(
    request: Mapping[str, Any], local: Mapping[str, Any], graph: Mapping[str, Any],
    registry: Mapping[str, Any],
) -> Dict[str, Any]:
    target = local["target"]
    reasons = []
    account_edges = _ids(graph.get("business_owned_ad_accounts")) | _ids(graph.get("business_client_ad_accounts"))
    page_edges = _ids(graph.get("business_owned_pages")) | _ids(graph.get("business_client_pages"))
    app_edges = _ids(graph.get("business_owned_apps")) | _ids(graph.get("business_client_apps"))
    if target["ad_account_id"] not in {item.removeprefix("act_") for item in account_edges}:
        reasons.append("ACCOUNT_OWNERSHIP_MISSING")
    if target["page_id"] not in page_edges:
        reasons.append("PAGE_OWNERSHIP_MISSING")
    if target["application_id"] not in app_edges:
        reasons.append("APP_OWNERSHIP_MISSING")
    account_business = str(dict(dict(graph.get("account") or {}).get("business") or {}).get("id") or "")
    if account_business != target["business_id"]:
        reasons.append("ACCOUNT_OWNERSHIP_MISSING")
    page = dict(graph.get("page") or {})
    if str(page.get("id") or "") != target["page_id"] or page.get("is_published") is not True:
        reasons.append("PAGE_TASK_MISSING")
    if str(dict(graph.get("app") or {}).get("id") or "") != target["application_id"]:
        reasons.append("APP_OWNERSHIP_MISSING")
    principal_id = str(dict(graph.get("me") or {}).get("id") or "")
    executor_app_id = str(dict(dict(graph.get("debug_token") or {}).get("data") or {}).get("app_id") or "")
    registry_identities = {
        (str(item.get("actor_id") or ""), str(item.get("application_id") or ""))
        for item in list(registry.get("principals") or []) if isinstance(item, dict)
    }
    assigned_account = _assigned_tasks(graph.get("account_assigned_users"), principal_id)
    assigned_page = _assigned_tasks(graph.get("page_assigned_users"), principal_id)
    app_users = _role_user_ids(graph.get("app_roles"))
    app_roles = _role_for_user(graph.get("app_roles"), principal_id)
    if (
        not principal_id
        or not executor_app_id
        or (principal_id, executor_app_id) not in registry_identities
        or not {str(value).upper() for value in request["required_account_tasks"]}.issubset(assigned_account)
        or not MIN_REQUIRED_PAGE_TASKS.issubset(assigned_page)
        or principal_id not in app_users
        or not ALLOWED_APP_ROLES.intersection(app_roles)
    ):
        reasons.append("ACTOR_PROVENANCE_UNRESOLVED")
    if (
        str(dict(dict(graph.get("debug_token") or {}).get("data") or {}).get("type") or "").upper()
        == "SYSTEM_USER"
        and principal_id not in _ids(graph.get("business_system_users"))
    ):
        reasons.append("ACTOR_PROVENANCE_UNRESOLVED")
    return _check("PASS" if not reasons else "INCOMPLETE", reasons, [
        "business_owned_ad_accounts", "business_client_ad_accounts", "business_owned_pages",
        "business_client_pages", "business_owned_apps", "business_client_apps",
        "account_assigned_users", "page_assigned_users", "app_roles",
    ])


def _graph_completeness_check(graph: Mapping[str, Any]) -> Dict[str, Any]:
    reasons = []
    edge_keys = {
        "permissions", "account_assigned_users", "page_assigned_users", "app_roles",
        "study_cells", "study_objectives", "activities",
    }
    edge_keys.update(key for key in graph if key.startswith("business_") or key.startswith("cell_"))
    for key, raw in graph.items():
        if key == "evidence_hash":
            continue
        body = dict(raw or {}) if isinstance(raw, dict) else {}
        if not body or body.get("error"):
            reasons.append("GRAPH_READ_FAILED")
        if key in edge_keys and body.get("pagination_complete") is not True:
            reasons.append("PAGINATION_INCOMPLETE")
    return _check(
        "PASS" if not reasons else "INCOMPLETE", reasons,
        [key for key in graph if key != "evidence_hash"],
    )


def _capability_semantics_check(local: Mapping[str, Any], graph: Mapping[str, Any]) -> Dict[str, Any]:
    reasons = []
    target = dict(local["target"])
    account = dict(graph.get("account") or {})
    promotable = account.get("ad_account_promotable_objects")
    if not _contains_application_id(promotable, target["application_id"]):
        reasons.append("PROMOTED_OBJECT_MISMATCH")
    objectives = list(dict(graph.get("study_objectives") or {}).get("data") or [])
    if (
        len(objectives) != 1
        or not str(dict(objectives[0] or {}).get("id") or "")
        or str(dict(objectives[0] or {}).get("type") or "").upper()
        not in ALLOWED_STUDY_OBJECTIVE_TYPES
    ):
        reasons.append("STUDY_PLAN_UNBOUND")
    return _check(
        "PASS" if not reasons else "FAIL", reasons,
        ["account.ad_account_promotable_objects", "study_objectives"],
    )


def _topology_check(local: Mapping[str, Any], graph: Mapping[str, Any]) -> Dict[str, Any]:
    reasons = []
    target = local["target"]
    cells = local["cells"]
    study = dict(graph.get("study") or {})
    if str(study.get("id") or "") != target["study_id"] or str(study.get("type") or "").upper() != "SPLIT_TEST":
        reasons.append("STUDY_NOT_SPLIT_TEST")
    graph_cells = {str(item.get("id") or ""): dict(item) for item in list(dict(graph.get("study_cells") or {}).get("data") or [])}
    if set(graph_cells) != {item["study_cell_id"] for item in cells}:
        reasons.append("CELL_SET_MISMATCH")
    identity_fields = ("study_cell_id", "adset_id", "ad_id", "creative_id")
    if (
        len(cells) != 2
        or any(int(item["target_allocation"]) != 50 for item in cells)
        or any(len({str(item[field]) for item in cells}) != 2 for field in identity_fields)
    ):
        reasons.append("CELL_SET_MISMATCH")
    for cell in cells:
        observed = graph_cells.get(cell["study_cell_id"], {})
        if int(observed.get("treatment_percentage") or 0) != cell["target_allocation"]:
            reasons.append("CELL_OBJECT_BINDING_MISMATCH")
        if (
            int(observed.get("control_percentage") or 0) != 0
            or int(observed.get("ad_entities_count") or 0) != 1
            or {str(value) for value in list(observed.get("ad_ids") or [])} != {cell["ad_id"]}
        ):
            reasons.append("CELL_OBJECT_BINDING_MISMATCH")
        if _ids(graph.get(f"cell_{cell['cell_key']}_adsets")) != {cell["adset_id"]}:
            reasons.append("CELL_OBJECT_BINDING_MISMATCH")
        if _ids(graph.get(f"cell_{cell['cell_key']}_campaigns")) != {target["campaign_id"]}:
            reasons.append("CELL_OBJECT_BINDING_MISMATCH")
        if target["ad_account_id"] not in {
            value.removeprefix("act_")
            for value in _ids(graph.get(f"cell_{cell['cell_key']}_adaccounts"))
        }:
            reasons.append("CELL_OBJECT_BINDING_MISMATCH")
        account = target["ad_account_id"]
        campaign = dict(graph.get("first_campaign") or {})
        adset = dict(graph.get(f"first_adset_{cell['cell_key']}") or {})
        ad = dict(graph.get(f"first_ad_{cell['cell_key']}") or {})
        creative = dict(graph.get(f"first_creative_{cell['cell_key']}") or {})
        if (
            str(campaign.get("account_id") or "").removeprefix("act_") != account
            or str(adset.get("account_id") or "").removeprefix("act_") != account
            or str(ad.get("account_id") or "").removeprefix("act_") != account
            or str(creative.get("account_id") or "").removeprefix("act_") != account
        ):
            reasons.append("OBJECT_ACCOUNT_MISMATCH")
        if (
            str(campaign.get("id") or "") != target["campaign_id"]
            or str(adset.get("id") or "") != cell["adset_id"]
            or str(ad.get("id") or "") != cell["ad_id"]
            or str(creative.get("id") or "") != cell["creative_id"]
            or str(adset.get("campaign_id") or "") != target["campaign_id"]
            or str(ad.get("campaign_id") or "") != target["campaign_id"]
            or str(ad.get("adset_id") or "") != cell["adset_id"]
            or str(dict(ad.get("creative") or {}).get("id") or "") != cell["creative_id"]
        ):
            reasons.append("CELL_OBJECT_BINDING_MISMATCH")
        story = dict(creative.get("object_story_spec") or {})
        link = dict(story.get("link_data") or {})
        if str(story.get("page_id") or "") != target["page_id"]:
            reasons.append("CREATIVE_PAGE_MISMATCH")
        promoted = dict(adset.get("promoted_object") or {})
        if str(promoted.get("application_id") or "") != target["application_id"]:
            reasons.append("PROMOTED_OBJECT_MISMATCH")
        actual_creative = {
            "message": str(link.get("message") or ""), "headline": str(link.get("name") or ""),
            "description": str(link.get("description") or ""),
            "cta": str(dict(link.get("call_to_action") or {}).get("type") or ""),
        }
        if actual_creative != cell["expected_creative"]:
            reasons.append("LEGACY_STUDY_INADMISSIBLE")
    object_lookup = {target["campaign_id"]: dict(graph.get("first_campaign") or {})}
    for cell in cells:
        object_lookup.update({
            cell["adset_id"]: dict(graph.get(f"first_adset_{cell['cell_key']}") or {}),
            cell["ad_id"]: dict(graph.get(f"first_ad_{cell['cell_key']}") or {}),
            cell["creative_id"]: dict(graph.get(f"first_creative_{cell['cell_key']}") or {}),
        })
    object_lookup[target["study_id"]] = dict(graph.get("study") or {})
    object_lookup[f"{target['study_id']}/cells"] = dict(graph.get("study_cells") or {})
    # Creation readback proves frozen non-status fields. Current delivery status is
    # independently governed by activation_provenance and must not masquerade as
    # a creation-field mismatch after an approved activation.
    for object_id in [target["campaign_id"]] + [
        value for cell in cells for value in (cell["adset_id"], cell["ad_id"])
    ]:
        object_lookup[object_id]["status"] = "PAUSED"
    strict_readback = MetaCopyOnlyReadback(
        get_json=lambda object_id, _fields: deepcopy(object_lookup.get(object_id, {})),
    ).verify(plan=dict(local["create"].get("plan") or {}), object_ids=dict(local["create"].get("object_ids") or {}))
    if strict_readback.get("status") != "SUCCESS":
        reasons.append("LEGACY_STUDY_INADMISSIBLE")
    for key, value in graph.items():
        if key.startswith("first_"):
            last = graph.get("last_" + key.removeprefix("first_"))
            if isinstance(value, dict) and isinstance(last, dict) and hash_json(_redact(value)) != hash_json(_redact(last)):
                reasons.append("OBJECT_DRIFT_DURING_AUDIT")
    return _check("PASS" if not reasons else "FAIL", reasons, ["study", "study_cells", "cell_edges", "object_double_read"])


def _status_transition(activity: Mapping[str, Any]) -> Tuple[str, str, bool]:
    transitions: List[Tuple[str, str]] = []

    def visit(value: Any, *, depth: int = 0) -> None:
        if depth > 3:
            return
        if isinstance(value, str):
            try:
                decoded = json.loads(value)
            except ValueError:
                return
            visit(decoded, depth=depth + 1)
            return
        if isinstance(value, list):
            for item in value:
                visit(item, depth=depth + 1)
            return
        if not isinstance(value, dict):
            return
        field = str(value.get("field") or value.get("name") or value.get("attribute") or "").lower()
        before = value.get("old_value", value.get("from"))
        after = value.get("new_value", value.get("to"))
        if before not in (None, "") and after not in (None, "") and field in {"", "status", "effective_status"}:
            transitions.append((str(before).upper(), str(after).upper()))
        for key in ("status", "changes", "changed_fields", "data"):
            nested = value.get(key)
            if isinstance(nested, (dict, list, str)):
                visit(nested, depth=depth + 1)

    for key in ("changed_data", "extra_data"):
        visit(activity.get(key))
    unique = sorted(set(transitions))
    if len(unique) != 1:
        return "", "", False
    before, after = unique[0]
    if before not in {"ACTIVE", "PAUSED"} or after not in {"ACTIVE", "PAUSED"}:
        return "", "", False
    return before, after, True


def _activation_check(
    local: Mapping[str, Any], graph: Mapping[str, Any], registry: Mapping[str, Any],
) -> Dict[str, Any]:
    target_ids = {local["target"]["campaign_id"]}
    target_ids.update(item["adset_id"] for item in local["cells"])
    target_ids.update(item["ad_id"] for item in local["cells"])
    statuses = {}
    statuses[local["target"]["campaign_id"]] = str(dict(graph.get("last_campaign") or {}).get("effective_status") or dict(graph.get("last_campaign") or {}).get("status") or "").upper()
    for cell in local["cells"]:
        for kind in ("adset", "ad"):
            body = dict(graph.get(f"last_{kind}_{cell['cell_key']}") or {})
            statuses[cell[f"{kind}_id"]] = str(body.get("effective_status") or body.get("status") or "").upper()
    active = {key for key, value in statuses.items() if value == "ACTIVE"}
    unknown = {key for key, value in statuses.items() if value not in {"ACTIVE", "PAUSED"}}
    reasons = []
    if unknown:
        reasons.append("GRAPH_READ_FAILED")
    activities = list(dict(graph.get("activities") or {}).get("data") or [])
    activation_events = []
    unclassified_target_events = []
    for raw in activities:
        item = dict(raw or {})
        if str(item.get("object_id") or "") not in target_ids:
            continue
        before, after, classified = _status_transition(item)
        if not classified:
            unclassified_target_events.append(item)
            continue
        if before == "PAUSED" and after == "ACTIVE":
            activation_events.append(item)
    allowed = {
        (str(item["actor_id"]), str(item["application_id"]))
        for item in registry["principals"] if "ACTIVATE" in item["roles"]
    }
    unauthorized = [
        item for item in activation_events
        if (str(item.get("actor_id") or ""), str(item.get("application_id") or "")) not in allowed
    ]
    if unauthorized:
        return _check("POLLUTED", ["EXTERNAL_ACTIVATION_DETECTED"], ["activities"])
    if unclassified_target_events:
        reasons.append("ACTOR_PROVENANCE_UNRESOLVED")
    if active:
        activation = dict(local.get("activation") or {})
        if activation.get("status") != "PASS":
            reasons.append("CREATE_ACTIVATION_NOT_SEPARATE")
        activation_plan = dict(activation.get("plan") or {})
        planned_activation_ids = {
            str(item.get("target_id") or "")
            for item in dict(activation_plan.get("steps") or {}).values()
            if isinstance(item, dict)
        }
        activation_cells = list(activation_plan.get("cells") or [])
        if not activation_cells:
            activation_cells = list(activation_plan.get("compiled_delivery_cells") or [])
        for cell in activation_cells:
            planned_activation_ids.update(
                str(item.get("target_id") or "")
                for item in dict(dict(cell or {}).get("steps") or {}).values()
                if isinstance(item, dict)
            )
        planned_activation_ids.discard("")
        if planned_activation_ids != active:
            reasons.append("CREATE_ACTIVATION_NOT_SEPARATE")
        events_by_object = {}
        for event in activation_events:
            events_by_object.setdefault(str(event.get("object_id") or ""), []).append(event)
        receipts_by_step = {
            str(item.get("step_name") or "").upper(): dict(item)
            for item in list(activation.get("receipts") or [])
        }
        receipt_step_by_object = {}
        for step_name, raw_step in dict(activation_plan.get("steps") or {}).items():
            target_id = str(dict(raw_step or {}).get("target_id") or "")
            if target_id:
                receipt_step_by_object[target_id] = str(step_name).upper()
        for index, raw_cell in enumerate(activation_cells, start=1):
            cell = dict(raw_cell or {})
            cell_key = str(cell.get("cell_key") or f"C{index}").strip().upper()
            for step_name, raw_step in dict(cell.get("steps") or {}).items():
                target_id = str(dict(raw_step or {}).get("target_id") or "")
                if target_id:
                    receipt_step_by_object[target_id] = f"{cell_key}_{str(step_name).upper()}"
        for object_id in active:
            matches = events_by_object.get(object_id, [])
            if len(matches) != 1:
                reasons.append("ACTIVATION_EVENT_MISSING")
                continue
            event = matches[0]
            identity = (str(event.get("actor_id") or ""), str(event.get("application_id") or ""))
            if identity not in allowed:
                reasons.append("EXTERNAL_ACTIVATION_DETECTED")
                continue
            try:
                approved_at = _utc(
                    activation.get("approval", {}).get("approved_at"),
                    "ACTIVATION_OUTSIDE_APPROVAL_TTL",
                )
                expires_at = _utc(
                    activation.get("approval", {}).get("expires_at"),
                    "ACTIVATION_OUTSIDE_APPROVAL_TTL",
                )
                event_at = _utc(
                    event.get("event_time") or event.get("date_time_in_timezone"),
                    "ACTIVATION_OUTSIDE_APPROVAL_TTL",
                )
                consumed_at = _utc(
                    activation.get("approval", {}).get("consumed_at"),
                    "ACTIVATION_OUTSIDE_APPROVAL_TTL",
                )
                task_created_at = _utc(
                    activation.get("task", {}).get("created_at"),
                    "ACTIVATION_OUTSIDE_APPROVAL_TTL",
                )
                receipt = receipts_by_step.get(receipt_step_by_object.get(object_id, ""), {})
                receipt_at = _utc(
                    receipt.get("created_at"), "ACTIVATION_OUTSIDE_APPROVAL_TTL",
                )
                if not max(approved_at, consumed_at, task_created_at) <= event_at <= min(
                    expires_at, receipt_at,
                ):
                    reasons.append("ACTIVATION_OUTSIDE_APPROVAL_TTL")
            except G004ContractError:
                reasons.append("ACTIVATION_OUTSIDE_APPROVAL_TTL")
        if "EXTERNAL_ACTIVATION_DETECTED" in reasons:
            return _check("POLLUTED", reasons, ["activities", "activation_local_chain"])
        # The registry hash proves integrity only. G0-05 must attach a trusted
        # detached identity attestation before an ACTIVE path can become Gate evidence.
        reasons.append("RECEIPT_UNSIGNED")
    elif activation_events:
        reasons.append("EXTERNAL_ACTIVATION_DETECTED")
        return _check("POLLUTED", reasons, ["activities"])
    if dict(graph.get("activities") or {}).get("pagination_complete") is not True:
        reasons.append("PAGINATION_INCOMPLETE")
    return _check("PASS" if not reasons else "INCOMPLETE", reasons, ["activities", "object_statuses"])


def build_receipt(
    *, request: Mapping[str, Any], registry: Mapping[str, Any], local: Mapping[str, Any],
    graph: Mapping[str, Any], transport: Mapping[str, Any], source_sha256: str,
    evidence_bundle_hash: str, started_at: datetime, finished_at: datetime,
) -> Dict[str, Any]:
    policy = dict(request["freshness_policy"])
    checks = {
        "graph_completeness": _graph_completeness_check(graph),
        "plan_binding": _check(
            str(local["create"].get("status") or "FAIL"),
            list(local["create"].get("reason_codes") or [])
            + list(local.get("preflight", {}).get("reason_codes") or []),
            ["local_create_chain", "server_owned_preflight"],
        ),
        "token_permission": _permission_check(request, local, graph),
        "business_ownership": _ownership_check(request, local, graph, registry),
        "capability_semantics": _capability_semantics_check(local, graph),
        "topology": _topology_check(local, graph),
        "activation_provenance": _activation_check(local, graph, registry),
        "freshness": _check("PASS", [], ["object_double_read", "activity_cutoff"]),
        "zero_write": _check(
            "PASS" if all(int(transport.get(key) or 0) == 0 for key in (
                "post_count", "put_count", "patch_count", "delete_count", "redirect_count",
                "batch_count", "async_job_count", "meta_object_writes", "local_db_writes",
            )) else "FAIL",
            [] if transport.get("allowed_methods") == ["GET"] else ["TRANSPORT_METHOD_FORBIDDEN"],
            ["transport_proof"],
        ),
    }
    if (finished_at - started_at).total_seconds() > policy["max_run_seconds"]:
        checks["freshness"] = _check("INCOMPLETE", ["RECEIPT_EXPIRED"], ["run_duration"])
    for key, body in graph.items():
        if not isinstance(body, dict) or key in {"evidence_hash"}:
            continue
        updated = str(body.get("updated_time") or "")
        if updated:
            try:
                if _utc(updated, "G004_INPUT_SCHEMA_INVALID") > finished_at - timedelta(seconds=policy["activity_settlement_seconds"]):
                    checks["freshness"] = _check("INCOMPLETE", ["ACTIVITY_LAG_WINDOW_OPEN"], [key])
            except G004ContractError:
                checks["freshness"] = _check("INCOMPLETE", ["ACTIVITY_LAG_WINDOW_OPEN"], [key])
    statuses = [body["status"] for body in checks.values()]
    if "POLLUTED" in statuses:
        outcome = "POLLUTED"
    elif "FAIL" in statuses:
        outcome = "FAIL"
    elif "INCOMPLETE" in statuses:
        outcome = "INCOMPLETE"
    else:
        outcome = "PASS"
    checked_at = finished_at.astimezone(timezone.utc)
    body = {
        "schema_version": RECEIPT_VERSION,
        "assessment_id": f"g004_{hash_json({'audit_id': request['audit_id'], 'request_nonce': request['request_nonce']})[:24]}",
        "audit_id": request["audit_id"], "engine_version": ENGINE_VERSION,
        "graph_api_version": GRAPH_API_VERSION, "sdk_contract_version": SDK_CONTRACT_VERSION,
        "topology_contract_version": TOPOLOGY_VERSION, "canonical_version": CANONICAL_VERSION,
        "request_hash": hash_json(request), "source_snapshot_sha256": source_sha256,
        "checked_at": checked_at.isoformat(),
        "expires_at": (checked_at + timedelta(seconds=policy["receipt_ttl_seconds"])).isoformat(),
        "target": dict(local["target"]),
        "plan_binding": {
            "create_binding_hash": local["create"].get("binding_hash"),
            "activation_binding_hash": dict(local.get("activation") or {}).get("binding_hash"),
            "local_evidence_hash": local["local_evidence_hash"],
            "actor_binding_registry_hash": hash_json(registry),
        },
        "checks": checks,
        "evidence_manifest": [entry for entry in transport.get("journal", [])],
        "evidence_bundle_hash": evidence_bundle_hash,
        "graph_evidence_hash": graph.get("evidence_hash"),
        "transport_proof": {key: value for key, value in transport.items() if key != "journal"},
        "outcome": outcome,
        "gate0_fragment": "PERMISSION_TOPOLOGY_PROVEN" if outcome == "PASS" else "INELIGIBLE",
        "gate0_result_ceiling": "QUASI_ONLY",
        "not_gate_receipt": True,
        "attestation_status": "PENDING_ATTESTATION",
        "blocking_reasons": sorted({
            reason for item in checks.values() for reason in item["reason_codes"]
        }),
    }
    body["receipt_body_hash"] = hash_json(body)
    return body


def audit_snapshot_bundle(
    *, request: Mapping[str, Any], actor_registry: Mapping[str, Any], db_path: Path,
    expected_db_sha256: str, session: Any, access_token: str,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    normalized = normalize_request(request)
    registry = normalize_actor_registry(actor_registry, normalized["actor_binding_registry_hash"])
    source = Path(db_path).resolve()
    expected = str(expected_db_sha256 or "").lower()
    try:
        source_before = (source.stat().st_size, source.stat().st_mtime_ns)
    except OSError as exc:
        raise G004SourceError("G004_SOURCE_UNREADABLE") from exc
    if not _SHA256_RE.fullmatch(expected) or sha256_file(source) != expected:
        raise G004SourceError("G004_SOURCE_HASH_MISMATCH")
    started = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    requested_at = _utc(normalized["requested_at"], "G004_INPUT_SCHEMA_INVALID")
    if abs((requested_at - started).total_seconds()) > normalized["freshness_policy"]["clock_skew_seconds"]:
        raise G004ContractError("G004_INPUT_SCHEMA_INVALID")
    with open_readonly_snapshot(source) as conn:
        before = int(conn.execute("PRAGMA data_version").fetchone()[0])
        local = load_local_evidence(conn, normalized)
        client = GetOnlyGraphClient(
            session=session, access_token=access_token, now=started,
            allowed_paths=allowed_graph_paths(local),
            max_pages=normalized["freshness_policy"]["max_pages"],
            max_items=normalized["freshness_policy"]["max_events"],
        )
        graph = collect_graph_evidence(client, local, normalized)
        after = int(conn.execute("PRAGMA data_version").fetchone()[0])
        if before != after:
            raise G004SourceError("G004_SOURCE_DRIFTED")
    try:
        source_after = (source.stat().st_size, source.stat().st_mtime_ns)
    except OSError as exc:
        raise G004SourceError("G004_SOURCE_DRIFTED") from exc
    if source_after != source_before or sha256_file(source) != expected:
        raise G004SourceError("G004_SOURCE_DRIFTED")
    for suffix in ("-wal", "-journal", "-shm"):
        sidecar = Path(f"{source}{suffix}")
        if sidecar.exists() and sidecar.stat().st_size:
            raise G004SourceError(f"G004_SOURCE_SIDECAR_PRESENT:{suffix}")
    finished = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    journal = [entry.__dict__ for entry in client.journal]
    transport = {**client.proof(), "journal": journal, "local_db_writes": 0}
    evidence_bundle = {
        "schema_version": "gle-g0-04-redacted-evidence-bundle-v1",
        "request_hash": hash_json(normalized),
        "source_snapshot_sha256": expected,
        "local_evidence_hash": local["local_evidence_hash"],
        "target": dict(local["target"]),
        "graph": _evidence_safe({key: value for key, value in graph.items() if key != "evidence_hash"}),
        "transport_journal": journal,
    }
    evidence_bundle["evidence_bundle_hash"] = hash_json(evidence_bundle)
    receipt = build_receipt(
        request=normalized, registry=registry, local=local, graph=graph,
        transport=transport, source_sha256=expected,
        evidence_bundle_hash=evidence_bundle["evidence_bundle_hash"],
        started_at=started, finished_at=finished,
    )
    return {"receipt": receipt, "evidence_bundle": evidence_bundle}


def audit_snapshot(
    *, request: Mapping[str, Any], actor_registry: Mapping[str, Any], db_path: Path,
    expected_db_sha256: str, session: Any, access_token: str,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    return audit_snapshot_bundle(
        request=request, actor_registry=actor_registry, db_path=db_path,
        expected_db_sha256=expected_db_sha256, session=session,
        access_token=access_token, now=now,
    )["receipt"]


def exit_code_for_receipt(receipt: Mapping[str, Any]) -> int:
    return 0 if receipt.get("outcome") == "PASS" else 2
