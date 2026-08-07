from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, Dict, List

from app.growth.common import canonical_json, payload_hash, utc_now
from app.growth.schema import ensure_growth_schema


ROUTE_KEY = "ad_observation.materialize"
ACTOR = "system:daily-report-observer"


def _stable_id(*parts: Any) -> str:
    raw = "|".join(str(part or "").strip() for part in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _target_app(item: Dict[str, Any]) -> str:
    explicit = str(item.get("target_app") or item.get("project") or "").strip().lower()
    if explicit in {"linky", "timo"}:
        return explicit
    account = str(item.get("account_id") or "").strip().upper()
    if "-LK" in account or "LINKY" in account:
        return "linky"
    if "-TM" in account or "TIMO" in account or account == "MIAO10":
        return "timo"
    return "unknown"


def _observation_identity(item: Dict[str, Any]) -> str:
    explicit = str(item.get("observation_identity") or "").strip()
    if explicit:
        return explicit
    object_id = str(item.get("object_id") or "").strip()
    return _stable_id("legacy_object_identity_v1", object_id) if object_id else ""


def _observation_experiment_identity(observation_identity: str) -> str:
    return _stable_id("ad_observation_location_v1", observation_identity)


def _existing_observation_experiment(
    conn: sqlite3.Connection,
    *,
    experiment_id: str,
    experiment_code: str,
    observation_identity: str,
) -> sqlite3.Row | None:
    stable = conn.execute(
        "SELECT * FROM ad_experiment WHERE experiment_id=?",
        (experiment_id,),
    ).fetchone()
    if stable:
        return stable
    legacy = None
    for row in conn.execute(
        "SELECT * FROM ad_experiment WHERE created_by=? AND state='MATURING'",
        (ACTOR,),
    ).fetchall():
        try:
            hypothesis = json.loads(str(row["hypothesis_json"] or "{}"))
        except (TypeError, ValueError):
            continue
        if (
            str(hypothesis.get("mode") or "") == "passive_observation"
            and str(hypothesis.get("observation_identity") or "") == observation_identity
        ):
            legacy = row
            break
    if not legacy:
        return None

    legacy_id = str(legacy["experiment_id"])
    columns = [str(row["name"]) for row in conn.execute("PRAGMA table_info(ad_experiment)").fetchall()]
    values = [legacy[column] for column in columns]
    values[columns.index("experiment_id")] = experiment_id
    values[columns.index("experiment_code")] = experiment_code
    conn.execute(
        f"INSERT INTO ad_experiment ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
        values,
    )
    for table in (
        "ad_experiment_events",
        "ad_experiment_evaluation",
        "experiment_context_snapshots",
        "growth_decision_episode",
    ):
        conn.execute(
            f"UPDATE {table} SET experiment_id=? WHERE experiment_id=?",
            (experiment_id, legacy_id),
        )
    conn.execute(
        "UPDATE growth_decision SET target_id=? WHERE target_type='EXPERIMENT' AND target_id=?",
        (experiment_id, legacy_id),
    )
    conn.execute(
        "UPDATE growth_operation_action SET target_id=? WHERE target_type='EXPERIMENT' AND target_id=?",
        (experiment_id, legacy_id),
    )
    conn.execute("DELETE FROM ad_experiment WHERE experiment_id=?", (legacy_id,))
    return conn.execute(
        "SELECT * FROM ad_experiment WHERE experiment_id=?",
        (experiment_id,),
    ).fetchone()


def materialize_observation_tasks(
    conn: sqlite3.Connection,
    report: Dict[str, Any],
    *,
    actor: str = ACTOR,
) -> Dict[str, Any]:
    """Create or refresh one read-only observation task per stable dashboard row."""
    ensure_growth_schema(conn)
    if str(report.get("data_mode") or "").strip().lower() != "real":
        return {"eligible": 0, "created": 0, "refreshed": 0, "deduplicated": 0, "skipped": 0}
    report_id = str(report.get("report_id") or "").strip()
    rule_version = str(report.get("rule_version") or "").strip()
    if not report_id or not rule_version:
        return {"eligible": 0, "created": 0, "refreshed": 0, "deduplicated": 0, "skipped": 0}

    objects: Dict[str, Dict[str, Any]] = {}
    objects_by_object_id: Dict[str, List[Dict[str, Any]]] = {}
    for raw_item in report.get("ad_objects") or []:
        if not isinstance(raw_item, dict):
            continue
        item = dict(raw_item)
        object_id = str(item.get("object_id") or "").strip()
        identity = _observation_identity(item)
        if not object_id or not identity:
            continue
        if identity in objects:
            raise ValueError("ad_observation_identity_duplicate")
        objects[identity] = item
        objects_by_object_id.setdefault(object_id, []).append(item)
    result = {"eligible": 0, "created": 0, "refreshed": 0, "deduplicated": 0, "skipped": 0}
    now = utc_now()
    for recommendation in report.get("recommendations") or []:
        if not isinstance(recommendation, dict):
            result["skipped"] += 1
            continue
        action = str(recommendation.get("action_type") or recommendation.get("primary_action") or "").strip().lower()
        if action != "observe" or str(recommendation.get("data_origin") or "NATIVE_V2").upper() != "NATIVE_V2":
            continue
        object_id = str(recommendation.get("object_id") or "").strip()
        recommendation_id = str(recommendation.get("recommendation_id") or "").strip()
        observation_identity = str(recommendation.get("observation_identity") or "").strip()
        if not observation_identity:
            candidates = objects_by_object_id.get(object_id) or []
            if len(candidates) == 1:
                observation_identity = _observation_identity(candidates[0])
        object_row = objects.get(observation_identity)
        if not object_id or not recommendation_id or not object_row:
            result["skipped"] += 1
            continue
        result["eligible"] += 1
        idempotency_key = _stable_id(report_id, recommendation_id, rule_version)
        request = {
            "report_id": report_id,
            "recommendation_id": recommendation_id,
            "rule_version": rule_version,
            "object_id": object_id,
            "observation_identity": observation_identity,
        }
        digest = payload_hash(request)
        existing_request = conn.execute(
            "SELECT request_hash FROM growth_idempotency_record WHERE route_key=? AND idempotency_key=?",
            (ROUTE_KEY, idempotency_key),
        ).fetchone()
        if existing_request:
            if str(existing_request["request_hash"]) != digest:
                raise ValueError("ad_observation_idempotency_conflict")
            result["deduplicated"] += 1
            continue

        identity = _observation_experiment_identity(observation_identity)
        experiment_id = f"adexp_observe_{identity[:24]}"
        experiment_code = f"OBS-{identity[:20].upper()}"
        display_name = str(
            object_row.get("ad")
            or recommendation.get("object_name")
            or object_id
        ).strip()
        target_app = _target_app(object_row)
        evidence = dict(recommendation.get("evidence") or {})
        scorecard = dict(evidence.get("scorecard") or {})
        hypothesis = {
            "mode": "passive_observation",
            "display_name": display_name,
            "diagnosis": str(recommendation.get("diagnosis_type_zh") or recommendation.get("diagnosis_type") or "继续观察"),
            "reason": str(recommendation.get("reason_zh") or ""),
            "rule_version": rule_version,
            "observation_identity": observation_identity,
            "causal_claim": False,
            "latest_observation": {
                "report_id": report_id,
                "recommendation_id": recommendation_id,
                "observation_identity": observation_identity,
                "report_date": str(report.get("report_date") or ""),
                "score": scorecard.get("score"),
                "band": scorecard.get("band"),
                "band_zh": scorecard.get("band_zh"),
                "available_weight": scorecard.get("available_weight"),
                "confidence": scorecard.get("confidence"),
                "maturity": dict(scorecard.get("maturity") or {}),
                "benchmark_version": scorecard.get("benchmark_version"),
                "threshold_source": scorecard.get("threshold_source"),
                "metrics": {
                    "installs": object_row.get("installs"),
                    "cpi": object_row.get("cpi"),
                    "ctr": object_row.get("ctr"),
                    "real_bind_count": object_row.get("real_bind_count"),
                    "real_bind_cpa": object_row.get("real_bind_cpa"),
                },
                "technical_metrics": {
                    "spend": object_row.get("spend"),
                    "impressions": object_row.get("impressions"),
                    "clicks": object_row.get("clicks"),
                    "cpm": object_row.get("cpm"),
                    "registrations": object_row.get("registrations"),
                    "auto_apply_user_count": object_row.get("auto_apply_user_count"),
                    "user_engaged_im_users": object_row.get("user_engaged_im_users"),
                },
                "technical_audit": dict(scorecard.get("technical_audit") or {}),
            },
        }
        existing_experiment = _existing_observation_experiment(
            conn,
            experiment_id=experiment_id,
            experiment_code=experiment_code,
            observation_identity=observation_identity,
        )
        if existing_experiment:
            conn.execute(
                """
                UPDATE ad_experiment
                SET target_app=?,country=?,account_id=?,source_report_id=?,source_recommendation_id=?,
                    hypothesis_json=?,updated_at=?
                WHERE experiment_id=?
                """,
                (
                    target_app,
                    str(object_row.get("country") or recommendation.get("country") or ""),
                    str(object_row.get("account_id") or ""),
                    report_id,
                    recommendation_id,
                    canonical_json(hypothesis),
                    now,
                    experiment_id,
                ),
            )
            event_type = "OBSERVATION_SOURCE_REFRESHED"
            result["refreshed"] += 1
        else:
            conn.execute(
                """
                INSERT INTO ad_experiment
                (experiment_id,experiment_code,target_app,country,platform,account_id,
                 source_report_id,source_recommendation_id,source_ad_id,experiment_type,
                 hypothesis_json,primary_metric,guardrail_metrics_json,maturity_rule_json,
                 stop_rule_json,control_definition_json,variant_definition_json,state,state_reason,
                 created_by,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,'NEW_AD_TEST',?,'real_bind_cpa',?,?,?,?,?,'MATURING',?,?,?,?)
                """,
                (
                    experiment_id,
                    experiment_code,
                    target_app,
                    str(object_row.get("country") or recommendation.get("country") or ""),
                    "meta",
                    str(object_row.get("account_id") or ""),
                    report_id,
                    recommendation_id,
                    object_id,
                    canonical_json(hypothesis),
                    canonical_json(["installs", "cpi", "ctr", "real_bind_count", "real_bind_cpa"]),
                    canonical_json({"minimum_conversions": 10, "minimum_installs": 100, "checkpoints": ["D1", "D3", "D7"]}),
                    canonical_json({"meta_writes_forbidden": True}),
                    canonical_json({"object_id": object_id, "observation_identity": observation_identity, "mode": "passive_observation"}),
                    canonical_json({"mode": "passive_observation"}),
                    "system_observation_materialized",
                    actor,
                    now,
                    now,
                ),
            )
            event_type = "OBSERVATION_AUTO_MATERIALIZED"
            result["created"] += 1
        conn.execute(
            """
            INSERT INTO ad_experiment_events
            (event_id,experiment_id,from_state,to_state,event_type,actor,reason,evidence_json,created_at)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                f"adevent_observe_{idempotency_key[:24]}",
                experiment_id,
                "MATURING" if existing_experiment else "",
                "MATURING",
                event_type,
                actor,
                "read_only_observation",
                canonical_json(request),
                now,
            ),
        )
        response = {"experiment_id": experiment_id, "event_type": event_type, "meta_writes_performed": False}
        conn.execute(
            """
            INSERT INTO growth_idempotency_record
            (route_key,idempotency_key,request_hash,response_status,response_json,created_at)
            VALUES (?,?,?,201,?,?)
            """,
            (ROUTE_KEY, idempotency_key, digest, canonical_json(response), now),
        )
    return result
