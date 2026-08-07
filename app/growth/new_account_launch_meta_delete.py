from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List

from app.growth.common import canonical_json, payload_hash, utc_now
from app.growth.new_account_launch_retention import (
    ensure_new_account_launch_retention_tables,
    launch_retention_status,
    purge_new_account_launch,
)


DELETE_MODE = "DELETE_ORDER_AND_META_OBJECTS"
DELETE_CONFIRMATION = "DELETE_ORDER_AND_META_OBJECTS"

_BACKGROUND_DELETE_LOCK = threading.Lock()
_BACKGROUND_DELETE_IDS: set[str] = set()


class LaunchMetaDeleteConflict(RuntimeError):
    pass


class LaunchMetaDeleteManualReview(RuntimeError):
    pass


def launch_meta_delete_status(conn: sqlite3.Connection, launch_id: str) -> Dict[str, Any]:
    ensure_new_account_launch_retention_tables(conn)
    fingerprint = hashlib.sha256(str(launch_id or "").encode("utf-8")).hexdigest()[:20]
    row = conn.execute(
        """SELECT delete_id,status,object_ids_json,results_json,response_json,created_at,updated_at
           FROM ad_new_account_launch_meta_delete_audit
           WHERE launch_fingerprint=? ORDER BY created_at DESC LIMIT 1""",
        (fingerprint,),
    ).fetchone()
    if not row:
        return {
            "launch_id": str(launch_id or ""), "status": "NONE",
            "completed_count": 0, "target_count": 0, "progress_percent": 0,
            "can_leave": True, "terminal": False, "requires_manual_review": False,
        }
    record = dict(row)
    targets = json.loads(str(record.get("object_ids_json") or "[]"))
    results = json.loads(str(record.get("results_json") or "[]"))
    completed = sum(1 for item in results if isinstance(item, dict) and bool(item.get("verified_deleted")))
    total = len(targets)
    status = str(record.get("status") or "")
    stale_started = False
    if status == "STARTED":
        try:
            updated_at = datetime.fromisoformat(str(record.get("updated_at") or "").replace("Z", "+00:00"))
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=timezone.utc)
            stale_started = datetime.now(timezone.utc) - updated_at > timedelta(minutes=5)
        except ValueError:
            stale_started = True
    visible_status = "MANUAL_REVIEW" if stale_started else status
    response = json.loads(str(record.get("response_json") or "{}"))
    return {
        "launch_id": str(launch_id or ""),
        "delete_id": str(record.get("delete_id") or ""),
        "status": visible_status,
        "status_zh": {
            "STARTED": "正在删除",
            "MANUAL_REVIEW": "删除需要核对",
            "SUCCESS": "已删除",
        }.get(visible_status, "删除状态未知"),
        "completed_count": completed,
        "target_count": total,
        "progress_percent": round((completed / total) * 100) if total else (100 if visible_status == "SUCCESS" else 0),
        "can_leave": True,
        "terminal": visible_status in {"SUCCESS", "MANUAL_REVIEW"},
        "requires_manual_review": visible_status == "MANUAL_REVIEW",
        "stale_started": stale_started,
        "created_at": str(record.get("created_at") or ""),
        "updated_at": str(record.get("updated_at") or ""),
        "result": response if visible_status == "SUCCESS" else {},
        "meta_writes_performed": bool(visible_status == "SUCCESS" and response.get("meta_writes_performed")),
    }


class NewAccountLaunchMetaDeleteService:
    """Fail-closed deletion for Meta objects exclusively owned by one launch."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        session: Any,
        access_token: str,
        graph_root: str,
        live_delete_enabled: bool,
    ) -> None:
        self.conn = conn
        self.session = session
        self.access_token = str(access_token or "").strip()
        self.graph_root = str(graph_root or "").strip().rstrip("/")
        self.live_delete_enabled = bool(
            live_delete_enabled and session is not None and self.access_token and self.graph_root
        )
        ensure_new_account_launch_retention_tables(conn)

    def preview(self, launch_id: str) -> Dict[str, Any]:
        launch = str(launch_id or "").strip()
        rows = list(self.conn.execute(
            """
            SELECT experiment_id,account_id,source_campaign_id,source_adset_id,source_ad_id
            FROM ad_experiment WHERE source_report_id=? ORDER BY created_at,experiment_code
            """,
            (launch,),
        ).fetchall())
        if not rows:
            raise LaunchMetaDeleteConflict("launch_not_found")
        accounts = {str(row["account_id"] or "").removeprefix("act_") for row in rows}
        campaigns = self._unique(row["source_campaign_id"] for row in rows)
        adsets = self._unique(row["source_adset_id"] for row in rows)
        ads = self._unique(row["source_ad_id"] for row in rows)
        reasons: List[str] = []
        retention = launch_retention_status(self.conn, launch)
        if not retention["can_permanently_delete"]:
            reasons.append(str(retention["permanent_delete_blocked_reason"] or "launch_cannot_be_deleted"))
        if len(accounts) != 1 or "" in accounts:
            reasons.append("launch_account_identity_invalid")
        if len(campaigns) != 1 or len(adsets) != len(rows) or len(ads) != len(rows):
            reasons.append("launch_meta_object_shape_invalid")
        shared = self._shared_references(launch, campaigns, adsets, ads)
        if shared:
            reasons.append("meta_objects_shared_by_other_orders")
        provenance_verified = self._provenance_verified(launch, campaigns, adsets, ads)
        if not provenance_verified:
            reasons.append("meta_object_ownership_not_verified")
        if not self.live_delete_enabled:
            reasons.append("meta_delete_execution_unavailable")

        remote = {"campaigns": [], "adsets": [], "ads": []}
        relationships_verified = False
        all_paused = False
        status_snapshot: List[Dict[str, str]] = []
        if not reasons or set(reasons).issubset({"launch_must_be_archived_first"}):
            try:
                remote["campaigns"] = [
                    self._get_existing(campaigns[0], "id,name,status,effective_status")
                ] if campaigns else []
                remote["adsets"] = [
                    self._get_existing(value, "id,name,status,effective_status,campaign_id")
                    for value in adsets
                ]
                remote["ads"] = [
                    self._get_existing(value, "id,name,status,effective_status,adset_id,campaign_id")
                    for value in ads
                ]
                expected_campaign = campaigns[0] if campaigns else ""
                expected_adsets = set(adsets)
                relationships_verified = bool(
                    expected_campaign
                    and all(str(item.get("campaign_id") or "") == expected_campaign for item in remote["adsets"])
                    and all(
                        str(item.get("campaign_id") or "") == expected_campaign
                        and str(item.get("adset_id") or "") in expected_adsets
                        for item in remote["ads"]
                    )
                )
                all_paused = all(
                    str(item.get("status") or "").upper() == "PAUSED"
                    for group in remote.values() for item in group
                )
                status_snapshot = [
                    {
                        "object_type": object_type,
                        "object_id": str(item.get("id") or ""),
                        "status": str(item.get("status") or "").upper(),
                        "effective_status": str(item.get("effective_status") or "").upper(),
                    }
                    for object_type, group in (
                        ("AD", remote["ads"]),
                        ("ADSET", remote["adsets"]),
                        ("CAMPAIGN", remote["campaigns"]),
                    )
                    for item in group
                ]
            except Exception:
                reasons.append("meta_object_readback_failed")
        if remote["campaigns"] or remote["adsets"] or remote["ads"]:
            if not relationships_verified:
                reasons.append("meta_object_relationship_mismatch")

        active_object_count = sum(
            1 for item in status_snapshot
            if "ACTIVE" in {item["status"], item["effective_status"]}
        )

        plan = {
            "version": "NEW_ACCOUNT_LAUNCH_META_DELETE_V2",
            "mode": DELETE_MODE,
            "launch_id": launch,
            "account_id": next(iter(accounts), ""),
            "campaign_ids": campaigns,
            "adset_ids": adsets,
            "ad_ids": ads,
            "delete_order": [
                *[{"object_type": "AD", "object_id": value} for value in ads],
                *[{"object_type": "ADSET", "object_id": value} for value in adsets],
                *[{"object_type": "CAMPAIGN", "object_id": value} for value in campaigns],
            ],
            "status_snapshot": status_snapshot,
        }
        deduped_reasons = list(dict.fromkeys(value for value in reasons if value))
        return {
            "launch_id": launch,
            "eligible": not deduped_reasons,
            "blocked_reasons": deduped_reasons,
            "ownership_verified": provenance_verified,
            "shared_references": shared,
            "relationships_verified": relationships_verified,
            "all_paused": all_paused,
            "active_object_count": active_object_count,
            "counts": {"ads": len(ads), "adsets": len(adsets), "campaigns": len(campaigns)},
            "objects": remote,
            "plan": plan,
            "plan_hash": payload_hash(plan),
            "meta_writes_performed": False,
        }

    def execute(
        self,
        launch_id: str,
        *,
        actor: str,
        confirmation: str,
        plan_hash_value: str,
        idempotency_key: str,
    ) -> Dict[str, Any]:
        queued = self.enqueue(
            launch_id, actor=actor, confirmation=confirmation,
            plan_hash_value=plan_hash_value, idempotency_key=idempotency_key,
        )
        return self.continue_started(
            str(queued["delete_id"]), str(launch_id), actor=actor,
            allow_manual_resume=True,
        )

    def enqueue(
        self,
        launch_id: str,
        *,
        actor: str,
        confirmation: str,
        plan_hash_value: str,
        idempotency_key: str,
    ) -> Dict[str, Any]:
        if str(confirmation or "").strip().upper() != DELETE_CONFIRMATION:
            raise LaunchMetaDeleteConflict("meta_delete_confirmation_required")
        if not str(idempotency_key or "").strip():
            raise LaunchMetaDeleteConflict("idempotency_key_required")
        request_hash = payload_hash({
            "launch_id": launch_id,
            "confirmation": DELETE_CONFIRMATION,
            "plan_hash": str(plan_hash_value or "").strip(),
        })
        existing = self.conn.execute(
            "SELECT * FROM ad_new_account_launch_meta_delete_audit WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
        if existing:
            if str(existing["request_hash"]) != request_hash:
                raise LaunchMetaDeleteConflict("idempotency_key_payload_conflict")
            return launch_meta_delete_status(self.conn, launch_id)
        preview = self.preview(launch_id)
        if not preview["eligible"]:
            raise LaunchMetaDeleteConflict(preview["blocked_reasons"][0])
        if str(plan_hash_value or "").strip() != preview["plan_hash"]:
            raise LaunchMetaDeleteConflict("meta_delete_plan_changed")
        delete_id = "meta_delete_" + hashlib.sha256(
            f"{launch_id}:{idempotency_key}:{request_hash}".encode("utf-8")
        ).hexdigest()[:24]
        now = utc_now()
        self.conn.execute(
            """
            INSERT INTO ad_new_account_launch_meta_delete_audit
            (delete_id,launch_fingerprint,idempotency_key,request_hash,plan_hash,status,
             requested_by,object_ids_json,results_json,response_json,created_at,updated_at)
            VALUES (?,?,?,?,?,'STARTED',?,?, '[]','{}',?,?)
            """,
            (
                delete_id, hashlib.sha256(str(launch_id).encode("utf-8")).hexdigest()[:20],
                idempotency_key, request_hash, preview["plan_hash"], actor,
                canonical_json(preview["plan"]["delete_order"]), now, now,
            ),
        )
        self.conn.commit()
        return launch_meta_delete_status(self.conn, launch_id)

    def continue_started(
        self, delete_id: str, launch_id: str, *, actor: str,
        allow_manual_resume: bool = False,
    ) -> Dict[str, Any]:
        existing = self.conn.execute(
            "SELECT * FROM ad_new_account_launch_meta_delete_audit WHERE delete_id=?",
            (str(delete_id),),
        ).fetchone()
        if not existing:
            raise LaunchMetaDeleteConflict("meta_delete_attempt_not_found")
        status = str(existing["status"] or "")
        if status == "SUCCESS":
            return json.loads(str(existing["response_json"] or "{}"))
        if status == "MANUAL_REVIEW":
            if not allow_manual_resume:
                raise LaunchMetaDeleteManualReview("meta_delete_requires_manual_review")
            claimed = self.conn.execute(
                """UPDATE ad_new_account_launch_meta_delete_audit
                   SET status='STARTED',updated_at=?
                   WHERE delete_id=? AND status='MANUAL_REVIEW'""",
                (utc_now(), str(delete_id)),
            )
            self.conn.commit()
            if int(claimed.rowcount or 0) != 1:
                raise LaunchMetaDeleteManualReview("meta_delete_attempt_already_started")
            existing = self.conn.execute(
                "SELECT * FROM ad_new_account_launch_meta_delete_audit WHERE delete_id=?",
                (str(delete_id),),
            ).fetchone()
        if str(existing["status"] or "") != "STARTED":
            raise LaunchMetaDeleteManualReview("meta_delete_attempt_not_runnable")
        try:
            return self._resume_existing_attempt(existing, str(launch_id), actor=actor)
        except Exception:
            latest = self.conn.execute(
                "SELECT results_json,status FROM ad_new_account_launch_meta_delete_audit WHERE delete_id=?",
                (str(delete_id),),
            ).fetchone()
            if latest and str(latest["status"] or "") != "SUCCESS":
                current_results = json.loads(str(latest["results_json"] or "[]"))
                self._update_audit(str(delete_id), "MANUAL_REVIEW", current_results)
            raise

    def run_enqueued(self, delete_id: str, launch_id: str, *, actor: str) -> Dict[str, Any]:
        normalized_delete_id = str(delete_id or "")
        with _BACKGROUND_DELETE_LOCK:
            if normalized_delete_id in _BACKGROUND_DELETE_IDS:
                return launch_meta_delete_status(self.conn, launch_id)
            _BACKGROUND_DELETE_IDS.add(normalized_delete_id)
        try:
            worker_claim = canonical_json({
                "worker_claim": hashlib.sha256(
                    f"{normalized_delete_id}:{threading.get_ident()}:{utc_now()}".encode("utf-8")
                ).hexdigest()[:20],
            })
            with self.conn:
                claimed = self.conn.execute(
                    """UPDATE ad_new_account_launch_meta_delete_audit
                       SET response_json=?,updated_at=?
                       WHERE delete_id=? AND status='STARTED' AND response_json='{}'""",
                    (worker_claim, utc_now(), normalized_delete_id),
                )
            if int(claimed.rowcount or 0) != 1:
                return launch_meta_delete_status(self.conn, launch_id)
            return self.continue_started(normalized_delete_id, launch_id, actor=actor)
        finally:
            with _BACKGROUND_DELETE_LOCK:
                _BACKGROUND_DELETE_IDS.discard(normalized_delete_id)

    def _resume_existing_attempt(
        self, existing: sqlite3.Row, launch_id: str, *, actor: str,
    ) -> Dict[str, Any]:
        targets = json.loads(str(existing["object_ids_json"] or "[]"))
        results = json.loads(str(existing["results_json"] or "[]"))
        self._validate_resume_scope(launch_id, targets)
        counts = {
            "ads": sum(1 for item in targets if str(item.get("object_type") or "") == "AD"),
            "adsets": sum(1 for item in targets if str(item.get("object_type") or "") == "ADSET"),
            "campaigns": sum(1 for item in targets if str(item.get("object_type") or "") == "CAMPAIGN"),
        }
        return self._continue_attempt(
            str(existing["delete_id"]), launch_id, actor=actor,
            targets=targets, results=results, counts=counts,
        )

    def _continue_attempt(
        self, delete_id: str, launch_id: str, *, actor: str,
        targets: List[Dict[str, Any]], results: List[Dict[str, Any]],
        counts: Dict[str, int],
    ) -> Dict[str, Any]:
        prior_by_key = {
            (str(item.get("object_type") or ""), str(item.get("object_id") or "")): dict(item)
            for item in results if isinstance(item, dict)
        }
        reconciled_results: List[Dict[str, Any]] = []
        for target in targets:
            object_type = str(target["object_type"])
            object_id = str(target["object_id"])
            prior = prior_by_key.get((object_type, object_id))
            if prior:
                if not bool(prior.get("verified_deleted")):
                    verified = self._verify_deleted_once(object_id)
                    prior.update({
                        "verified_deleted": verified,
                        "error": "" if verified else str(prior.get("error") or "meta_delete_readback_uncertain"),
                        "reconciled_readback": True,
                    })
                result = prior
            elif self._verify_deleted_once(object_id):
                result = {
                    "object_type": object_type,
                    "object_id": object_id,
                    "delete_acknowledged": False,
                    "verified_deleted": True,
                    "error": "",
                    "reconciled_preexisting_deleted": True,
                }
            else:
                self._validate_live_target(launch_id, object_type, object_id)
                result = self._delete_once(object_type, object_id)
            reconciled_results.append(result)
            self._update_audit(delete_id, "STARTED", reconciled_results)
            if not result["verified_deleted"]:
                self._update_audit(delete_id, "MANUAL_REVIEW", reconciled_results)
                raise LaunchMetaDeleteManualReview("meta_delete_result_uncertain")
        try:
            purge_result = purge_new_account_launch(
                self.conn, launch_id, actor=actor,
                reason="manual_permanent_delete_with_meta_objects",
            )
        except Exception as exc:
            self._update_audit(delete_id, "MANUAL_REVIEW", reconciled_results)
            raise LaunchMetaDeleteManualReview("meta_deleted_order_purge_failed") from exc
        response = {
            **purge_result,
            "delete_id": delete_id,
            "meta_delete_status": "VERIFIED_DELETED",
            "meta_deleted_counts": dict(counts),
            "meta_writes_performed": True,
        }
        self._update_audit(delete_id, "SUCCESS", reconciled_results, response=response)
        return response

    def _validate_resume_scope(self, launch_id: str, targets: List[Dict[str, Any]]) -> None:
        retention = launch_retention_status(self.conn, launch_id)
        if not retention["can_permanently_delete"]:
            raise LaunchMetaDeleteConflict(
                str(retention["permanent_delete_blocked_reason"] or "launch_cannot_be_deleted")
            )
        rows = list(self.conn.execute(
            """
            SELECT source_campaign_id,source_adset_id,source_ad_id
            FROM ad_experiment WHERE source_report_id=? ORDER BY created_at,experiment_code
            """,
            (launch_id,),
        ).fetchall())
        campaigns = self._unique(row["source_campaign_id"] for row in rows)
        adsets = self._unique(row["source_adset_id"] for row in rows)
        ads = self._unique(row["source_ad_id"] for row in rows)
        expected = [
            *[{"object_type": "AD", "object_id": value} for value in ads],
            *[{"object_type": "ADSET", "object_id": value} for value in adsets],
            *[{"object_type": "CAMPAIGN", "object_id": value} for value in campaigns],
        ]
        normalized_targets = [
            {"object_type": str(item.get("object_type") or ""), "object_id": str(item.get("object_id") or "")}
            for item in targets if isinstance(item, dict)
        ]
        if normalized_targets != expected:
            raise LaunchMetaDeleteConflict("meta_delete_frozen_scope_changed")
        if self._shared_references(launch_id, campaigns, adsets, ads):
            raise LaunchMetaDeleteConflict("meta_objects_shared_by_other_orders")
        if not self._provenance_verified(launch_id, campaigns, adsets, ads):
            raise LaunchMetaDeleteConflict("meta_object_ownership_not_verified")
        if not self.live_delete_enabled:
            raise LaunchMetaDeleteConflict("meta_delete_execution_unavailable")

    def _validate_live_target(self, launch_id: str, object_type: str, object_id: str) -> None:
        if object_type == "AD":
            row = self.conn.execute(
                """SELECT source_campaign_id,source_adset_id FROM ad_experiment
                   WHERE source_report_id=? AND source_ad_id=?""",
                (launch_id, object_id),
            ).fetchone()
            remote = self._get_existing(object_id, "id,status,effective_status,campaign_id,adset_id")
            valid = bool(row) and str(remote.get("campaign_id") or "") == str(row["source_campaign_id"] or "") \
                and str(remote.get("adset_id") or "") == str(row["source_adset_id"] or "")
        elif object_type == "ADSET":
            row = self.conn.execute(
                """SELECT source_campaign_id FROM ad_experiment
                   WHERE source_report_id=? AND source_adset_id=?""",
                (launch_id, object_id),
            ).fetchone()
            remote = self._get_existing(object_id, "id,status,effective_status,campaign_id")
            valid = bool(row) and str(remote.get("campaign_id") or "") == str(row["source_campaign_id"] or "")
        elif object_type == "CAMPAIGN":
            row = self.conn.execute(
                """SELECT 1 FROM ad_experiment
                   WHERE source_report_id=? AND source_campaign_id=? LIMIT 1""",
                (launch_id, object_id),
            ).fetchone()
            remote = self._get_existing(object_id, "id,status,effective_status")
            valid = bool(row) and str(remote.get("id") or "") == object_id
        else:
            raise LaunchMetaDeleteConflict("meta_delete_object_type_invalid")
        if not valid:
            raise LaunchMetaDeleteConflict("meta_object_relationship_mismatch")

    def _shared_references(
        self, launch_id: str, campaigns: List[str], adsets: List[str], ads: List[str],
    ) -> List[Dict[str, str]]:
        shared: List[Dict[str, str]] = []
        for column, object_type, values in (
            ("source_campaign_id", "CAMPAIGN", campaigns),
            ("source_adset_id", "ADSET", adsets),
            ("source_ad_id", "AD", ads),
        ):
            for value in values:
                rows = self.conn.execute(
                    f"SELECT DISTINCT source_report_id FROM ad_experiment WHERE {column}=? AND source_report_id<>?",
                    (value, launch_id),
                ).fetchall()
                shared.extend({
                    "object_type": object_type,
                    "object_id": value,
                    "launch_id": str(row["source_report_id"]),
                } for row in rows)
        return shared

    def _provenance_verified(
        self, launch_id: str, campaigns: List[str], adsets: List[str], ads: List[str],
    ) -> bool:
        expected = set(campaigns + adsets + ads)
        rows = self.conn.execute(
            """
            SELECT a.payload_json,t.meta_object_ids_json
            FROM growth_operation_action a JOIN meta_execution_task t
              ON t.operation_action_id=a.operation_action_id
            WHERE a.action_type='CREATE_PAUSED_AD' AND a.status='VERIFIED' AND t.status='SUCCESS'
            """
        ).fetchall()
        for row in rows:
            payload = json.loads(str(row["payload_json"] or "{}"))
            ids = self._flatten_ids(json.loads(str(row["meta_object_ids_json"] or "{}")))
            action_launch = str(payload.get("launch_id") or dict(payload.get("plan") or {}).get("launch_id") or "")
            if action_launch == launch_id and expected.issubset(ids):
                return True
        if not ads or not self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ad_creative_direction_mapping'",
        ).fetchone():
            return False
        mapped = self.conn.execute(
            f"SELECT COUNT(*) FROM ad_creative_direction_mapping WHERE launch_id=? AND ad_id IN ({','.join('?' for _ in ads)})",
            (launch_id, *ads),
        ).fetchone()
        return int(mapped[0] or 0) == len(ads)

    def _get_existing(self, object_id: str, fields: str) -> Dict[str, Any]:
        response = self.session.get(
            f"{self.graph_root}/{object_id}",
            params={"access_token": self.access_token, "fields": fields}, timeout=25,
        )
        body = self._body(response)
        if str(body.get("id") or "") != object_id:
            raise LaunchMetaDeleteConflict("meta_object_not_confirmed")
        return body

    def _delete_once(self, object_type: str, object_id: str) -> Dict[str, Any]:
        delete_acknowledged = False
        delete_error = ""
        try:
            response = self.session.delete(
                f"{self.graph_root}/{object_id}",
                data={"access_token": self.access_token}, timeout=25,
            )
            body = self._body(response)
            delete_acknowledged = body.get("success") is True
            if not delete_acknowledged:
                delete_error = "meta_delete_not_acknowledged"
        except Exception as exc:
            delete_error = type(exc).__name__
        verified_deleted = self._verify_deleted_once(object_id)
        return {
            "object_type": object_type,
            "object_id": object_id,
            "delete_acknowledged": delete_acknowledged,
            "verified_deleted": verified_deleted,
            "error": "" if verified_deleted else (delete_error or "meta_delete_readback_uncertain"),
        }

    def _verify_deleted_once(self, object_id: str) -> bool:
        try:
            response = self.session.get(
                f"{self.graph_root}/{object_id}",
                params={
                    "access_token": self.access_token,
                    "fields": "id,status,effective_status",
                },
                timeout=25,
            )
            body = self._body(response, allow_error=True)
            return self._is_missing_response(response, body)
        except Exception:
            return False

    def _update_audit(
        self, delete_id: str, status: str, results: List[Dict[str, Any]],
        *, response: Dict[str, Any] | None = None,
    ) -> None:
        with self.conn:
            if response is None:
                self.conn.execute(
                    """UPDATE ad_new_account_launch_meta_delete_audit
                       SET status=?,results_json=?,updated_at=? WHERE delete_id=?""",
                    (status, canonical_json(results), utc_now(), delete_id),
                )
            else:
                self.conn.execute(
                    """UPDATE ad_new_account_launch_meta_delete_audit
                       SET status=?,results_json=?,response_json=?,updated_at=? WHERE delete_id=?""",
                    (status, canonical_json(results), canonical_json(response), utc_now(), delete_id),
                )

    @staticmethod
    def _body(response: Any, *, allow_error: bool = False) -> Dict[str, Any]:
        body = response.json() if hasattr(response, "json") else {}
        if not isinstance(body, dict):
            raise LaunchMetaDeleteConflict("meta_invalid_response")
        if body.get("error") and not allow_error:
            raise LaunchMetaDeleteConflict("meta_graph_error")
        return body

    @staticmethod
    def _is_missing_response(response: Any, body: Dict[str, Any]) -> bool:
        if int(getattr(response, "status_code", 0) or 0) in {404, 410}:
            return True
        if "DELETED" in {
            str(body.get("status") or "").upper(),
            str(body.get("effective_status") or "").upper(),
        }:
            return True
        error = dict(body.get("error") or {})
        message = str(error.get("message") or "").lower()
        return str(error.get("error_subcode") or "") == "33" or any(
            marker in message for marker in (
                "unsupported get request", "object does not exist", "cannot be loaded",
            )
        )

    @staticmethod
    def _unique(values: Iterable[Any]) -> List[str]:
        return list(dict.fromkeys(str(value or "").strip() for value in values if str(value or "").strip()))

    @staticmethod
    def _flatten_ids(value: Any) -> set[str]:
        if isinstance(value, dict):
            result: set[str] = set()
            for item in value.values():
                result.update(NewAccountLaunchMetaDeleteService._flatten_ids(item))
            return result
        if isinstance(value, (list, tuple, set)):
            result = set()
            for item in value:
                result.update(NewAccountLaunchMetaDeleteService._flatten_ids(item))
            return result
        normalized = str(value or "").strip()
        return {normalized} if normalized else set()
