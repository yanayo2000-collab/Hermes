from __future__ import annotations

from pathlib import Path

import pytest

from app.growth.errors import GrowthValidationError
from app.growth.meta_graph_adapter import MetaGraphExecutionAdapter, MetaGraphWritePolicy


ROOT = Path(__file__).resolve().parents[1]


class _NoNetworkSession:
    def get(self, *args, **kwargs):  # pragma: no cover - validation must finish first
        raise AssertionError("network access is not part of payload validation")

    def post(self, *args, **kwargs):  # pragma: no cover - validation must finish first
        raise AssertionError("no Meta write is allowed by this test")


def _adapter() -> MetaGraphExecutionAdapter:
    return MetaGraphExecutionAdapter(
        session=_NoNetworkSession(),
        access_token="not-logged",
        policy=MetaGraphWritePolicy(
            enabled=True,
            allowed_account_ids=frozenset({"account-1"}),
        ),
    )


def _payload(max_write_requests: int) -> dict:
    return {
        "account_id": "account-1",
        "action_type": "CREATE_PAUSED_AD",
        "approval": {
            "status": "APPROVED",
            "approval_id": "approval-1",
            "approved_by": "operator:test",
            "approved_at": "2026-08-16T00:00:00+00:00",
        },
        "plan": {
            "reuse_campaign_id": "120250176932910544",
            "max_write_requests": max_write_requests,
            "steps": {
                "ADSET_CREATE": {},
                "IMAGE_UPLOAD": {},
                "CREATIVE_CREATE": {},
                "AD_CREATE": {},
            },
        },
    }


def test_reuse_campaign_rebuild_accepts_exact_four_write_contract() -> None:
    assert _adapter()._validate_approved_payload(_payload(4)) == "account-1"


def test_reuse_campaign_rebuild_rejects_stale_five_write_contract() -> None:
    with pytest.raises(GrowthValidationError, match="meta_write_request_limit_invalid"):
        _adapter()._validate_approved_payload(_payload(5))


def test_creation_incident_ui_has_one_confirmation_and_in_place_progress() -> None:
    workspace = (ROOT / "app/static/ops/growth-workspace.js").read_text()
    page = (ROOT / "app/main_pages.py").read_text()

    assert "function confirmRecoverableCreationIncident" in workspace
    assert "data-growth-order-auto-recover-plan" in workspace
    assert "data-launch-order-auto-recover" in workspace
    assert "id=\"growthConfirmOrderRecovery\">处理创建异常" in workspace
    assert "await openLaunchIncidentResolution(planId,payload,plan)" in workspace
    assert "await loadList({silent:true});await openLaunchBatchWorkflow(resumedPlanId)" in workspace
    assert "confirmation:'CONTINUE_SAME_PLAN'" in workspace
    assert "仅重建未完成步骤" in workspace
    assert "创建后状态" in workspace
    assert "20260816-gle-auto-recovery-loop-v1" in page


def test_creation_incident_entry_does_not_open_the_old_nested_loop() -> None:
    workspace = (ROOT / "app/static/ops/growth-workspace.js").read_text()
    bind_start = workspace.index("function renderExperimentQueue")
    bind_end = workspace.index("function taskGroupsHtml", bind_start)
    binding = workspace[bind_start:bind_end]

    assert "data-growth-order-incident-plan" not in binding
    assert "openLaunchBatchWorkflow(String(button.dataset.growthOrderIncidentPlan" not in binding
    assert "confirmRecoverableCreationIncident(String(button.dataset.growthOrderAutoRecoverPlan" in binding
