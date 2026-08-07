from __future__ import annotations

import hashlib
import sqlite3
import subprocess
import sys
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import pytest

from app.growth.common import canonical_json as db_json, payload_hash
from app.growth.gate0_topology_audit import (
    G004ContractError,
    G004GraphError,
    G004SourceError,
    GetOnlyGraphClient,
    audit_snapshot,
    audit_snapshot_bundle,
    canonical_json,
    hash_json,
    normalize_request,
    open_readonly_snapshot,
)
from app.growth.primary_text_only_compiler import attach_compiler_receipt
from app.growth.schema import ensure_growth_schema


NOW = datetime(2026, 8, 7, 4, 0, tzinfo=timezone.utc)


def _cell(key: str, role: str, message: str) -> dict:
    return {
        "cell_key": key, "experiment_id": f"experiment-{key}",
        "experiment_code": f"EXP-{key}", "role": role,
        "creative_direction": {"key": "points_reward"},
        "audience_strategy": {
            "strategy_key": "BROAD", "label": "Broad", "detailed_targeting": {},
            "meta_targeting_ids": [], "verification_status": "VERIFIED_EMPTY_BY_DEFINITION",
        },
        "allocation_percent": 50, "study_cell_name": f"Study-{key}",
        "frozen_creative_id": "image-1", "copy_version_id": f"copy-{key}",
        "copy_benchmark_version": "benchmark-v1", "copy_hypothesis": f"hypothesis-{key}",
        "asset_sha256": "a" * 64,
        "steps": {
            "IMAGE_UPLOAD": {"image_id": "image-1", "image_path": "/tmp/image.png"},
            "CREATIVE_CREATE": {
                "name": f"Creative-{key}",
                "object_story_spec": {
                    "page_id": "page-1",
                    "link_data": {
                        "link": "https://example.com/app", "message": message,
                        "name": "Same headline", "description": "Same description",
                        "call_to_action": {
                            "type": "INSTALL_MOBILE_APP",
                            "value": {"link": "https://example.com/app"},
                        },
                    },
                },
            },
            "ADSET_CREATE": {
                "name": f"AdSet-{key}", "daily_budget": 2000,
                "optimization_goal": "APP_INSTALLS", "billing_event": "IMPRESSIONS",
                "bid_strategy": "COST_CAP", "bid_amount": 55,
                "targeting": {
                    "geo_locations": {"countries": ["MX"], "location_types": ["home", "recent"]},
                    "genders": [2], "age_min": 18, "age_max": 40, "locales": [23],
                    "app_install_state": "not_installed", "user_os": ["Android_ver_8.0_and_above"],
                    "user_device": ["Android_Smartphone"],
                    "targeting_automation": {"advantage_audience": 0},
                },
                "promoted_object": {
                    "application_id": "app-1", "object_store_url": "https://example.com/app",
                },
                "attribution_spec": [{"event_type": "CLICK_THROUGH", "window_days": 1}],
                "status": "PAUSED",
            },
            "AD_CREATE": {"name": f"Ad-{key}", "status": "PAUSED"},
        },
    }


def _plan() -> dict:
    checked = NOW - timedelta(minutes=10)
    expires = NOW + timedelta(hours=1)
    start = NOW + timedelta(hours=2)
    end = start + timedelta(days=7)
    return {
        "plan_id": "action-create", "plan_version": "NEW_ACCOUNT_COPY_BATCH_V1",
        "launch_id": "launch-1", "experiment_id": "experiment-C1",
        "experiment_ids": ["experiment-C1", "experiment-C2"],
        "experiment_type": "COPY_ONLY", "unique_variable": "PRIMARY_TEXT",
        "action_type": "CREATE_PAUSED_AD", "target_account_id": "account-1",
        "target_object_type": "LAUNCH", "target_object_id": "launch-1",
        "campaign": {
            "name": "Campaign", "objective": "OUTCOME_APP_PROMOTION",
            "buying_type": "AUCTION", "special_ad_categories": [], "status": "PAUSED",
        },
        "cells": [_cell("C1", "BASELINE", "Baseline text"), _cell("C2", "CHALLENGER", "Challenger text")],
        "baseline_experiment_id": "experiment-C1", "test_variable": "copy_variant",
        "sdk_contract_version": "gle-meta-sdk-v1", "copy_benchmark_versions": ["benchmark-v1"],
        "frozen_creative_id": "image-1",
        "study": {
            "business_id": "business-1", "name": "Study", "type": "SPLIT_TEST",
            "start_time": start.isoformat(), "end_time": end.isoformat(),
        },
        "audience_preflight": {
            "preflight_id": "preflight-1", "launch_id": "launch-1",
            "source": "meta_graph_read_only", "status": "VERIFIED",
            "account_id": "account-1", "account_name": "Account 1",
            "business_id": "business-1", "country": "MX", "test_variable": "copy_variant",
            "strategy_keys": ["BROAD", "BROAD"], "targeting_ids": [],
            "delivery_estimates": {"C1": {}, "C2": {}}, "intersection_estimate": {},
            "overlap_ratio": 0.0, "checked_at": checked.isoformat(),
            "expires_at": expires.isoformat(), "start_time": start.isoformat(),
            "end_time": end.isoformat(), "meta_writes_performed": False,
        },
        "invariants": {
            "base_conditions": {
                "country": "MX", "country_label": "Mexico", "gender": "female",
                "gender_label": "Female", "age_min": 18, "age_max": 40,
                "language": "es_419", "language_label": "Spanish",
            },
            "audience_strategy": {
                "strategy_key": "BROAD", "label": "Broad", "detailed_targeting": {},
                "meta_targeting_ids": [], "verification_status": "VERIFIED_EMPTY_BY_DEFINITION",
            },
            "advantage_audience": "DISABLED", "gender_as_suggestion": False,
            "age_as_suggestion": False, "frozen_creative_id": "image-1",
            "single_variable": "copy_variant", "randomization": "META_SPLIT_TEST_REQUIRED",
            "copy_versions": ["copy-C1", "copy-C2"], "budget_mode": "ABO",
            "equal_daily_budget_usd": 20.0, "bid_strategy": "COST_CAP",
            "cost_cap_usd": 0.55, "optimization_goal": "APP_INSTALLS",
            "placement": "ADVANTAGE_PLUS",
            "attribution": "1d_click_1d_view_1d_engaged_video_view",
        },
        "delivery_guardrails": {
            "version": "v1",
            "ctr_floor": {"minimum_impressions": 800, "minimum_ctr": 0.012, "action": "PAUSE_AD"},
            "zero_install_spend": {
                "minimum_attribution_hours": 24, "spend_limit_usd": 1.2,
                "maximum_installs": 0, "action": "PAUSE_AD",
            },
            "high_cpi": {"minimum_installs": 10, "maximum_cpi_usd": 0.55, "action": "PAUSE_AD"},
        },
        "max_write_requests": 10,
        "execution_policy": {
            "live_creation_allowed": True, "blocked_reason": "",
            "required_readback": ["study_id", "cell_ids", "adset_ids", "ads", "strict_targeting"],
        },
        "evaluation_window": {"checkpoints": ["D1", "D3", "D7"]},
        "expires_at": (NOW + timedelta(minutes=50)).isoformat(),
    }


def _request(registry_hash: str, *, activation: bool = False) -> dict:
    return {
        "schema_version": "gle-g0-04-audit-request-v1", "audit_id": "audit-1",
        "requested_at": NOW.isoformat(), "request_nonce": "nonce-1",
        "graph_api_version": "v25.0", "sdk_contract_version": "gle-meta-sdk-v1",
        "topology_contract_version": "gle-g0-04-topology-v1",
        "create_operation_action_id": "action-create",
        "activation_operation_action_id": "action-activate" if activation else "",
        "actor_binding_registry_hash": registry_hash,
        "required_permissions": [
            "ads_management", "ads_read", "business_management", "pages_manage_metadata",
            "pages_read_engagement", "pages_show_list",
        ],
        "required_account_tasks": ["ADVERTISE", "MANAGE"],
        "freshness_policy": {
            "max_run_seconds": 120, "receipt_ttl_seconds": 300,
            "activity_settlement_seconds": 300, "clock_skew_seconds": 60,
            "max_pages": 5, "max_events": 100,
        },
    }


def _registry() -> dict:
    return {
        "schema_version": "gle-g0-04-actor-binding-registry-v1",
        "principals": [{"actor_id": "operator-1", "application_id": "app-system", "roles": ["ACTIVATE"]}],
    }


def _insert_activation_chain(conn: sqlite3.Connection, object_ids: dict) -> None:
    created = (NOW - timedelta(minutes=35)).isoformat()
    approval_created = (NOW - timedelta(minutes=34)).isoformat()
    approved = (NOW - timedelta(minutes=33)).isoformat()
    dry_run_at = (NOW - timedelta(minutes=31)).isoformat()
    consumed = (NOW - timedelta(minutes=30)).isoformat()
    task_created = (NOW - timedelta(minutes=29)).isoformat()
    plan = {
        "plan_id": "action-activate", "plan_version": "NEW_ACCOUNT_DELIVERY_BATCH_V1",
        "action_type": "REACTIVATE_AD", "target_account_id": "account-1",
        "target_object_type": "LAUNCH", "target_object_id": "launch-1",
        "launch_id": "launch-1", "experiment_ids": ["experiment-C1", "experiment-C2"],
        "steps": {
            "CAMPAIGN_STATUS_UPDATE": {
                "target_id": "campaign-1", "object_key": "campaign_id",
                "before_status": "PAUSED", "status": "ACTIVE",
            },
        },
        "cells": [
            {
                "cell_key": f"C{index}", "experiment_id": f"experiment-C{index}",
                "steps": {
                    "ADSET_STATUS_UPDATE": {
                        "target_id": f"adset-{index}", "object_key": f"c{index}_adset_id",
                        "before_status": "PAUSED", "status": "ACTIVE",
                    },
                    "AD_STATUS_UPDATE": {
                        "target_id": f"ad-{index}", "object_key": f"c{index}_ad_id",
                        "before_status": "PAUSED", "status": "ACTIVE",
                    },
                },
            }
            for index in (1, 2)
        ],
        "max_write_requests": 5, "expires_at": (NOW + timedelta(minutes=20)).isoformat(),
    }
    approval = {
        "approval_id": "approval-activate", "status": "APPROVED",
        "approved_by": "operator:reviewer", "approved_at": approved, "consumed_at": consumed,
    }
    conn.execute(
        """INSERT INTO growth_operation_action
        (operation_action_id,decision_id,action_type,action_scope,target_type,target_id,
         payload_json,status,created_by,created_at,updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "action-activate", "decision-activate", "REACTIVATE_AD", "EXPERIMENT", "LAUNCH",
            "launch-1", db_json({"plan": plan}), "VERIFIED", "operator:planner", created, consumed,
        ),
    )
    conn.execute(
        """INSERT INTO growth_operation_approval
        (approval_id,operation_action_id,plan_hash,plan_json,status,proposed_by,approved_by,
         approved_at,expires_at,consumed_at,idempotency_key,request_hash,created_at,updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "approval-activate", "action-activate", payload_hash(plan), db_json(plan), "APPROVED",
            "operator:planner", "operator:reviewer", approved, plan["expires_at"], consumed,
            "approval-activate-key", "approval-activate-request", approval_created, consumed,
        ),
    )
    conn.execute(
        """INSERT INTO meta_execution_task
        (execution_task_id,operation_action_id,idempotency_key,request_hash,status,payload_json,
         meta_object_ids_json,created_at,updated_at,finished_at)
        VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            "task-activate", "action-activate", "task-activate-key", "task-activate-request",
            "SUCCESS", db_json({
                "plan": plan, "approval": approval, "account_id": "account-1",
                "execution_mode": "live",
            }), db_json(object_ids), task_created, task_created, task_created,
        ),
    )
    steps = [
        "CAMPAIGN_STATUS_UPDATE", "C1_ADSET_STATUS_UPDATE", "C1_AD_STATUS_UPDATE",
        "C2_ADSET_STATUS_UPDATE", "C2_AD_STATUS_UPDATE", "VERIFY", "RECEIPT",
    ]
    for index, step in enumerate(steps, start=1):
        result = {"status": "SUCCESS"} if step not in {"VERIFY", "RECEIPT"} else {}
        verification = {"status": "SUCCESS"} if step != "RECEIPT" else {"status": "SUCCESS"}
        if step == "RECEIPT":
            result = {"final_status": "SUCCESS"}
        conn.execute(
            """INSERT INTO meta_execution_task_receipt
            (receipt_id,execution_task_id,step_name,step_status,step_result_json,
             meta_object_ids_json,verification_result_json,created_at)
            VALUES (?,?,?,?,?,?,?,?)""",
            (
                f"activation-receipt-{index:02d}", "task-activate", step,
                "SUCCESS" if step == "RECEIPT" else "VERIFIED",
                db_json(result), db_json(object_ids), db_json(verification),
                (NOW - timedelta(minutes=27) + timedelta(seconds=index)).isoformat(),
            ),
        )
    dry = {
        "plan_id": "action-activate", "status": "DRY_RUN_VERIFIED",
        "execution_mode": "dry_run", "plan_hash": payload_hash(plan),
        "approval_id": "approval-activate", "approved_by": "operator:reviewer",
    }
    conn.execute(
        """INSERT INTO growth_idempotency_record
        (route_key,idempotency_key,request_hash,response_status,response_json,created_at)
        VALUES ('ad_experiment.plan_dry_run','activation-dry-key','activation-dry-request',200,?,?)""",
        (db_json(dry), dry_run_at),
    )


def _database(path: Path, *, include_activation: bool = False) -> str:
    plan = attach_compiler_receipt(_plan())
    preflight = plan["audience_preflight"]
    object_ids = {
        "campaign_id": "campaign-1", "study_id": "study-1",
        "c1_study_cell_id": "cell-1", "c1_adset_id": "adset-1",
        "c1_ad_id": "ad-1", "c1_creative_id": "creative-1", "c1_image_hash": "imagehash-1",
        "c2_study_cell_id": "cell-2", "c2_adset_id": "adset-2",
        "c2_ad_id": "ad-2", "c2_creative_id": "creative-2", "c2_image_hash": "imagehash-2",
    }
    created = (NOW - timedelta(hours=1)).isoformat()
    approved = (NOW - timedelta(minutes=50)).isoformat()
    consumed = (NOW - timedelta(minutes=45)).isoformat()
    approval = {
        "approval_id": "approval-create", "status": "APPROVED",
        "approved_by": "operator:reviewer", "approved_at": approved, "consumed_at": consumed,
    }
    with sqlite3.connect(path) as conn:
        ensure_growth_schema(conn)
        conn.execute(
            """INSERT INTO growth_operation_action
            (operation_action_id,decision_id,action_type,action_scope,target_type,target_id,
             payload_json,status,created_by,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            ("action-create", "decision-1", "CREATE_PAUSED_AD", "EXPERIMENT", "LAUNCH", "launch-1",
             db_json({"plan": plan}), "VERIFIED", "operator:planner", created, consumed),
        )
        conn.execute(
            """INSERT INTO growth_operation_approval
            (approval_id,operation_action_id,plan_hash,plan_json,status,proposed_by,approved_by,
             approved_at,expires_at,consumed_at,idempotency_key,request_hash,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("approval-create", "action-create", payload_hash(plan), db_json(plan), "APPROVED",
             "operator:planner", "operator:reviewer", approved, plan["expires_at"], consumed,
             "approval-key", "approval-request", created, consumed),
        )
        conn.execute(
            """INSERT INTO meta_execution_task
            (execution_task_id,operation_action_id,idempotency_key,request_hash,status,payload_json,
             meta_object_ids_json,created_at,updated_at,finished_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            ("task-create", "action-create", "task-key", "task-request", "SUCCESS",
             db_json({"plan": plan, "approval": approval, "account_id": "account-1", "execution_mode": "live"}),
             db_json(object_ids), consumed, consumed, consumed),
        )
        cumulative = {}
        step_ids = {
            "CAMPAIGN_CREATE": {"campaign_id": "campaign-1"},
            "C1_IMAGE_UPLOAD": {"c1_image_hash": "imagehash-1"},
            "C1_CREATIVE_CREATE": {"c1_creative_id": "creative-1"},
            "C1_ADSET_CREATE": {"c1_adset_id": "adset-1"},
            "C1_AD_CREATE": {"c1_ad_id": "ad-1"},
            "C2_IMAGE_UPLOAD": {"c2_image_hash": "imagehash-2"},
            "C2_CREATIVE_CREATE": {"c2_creative_id": "creative-2"},
            "C2_ADSET_CREATE": {"c2_adset_id": "adset-2"},
            "C2_AD_CREATE": {"c2_ad_id": "ad-2"},
            "STUDY_CREATE": {
                "study_id": "study-1", "c1_study_cell_id": "cell-1",
                "c2_study_cell_id": "cell-2",
            },
        }
        ordered_steps = list(step_ids) + ["VERIFY", "RECEIPT"]
        for index, step in enumerate(ordered_steps, start=1):
            cumulative.update(step_ids.get(step, {}))
            status = "VERIFIED" if step == "VERIFY" else "SUCCESS"
            result = {"status": "SUCCESS"} if step in step_ids else {}
            verification = {"status": "SUCCESS"} if step == "VERIFY" else {}
            if step == "RECEIPT":
                cumulative = dict(object_ids)
                result = {"final_status": "SUCCESS"}
                verification = {"status": "SUCCESS"}
            conn.execute(
                """INSERT INTO meta_execution_task_receipt
                (receipt_id,execution_task_id,step_name,step_status,step_result_json,
                 meta_object_ids_json,verification_result_json,created_at)
                VALUES (?,?,?,?,?,?,?,?)""",
                (
                    f"receipt-{index:02d}", "task-create", step, status, db_json(result),
                    db_json(cumulative), db_json(verification),
                    (NOW - timedelta(minutes=44) + timedelta(seconds=index)).isoformat(),
                ),
            )
        dry = {
            "plan_id": "action-create", "status": "DRY_RUN_VERIFIED", "execution_mode": "dry_run",
            "plan_hash": payload_hash(plan), "approval_id": "approval-create",
            "approved_by": "operator:reviewer",
            "compiler_receipt_hash": plan["compiler_receipt"]["receipt_hash"],
        }
        conn.execute(
            """INSERT INTO growth_idempotency_record
            (route_key,idempotency_key,request_hash,response_status,response_json,created_at)
            VALUES ('ad_experiment.plan_dry_run','dry-key','dry-request',200,?,?)""",
            (db_json(dry), consumed),
        )
        conn.execute(
            """INSERT INTO ad_audience_preflight
            (preflight_id,launch_id,account_id,business_id,country,strategy_keys_json,
             evidence_json,evidence_hash,status,checked_at,expires_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                preflight["preflight_id"], preflight["launch_id"], preflight["account_id"],
                preflight["business_id"], preflight["country"], db_json(preflight["strategy_keys"]),
                db_json(preflight), payload_hash(preflight), "VERIFIED",
                preflight["checked_at"], preflight["expires_at"],
            ),
        )
        if include_activation:
            _insert_activation_chain(conn, object_ids)
        conn.commit()
    return hashlib.sha256(path.read_bytes()).hexdigest()


class _Response:
    history = []

    def __init__(self, body: dict, status: int = 200) -> None:
        self.body = deepcopy(body)
        self.status_code = status

    def json(self) -> dict:
        return deepcopy(self.body)


class _Session:
    def __init__(
        self, *, active: bool = False, cursor_loop: bool = False,
        overrides: Optional[dict] = None, drift_path: str = "",
    ) -> None:
        self.active = active
        self.cursor_loop = cursor_loop
        self.overrides = overrides or {}
        self.drift_path = drift_path
        self.get_calls = []
        self.non_get_calls = 0

    def post(self, *args, **kwargs):
        self.non_get_calls += 1
        raise AssertionError("POST forbidden")

    def get(self, url, *, params, timeout, allow_redirects):
        assert allow_redirects is False
        assert params["access_token"] == "secret-token"
        path = url.split("/v25.0/", 1)[1]
        self.get_calls.append(path)
        if path in self.overrides:
            return _Response(self.overrides[path])
        if self.cursor_loop and path == "me/permissions":
            return _Response({
                "data": [{"permission": "ads_read", "status": "granted"}],
                "paging": {"next": "https://invalid", "cursors": {"after": "same"}},
            })
        body = self._body(path)
        if path == self.drift_path and self.get_calls.count(path) > 1:
            body["updated_time"] = (NOW - timedelta(hours=1)).isoformat()
        return _Response(body)

    def _body(self, path: str) -> dict:
        old = (NOW - timedelta(hours=2)).isoformat()
        status = "ACTIVE" if self.active else "PAUSED"
        edges = {
            "me/permissions": {"data": [
                {"permission": value, "status": "granted"}
                for value in (
                    "ads_management", "ads_read", "business_management", "pages_manage_metadata",
                    "pages_read_engagement", "pages_show_list",
                )
            ]},
            "act_account-1/assigned_users": {"data": [{"id": "operator-1", "tasks": ["ADVERTISE", "MANAGE"]}]},
            "business-1/owned_ad_accounts": {"data": [{"id": "act_account-1"}]},
            "business-1/client_ad_accounts": {"data": []},
            "business-1/owned_pages": {"data": [{"id": "page-1"}]},
            "business-1/client_pages": {"data": []},
            "business-1/owned_apps": {"data": [{"id": "app-1"}]},
            "business-1/client_apps": {"data": []},
            "business-1/system_users": {"data": [{"id": "operator-1"}]},
            "page-1/assigned_users": {"data": [{"id": "operator-1", "tasks": ["ADVERTISE", "CREATE_CONTENT"]}]},
            "app-1/roles": {"data": [{"user": {"id": "operator-1"}, "role": "ADMINISTRATOR"}]},
            "study-1/cells": {"data": [
                {
                    "id": "cell-1", "name": "Study-C1", "treatment_percentage": 50,
                    "control_percentage": 0, "ad_entities_count": 1, "ad_ids": ["ad-1"],
                },
                {
                    "id": "cell-2", "name": "Study-C2", "treatment_percentage": 50,
                    "control_percentage": 0, "ad_entities_count": 1, "ad_ids": ["ad-2"],
                },
            ]},
            "study-1/objectives": {"data": [{"id": "objective-1", "type": "COST_PER_ACTION"}]},
            "cell-1/adsets": {"data": [{"id": "adset-1"}]},
            "cell-2/adsets": {"data": [{"id": "adset-2"}]},
            "cell-1/campaigns": {"data": [{"id": "campaign-1"}]},
            "cell-2/campaigns": {"data": [{"id": "campaign-1"}]},
            "cell-1/adaccounts": {"data": [{"id": "act_account-1"}]},
            "cell-2/adaccounts": {"data": [{"id": "act_account-1"}]},
        }
        if path in edges:
            return edges[path]
        if path == "act_account-1/activities":
            if not self.active:
                return {"data": []}
            return {"data": [
                {
                    "id": f"activity-{index}", "object_id": object_id,
                    "event_time": (NOW - timedelta(minutes=30)).isoformat(),
                    "changed_data": {"old_value": "PAUSED", "new_value": "ACTIVE"},
                    "actor_id": "external-human", "application_id": "external-app",
                }
                for index, object_id in enumerate(("campaign-1", "adset-1", "adset-2"), 1)
            ]}
        nodes = {
            "debug_token": {"data": {
                "is_valid": True, "app_id": "app-system", "user_id": "operator-1", "type": "USER",
                "scopes": [
                    "ads_management", "ads_read", "business_management", "pages_manage_metadata",
                    "pages_read_engagement", "pages_show_list",
                ],
                "expires_at": int((NOW + timedelta(hours=2)).timestamp()),
                "data_access_expires_at": int((NOW + timedelta(hours=2)).timestamp()),
            }},
            "me": {"id": "operator-1", "name": "Operator"},
            "act_account-1": {
                "id": "act_account-1", "account_id": "account-1", "account_status": 1,
                "business": {"id": "business-1"}, "user_tasks": ["ADVERTISE", "MANAGE"],
                "ad_account_promotable_objects": {"application_id": "app-1"},
                "updated_time": old,
            },
            "business-1": {"id": "business-1", "updated_time": old},
            "page-1": {"id": "page-1", "is_published": True},
            "app-1": {"id": "app-1"},
            "study-1": {
                "id": "study-1", "name": "Study", "type": "SPLIT_TEST",
                "start_time": _plan()["study"]["start_time"],
                "end_time": _plan()["study"]["end_time"], "updated_time": old,
            },
            "campaign-1": {
                "id": "campaign-1", "account_id": "account-1", "name": "Campaign",
                "objective": "OUTCOME_APP_PROMOTION", "buying_type": "AUCTION",
                "special_ad_categories": [], "status": status,
                "effective_status": status, "updated_time": old,
            },
        }
        for index in (1, 2):
            planned = _cell(
                f"C{index}", "BASELINE" if index == 1 else "CHALLENGER",
                "Baseline text" if index == 1 else "Challenger text",
            )
            adset = deepcopy(planned["steps"]["ADSET_CREATE"])
            adset.update({
                "id": f"adset-{index}", "account_id": "account-1",
                "campaign_id": "campaign-1", "status": status,
                "effective_status": status, "updated_time": old,
            })
            nodes[f"adset-{index}"] = adset
            ad = deepcopy(planned["steps"]["AD_CREATE"])
            ad.update({
                "id": f"ad-{index}", "account_id": "account-1", "campaign_id": "campaign-1",
                "adset_id": f"adset-{index}", "creative": {"id": f"creative-{index}"},
                "status": status, "effective_status": status, "updated_time": old,
            })
            nodes[f"ad-{index}"] = ad
            creative = deepcopy(planned["steps"]["CREATIVE_CREATE"])
            creative["object_story_spec"]["link_data"]["image_hash"] = f"imagehash-{index}"
            creative.update({
                "id": f"creative-{index}", "account_id": "account-1", "updated_time": old,
            })
            nodes[f"creative-{index}"] = creative
        return nodes[path]


def _run(tmp_path: Path, *, active: bool = False):
    db = tmp_path / "source.db"
    digest = _database(db)
    registry = _registry()
    session = _Session(active=active)
    receipt = audit_snapshot(
        request=_request(hash_json(registry)), actor_registry=registry, db_path=db,
        expected_db_sha256=digest, session=session, access_token="secret-token", now=NOW,
    )
    return receipt, session


def _run_with_session(tmp_path: Path, session: _Session):
    tmp_path.mkdir(parents=True, exist_ok=True)
    db = tmp_path / "source.db"
    digest = _database(db)
    registry = _registry()
    return audit_snapshot(
        request=_request(hash_json(registry)), actor_registry=registry, db_path=db,
        expected_db_sha256=digest, session=session, access_token="secret-token", now=NOW,
    )


def _governed_activity_events(*, event_time: datetime) -> dict:
    return {"data": [
        {
            "id": f"activity-{index}", "object_id": object_id,
            "event_time": event_time.isoformat(),
            "changed_data": {"old_value": "PAUSED", "new_value": "ACTIVE"},
            "actor_id": "operator-1", "application_id": "app-system",
        }
        for index, object_id in enumerate(
            ("campaign-1", "adset-1", "ad-1", "adset-2", "ad-2"), 1,
        )
    ]}


def _run_governed_active(tmp_path: Path, *, event_time: datetime):
    db = tmp_path / "source.db"
    digest = _database(db, include_activation=True)
    registry = _registry()
    session = _Session(
        active=True,
        overrides={"act_account-1/activities": _governed_activity_events(event_time=event_time)},
    )
    receipt = audit_snapshot(
        request=_request(hash_json(registry), activation=True), actor_registry=registry,
        db_path=db, expected_db_sha256=digest, session=session,
        access_token="secret-token", now=NOW,
    )
    return receipt, session


def test_clean_paused_topology_emits_bounded_evidence_fragment(tmp_path: Path) -> None:
    receipt, session = _run(tmp_path)
    assert receipt["outcome"] == "PASS"
    assert receipt["gate0_fragment"] == "PERMISSION_TOPOLOGY_PROVEN"
    assert receipt["gate0_result_ceiling"] == "QUASI_ONLY"
    assert receipt["not_gate_receipt"] is True
    assert receipt["attestation_status"] == "PENDING_ATTESTATION"
    assert receipt["transport_proof"]["allowed_methods"] == ["GET"]
    assert receipt["transport_proof"]["meta_object_writes"] == 0
    assert receipt["transport_proof"]["local_db_writes"] == 0
    assert session.non_get_calls == 0
    assert "secret-token" not in canonical_json(receipt)
    unsigned = dict(receipt)
    body_hash = unsigned.pop("receipt_body_hash")
    assert hash_json(unsigned) == body_hash


def test_redacted_evidence_bundle_is_hash_bound_and_contains_no_copy(tmp_path: Path) -> None:
    db = tmp_path / "source.db"
    digest = _database(db)
    registry = _registry()
    result = audit_snapshot_bundle(
        request=_request(hash_json(registry)), actor_registry=registry, db_path=db,
        expected_db_sha256=digest, session=_Session(), access_token="secret-token", now=NOW,
    )
    bundle = dict(result["evidence_bundle"])
    bundle_hash = bundle.pop("evidence_bundle_hash")
    assert hash_json(bundle) == bundle_hash
    assert result["receipt"]["evidence_bundle_hash"] == bundle_hash
    serialized = canonical_json(result)
    assert "Baseline text" not in serialized
    assert "Challenger text" not in serialized
    assert "secret-token" not in serialized


def test_external_manual_activation_is_polluted_not_retroactively_approved(tmp_path: Path) -> None:
    receipt, _ = _run(tmp_path, active=True)
    assert receipt["outcome"] == "POLLUTED"
    assert receipt["gate0_fragment"] == "INELIGIBLE"
    assert "EXTERNAL_ACTIVATION_DETECTED" in receipt["blocking_reasons"]
    assert receipt["checks"]["activation_provenance"]["status"] == "POLLUTED"
    assert receipt["checks"]["topology"]["status"] == "PASS"


def test_governed_active_topology_passes_but_requires_detached_attestation(tmp_path: Path) -> None:
    receipt, session = _run_governed_active(
        tmp_path, event_time=NOW - timedelta(minutes=28),
    )
    assert receipt["outcome"] == "INCOMPLETE"
    assert receipt["checks"]["topology"]["status"] == "PASS"
    assert receipt["checks"]["activation_provenance"]["status"] == "INCOMPLETE"
    assert receipt["checks"]["activation_provenance"]["reason_codes"] == ["RECEIPT_UNSIGNED"]
    assert session.non_get_calls == 0


def test_activation_event_before_task_cannot_borrow_later_receipts(tmp_path: Path) -> None:
    receipt, _ = _run_governed_active(
        tmp_path, event_time=NOW - timedelta(minutes=32),
    )
    activation = receipt["checks"]["activation_provenance"]
    assert activation["status"] == "INCOMPLETE"
    assert "ACTIVATION_OUTSIDE_APPROVAL_TTL" in activation["reason_codes"]


def test_receipt_is_deterministic_for_identical_snapshot(tmp_path: Path) -> None:
    db = tmp_path / "source.db"
    digest = _database(db)
    registry = _registry()
    request = _request(hash_json(registry))
    one = audit_snapshot(
        request=request, actor_registry=registry, db_path=db, expected_db_sha256=digest,
        session=_Session(), access_token="secret-token", now=NOW,
    )
    two = audit_snapshot(
        request=request, actor_registry=registry, db_path=db, expected_db_sha256=digest,
        session=_Session(), access_token="secret-token", now=NOW,
    )
    assert one == two


def test_unknown_request_key_and_unsorted_permission_fail_closed() -> None:
    registry = _registry()
    request = _request(hash_json(registry))
    request["unknown"] = True
    with pytest.raises(G004ContractError, match="G004_INPUT_SCHEMA_INVALID"):
        normalize_request(request)
    request.pop("unknown")
    request["required_permissions"] = list(reversed(request["required_permissions"]))
    with pytest.raises(G004ContractError, match="G004_INPUT_SCHEMA_INVALID"):
        normalize_request(request)


def test_exact_endpoint_allowlist_and_cursor_loop_fail_closed() -> None:
    client = GetOnlyGraphClient(
        session=_Session(), access_token="secret-token", now=NOW, allowed_paths={"me"},
    )
    with pytest.raises(G004GraphError, match="ENDPOINT_NOT_ALLOWLISTED"):
        client.get("other", fields="id")
    looping = GetOnlyGraphClient(
        session=_Session(cursor_loop=True), access_token="secret-token", now=NOW,
        allowed_paths={"me/permissions"}, max_pages=3,
    )
    with pytest.raises(G004GraphError, match="CURSOR_LOOP"):
        looping.get_edge("me/permissions", fields="permission,status")


@pytest.mark.parametrize(
    "path,body,expected",
    [
        (
            "me/permissions",
            {"data": [{"permission": "ads_read", "status": "granted"}]},
            "TOKEN_SCOPE_MISSING",
        ),
        ("business-1/owned_pages", {"data": []}, "PAGE_OWNERSHIP_MISSING"),
        ("page-1", {"id": "page-1", "is_published": False}, "PAGE_TASK_MISSING"),
        ("app-1/roles", {"data": []}, "ACTOR_PROVENANCE_UNRESOLVED"),
        ("app-1/roles", {"data": [{"user": {"id": "operator-1"}, "role": "TESTER"}]}, "ACTOR_PROVENANCE_UNRESOLVED"),
    ],
)
def test_permission_and_ownership_breaks_are_incomplete(
    tmp_path: Path, path: str, body: dict, expected: str,
) -> None:
    receipt = _run_with_session(tmp_path, _Session(overrides={path: body}))
    assert receipt["outcome"] == "INCOMPLETE"
    assert expected in receipt["blocking_reasons"]


def test_unrelated_assignment_rows_cannot_be_spliced_into_one_principal(tmp_path: Path) -> None:
    receipt = _run_with_session(tmp_path, _Session(overrides={
        "act_account-1/assigned_users": {"data": [{"id": "account-user", "tasks": ["ADVERTISE", "MANAGE"]}]},
        "page-1/assigned_users": {"data": [{"id": "page-user", "tasks": ["ADVERTISE"]}]},
        "app-1/roles": {"data": [{"user": {"id": "app-user"}, "role": "ADMINISTRATOR"}]},
    }))
    assert receipt["outcome"] == "INCOMPLETE"
    assert "ACTOR_PROVENANCE_UNRESOLVED" in receipt["blocking_reasons"]


@pytest.mark.parametrize(
    "path,body,expected",
    [
        ("business-1", {"error": {"code": 100}}, "GRAPH_READ_FAILED"),
        ("study-1/objectives", {"error": {"code": 100}}, "GRAPH_READ_FAILED"),
        ("study-1/objectives", {"data": [{"id": "objective-1", "type": "WRONG"}]}, "STUDY_PLAN_UNBOUND"),
        (
            "act_account-1",
            {
                "id": "act_account-1", "account_id": "account-1", "account_status": 1,
                "business": {"id": "business-1"}, "user_tasks": ["ADVERTISE", "MANAGE"],
                "ad_account_promotable_objects": {"application_id": "other-app"},
                "updated_time": (NOW - timedelta(hours=2)).isoformat(),
            },
            "PROMOTED_OBJECT_MISMATCH",
        ),
    ],
)
def test_required_graph_call_and_capability_semantics_fail_closed(
    tmp_path: Path, path: str, body: dict, expected: str,
) -> None:
    receipt = _run_with_session(tmp_path, _Session(overrides={path: body}))
    assert receipt["outcome"] in {"INCOMPLETE", "FAIL"}
    assert expected in receipt["blocking_reasons"]


def test_hidden_extra_cell_and_object_drift_fail_topology(tmp_path: Path) -> None:
    extra = _Session(overrides={
        "study-1/cells": {"data": [
            {"id": "cell-1", "treatment_percentage": 50},
            {"id": "cell-2", "treatment_percentage": 50},
            {"id": "cell-hidden", "treatment_percentage": 1},
        ]},
    })
    receipt = _run_with_session(tmp_path, extra)
    assert receipt["checks"]["topology"]["status"] == "FAIL"
    assert "CELL_SET_MISMATCH" in receipt["blocking_reasons"]

    drifted = _run_with_session(tmp_path / "drift", _Session(drift_path="campaign-1"))
    assert drifted["checks"]["topology"]["status"] == "FAIL"
    assert "OBJECT_DRIFT_DURING_AUDIT" in drifted["blocking_reasons"]


@pytest.mark.parametrize(
    "path,mutate",
    [
        ("study-1/cells", lambda value: value["data"][0].update({"ad_ids": ["wrong-ad"], "ad_entities_count": 9})),
        ("study-1", lambda value: value.update({"start_time": (NOW + timedelta(days=1)).isoformat()})),
        ("campaign-1", lambda value: value.update({"objective": "OUTCOME_TRAFFIC"})),
        ("adset-1", lambda value: value.update({"daily_budget": 9999})),
        ("adset-1", lambda value: value["targeting"]["geo_locations"].update({"countries": ["BR"]})),
        ("creative-1", lambda value: value["object_story_spec"]["link_data"].update({"image_hash": "wrong"})),
        ("creative-1", lambda value: value["object_story_spec"]["link_data"].update({"link": "https://other.invalid"})),
    ],
)
def test_frozen_plan_projection_mutations_fail_topology(
    tmp_path: Path, path: str, mutate,
) -> None:
    body = _Session()._body(path)
    mutate(body)
    receipt = _run_with_session(tmp_path, _Session(overrides={path: body}))
    assert receipt["checks"]["topology"]["status"] == "FAIL"
    assert "LEGACY_STUDY_INADMISSIBLE" in receipt["blocking_reasons"] or "CELL_OBJECT_BINDING_MISMATCH" in receipt["blocking_reasons"]


def test_token_principal_and_expiry_fail_closed(tmp_path: Path) -> None:
    debug = {"data": {
        "is_valid": True, "app_id": "app-system", "user_id": "other-user", "type": "USER",
        "scopes": [
            "ads_management", "ads_read", "business_management", "pages_manage_metadata",
            "pages_read_engagement", "pages_show_list",
        ],
        "expires_at": int((NOW + timedelta(seconds=60)).timestamp()),
        "data_access_expires_at": int((NOW + timedelta(seconds=60)).timestamp()),
    }}
    receipt = _run_with_session(tmp_path, _Session(overrides={"debug_token": debug}))
    assert receipt["outcome"] == "INCOMPLETE"
    assert "TOKEN_PRINCIPAL_MISMATCH" in receipt["blocking_reasons"]
    assert "TOKEN_EXPIRED" in receipt["blocking_reasons"]


def test_list_shaped_external_activation_and_unknown_activity_fail_closed(tmp_path: Path) -> None:
    external = {
        "data": [{
            "id": "activity-list", "object_id": "campaign-1",
            "event_time": (NOW - timedelta(minutes=30)).isoformat(),
            "changed_data": [{
                "field": "status", "old_value": "PAUSED", "new_value": "ACTIVE",
            }],
            "actor_id": "external-human", "application_id": "external-app",
        }],
    }
    polluted = _run_with_session(
        tmp_path / "external", _Session(overrides={"act_account-1/activities": external}),
    )
    assert polluted["checks"]["activation_provenance"]["status"] == "POLLUTED"

    unknown = {"data": [{
        "id": "activity-unknown", "object_id": "campaign-1",
        "event_time": (NOW - timedelta(minutes=30)).isoformat(),
        "changed_data": {"future_schema": {"before": "PAUSED", "after": "ACTIVE"}},
        "actor_id": "operator-1", "application_id": "app-system",
    }]}
    incomplete = _run_with_session(
        tmp_path / "unknown", _Session(overrides={"act_account-1/activities": unknown}),
    )
    assert incomplete["checks"]["activation_provenance"]["status"] == "INCOMPLETE"
    assert "ACTOR_PROVENANCE_UNRESOLVED" in incomplete["blocking_reasons"]

    legal = deepcopy(external["data"][0])
    legal.update({"id": "activity-legal", "actor_id": "operator-1", "application_id": "app-system"})
    mixed = _run_with_session(
        tmp_path / "mixed",
        _Session(overrides={"act_account-1/activities": {"data": [legal, external["data"][0]]}}),
    )
    assert mixed["checks"]["activation_provenance"]["status"] == "POLLUTED"


def test_preflight_hash_mismatch_blocks_local_plan_binding(tmp_path: Path) -> None:
    db = tmp_path / "source.db"
    _database(db)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE ad_audience_preflight SET evidence_hash=?", ("0" * 64,))
        conn.commit()
    digest = hashlib.sha256(db.read_bytes()).hexdigest()
    registry = _registry()
    receipt = audit_snapshot(
        request=_request(hash_json(registry)), actor_registry=registry, db_path=db,
        expected_db_sha256=digest, session=_Session(), access_token="secret-token", now=NOW,
    )
    assert receipt["checks"]["plan_binding"]["status"] == "FAIL"
    assert "EVIDENCE_HASH_MISMATCH" in receipt["blocking_reasons"]


@pytest.mark.parametrize(
    "statement",
    [
        "DELETE FROM meta_execution_task_receipt WHERE step_name='C1_AD_CREATE'",
        "UPDATE meta_execution_task_receipt SET step_status='UNKNOWN' WHERE step_name='C1_AD_CREATE'",
        "UPDATE meta_execution_task_receipt SET meta_object_ids_json='{}' WHERE step_name='C1_AD_CREATE'",
        "UPDATE meta_execution_task_receipt SET verification_result_json='{}' WHERE step_name='VERIFY'",
        "UPDATE meta_execution_task_receipt SET created_at='2000-01-01T00:00:00+00:00' WHERE step_name='RECEIPT'",
    ],
)
def test_incomplete_or_unordered_worker_receipts_fail_local_chain(tmp_path: Path, statement: str) -> None:
    db = tmp_path / "source.db"
    _database(db)
    with sqlite3.connect(db) as conn:
        conn.execute(statement)
        conn.commit()
    registry = _registry()
    receipt = audit_snapshot(
        request=_request(hash_json(registry)), actor_registry=registry, db_path=db,
        expected_db_sha256=hashlib.sha256(db.read_bytes()).hexdigest(),
        session=_Session(), access_token="secret-token", now=NOW,
    )
    assert receipt["checks"]["plan_binding"]["status"] == "FAIL"
    assert "STUDY_PLAN_UNBOUND" in receipt["blocking_reasons"]


def test_actor_registry_hash_is_exact() -> None:
    registry = _registry()
    request = _request("0" * 64)
    with pytest.raises(G004ContractError, match="REQUEST_HASH_MISMATCH"):
        from app.growth.gate0_topology_audit import normalize_actor_registry

        normalize_actor_registry(registry, request["actor_binding_registry_hash"])


def test_database_hash_and_sidecar_are_fail_closed(tmp_path: Path) -> None:
    db = tmp_path / "source.db"
    digest = _database(db)
    registry = _registry()
    with pytest.raises(G004SourceError, match="G004_SOURCE_HASH_MISMATCH"):
        audit_snapshot(
            request=_request(hash_json(registry)), actor_registry=registry, db_path=db,
            expected_db_sha256="0" * 64, session=_Session(), access_token="secret-token", now=NOW,
        )
    Path(f"{db}-wal").write_bytes(b"not-empty")
    with pytest.raises(G004SourceError, match="G004_SOURCE_SIDECAR_PRESENT"):
        audit_snapshot(
            request=_request(hash_json(registry)), actor_registry=registry, db_path=db,
            expected_db_sha256=digest, session=_Session(), access_token="secret-token", now=NOW,
        )


def test_query_only_connection_rejects_write(tmp_path: Path) -> None:
    db = tmp_path / "source.db"
    _database(db)
    with open_readonly_snapshot(db) as conn:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("UPDATE growth_operation_action SET status='FAILED'")


def test_cli_requires_explicit_read_only_execution(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable, "scripts/audit_gle_gate0_topology.py",
            "--request", str(tmp_path / "request.json"),
            "--actor-registry", str(tmp_path / "registry.json"),
            "--database", str(tmp_path / "source.db"),
            "--database-sha256", "0" * 64,
            "--output", str(tmp_path / "receipt.json"),
            "--evidence-output", str(tmp_path / "evidence.json"),
            "--manifest-output", str(tmp_path / "manifest.json"),
        ],
        cwd=Path(__file__).resolve().parents[1], text=True, capture_output=True, check=False,
    )
    assert result.returncode == 64
    assert result.stderr.strip() == "G004_INPUT_SCHEMA_INVALID"
    assert "access_token" not in result.stderr.lower()
