"""Fail-closed governance contract for GLE phase 1.

This module remains a pure contract engine: it validates a versioned contract
and derives maximum permissions without performing I/O beyond config loading.
Execution integrations consume its result from separate fail-closed gates.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Optional, Tuple


BASELINE = "FINAL_EXECUTION_PLAN_v1.1"
CONTRACT_VERSION = "gle-phase1-governance-v1"
MODES = {"OFF", "LIVE_SHADOW", "BOUNDED_EXECUTION", "CLOSED"}
GATE_STATUSES = {"NOT_STARTED", "PASS", "FAIL"}
GOLDEN_PATH_ACTIONS = (
    "CREATE_CANARY_PAUSED",
    "ACTIVATE_CANARY",
    "GENERATE_NEXT_EXPERIMENT_DRAFT",
    "PAUSE_LOSER",
    "CREATE_NEXT_CHALLENGER_PAUSED",
)
LIVE_SHADOW_ACTIONS = GOLDEN_PATH_ACTIONS[:3]
META_WRITE_ACTIONS = frozenset(
    action
    for action in GOLDEN_PATH_ACTIONS
    if action != "GENERATE_NEXT_EXPERIMENT_DRAFT"
)

_TOP_LEVEL_KEYS = {
    "baseline",
    "contract_version",
    "global_enabled",
    "mode",
    "golden_path",
    "canary",
    "action_allowlist",
    "gates",
    "canonical_versions",
    "kill_switches",
    "owners",
}
_GATE_KEYS = {f"gate_{number}" for number in range(4)}
_VERSION_KEYS = {"schema", "evaluator", "policy", "dataset"}
_OWNER_KEYS = {"gate_owner", "business_signer", "technical_signer", "data_signer"}
_KILL_SWITCH_KEYS = {
    "block_all_actions",
    "block_all_meta_writes",
    "block_account_writes",
    "block_action_writes",
    "disable_evaluation_scheduler",
    "block_new_experiment_activation",
    "force_manual_review_for_uncertain_post",
}
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_OWNER_PLACEHOLDERS = {"UNASSIGNED", "TBD", "UNKNOWN", "N/A", "NONE"}


class GovernanceValidationError(ValueError):
    """Raised when the phase-1 governance contract is not structurally valid."""


@dataclass(frozen=True)
class GovernanceContract:
    data: Mapping[str, Any]
    canonical_hash: str


@dataclass(frozen=True)
class EffectivePermissions:
    enabled: bool
    configured_mode: str
    effective_mode: str
    allowed_actions: Tuple[str, ...]
    meta_write_allowed: bool
    evaluation_scheduling_allowed: bool
    manual_review_required_for_uncertain_post: bool
    reasons: Tuple[str, ...]
    contract_hash: str


def _fail(code: str, detail: str = "") -> None:
    suffix = f": {detail}" if detail else ""
    raise GovernanceValidationError(f"{code}{suffix}")


def _require_object(value: Any, code: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        _fail(code)
    return value


def _require_exact_keys(value: Mapping[str, Any], keys: set[str], code: str) -> None:
    if set(value) != keys:
        _fail(code, f"expected={sorted(keys)} actual={sorted(value)}")


def _is_hash(value: Any) -> bool:
    return isinstance(value, str) and _HASH_RE.fullmatch(value) is not None


def _is_signed_timestamp(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def canonical_hash(data: Mapping[str, Any]) -> str:
    """Return a deterministic SHA-256 over canonical UTF-8 JSON."""

    try:
        payload = json.dumps(
            data,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        _fail("canonical_json_invalid", str(exc))
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def validate_contract(raw: Any) -> GovernanceContract:
    """Strictly validate a governance document without granting permissions."""

    data = _require_object(raw, "contract_not_object")
    _require_exact_keys(data, _TOP_LEVEL_KEYS, "top_level_schema_invalid")
    if data["baseline"] != BASELINE:
        _fail("baseline_invalid")
    if data["contract_version"] != CONTRACT_VERSION:
        _fail("contract_version_invalid")
    if type(data["global_enabled"]) is not bool:
        _fail("global_enabled_not_boolean")
    if data["mode"] not in MODES:
        _fail("mode_invalid")

    golden_path = _require_object(data["golden_path"], "golden_path_invalid")
    _require_exact_keys(
        golden_path, {"experiment_type", "unique_variable"}, "golden_path_invalid"
    )
    if golden_path != {
        "experiment_type": "COPY_ONLY",
        "unique_variable": "PRIMARY_TEXT",
    }:
        _fail("golden_path_invalid")

    canary = _require_object(data["canary"], "canary_schema_invalid")
    _require_exact_keys(canary, {"account_ids", "markets"}, "canary_schema_invalid")
    for field in ("account_ids", "markets"):
        values = canary[field]
        if not isinstance(values, list) or any(
            not isinstance(value, str) or not value.strip() for value in values
        ):
            _fail("canary_schema_invalid", field)
        if len(values) != len(set(values)):
            _fail("canary_duplicate", field)
    if len(canary["account_ids"]) > 1:
        _fail("single_account_required")
    if len(canary["markets"]) > 1:
        _fail("single_market_required")

    actions = data["action_allowlist"]
    if not isinstance(actions, list) or any(not isinstance(item, str) for item in actions):
        _fail("action_allowlist_invalid")
    if len(actions) != len(set(actions)):
        _fail("action_allowlist_duplicate")
    invalid_actions = sorted(set(actions) - set(GOLDEN_PATH_ACTIONS))
    if invalid_actions:
        _fail("action_not_allowed", ",".join(invalid_actions))

    gates = _require_object(data["gates"], "gates_schema_invalid")
    _require_exact_keys(gates, _GATE_KEYS, "gates_schema_invalid")
    for gate_name, gate_raw in gates.items():
        gate = _require_object(gate_raw, "gate_schema_invalid")
        _require_exact_keys(gate, {"status", "receipt_hash"}, "gate_schema_invalid")
        status = gate["status"]
        receipt_hash = gate["receipt_hash"]
        if status not in GATE_STATUSES:
            _fail("gate_status_invalid", gate_name)
        if status == "NOT_STARTED" and receipt_hash is not None:
            _fail("unexpected_receipt_hash", gate_name)
        if status in {"PASS", "FAIL"} and not _is_hash(receipt_hash):
            _fail("receipt_hash_required", gate_name)
    for gate_number in range(1, 4):
        gate_name = f"gate_{gate_number}"
        if gates[gate_name]["status"] != "PASS":
            continue
        missing_predecessors = [
            f"gate_{predecessor}"
            for predecessor in range(gate_number)
            if gates[f"gate_{predecessor}"]["status"] != "PASS"
        ]
        if missing_predecessors:
            _fail(
                "gate_sequence_invalid",
                f"{gate_name} requires {','.join(missing_predecessors)} PASS",
            )

    versions = _require_object(
        data["canonical_versions"], "canonical_versions_schema_invalid"
    )
    _require_exact_keys(versions, _VERSION_KEYS, "canonical_versions_schema_invalid")
    for version_name, version_raw in versions.items():
        version = _require_object(version_raw, "canonical_version_invalid")
        _require_exact_keys(version, {"version", "hash"}, "canonical_version_invalid")
        if version["version"] == "UNFROZEN":
            if version["hash"] is not None:
                _fail("unfrozen_version_has_hash", version_name)
        elif (
            not isinstance(version["version"], str)
            or not version["version"].strip()
            or not _is_hash(version["hash"])
        ):
            _fail("canonical_version_invalid", version_name)

    switches = _require_object(data["kill_switches"], "kill_switches_schema_invalid")
    _require_exact_keys(switches, _KILL_SWITCH_KEYS, "kill_switches_schema_invalid")
    if any(type(value) is not bool for value in switches.values()):
        _fail("kill_switch_not_boolean")

    owners = _require_object(data["owners"], "owners_schema_invalid")
    _require_exact_keys(owners, _OWNER_KEYS, "owners_schema_invalid")
    for role, owner_raw in owners.items():
        owner = _require_object(owner_raw, "owner_schema_invalid")
        _require_exact_keys(
            owner, {"name", "signed_at", "signature_hash"}, "owner_schema_invalid"
        )
        if owner["name"] == "UNASSIGNED":
            if owner["signed_at"] is not None or owner["signature_hash"] is not None:
                _fail("unassigned_owner_has_signature", role)
        elif (
            not isinstance(owner["name"], str)
            or len(owner["name"].strip()) < 2
            or owner["name"].strip().upper() in _OWNER_PLACEHOLDERS
            or not _is_signed_timestamp(owner["signed_at"])
            or not _is_hash(owner["signature_hash"])
        ):
            _fail("owner_signature_invalid", role)

    return GovernanceContract(data=data, canonical_hash=canonical_hash(data))


def load_governance(path: Path | str) -> GovernanceContract:
    """Load and validate a contract, failing closed on any file or JSON error."""

    config_path = Path(path)
    try:
        payload = config_path.read_text(encoding="utf-8")
    except (FileNotFoundError, IsADirectoryError, PermissionError, OSError) as exc:
        _fail("config_missing", str(exc))
    try:
        raw = json.loads(payload)
    except (json.JSONDecodeError, UnicodeError) as exc:
        _fail("config_json_invalid", str(exc))
    return validate_contract(raw)


def _owners_complete(data: Mapping[str, Any]) -> bool:
    return all(owner["name"] != "UNASSIGNED" for owner in data["owners"].values())


def _versions_frozen(data: Mapping[str, Any]) -> bool:
    return all(
        version["version"] != "UNFROZEN"
        for version in data["canonical_versions"].values()
    )


def _gate_passed(data: Mapping[str, Any], gate_name: str) -> bool:
    gate = data["gates"][gate_name]
    return gate["status"] == "PASS" and _is_hash(gate["receipt_hash"])


def _force_off_value(env: Mapping[str, str]) -> tuple[bool, bool]:
    raw = env.get("GLE_PHASE1_FORCE_OFF")
    if raw is None or raw.strip().lower() in {"", "0", "false", "no", "off"}:
        return False, False
    if raw.strip().lower() in {"1", "true", "yes", "on"}:
        return True, False
    return True, True


def effective_permissions(
    contract: GovernanceContract,
    *,
    env: Optional[Mapping[str, str]] = None,
) -> EffectivePermissions:
    """Compute permissions; every incomplete prerequisite only removes access."""

    data = contract.data
    configured_mode = data["mode"]
    reasons: list[str] = []
    force_off, invalid_force_off = _force_off_value(os.environ if env is None else env)
    if invalid_force_off:
        reasons.append("INVALID_FORCE_OFF_VALUE")
    elif force_off:
        reasons.append("FORCE_OFF")
    if not data["global_enabled"]:
        reasons.append("GLOBAL_DISABLED")
    if not _owners_complete(data):
        reasons.append("OWNER_SIGNATURES_INCOMPLETE")
    if not _versions_frozen(data):
        reasons.append("VERSIONS_UNFROZEN")
    if configured_mode in {"LIVE_SHADOW", "BOUNDED_EXECUTION"} and (
        len(data["canary"]["account_ids"]) != 1
        or len(data["canary"]["markets"]) != 1
    ):
        reasons.append("CANARY_SCOPE_INCOMPLETE")

    if configured_mode == "OFF":
        reasons.append("MODE_OFF")
    elif configured_mode == "CLOSED":
        reasons.append("MODE_CLOSED")
    elif configured_mode == "LIVE_SHADOW":
        for gate_name in ("gate_0", "gate_1"):
            if not _gate_passed(data, gate_name):
                reasons.append(f"{gate_name.upper()}_NOT_PASS")
    elif configured_mode == "BOUNDED_EXECUTION":
        for gate_name in ("gate_0", "gate_1", "gate_2"):
            if not _gate_passed(data, gate_name):
                reasons.append(f"{gate_name.upper()}_NOT_PASS")

    enabled = not reasons
    effective_mode = configured_mode if configured_mode == "CLOSED" else (
        configured_mode if enabled else "OFF"
    )
    if not enabled:
        allowed: tuple[str, ...] = ()
    else:
        ceiling = (
            LIVE_SHADOW_ACTIONS
            if configured_mode == "LIVE_SHADOW"
            else GOLDEN_PATH_ACTIONS
        )
        allowlist = set(data["action_allowlist"])
        allowed = tuple(action for action in ceiling if action in allowlist)

        switches = data["kill_switches"]
        if switches["block_all_actions"]:
            allowed = ()
        elif (
            switches["block_all_meta_writes"]
            or switches["block_account_writes"]
            or switches["block_action_writes"]
        ):
            allowed = tuple(action for action in allowed if action not in META_WRITE_ACTIONS)
        if switches["block_new_experiment_activation"]:
            allowed = tuple(action for action in allowed if action != "ACTIVATE_CANARY")

    switches = data["kill_switches"]
    return EffectivePermissions(
        enabled=enabled,
        configured_mode=configured_mode,
        effective_mode=effective_mode,
        allowed_actions=allowed,
        meta_write_allowed=any(action in META_WRITE_ACTIONS for action in allowed),
        evaluation_scheduling_allowed=(
            enabled and not switches["disable_evaluation_scheduler"]
        ),
        manual_review_required_for_uncertain_post=switches[
            "force_manual_review_for_uncertain_post"
        ],
        reasons=tuple(reasons),
        contract_hash=contract.canonical_hash,
    )
