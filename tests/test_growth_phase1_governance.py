from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.growth.phase1_governance import (
    GOLDEN_PATH_ACTIONS,
    GovernanceValidationError,
    canonical_hash,
    effective_permissions,
    load_governance,
    validate_contract,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config" / "gle_phase1_governance_v1.json"


def _default_raw() -> dict:
    return json.loads(DEFAULT_CONFIG.read_text(encoding="utf-8"))


def _write_config(tmp_path: Path, raw: dict) -> Path:
    path = tmp_path / "governance.json"
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    return path


def _hash(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _assign_owners(raw: dict) -> None:
    for role, owner in raw["owners"].items():
        owner.update(
            {
                "name": f"Named {role}",
                "signed_at": "2026-08-06T12:00:00+08:00",
                "signature_hash": _hash(role),
            }
        )


def _freeze_versions(raw: dict) -> None:
    for key, version in raw["canonical_versions"].items():
        version.update(
            {
                "version": f"{key}-v1",
                "hash": _hash(key),
            }
        )


def _pass_gate(raw: dict, gate: str) -> None:
    raw["gates"][gate] = {
        "status": "PASS",
        "receipt_hash": _hash(gate),
    }


def _ready_raw(mode: str) -> dict:
    raw = _default_raw()
    raw["global_enabled"] = True
    raw["mode"] = mode
    raw["canary"] = {"account_ids": ["act_canary_1"], "markets": ["MX"]}
    raw["action_allowlist"] = list(GOLDEN_PATH_ACTIONS)
    raw["kill_switches"] = {key: False for key in raw["kill_switches"]}
    raw["kill_switches"]["force_manual_review_for_uncertain_post"] = True
    _assign_owners(raw)
    _freeze_versions(raw)
    _pass_gate(raw, "gate_0")
    _pass_gate(raw, "gate_1")
    if mode in {"BOUNDED_EXECUTION", "CLOSED"}:
        _pass_gate(raw, "gate_2")
    if mode == "CLOSED":
        _pass_gate(raw, "gate_3")
    return raw


def test_default_contract_is_off_empty_and_fail_closed() -> None:
    contract = load_governance(DEFAULT_CONFIG)
    permissions = effective_permissions(contract, env={})

    assert contract.data["baseline"] == "FINAL_EXECUTION_PLAN_v1.1"
    assert contract.data["global_enabled"] is False
    assert contract.data["mode"] == "OFF"
    assert contract.data["canary"] == {"account_ids": [], "markets": []}
    assert contract.data["action_allowlist"] == []
    assert {gate["status"] for gate in contract.data["gates"].values()} == {
        "NOT_STARTED"
    }
    assert {
        item["version"] for item in contract.data["canonical_versions"].values()
    } == {"UNFROZEN"}
    assert all(contract.data["kill_switches"].values())
    assert {owner["name"] for owner in contract.data["owners"].values()} == {
        "UNASSIGNED"
    }
    assert permissions.enabled is False
    assert permissions.effective_mode == "OFF"
    assert permissions.allowed_actions == ()
    assert permissions.meta_write_allowed is False


def test_canonical_hash_is_deterministic_across_key_order() -> None:
    raw = _default_raw()
    reordered = dict(reversed(list(raw.items())))
    reordered["owners"] = dict(reversed(list(raw["owners"].items())))

    assert canonical_hash(raw) == canonical_hash(reordered)
    assert canonical_hash(raw).startswith("sha256:")


def test_missing_config_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(GovernanceValidationError, match="config_missing"):
        load_governance(tmp_path / "missing.json")


def test_force_off_is_one_way_and_invalid_env_fails_closed(tmp_path: Path) -> None:
    raw = _ready_raw("LIVE_SHADOW")
    contract = load_governance(_write_config(tmp_path, raw))

    assert effective_permissions(contract, env={}).enabled is True
    assert effective_permissions(
        contract, env={"GLE_PHASE1_FORCE_OFF": "0"}
    ).enabled is True

    forced = effective_permissions(
        contract, env={"GLE_PHASE1_FORCE_OFF": "true"}
    )
    assert forced.enabled is False
    assert forced.effective_mode == "OFF"
    assert forced.allowed_actions == ()
    assert "FORCE_OFF" in forced.reasons

    invalid = effective_permissions(
        contract, env={"GLE_PHASE1_FORCE_OFF": "unexpected"}
    )
    assert invalid.enabled is False
    assert invalid.allowed_actions == ()
    assert "INVALID_FORCE_OFF_VALUE" in invalid.reasons

    raw["global_enabled"] = False
    disabled = load_governance(_write_config(tmp_path, raw))
    assert effective_permissions(
        disabled, env={"GLE_PHASE1_FORCE_OFF": "false"}
    ).enabled is False


def test_unsigned_owner_and_unfrozen_version_each_block_promotion(
    tmp_path: Path,
) -> None:
    raw = _ready_raw("LIVE_SHADOW")
    raw["owners"]["data_signer"] = {
        "name": "UNASSIGNED",
        "signed_at": None,
        "signature_hash": None,
    }
    permissions = effective_permissions(
        load_governance(_write_config(tmp_path, raw)), env={}
    )
    assert permissions.enabled is False
    assert "OWNER_SIGNATURES_INCOMPLETE" in permissions.reasons

    raw = _ready_raw("LIVE_SHADOW")
    raw["canonical_versions"]["dataset"] = {
        "version": "UNFROZEN",
        "hash": None,
    }
    permissions = effective_permissions(
        load_governance(_write_config(tmp_path, raw)), env={}
    )
    assert permissions.enabled is False
    assert "VERSIONS_UNFROZEN" in permissions.reasons


def test_gate_pass_requires_receipt_hash(tmp_path: Path) -> None:
    raw = _ready_raw("LIVE_SHADOW")
    raw["gates"]["gate_1"]["receipt_hash"] = None

    with pytest.raises(GovernanceValidationError, match="receipt_hash_required"):
        load_governance(_write_config(tmp_path, raw))


@pytest.mark.parametrize("later_gate", ["gate_1", "gate_2", "gate_3"])
def test_gate_pass_cannot_skip_any_predecessor(
    tmp_path: Path, later_gate: str
) -> None:
    raw = _default_raw()
    _pass_gate(raw, later_gate)

    with pytest.raises(GovernanceValidationError, match="gate_sequence_invalid"):
        load_governance(_write_config(tmp_path, raw))


def test_live_shadow_requires_gate_0_and_1_pass(tmp_path: Path) -> None:
    raw = _ready_raw("LIVE_SHADOW")
    allowed = effective_permissions(
        load_governance(_write_config(tmp_path, raw)), env={}
    )
    assert allowed.enabled is True
    assert allowed.effective_mode == "LIVE_SHADOW"
    assert "CREATE_CANARY_PAUSED" in allowed.allowed_actions
    assert "ACTIVATE_CANARY" in allowed.allowed_actions
    assert "PAUSE_LOSER" not in allowed.allowed_actions

    raw["gates"]["gate_1"] = {
        "status": "NOT_STARTED",
        "receipt_hash": None,
    }
    blocked = effective_permissions(
        load_governance(_write_config(tmp_path, raw)), env={}
    )
    assert blocked.enabled is False
    assert "GATE_1_NOT_PASS" in blocked.reasons


def test_bounded_execution_requires_gate_2_and_closed_never_expands(
    tmp_path: Path,
) -> None:
    raw = _ready_raw("BOUNDED_EXECUTION")
    allowed = effective_permissions(
        load_governance(_write_config(tmp_path, raw)), env={}
    )
    assert allowed.enabled is True
    assert set(allowed.allowed_actions) == set(GOLDEN_PATH_ACTIONS)

    raw["gates"]["gate_2"] = {
        "status": "NOT_STARTED",
        "receipt_hash": None,
    }
    blocked = effective_permissions(
        load_governance(_write_config(tmp_path, raw)), env={}
    )
    assert blocked.enabled is False
    assert "GATE_2_NOT_PASS" in blocked.reasons

    raw = _ready_raw("CLOSED")
    raw["gates"] = {
        gate_name: {"status": "NOT_STARTED", "receipt_hash": None}
        for gate_name in raw["gates"]
    }
    closed = effective_permissions(load_governance(_write_config(tmp_path, raw)), env={})
    assert closed.enabled is False
    assert closed.effective_mode == "CLOSED"
    assert closed.allowed_actions == ()
    assert "MODE_CLOSED" in closed.reasons


def test_bounded_execution_rechecks_all_predecessor_gates(tmp_path: Path) -> None:
    raw = _ready_raw("BOUNDED_EXECUTION")
    contract = load_governance(_write_config(tmp_path, raw))
    contract.data["gates"]["gate_0"] = {
        "status": "NOT_STARTED",
        "receipt_hash": None,
    }

    blocked = effective_permissions(contract, env={})
    assert blocked.enabled is False
    assert "GATE_0_NOT_PASS" in blocked.reasons
    assert blocked.allowed_actions == ()


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("account_ids", ["act_1", "act_2"], "single_account_required"),
        ("markets", ["MX", "BR"], "single_market_required"),
    ],
)
def test_multiple_accounts_or_markets_are_rejected(
    tmp_path: Path, field: str, value: list[str], message: str
) -> None:
    raw = _default_raw()
    raw["canary"][field] = value

    with pytest.raises(GovernanceValidationError, match=message):
        load_governance(_write_config(tmp_path, raw))


def test_non_golden_path_action_and_variable_are_rejected(tmp_path: Path) -> None:
    raw = _default_raw()
    raw["action_allowlist"] = ["AUTO_INCREASE_BUDGET"]
    with pytest.raises(GovernanceValidationError, match="action_not_allowed"):
        validate_contract(raw)

    raw = _default_raw()
    raw["golden_path"]["unique_variable"] = "IMAGE"
    with pytest.raises(GovernanceValidationError, match="golden_path_invalid"):
        validate_contract(raw)


def test_live_modes_require_exactly_one_canary_account_and_market(
    tmp_path: Path,
) -> None:
    raw = _ready_raw("LIVE_SHADOW")
    raw["canary"] = {"account_ids": [], "markets": []}
    permissions = effective_permissions(
        load_governance(_write_config(tmp_path, raw)), env={}
    )
    assert permissions.enabled is False
    assert "CANARY_SCOPE_INCOMPLETE" in permissions.reasons


def test_kill_switches_only_narrow_effective_permissions(tmp_path: Path) -> None:
    raw = _ready_raw("BOUNDED_EXECUTION")
    raw["kill_switches"]["block_all_meta_writes"] = True
    permissions = effective_permissions(
        load_governance(_write_config(tmp_path, raw)), env={}
    )
    assert permissions.enabled is True
    assert permissions.allowed_actions == ("GENERATE_NEXT_EXPERIMENT_DRAFT",)
    assert permissions.meta_write_allowed is False

    raw["kill_switches"]["block_all_actions"] = True
    permissions = effective_permissions(
        load_governance(_write_config(tmp_path, raw)), env={}
    )
    assert permissions.enabled is True
    assert permissions.allowed_actions == ()
    assert permissions.meta_write_allowed is False
