from __future__ import annotations

import sqlite3
from typing import Any, Dict, Iterable, List, Optional

from app.growth.common import canonical_json, decode_json, new_id, utc_now
from app.growth.delivery_guardrails import evaluate_delivery_stop_loss
from app.growth.errors import GrowthNotFound, GrowthValidationError
from app.growth.schema import ensure_growth_schema


AUTONOMY_LEVELS = (
    "L0_OBSERVE",
    "L1_RECOMMEND",
    "L2_PAUSED_CREATE",
    "L3_BOUNDED_LIVE",
)

UNIFIED_ACTIONS = (
    "OBSERVE",
    "CHECK_DATA",
    "CREATE_NEXT_TEST",
    "ADD_PAUSED_ADSET",
    "COPY_SCALE",
    "REPLACE_CREATIVE",
    "COPY_TEST",
    "INCREASE_BUDGET",
    "DECREASE_BUDGET",
    "PAUSE_AD",
    "REACTIVATE_AD",
)

LOCAL_ACTIONS = frozenset({"OBSERVE", "CHECK_DATA"})
PAUSED_CREATE_ACTIONS = frozenset({"CREATE_NEXT_TEST", "ADD_PAUSED_ADSET", "COPY_TEST"})
LIVE_ACTIONS = frozenset({
    "COPY_SCALE", "REPLACE_CREATIVE", "INCREASE_BUDGET", "DECREASE_BUDGET",
    "PAUSE_AD", "REACTIVATE_AD",
})


class GrowthAutonomyService:
    """Account-scoped delegation and evaluator-to-next-action state machine.

    This service only records policy decisions and operator-facing next actions.
    It never calls Meta and never creates an execution task.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        ensure_growth_schema(conn)

    def get_policy(self, account_id: str) -> Dict[str, Any]:
        normalized = self._account_id(account_id)
        row = self.conn.execute(
            "SELECT * FROM growth_autonomy_policy WHERE account_id=?", (normalized,),
        ).fetchone()
        if not row:
            return {
                "account_id": normalized,
                "level": "L0_OBSERVE",
                "allowed_action_types": ["OBSERVE", "CHECK_DATA"],
                "max_daily_budget_usd": 0.0,
                "max_budget_change_pct": 0.0,
                "minimum_installs": 100,
                "minimum_real_joins": 10,
                "require_real_join_attribution": True,
                "status": "DEFAULT",
                "reason": "尚未授权该账户执行广告写入",
            }
        result = dict(row)
        result["allowed_action_types"] = decode_json(result.pop("allowed_action_types_json"), [])
        result["require_real_join_attribution"] = bool(result["require_real_join_attribution"])
        return result

    def set_policy(
        self, account_id: str, *, level: str, allowed_action_types: Iterable[str],
        actor: str, reason: str, max_daily_budget_usd: float = 0,
        max_budget_change_pct: float = 0, minimum_installs: int = 100,
        minimum_real_joins: int = 10, require_real_join_attribution: bool = True,
    ) -> Dict[str, Any]:
        normalized = self._account_id(account_id)
        normalized_level = str(level or "").strip().upper()
        if normalized_level not in AUTONOMY_LEVELS:
            raise GrowthValidationError("invalid_autonomy_level")
        actions = sorted({str(item or "").strip().upper() for item in allowed_action_types if str(item or "").strip()})
        if any(action not in UNIFIED_ACTIONS for action in actions):
            raise GrowthValidationError("invalid_autonomy_action")
        permitted = set(LOCAL_ACTIONS)
        if normalized_level in {"L1_RECOMMEND", "L2_PAUSED_CREATE", "L3_BOUNDED_LIVE"}:
            permitted.update(UNIFIED_ACTIONS)
        if normalized_level == "L0_OBSERVE" and not set(actions).issubset(LOCAL_ACTIONS):
            raise GrowthValidationError("observe_level_cannot_authorize_meta_action")
        if normalized_level == "L1_RECOMMEND" and set(actions) & (PAUSED_CREATE_ACTIONS | LIVE_ACTIONS):
            raise GrowthValidationError("recommend_level_cannot_authorize_meta_write")
        if normalized_level == "L2_PAUSED_CREATE" and set(actions) & LIVE_ACTIONS:
            raise GrowthValidationError("paused_create_level_cannot_authorize_live_action")
        if not set(actions).issubset(permitted):
            raise GrowthValidationError("autonomy_action_not_permitted")
        if float(max_daily_budget_usd or 0) < 0:
            raise GrowthValidationError("invalid_max_daily_budget")
        if not 0 <= float(max_budget_change_pct or 0) <= 100:
            raise GrowthValidationError("invalid_max_budget_change_pct")
        if int(minimum_installs or 0) < 0 or int(minimum_real_joins or 0) < 0:
            raise GrowthValidationError("invalid_autonomy_maturity_threshold")
        now = utc_now()
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO growth_autonomy_policy
                (account_id,level,allowed_action_types_json,max_daily_budget_usd,
                 max_budget_change_pct,minimum_installs,minimum_real_joins,
                 require_real_join_attribution,status,reason,updated_by,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?, 'ACTIVE',?,?,?,?)
                ON CONFLICT(account_id) DO UPDATE SET
                  level=excluded.level,
                  allowed_action_types_json=excluded.allowed_action_types_json,
                  max_daily_budget_usd=excluded.max_daily_budget_usd,
                  max_budget_change_pct=excluded.max_budget_change_pct,
                  minimum_installs=excluded.minimum_installs,
                  minimum_real_joins=excluded.minimum_real_joins,
                  require_real_join_attribution=excluded.require_real_join_attribution,
                  status='ACTIVE',reason=excluded.reason,updated_by=excluded.updated_by,
                  updated_at=excluded.updated_at
                """,
                (
                    normalized, normalized_level, canonical_json(actions),
                    float(max_daily_budget_usd or 0), float(max_budget_change_pct or 0),
                    int(minimum_installs or 0), int(minimum_real_joins or 0),
                    1 if require_real_join_attribution else 0, str(reason or "").strip(),
                    str(actor or "").strip(), now, now,
                ),
            )
        return self.get_policy(normalized)

    def capability_catalog(self, account_id: str) -> Dict[str, Any]:
        policy = self.get_policy(account_id)
        allowed = set(policy["allowed_action_types"])
        groups = [
            ("扩大投放", ["ADD_PAUSED_ADSET", "COPY_SCALE", "INCREASE_BUDGET"]),
            ("优化素材与文案", ["REPLACE_CREATIVE", "COPY_TEST", "CREATE_NEXT_TEST"]),
            ("控制风险", ["DECREASE_BUDGET", "PAUSE_AD", "REACTIVATE_AD"]),
        ]
        return {
            "account_id": policy["account_id"],
            "policy": policy,
            "groups": [
                {
                    "label": label,
                    "actions": [
                        {
                            "action_type": action,
                            "authorized": action in allowed,
                            "requires_approval": action not in LOCAL_ACTIONS,
                        }
                        for action in actions
                    ],
                }
                for label, actions in groups
            ],
            "meta_writes_performed": False,
        }

    def sync_evaluations(self, *, account_id: str = "") -> Dict[str, Any]:
        normalized = self._account_id(account_id) if account_id else ""
        created: List[Dict[str, Any]] = []
        for row in self.conn.execute(
            "SELECT * FROM ad_creative_group_evaluation ORDER BY evaluated_at,group_evaluation_id"
        ).fetchall():
            context = self._launch_context(str(row["launch_id"] or ""))
            if not self._syncable_context(context, normalized):
                continue
            stop_actions = self._sync_group_stop_actions(dict(row), context)
            if stop_actions:
                created.extend(stop_actions)
                continue
            created_item = self._sync_group_evaluation(dict(row), context)
            if created_item:
                created.append(created_item)
        for row in self.conn.execute(
            "SELECT * FROM ad_experiment_evaluation ORDER BY evaluated_at,evaluation_id"
        ).fetchall():
            context = self._experiment_context(str(row["experiment_id"] or ""))
            if not self._syncable_context(context, normalized):
                continue
            created_item = self._sync_single_evaluation(dict(row), context)
            if created_item:
                created.append(created_item)
        for row in self.conn.execute(
            "SELECT * FROM ad_audience_pair_evaluation ORDER BY evaluated_at,pair_evaluation_id"
        ).fetchall():
            context = self._launch_context(str(row["launch_id"] or ""))
            if not self._syncable_context(context, normalized):
                continue
            created_item = self._sync_audience_evaluation(dict(row), context)
            if created_item:
                created.append(created_item)
        return {"created": created, "count": len(created), "meta_writes_performed": False}

    def list_next_actions(
        self, *, account_id: str = "", status: str = "", limit: int = 100,
    ) -> Dict[str, Any]:
        clauses: List[str] = []
        params: List[Any] = []
        if account_id:
            clauses.append("account_id=?")
            params.append(self._account_id(account_id))
        if status:
            clauses.append("status=?")
            params.append(str(status).strip().upper())
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(int(limit or 100), 500)))
        rows = self.conn.execute(
            f"SELECT * FROM growth_next_action{where} ORDER BY created_at DESC LIMIT ?", params,
        ).fetchall()
        return {"items": [self._serialize_action(row) for row in rows], "count": len(rows)}

    def _sync_group_evaluation(self, row: Dict[str, Any], context: Dict[str, str]) -> Optional[Dict[str, Any]]:
        checkpoint = str(row["checkpoint"])
        status = str(row["decision_status"])
        quality = str(row["data_quality_status"])
        metrics = decode_json(row["metrics_by_experiment_json"], {})
        if quality != "PASS" or status == "DATA_INCOMPLETE":
            action, summary = "CHECK_DATA", "数据不完整，先修复归因或数据同步"
        elif checkpoint in {"D1", "D3"}:
            action, summary = "OBSERVE", f"{checkpoint} 数据已更新，继续观察到下一检查点"
        elif status == "WINNER":
            action, summary = "COPY_SCALE", "已有胜出组，建议复制扩量并保留原组"
        elif status in {"TIE", "INCONCLUSIVE"}:
            action, summary = "CREATE_NEXT_TEST", "暂未分出胜负，建议生成下一轮单变量实验"
        else:
            action, summary = "OBSERVE", "继续观察"
        return self._insert_next_action(
            source_type="CREATIVE_GROUP", source_id=str(row["group_evaluation_id"]),
            account_id=context["account_id"], launch_id=str(row["launch_id"]),
            experiment_id=str(row.get("winner_experiment_id") or ""), checkpoint=checkpoint,
            action_type=action, summary=summary,
            evidence={
                "decision_status": status, "data_quality_status": quality,
                "metrics_by_experiment": metrics,
                "confidence_tier": decode_json(row["evidence_json"], {}).get("confidence_tier"),
            },
        )

    def _sync_group_stop_actions(
        self, row: Dict[str, Any], context: Dict[str, str],
    ) -> List[Dict[str, Any]]:
        checkpoint = str(row.get("checkpoint") or "").upper()
        if str(row.get("data_quality_status") or "").upper() != "PASS":
            return []
        metrics_by_experiment = decode_json(row.get("metrics_by_experiment_json"), {})
        actions: List[Dict[str, Any]] = []
        for experiment_id, raw_metrics in dict(metrics_by_experiment or {}).items():
            experiment = self.conn.execute(
                "SELECT source_ad_id,stop_rule_json FROM ad_experiment WHERE experiment_id=?",
                (str(experiment_id),),
            ).fetchone()
            if not experiment or not str(experiment["source_ad_id"] or "").strip():
                continue
            stop_rules = dict(decode_json(experiment["stop_rule_json"], {}).get("delivery_guardrails") or {})
            if not stop_rules:
                continue
            breaches = evaluate_delivery_stop_loss(
                dict(raw_metrics or {}), stop_rules, checkpoint=checkpoint,
            )
            if not breaches:
                continue
            action = self._insert_next_action(
                source_type="CREATIVE_GROUP",
                source_id=f"{row['group_evaluation_id']}:stop:{experiment_id}",
                account_id=context["account_id"], launch_id=str(row.get("launch_id") or ""),
                experiment_id=str(experiment_id), checkpoint=checkpoint,
                action_type="PAUSE_AD",
                summary=f"止损线触发：{'；'.join(item['summary'] for item in breaches)}。暂停该广告，其他实验组继续投放。",
                evidence={
                    "source_ad_id": str(experiment["source_ad_id"]),
                    "metrics": dict(raw_metrics or {}), "breaches": breaches,
                    "delivery_guardrails": stop_rules,
                },
            )
            if action:
                actions.append(action)
        return actions

    def _sync_single_evaluation(self, row: Dict[str, Any], context: Dict[str, str]) -> Optional[Dict[str, Any]]:
        checkpoint = str(row["checkpoint"])
        status = str(row["evaluation_status"])
        if status in {"DATA_INCOMPLETE", "INSUFFICIENT_SAMPLE", "NOT_ATTRIBUTABLE"}:
            action, summary = "CHECK_DATA", "当前证据不足，不对广告做强动作"
        elif checkpoint in {"D1", "D3"} or status in {"NEUTRAL", "MIXED_CHANGE", "PENDING"}:
            action, summary = "OBSERVE", f"{checkpoint} 暂不调整，继续观察"
        elif status == "EFFECTIVE":
            action, summary = "INCREASE_BUDGET", "调整有效，建议在护栏内逐步增加预算"
        elif status == "INEFFECTIVE":
            action, summary = "REPLACE_CREATIVE", "调整无效，建议更换素材并进入下一轮实验"
        else:
            action, summary = "OBSERVE", "继续观察"
        return self._insert_next_action(
            source_type="EXPERIMENT", source_id=str(row["evaluation_id"]),
            account_id=context["account_id"], launch_id="",
            experiment_id=str(row["experiment_id"]), checkpoint=checkpoint,
            action_type=action, summary=summary,
            evidence={
                "evaluation_status": status,
                "data_quality_status": str(row["data_quality_status"]),
                "post_metrics": decode_json(row["post_metrics_json"], {}),
            },
        )

    def _sync_audience_evaluation(self, row: Dict[str, Any], context: Dict[str, str]) -> Optional[Dict[str, Any]]:
        checkpoint = str(row["checkpoint"])
        status = str(row["decision_status"])
        if status == "DATA_INCOMPLETE":
            action, summary = "CHECK_DATA", "受众实验数据不完整，先修复数据再判断"
        elif checkpoint in {"D1", "D3"} or status in {"OBSERVE", "PROVISIONAL"}:
            action, summary = "OBSERVE", f"{checkpoint} 受众表现已更新，继续观察"
        elif status == "WINNER":
            action, summary = "ADD_PAUSED_ADSET", "已有胜出受众，建议新增暂停态广告组继续验证"
        else:
            action, summary = "CREATE_NEXT_TEST", "受众差异不明确，建议创建下一轮单变量实验"
        return self._insert_next_action(
            source_type="AUDIENCE_PAIR", source_id=str(row["pair_evaluation_id"]),
            account_id=context["account_id"], launch_id=str(row["launch_id"]),
            experiment_id=str(row.get("winner_experiment_id") or ""), checkpoint=checkpoint,
            action_type=action, summary=summary,
            evidence={
                "decision_status": status,
                "metrics": decode_json(row["metrics_json"], {}),
                "evaluation_evidence": decode_json(row["evidence_json"], {}),
            },
        )

    def _insert_next_action(
        self, *, source_type: str, source_id: str, account_id: str, launch_id: str,
        experiment_id: str, checkpoint: str, action_type: str, summary: str,
        evidence: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        if self.conn.execute(
            "SELECT 1 FROM growth_next_action WHERE source_type=? AND source_id=? AND action_type=?",
            (source_type, source_id, action_type),
        ).fetchone():
            return None
        policy = self.get_policy(account_id)
        allowed = set(policy["allowed_action_types"])
        joins = self._max_metric(evidence, "real_bind_count")
        installs = self._max_metric(evidence, "installs")
        block_reason = ""
        decision = "READY" if action_type in LOCAL_ACTIONS else "APPROVAL_REQUIRED"
        if action_type not in LOCAL_ACTIONS and policy["level"] in {"L0_OBSERVE", "L1_RECOMMEND"}:
            block_reason = "当前账户仅允许观察和生成建议"
        elif action_type not in allowed:
            block_reason = "当前放权策略未授权该动作"
        elif action_type in LIVE_ACTIONS and action_type != "PAUSE_AD" and bool(policy["require_real_join_attribution"]):
            if joins < int(policy["minimum_real_joins"]):
                block_reason = "真实入会归因未达到强动作门槛"
            elif installs < int(policy["minimum_installs"]):
                block_reason = "安装量未达到强动作门槛"
        if block_reason:
            decision = "BLOCKED"
        action_id = new_id("next")
        now = utc_now()
        policy_snapshot = {
            "level": policy["level"],
            "allowed_action_types": policy["allowed_action_types"],
            "minimum_installs": policy["minimum_installs"],
            "minimum_real_joins": policy["minimum_real_joins"],
        }
        with self.conn:
            self.conn.execute(
                """INSERT INTO growth_next_action
                (next_action_id,source_type,source_id,account_id,launch_id,experiment_id,
                 checkpoint,action_type,summary,evidence_json,policy_snapshot_json,status,
                 block_reason,meta_write_allowed,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    action_id, source_type, source_id, account_id, launch_id, experiment_id,
                    checkpoint, action_type, summary, canonical_json(evidence),
                    canonical_json(policy_snapshot), decision, block_reason,
                    1 if decision == "APPROVAL_REQUIRED" and action_type in allowed else 0,
                    now, now,
                ),
            )
        return self._serialize_action(self.conn.execute(
            "SELECT * FROM growth_next_action WHERE next_action_id=?", (action_id,),
        ).fetchone())

    def _launch_context(self, launch_id: str) -> Optional[Dict[str, str]]:
        row = self.conn.execute(
            "SELECT account_id,target_app,country FROM ad_experiment WHERE source_report_id=? ORDER BY created_at LIMIT 1",
            (launch_id,),
        ).fetchone()
        return dict(row) if row else None

    def _experiment_context(self, experiment_id: str) -> Optional[Dict[str, str]]:
        row = self.conn.execute(
            "SELECT account_id,target_app,country FROM ad_experiment WHERE experiment_id=?", (experiment_id,),
        ).fetchone()
        return dict(row) if row else None

    @staticmethod
    def _syncable_context(context: Optional[Dict[str, str]], account_id: str) -> bool:
        if not context:
            return False
        normalized = str(context.get("account_id") or "").strip()
        if normalized.startswith("act_"):
            normalized = normalized[4:]
        if not normalized or not normalized.isdigit():
            return False
        context["account_id"] = normalized
        return not account_id or normalized == account_id

    @staticmethod
    def _max_metric(payload: Any, key: str) -> float:
        values: List[float] = []
        def walk(value: Any) -> None:
            if isinstance(value, dict):
                for item_key, item_value in value.items():
                    if item_key == key:
                        try:
                            values.append(float(item_value or 0))
                        except (TypeError, ValueError):
                            pass
                    else:
                        walk(item_value)
            elif isinstance(value, list):
                for item in value:
                    walk(item)
        walk(payload)
        return max(values or [0.0])

    @staticmethod
    def _serialize_action(row: sqlite3.Row) -> Dict[str, Any]:
        result = dict(row)
        result["evidence"] = decode_json(result.pop("evidence_json"), {})
        result["policy_snapshot"] = decode_json(result.pop("policy_snapshot_json"), {})
        result["meta_write_allowed"] = bool(result["meta_write_allowed"])
        result["meta_writes_performed"] = False
        return result

    @staticmethod
    def _account_id(account_id: str) -> str:
        normalized = str(account_id or "").strip()
        if normalized.startswith("act_"):
            normalized = normalized[4:]
        if not normalized or not normalized.isdigit():
            raise GrowthValidationError("invalid_meta_account_id")
        return normalized
