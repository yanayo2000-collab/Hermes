from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.growth.api import (
    _get_gle_governance_status,
    _gle_governance_status_projection,
    _gle_workflow_assurance,
    create_growth_router,
)
from app.growth.errors import GrowthNotFound, GrowthValidationError
from app.growth.historical_cell_lineage_projection import (
    CEILING,
    historical_cell_lineage_projection,
)


ACCOUNT_ID = "1012060198097836"
BASELINE_ID = "adexp_1c90797d13d04928aa0a74e487d21fd1"
CHALLENGER_ID = "adexp_f9dd3e87bca6415b94b62ebfdf45fdf9"


@pytest.mark.parametrize(
    ("experiment_id", "cell_key", "role"),
    [
        (BASELINE_ID, "C1", "BASELINE"),
        (CHALLENGER_ID, "C2", "CHALLENGER"),
    ],
)
def test_exact_subject_gets_historical_direction_without_gate_promotion(
    experiment_id: str, cell_key: str, role: str,
) -> None:
    result = historical_cell_lineage_projection(
        account_id=ACCOUNT_ID,
        experiment_id=experiment_id,
    )

    assert result is not None
    assert result["status"] == "HISTORICAL_EXACT_CELL_LINEAGE_AVAILABLE"
    assert result["decision"] == "DIRECTIONAL_C2_BETTER_STATISTICALLY_INCONCLUSIVE"
    assert result["decision_strength"] == "DIRECTIONAL_ONLY"
    assert result["subject"]["requested_cell_key"] == cell_key
    assert result["subject"]["requested_role"] == role
    assert result["sample"] == {"impressions": 1298, "clicks": 32, "installs": 11}

    ctr = result["metrics"]["ctr"]
    assert ctr["baseline"]["rate"] == pytest.approx(10 / 505)
    assert ctr["challenger"]["rate"] == pytest.approx(22 / 793)
    assert ctr["challenger_relative_lift"] == pytest.approx(0.401008827238335)
    assert ctr["fisher_exact_two_sided_p_value"] == pytest.approx(0.4636710373)

    installs = result["metrics"]["install_per_impression"]
    assert installs["challenger_relative_lift"] == pytest.approx(1.8656998739)
    assert installs["fisher_exact_two_sided_p_value"] == pytest.approx(0.2187142847)
    click_to_install = result["metrics"]["click_to_install"]
    assert click_to_install["challenger_relative_lift"] == pytest.approx(1.0454545454545454)
    assert click_to_install["fisher_exact_two_sided_p_value"] == pytest.approx(0.4250188801)
    assert all(
        metric["fisher_exact_two_sided_p_value"] > 0.05
        for metric in result["metrics"].values()
    )

    assert result["natural_window"]["status"] == "PENDING_NATURAL_WINDOW"
    assert result["natural_window"]["historical_evidence_substitutes_natural_window"] is False
    assert result["ceiling"] == CEILING
    assert result["ceiling"]["causal_claim"] is False
    assert result["ceiling"]["gate0_result_effect"] == "UNCHANGED"


def test_projection_is_not_borrowed_by_another_subject() -> None:
    assert historical_cell_lineage_projection(
        account_id="different-account", experiment_id=BASELINE_ID,
    ) is None
    assert historical_cell_lineage_projection(
        account_id=ACCOUNT_ID, experiment_id="different-experiment",
    ) is None
    assert historical_cell_lineage_projection(
        account_id=ACCOUNT_ID, experiment_id="",
    ) is None


def test_governance_projection_exposes_two_lanes_and_remains_fail_closed() -> None:
    status = _gle_governance_status_projection(
        account_id=f"act_{ACCOUNT_ID}", experiment_id=CHALLENGER_ID,
    )

    assert status["gate0"]["status"] == "QUASI_ONLY"
    assert status["gate0"]["result_effect"] == "UNCHANGED"
    assert status["gate1"]["status"] == "NOT_READY"
    assert status["canonical_lineage"]["status"] == "MISSING_EXACT_CELL_LINEAGE"
    assert status["canonical_lineage"]["status_scope"] == "CURRENT_NATURAL_WINDOW"
    assert status["canonical_lineage"]["natural_window_status"] == "PENDING_NATURAL_WINDOW"
    historical = status["canonical_lineage"]["historical_evidence"]
    assert historical["status"] == "HISTORICAL_EXACT_CELL_LINEAGE_AVAILABLE"
    assert historical["decision"] == "DIRECTIONAL_C2_BETTER_STATISTICALLY_INCONCLUSIVE"
    assert status["ceilings"]["causal_claim"] is False
    assert status["ceilings"]["meta_write_allowed_by_gate"] is False

    assurance = _gle_workflow_assurance(ACCOUNT_ID, CHALLENGER_ID)
    assert assurance["historical_decision"] == "DIRECTIONAL_C2_BETTER_STATISTICALLY_INCONCLUSIVE"
    assert assurance["natural_lineage_status"] == "PENDING_NATURAL_WINDOW"
    assert assurance["exact_cell_lineage_status"] == "MISSING_EXACT_CELL_LINEAGE"
    assert assurance["causal_claim"] is False


class _Db:
    def __init__(self, path: Path) -> None:
        self.path = path

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn


def test_subject_resolver_and_router_are_read_only(tmp_path: Path) -> None:
    db = _Db(tmp_path / "projection.sqlite")
    with db.connect() as conn:
        conn.execute(
            "CREATE TABLE ad_experiment (experiment_id TEXT PRIMARY KEY, account_id TEXT)"
        )
        conn.execute(
            "INSERT INTO ad_experiment VALUES (?, ?)",
            (CHALLENGER_ID, ACCOUNT_ID),
        )
        conn.commit()

    router = create_growth_router(db=db, require_admin=lambda _request: {"user_id": "test"})
    assert "/api/ops/growth/governance-status" in {route.path for route in router.routes}

    with db.connect() as conn:
        before = conn.execute("SELECT COUNT(*) FROM ad_experiment").fetchone()[0]
    result = _get_gle_governance_status(
        db, account_id=f"act_{ACCOUNT_ID}", experiment_id=CHALLENGER_ID,
    )
    with db.connect() as conn:
        after = conn.execute("SELECT COUNT(*) FROM ad_experiment").fetchone()[0]
    assert after == before
    assert result["canonical_lineage"]["historical_evidence"]["decision_strength"] == "DIRECTIONAL_ONLY"

    with pytest.raises(GrowthValidationError, match="governance_subject_account_mismatch"):
        _get_gle_governance_status(
            db, account_id="different-account", experiment_id=CHALLENGER_ID,
        )
    with pytest.raises(GrowthNotFound, match="experiment_not_found"):
        _get_gle_governance_status(
            db, account_id=ACCOUNT_ID, experiment_id="missing-experiment",
        )
