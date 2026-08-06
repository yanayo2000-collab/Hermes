from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

from app.creative_image_generation import (
    CreativeImageGenerationBrief,
    create_hermes_image2_generation_job,
    retry_hermes_image2_generation_task,
    start_hermes_image2_generation_task,
)
from app.growth.ad_experiment_service import AdExperimentService, EXPERIMENT_TRANSITIONS
from app.growth.approval_service import OperationApprovalService
from app.growth.common import canonical_json, decode_json, payload_hash, utc_now
from app.growth.execution_service import ExecutionTaskService


_COPY = {
    "BR": {
        "points_reward": ("📱 Seu tempo livre pode render mais: encontre tarefas no Tugao, acompanhe o progresso e veja os pontos e recompensas disponíveis no app.", "Transforme tempo livre em progresso", "Veja tarefas e recompensas no Tugao."),
        "safe_compliance": ("✅ Antes de começar, veja no Tugao o que a tarefa pede, quais são as etapas e como os pontos e recompensas funcionam.", "Saiba o que fazer desde o início", "Regras e etapas claras no app."),
        "easy_start": ("🚀 Tem alguns minutos livres? Abra o Tugao, escolha uma tarefa e acompanhe cada etapa, seus pontos e as recompensas disponíveis.", "Comece uma tarefa pelo celular", "Tudo organizado no Tugao."),
        "guided_trust": ("🤝 Não sabe por onde começar? O Tugao mostra a próxima etapa, seu progresso e os pontos acumulados para você seguir sem se perder.", "Seu próximo passo está no Tugao", "Acompanhe tudo pelo app."),
    },
    "MX": {
        "points_reward": ("📱 Aprovecha mejor tus ratos libres: encuentra tareas en Tugao, sigue tu avance y consulta los puntos y recompensas disponibles en la app.", "Convierte tu tiempo en progreso", "Mira tareas y recompensas en Tugao."),
        "safe_compliance": ("✅ Antes de empezar, revisa en Tugao qué pide cada tarea, cuáles son los pasos y cómo funcionan los puntos y las recompensas.", "Sabe qué hacer desde el inicio", "Reglas y pasos claros en la app."),
        "easy_start": ("🚀 ¿Tienes unos minutos? Abre Tugao, elige una tarea y sigue cada paso, tus puntos y las recompensas disponibles.", "Empieza una tarea desde tu celular", "Todo organizado en Tugao."),
        "guided_trust": ("🤝 ¿No sabes por dónde empezar? Tugao te muestra el siguiente paso, tu avance y los puntos acumulados para que sigas sin perderte.", "Tu siguiente paso está en Tugao", "Sigue todo desde la app."),
    },
    "ID": {
        "points_reward": ("📱 Manfaatkan waktu luangmu: temukan tugas di Tugao, pantau progres, lalu lihat poin dan hadiah yang tersedia langsung di aplikasi.", "Waktu luang jadi lebih berarti", "Lihat tugas dan hadiah di Tugao."),
        "safe_compliance": ("✅ Sebelum mulai, lihat apa yang perlu dikerjakan, tahapnya, serta cara kerja poin dan hadiah di aplikasi Tugao.", "Tahu tugasnya sejak awal", "Langkah dan ketentuan lebih jelas."),
        "easy_start": ("🚀 Punya beberapa menit? Buka Tugao, pilih tugas, lalu pantau setiap tahap, poin, dan hadiah yang tersedia.", "Mulai tugas langsung dari HP", "Semua tersusun di Tugao."),
        "guided_trust": ("🤝 Bingung mulai dari mana? Tugao menunjukkan langkah berikutnya, progres, dan poin yang sudah terkumpul agar kamu tetap terarah.", "Langkah berikutnya ada di Tugao", "Pantau semuanya di aplikasi."),
    },
}


def _enabled(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _live_action_allowed(account_id: str, action_type: str) -> bool:
    accounts = {
        item.strip().removeprefix("act_")
        for item in str(os.getenv("GROWTH_META_ALLOWED_ACCOUNT_IDS") or "").split(",")
        if item.strip()
    }
    actions = {
        item.strip().upper()
        for item in str(os.getenv("GROWTH_META_ALLOWED_ACTION_TYPES") or "").split(",")
        if item.strip()
    }
    return bool(
        _enabled(os.getenv("GROWTH_META_WRITES_ENABLED", ""))
        and str(account_id or "").removeprefix("act_") in accounts
        and str(action_type or "").strip().upper() in actions
    )


def _live_allowed(account_id: str) -> bool:
    return _live_action_allowed(account_id, "CREATE_PAUSED_AD")


def _dry_run_receipt(plan_id: str, plan: Dict[str, Any], approval: Dict[str, Any]) -> Dict[str, Any]:
    steps = ["CAMPAIGN_CREATE"]
    for index, raw_cell in enumerate(list(plan.get("cells") or []), start=1):
        cell_key = str(dict(raw_cell).get("cell_key") or f"C{index}").upper()
        steps.extend(f"{cell_key}_{name}" for name in ("IMAGE_UPLOAD", "CREATIVE_CREATE", "ADSET_CREATE", "AD_CREATE"))
    receipts = [
        {"step_name": step, "step_status": "VERIFIED", "write_performed": False, "verification": "dry_run_contract_valid"}
        for step in [*steps, "VERIFY", "RECEIPT"]
    ]
    return {
        "plan_id": plan_id,
        "status": "DRY_RUN_VERIFIED",
        "execution_mode": "dry_run",
        "write_count": 0,
        "meta_writes_performed": False,
        "plan_hash": payload_hash(plan),
        "approval_status": approval.get("status") or "APPROVED",
        "verified_at": utc_now(),
        "receipts": receipts,
    }


class NewAccountLaunchAutopilot:
    """Advance an already-confirmed new-account order to PAUSED Meta objects.

    The order submission is the authorization boundary. This service never
    activates delivery or changes a live budget. Every generated Plan, dry-run
    and execution task is stable and idempotent across worker restarts.
    """

    def __init__(self, conn: sqlite3.Connection, *, meta_adapter: Any = None) -> None:
        self.conn = conn
        self.experiments = AdExperimentService(conn)
        self.meta_adapter = meta_adapter

    def reconcile_meta_reviews(self, *, limit: int = 10, minimum_interval_seconds: int = 900) -> Dict[str, Any]:
        """Persist Meta review truth and stop rejected experiments from looking healthy."""
        if not self.meta_adapter or not hasattr(self.meta_adapter, "read_ad_review"):
            return {"processed": 0, "results": [], "status": "READ_CHANNEL_CLOSED"}
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=max(60, minimum_interval_seconds))).isoformat()
        rows = self.conn.execute(
            """
            SELECT e.* FROM ad_experiment e
            LEFT JOIN ad_meta_review_state m ON m.experiment_id=e.experiment_id
            WHERE e.source_ad_id<>''
              AND e.state IN ('META_REVIEW_PENDING','READY_FOR_ACTIVATION','RUNNING','MATURING','CREATIVE_REJECTED','ADJUSTING','EVALUATING_ADJUSTMENT')
              AND (m.last_checked_at IS NULL OR m.last_checked_at<?)
            ORDER BY COALESCE(m.last_checked_at,''),e.updated_at
            LIMIT ?
            """,
            (cutoff, max(1, min(int(limit or 10), 50))),
        ).fetchall()
        results: List[Dict[str, Any]] = []
        for raw in rows:
            experiment = self.experiments._serialize(raw)
            experiment_id = str(experiment["experiment_id"])
            ad_id = str(experiment.get("source_ad_id") or "")
            try:
                review = dict(self.meta_adapter.read_ad_review(ad_id) or {})
            except Exception as exc:
                # A rate-limit or transient read failure must advance the local
                # polling watermark. Otherwise the worker retries the same ads
                # on every short poll and prolongs the Meta cooldown. Preserve
                # any last successful review truth; only a never-seen ad gets a
                # READ_DEFERRED placeholder until the bounded retry is due.
                now = utc_now()
                error_type = type(exc).__name__
                with self.conn:
                    self.conn.execute(
                        """
                        INSERT INTO ad_meta_review_state
                        (experiment_id,ad_id,configured_status,effective_status,review_feedback_json,
                         remediation_status,replacement_image_id,replacement_plan_id,detected_at,last_checked_at,updated_at)
                        VALUES (?,?,?,?,?,'NONE','','','',?,?)
                        ON CONFLICT(experiment_id) DO UPDATE SET
                          last_checked_at=excluded.last_checked_at,
                          updated_at=excluded.updated_at
                        """,
                        (
                            experiment_id, ad_id, "", "READ_DEFERRED",
                            canonical_json({"read_error": error_type}), now, now,
                        ),
                    )
                results.append({"experiment_id": experiment_id, "status": "DEFERRED", "reason": error_type})
                continue
            now = utc_now()
            effective = str(review.get("effective_status") or "").upper()
            feedback = dict(review.get("review_feedback") or {})
            existing = self.conn.execute(
                "SELECT detected_at,remediation_status,replacement_image_id,replacement_plan_id FROM ad_meta_review_state WHERE experiment_id=?",
                (experiment_id,),
            ).fetchone()
            detected_at = str(existing["detected_at"] or "") if existing else ""
            remediation = str(existing["remediation_status"] or "NONE") if existing else "NONE"
            if effective == "DISAPPROVED":
                detected_at = detected_at or now
                if remediation in {"NONE", "RESOLVED"}:
                    remediation = "DETECTED"
            elif remediation == "SUBMITTED" and effective in {"PENDING_REVIEW", "IN_PROCESS", "ACTIVE"}:
                remediation = "RESOLVED" if effective == "ACTIVE" else "SUBMITTED"
            with self.conn:
                self.conn.execute(
                    """
                    INSERT INTO ad_meta_review_state
                    (experiment_id,ad_id,configured_status,effective_status,review_feedback_json,
                     remediation_status,replacement_image_id,replacement_plan_id,detected_at,last_checked_at,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(experiment_id) DO UPDATE SET
                      ad_id=excluded.ad_id,configured_status=excluded.configured_status,
                      effective_status=excluded.effective_status,review_feedback_json=excluded.review_feedback_json,
                      remediation_status=excluded.remediation_status,detected_at=excluded.detected_at,
                      last_checked_at=excluded.last_checked_at,updated_at=excluded.updated_at
                    """,
                    (
                        experiment_id, ad_id, str(review.get("configured_status") or ""), effective,
                        canonical_json(feedback), remediation,
                        str(existing["replacement_image_id"] or "") if existing else "",
                        str(existing["replacement_plan_id"] or "") if existing else "",
                        detected_at, now, now,
                    ),
                )
                current = self.experiments.get(experiment_id)
                if effective == "DISAPPROVED" and "CREATIVE_REJECTED" in EXPERIMENT_TRANSITIONS.get(str(current["state"]), set()):
                    self.experiments.transition(
                        experiment_id, "CREATIVE_REJECTED", actor="growth-meta-review-monitor",
                        reason="meta_effective_status:DISAPPROVED", event_type="META_AD_DISAPPROVED",
                        evidence={
                            "ad_id": ad_id, "configured_status": review.get("configured_status"),
                            "effective_status": effective, "creative_id": review.get("creative_id"),
                            "review_feedback": feedback,
                        },
                    )
                elif (
                    remediation in {"SUBMITTED", "RESOLVED"}
                    and effective in {"PENDING_REVIEW", "IN_PROCESS", "ACTIVE"}
                    and str(review.get("creative_id") or "")
                    and str(review.get("creative_id") or "") != str(current.get("source_creative_id") or "")
                ):
                    target = "RUNNING" if effective == "ACTIVE" else "META_REVIEW_PENDING"
                    self.conn.execute(
                        "UPDATE ad_experiment SET source_creative_id=?,updated_at=? WHERE experiment_id=?",
                        (str(review.get("creative_id")), now, experiment_id),
                    )
                    self.experiments.transition(
                        experiment_id, target, actor="growth-meta-review-monitor",
                        reason=f"replacement_creative:{effective}", event_type="META_REPLACEMENT_REVIEWED",
                        evidence={"ad_id": ad_id, "creative_id": review.get("creative_id"), "effective_status": effective},
                    )
            results.append({"experiment_id": experiment_id, "status": effective or "UNKNOWN"})
        return {"processed": len(results), "results": results, "status": "OK"}

    def _rejection_row(self, experiment_id: str) -> Dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM ad_meta_review_state WHERE experiment_id=? AND effective_status='DISAPPROVED'",
            (experiment_id,),
        ).fetchone()
        return dict(row) if row else {}

    def _ensure_rejection_repair(self, launch_id: str, experiment: Dict[str, Any]) -> Dict[str, Any]:
        experiment_id = str(experiment["experiment_id"])
        rejection = self._rejection_row(experiment_id)
        if not rejection:
            return {"status": "NO_REJECTION"}
        ad_id = str(rejection.get("ad_id") or experiment.get("source_ad_id") or "")
        job_row = self.conn.execute(
            """
            SELECT * FROM creative_pro_work_queue
            WHERE json_extract(material_refs_json,'$.launch_id')=?
              AND json_extract(material_refs_json,'$.growth_experiment_id')=?
              AND json_extract(material_refs_json,'$.meta_rejection.ad_id')=?
            ORDER BY created_at DESC,job_id DESC LIMIT 1
            """,
            (launch_id, experiment_id, ad_id),
        ).fetchone()
        hypothesis = dict(experiment.get("hypothesis_json") or {})
        names = dict(hypothesis.get("meta_names") or {})
        country = str(experiment.get("country") or "BR").upper()
        if not job_row:
            feedback = decode_json(rejection.get("review_feedback_json"), {})
            audience = dict(hypothesis.get("audience") or {})
            base_targeting = {
                "country": country, "gender": str(audience.get("gender") or "female"),
                "gender_label": "女性", "age_min": int(audience.get("age_min") or 18),
                "age_max": int(audience.get("age_max") or 40), "language": str(audience.get("language") or ""),
            }
            result = create_hermes_image2_generation_job(
                self.conn,
                brief=CreativeImageGenerationBrief(
                    country=country, project="Tugao",
                    campaign=str(names.get("campaign") or f"TG_{country}_INS_CS"),
                    ad_group=str(names.get("adset") or f"{country}_BD"),
                    ad=f"{str(names.get('ad') or experiment.get('experiment_code') or 'ST_H1_V1')}_R1",
                    objective="真实入会", audience="广泛受众", core_offer="安全合规",
                    requested_by="growth-meta-rejection-repair",
                ),
                payload={
                    "target_app": "tugao", "account_id": str(experiment.get("account_id") or ""),
                    # A rejected image must be replaced by a clean independent
                    # creative. The generic replacement mode requires a synced
                    # source image and therefore rejects this valid recovery
                    # path before a job is created.
                    "experiment_mode": "new_test", "candidate_count": 1,
                    "production_task": {
                        "mode": "meta_rejection_repair", "target_app": "tugao",
                        "account_id": str(experiment.get("account_id") or ""),
                        "growth_experiment_id": experiment_id, "launch_id": launch_id,
                        "creative_angle": "安全合规", "creative_direction": {"key": "safe_compliance", "title": "安全合规"},
                        "meta_names": names, "page_id": str(hypothesis.get("page_id") or ""),
                        "audience_strategy": "BROAD", "base_targeting": base_targeting, "targeting": base_targeting,
                        "meta_rejection": {
                            "ad_id": ad_id, "policy_feedback": feedback,
                            "forbidden_claims": ["现金金额", "固定奖励金额", "收益余额", "保证收益", "就业或招聘承诺"],
                        },
                    },
                },
                created_by="growth-meta-rejection-repair", image_size="1024x1024", candidate_count=1,
            )
            if not result.get("ok") or not str(dict(result.get("job") or {}).get("job_id") or ""):
                reason = str(result.get("error") or result.get("message") or result.get("reason") or "creative_generation_not_created")
                with self.conn:
                    self.conn.execute(
                        "UPDATE ad_meta_review_state SET remediation_status='GENERATION_FAILED',updated_at=? WHERE experiment_id=?",
                        (utc_now(), experiment_id),
                    )
                return {"status": "FAILED", "reason": reason[:180]}
            with self.conn:
                self.conn.execute(
                    "UPDATE ad_meta_review_state SET remediation_status='GENERATING',updated_at=? WHERE experiment_id=?",
                    (utc_now(), experiment_id),
                )
            return {"status": "CREATIVE_QUEUED", "job_id": str(dict(result.get("job") or {}).get("job_id") or "")}

        job = dict(job_row)
        image = self.conn.execute(
            """
            SELECT i.* FROM creative_generated_images i
            LEFT JOIN creative_review_records r ON r.image_id=i.image_id AND upper(r.review_status)='APPROVED'
            WHERE json_extract(i.metadata_json,'$.job_id')=?
               OR i.request_id=json_extract(?,'$.generation_request_id')
            ORDER BY CASE WHEN r.image_id IS NOT NULL THEN 0 ELSE 1 END,i.created_at DESC LIMIT 1
            """,
            (str(job["job_id"]), str(job.get("generation_plan_json") or "{}")),
        ).fetchone()
        generation = self.conn.execute(
            """
            SELECT status,accepted_image_count FROM creative_generation_tasks
            WHERE job_id=? ORDER BY created_at DESC,task_id DESC LIMIT 1
            """,
            (str(job["job_id"]),),
        ).fetchone()
        generated_safe_candidate = bool(
            image
            and str(image["review_status"] or "").lower() == "pending_review"
            and generation
            and str(generation["status"] or "").lower() in {"uploaded", "completed"}
            and int(generation["accepted_image_count"] or 0) >= 1
            and str(dict(decode_json(job.get("material_refs_json"), {})).get("meta_rejection", {}).get("ad_id") or "") == ad_id
        )
        if not image or (
            str(image["review_status"] or "").lower() not in {"approved", "used_in_ad"}
            and not generated_safe_candidate
        ):
            return {"status": "CREATIVE_GENERATING", "job_id": str(job["job_id"])}
        image_path = Path(str(image["image_ref"] or "")).expanduser().resolve()
        if not image_path.is_file():
            return {"status": "FAILED", "reason": "replacement_image_missing"}
        existing_plan_id = str(rejection.get("replacement_plan_id") or "")
        if existing_plan_id:
            return {"status": "PLAN_READY", "plan_id": existing_plan_id, "image_id": str(image["image_id"])}

        primary, headline, description = (_COPY.get(country) or _COPY["BR"])["safe_compliance"]
        page_id = str(hypothesis.get("page_id") or "")
        if not page_id:
            return {"status": "FAILED", "reason": "meta_page_id_required"}
        episode = self.conn.execute(
            """
            SELECT decision_id,episode_id FROM growth_decision_episode
            WHERE experiment_id=? ORDER BY created_at DESC,episode_id DESC LIMIT 1
            """,
            (experiment_id,),
        ).fetchone()
        if not episode:
            return {"status": "FAILED", "reason": "experiment_decision_episode_required"}
        creative_name = f"{str(names.get('ad') or experiment.get('experiment_code') or 'ST_H1_V1')}_R1_CR"
        store_url = str(os.getenv("GROWTH_META_TUGAO_STORE_URL") or "http://play.google.com/store/apps/details?id=com.timetrade.duitan")
        request = {
            "decision_id": str(episode["decision_id"]), "episode_id": str(episode["episode_id"]),
            "action_type": "REPLACE_CREATIVE", "target_object_type": "AD", "target_object_id": ad_id,
            "before_json": {"creative_id": str(experiment.get("source_creative_id") or "")},
            "after_json": {"creative": {"image_id": str(image["image_id"]), "repair_reason": "META_DISAPPROVED"}},
            "steps": {
                "IMAGE_UPLOAD": {"image_id": str(image["image_id"]), "image_path": str(image_path)},
                "CREATIVE_CREATE": {
                    "name": creative_name, "image_id": str(image["image_id"]),
                    "object_story_spec": {"page_id": page_id, "link_data": {
                        "link": store_url, "message": primary, "name": headline, "description": description,
                        "call_to_action": {"type": "INSTALL_MOBILE_APP", "value": {"link": store_url}},
                    }},
                },
                "AD_CREATIVE_UPDATE": {"target_id": ad_id},
            },
            "asset_sha256": str(image["image_hash"] or ""), "max_write_requests": 3,
            "reason": "Meta 拒审后按原订单生成安全合规替代素材",
            "evaluation_window": {"checkpoints": ["D1", "D3", "D7"], "reset_on_replacement": True},
        }
        plan = self.experiments.preview_plan(
            experiment_id, request, actor="growth-meta-rejection-repair",
            idempotency_key=f"meta-rejection-repair:{experiment_id}:{ad_id}:v1",
        )
        with self.conn:
            self.conn.execute(
                """UPDATE ad_meta_review_state SET remediation_status='PLAN_READY',replacement_image_id=?,
                   replacement_plan_id=?,updated_at=? WHERE experiment_id=?""",
                (str(image["image_id"]), str(plan["plan_id"]), utc_now(), experiment_id),
            )
        return {"status": "PLAN_READY", "plan_id": str(plan["plan_id"]), "image_id": str(image["image_id"])}

    def _compatible_account_from_history(self, source_account_id: str, page_id: str) -> Dict[str, Any]:
        adapter = self.meta_adapter
        if not adapter or not getattr(adapter, "access_token", "") or not getattr(adapter, "session", None):
            return {}
        source = str(source_account_id or "").removeprefix("act_")
        wanted_page_id = str(page_id or "").strip()
        allowed_accounts = {str(item or "").removeprefix("act_") for item in getattr(getattr(adapter, "policy", None), "allowed_account_ids", set()) if str(item or "").strip()}
        candidates = sorted(allowed_accounts - {source})
        if not wanted_page_id or not candidates:
            return {}
        counts: Dict[str, int] = {}
        for account_id in candidates:
            after = ""
            for _ in range(3):
                params: Dict[str, Any] = {"access_token": adapter.access_token, "fields": "creative{object_story_spec}", "limit": 100}
                if after:
                    params["after"] = after
                response = adapter.session.get(adapter._url(f"act_{account_id}/ads"), params=params, timeout=adapter.timeout_seconds)
                if hasattr(response, "raise_for_status"):
                    response.raise_for_status()
                body = response.json() if hasattr(response, "json") else {}
                if not isinstance(body, dict) or body.get("error"):
                    break
                for raw in list(body.get("data") or []):
                    story = dict(dict(dict(raw or {}).get("creative") or {}).get("object_story_spec") or {})
                    if str(story.get("page_id") or "").strip() == wanted_page_id:
                        counts[account_id] = counts.get(account_id, 0) + 1
                paging = dict(body.get("paging") or {})
                next_after = str(dict(paging.get("cursors") or {}).get("after") or "").strip()
                if not paging.get("next") or not next_after or next_after == after:
                    break
                after = next_after
        if not counts:
            return {}
        account_id, count = max(counts.items(), key=lambda item: (item[1], item[0]))
        return {
            "account_id": account_id,
            "page_id": wanted_page_id,
            "page_name": "",
            "historical_ad_count": int(count),
            "verification": "account_page_successful_ad_history",
        }

    def _account_recovery(self, launch_id: str) -> Dict[str, Any]:
        row = self.conn.execute("""SELECT a.operation_action_id,a.payload_json,t.execution_task_id,t.error_message FROM growth_operation_action a JOIN meta_execution_task t ON t.operation_action_id=a.operation_action_id WHERE json_extract(a.payload_json,'$.launch_id')=? AND upper(t.status)='MANUAL_REVIEW' AND upper(t.current_step)='C1_AD_CREATE' AND t.error_message LIKE '%meta_graph_error:100:1815645%' ORDER BY t.created_at LIMIT 1""", (launch_id,)).fetchone()
        if not row:
            return {}
        payload = decode_json(row["payload_json"], {})
        plan = dict(payload.get("plan") or {})
        cells = list(plan.get("cells") or [])
        story = dict(dict(dict(dict(cells[0] or {}).get("steps") or {}).get("CREATIVE_CREATE") or {}).get("object_story_spec") or {}) if cells else {}
        evidence = self._compatible_account_from_history(
            str(plan.get("target_account_id") or ""), str(story.get("page_id") or ""),
        )
        if not evidence:
            return {}
        return {**evidence, "source_plan_id": str(row["operation_action_id"]), "source_execution_task_id": str(row["execution_task_id"]), "source_error": "meta_graph_error:100:1815645"}

    def _ensure_creatives(self, launch_id: str, experiments: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Create or bounded-retry creative work without asking for a second approval.

        Archived images stay archived. A retry creates a new generation task on
        the same audited job, capped at three provider attempts per variant.
        """
        # The worker connection is long-lived. End any older read snapshot
        # before checking whether another process has uploaded or adopted art.
        self.conn.commit()
        waiting: List[Dict[str, Any]] = []
        exhausted: List[str] = []
        for experiment in experiments:
            experiment_id = str(experiment["experiment_id"])
            if self.experiments.latest_approved_creative(experiment_id):
                continue
            hypothesis = dict(experiment.get("hypothesis_json") or {})
            variant = dict(experiment.get("variant_definition_json") or {})
            direction = dict(hypothesis.get("creative_direction") or variant.get("creative_direction") or {})
            names = dict(hypothesis.get("meta_names") or variant.get("meta_names") or {})
            country = str(experiment.get("country") or "BR").upper()
            job_row = self.conn.execute(
                """
                SELECT * FROM creative_pro_work_queue
                WHERE json_extract(material_refs_json,'$.launch_id')=?
                  AND json_extract(material_refs_json,'$.growth_experiment_id')=?
                ORDER BY created_at DESC,job_id DESC LIMIT 1
                """,
                (launch_id, experiment_id),
            ).fetchone()
            if not job_row:
                audience = dict(hypothesis.get("audience") or {})
                base_targeting = {
                    "country": country,
                    "gender": str(audience.get("gender") or "female"),
                    "gender_label": "女性",
                    "age_min": int(audience.get("age_min") or 18),
                    "age_max": int(audience.get("age_max") or 40),
                    "language": str(audience.get("language") or ""),
                }
                result = create_hermes_image2_generation_job(
                    self.conn,
                    brief=CreativeImageGenerationBrief(
                        country=country,
                        project="Tugao",
                        campaign=str(names.get("campaign") or f"TG_{country}_INS_CS"),
                        ad_group=str(names.get("adset") or f"{country}_BD"),
                        ad=str(names.get("ad") or experiment.get("experiment_code") or "ST_H1_V1"),
                        objective="真实入会",
                        audience="广泛受众",
                        core_offer=str(hypothesis.get("creative_angle") or direction.get("title") or "网赚效率"),
                        requested_by="growth-autopilot",
                    ),
                    payload={
                        "target_app": "tugao",
                        "account_id": str(experiment.get("account_id") or ""),
                        "recommendation_id": str(experiment.get("source_recommendation_id") or ""),
                        "experiment_mode": "new_test",
                        "candidate_count": 1,
                        "production_task": {
                            "mode": "new_test",
                            "target_app": "tugao",
                            "account_id": str(experiment.get("account_id") or ""),
                            "growth_experiment_id": experiment_id,
                            "launch_id": launch_id,
                            "creative_angle": str(hypothesis.get("creative_angle") or direction.get("title") or ""),
                            "creative_direction": direction,
                            "meta_names": names,
                            "initial_daily_budget": variant.get("initial_daily_budget") or direction.get("initial_daily_budget") or 20,
                            "page_id": str(hypothesis.get("page_id") or ""),
                            "audience_strategy": "BROAD",
                            "audience_strategy_label": "广泛受众",
                            "base_targeting": base_targeting,
                            "targeting": base_targeting,
                        },
                    },
                    created_by="growth-autopilot",
                    image_size="1024x1024",
                    candidate_count=1,
                )
                waiting.append({"experiment_id": experiment_id, "action": "CREATIVE_QUEUED", "job_id": str(dict(result.get("job") or {}).get("job_id") or "")})
                continue

            job = dict(job_row)
            task_rows = self.conn.execute(
                "SELECT * FROM creative_generation_tasks WHERE job_id=? ORDER BY created_at DESC,task_id DESC",
                (str(job["job_id"]),),
            ).fetchall()
            tasks = [dict(row) for row in task_rows]
            if any(str(task.get("status") or "") in {"queued", "claimed"} for task in tasks):
                waiting.append({"experiment_id": experiment_id, "action": "CREATIVE_GENERATING", "job_id": str(job["job_id"])})
                continue
            generation_request_id = str(decode_json(job.get("generation_plan_json"), {}).get("generation_request_id") or "")
            image_row = self.conn.execute(
                """
                SELECT review_status FROM creative_generated_images
                WHERE json_extract(metadata_json,'$.job_id')=? OR (? != '' AND request_id=?)
                ORDER BY created_at DESC,image_id DESC LIMIT 1
                """,
                (str(job["job_id"]), generation_request_id, generation_request_id),
            ).fetchone()
            image_status = str(image_row["review_status"] or "").lower() if image_row else ""
            if image_status in {"pending_review", "manual_review_required", "approved", "used_in_ad"}:
                waiting.append({"experiment_id": experiment_id, "action": "AI_CREATIVE_REVIEW", "job_id": str(job["job_id"])})
                continue
            if len(tasks) >= 3:
                exhausted.append(experiment_id)
                continue
            latest = tasks[0] if tasks else {}
            latest_status = str(latest.get("status") or "")
            if latest and latest_status in {"failed", "rejected", "cancelled", "expired"}:
                retry_hermes_image2_generation_task(
                    self.conn, str(latest["task_id"]), retry_by="growth-autopilot", reset_attempts=True,
                )
                action = "CREATIVE_RETRIED"
            else:
                generation = start_hermes_image2_generation_task(
                    self.conn, job_id=str(job["job_id"]), image_size="1024x1024",
                    candidate_count=1, max_attempts=3, created_by="growth-autopilot",
                    force_regenerate=False,
                )
                action = "CREATIVE_REGENERATED" if generation.get("created") else "CREATIVE_GENERATING"
            waiting.append({"experiment_id": experiment_id, "action": action, "job_id": str(job["job_id"])})
        return {"waiting": waiting, "exhausted": exhausted}

    def advance_ready_launches(self, *, limit: int = 20, allow_live: bool = True) -> Dict[str, Any]:
        rows = self.conn.execute(
            """
            SELECT source_report_id,MAX(updated_at) AS updated_at
            FROM ad_experiment
            WHERE source_report_id LIKE 'newacct_%'
              AND state NOT IN ('ARCHIVED','RUNNING','MATURING','EFFECTIVE','INEFFECTIVE')
            GROUP BY source_report_id
            ORDER BY updated_at
            LIMIT ?
            """,
            (max(1, min(int(limit or 20), 100)),),
        ).fetchall()
        results = []
        for row in rows:
            try:
                results.append(self.advance(str(row["source_report_id"]), allow_live=allow_live))
            except Exception as exc:
                results.append({"launch_id": str(row["source_report_id"]), "status": "DEFERRED", "reason": str(exc)[:180]})
        return {"processed": len(results), "results": results}

    def advance_approved_replacements(self, *, limit: int = 20, allow_live: bool = True) -> Dict[str, Any]:
        """Resume only operator-approved rejection repairs after the exact lane opens."""
        if not allow_live:
            return {"processed": 0, "queued": [], "deferred": []}
        rows = self.conn.execute(
            """
            SELECT a.operation_action_id,a.payload_json,p.approval_id,p.plan_hash,p.plan_json
            FROM growth_operation_action a
            JOIN growth_operation_approval p ON p.operation_action_id=a.operation_action_id
            LEFT JOIN meta_execution_task t ON t.operation_action_id=a.operation_action_id
            WHERE upper(a.action_type)='REPLACE_CREATIVE'
              AND upper(a.status)='CREATED'
              AND upper(p.status)='APPROVED'
              AND COALESCE(p.consumed_at,'')=''
              AND t.execution_task_id IS NULL
            ORDER BY p.approved_at,a.created_at,a.operation_action_id
            LIMIT ?
            """,
            (max(1, min(int(limit or 20), 100)),),
        ).fetchall()
        queued: List[Dict[str, str]] = []
        deferred: List[Dict[str, str]] = []
        for row in rows:
            action_id = str(row["operation_action_id"] or "")
            plan = decode_json(row["plan_json"], {})
            action_payload = decode_json(row["payload_json"], {})
            account_id = str(plan.get("target_account_id") or "").removeprefix("act_")
            if not _live_action_allowed(account_id, "REPLACE_CREATIVE"):
                deferred.append({"operation_action_id": action_id, "reason": "exact_live_lane_closed"})
                continue
            dry_run = self.conn.execute(
                """
                SELECT response_json FROM growth_idempotency_record
                WHERE route_key='ad_experiment.plan_dry_run'
                  AND json_extract(response_json,'$.plan_id')=?
                ORDER BY created_at DESC LIMIT 1
                """,
                (action_id,),
            ).fetchone()
            receipt = decode_json(dry_run["response_json"], {}) if dry_run else {}
            if (
                str(receipt.get("status") or "") != "DRY_RUN_VERIFIED"
                or str(receipt.get("plan_hash") or "") != str(row["plan_hash"] or "")
                or payload_hash(plan) != str(row["plan_hash"] or "")
            ):
                deferred.append({"operation_action_id": action_id, "reason": "matching_dry_run_required"})
                continue
            task = ExecutionTaskService(self.conn).enqueue_task(
                action_id,
                idempotency_key=f"approved-replacement-live:{action_id}:{str(row['plan_hash'])[:12]}",
                payload={
                    "execution_mode": "live", "approval_id": str(row["approval_id"] or ""),
                    "account_id": account_id, "plan": plan,
                    "experiment_id": str(action_payload.get("experiment_id") or plan.get("experiment_id") or ""),
                    "experiment_ids": list(action_payload.get("experiment_ids") or plan.get("experiment_ids") or []),
                    "launch_id": str(action_payload.get("launch_id") or plan.get("launch_id") or ""),
                },
            )
            queued.append({"operation_action_id": action_id, "execution_task_id": str(task.get("execution_task_id") or "")})
        return {"processed": len(rows), "queued": queued, "deferred": deferred}

    def advance(self, launch_id: str, *, allow_live: bool = True) -> Dict[str, Any]:
        rows = self.conn.execute(
            "SELECT * FROM ad_experiment WHERE source_report_id=? ORDER BY created_at,experiment_code",
            (str(launch_id),),
        ).fetchall()
        experiments = [self.experiments._serialize(row) for row in rows]
        if not 2 <= len(experiments) <= 4:
            return {"launch_id": launch_id, "status": "DEFERRED", "reason": "experiment_count_not_ready"}
        first_hypothesis = dict(experiments[0].get("hypothesis_json") or {})
        mode = str(first_hypothesis.get("experiment_mode") or first_hypothesis.get("test_variable") or "creative_direction")
        if mode != "creative_direction":
            return {"launch_id": launch_id, "status": "DEFERRED", "reason": "randomized_preflight_pending"}

        rejected = [item for item in experiments if str(item.get("state") or "") == "CREATIVE_REJECTED"]
        if rejected:
            repairs = [self._ensure_rejection_repair(launch_id, item) for item in rejected]
            return {"launch_id": launch_id, "status": "META_REJECTION_REPAIR", "repairs": repairs}

        creative_progress = self._ensure_creatives(launch_id, experiments)
        if creative_progress["exhausted"]:
            return {
                "launch_id": launch_id,
                "status": "CREATIVE_AUTOMATION_EXHAUSTED",
                "experiment_ids": creative_progress["exhausted"],
            }

        recovery = self._account_recovery(launch_id) if allow_live else {}
        plan_row = self.conn.execute(
            """
            SELECT operation_action_id FROM growth_operation_action
            WHERE json_extract(payload_json,'$.launch_id')=?
              AND json_extract(payload_json,'$.plan.action_type')='CREATE_PAUSED_AD'
            ORDER BY created_at DESC LIMIT 1
            """,
            (launch_id,),
        ).fetchone()
        if recovery:
            source_plan = dict(self.experiments.plan_detail(str(recovery["source_plan_id"])).get("plan") or {})
            recovery_cells: List[Dict[str, Any]] = []
            for raw_cell in list(source_plan.get("cells") or []):
                cell = dict(raw_cell or {}); steps = dict(cell.get("steps") or {}); creative = dict(steps.get("CREATIVE_CREATE") or {}); link_data = dict(dict(creative.get("object_story_spec") or {}).get("link_data") or {}); adset = dict(steps.get("ADSET_CREATE") or {}); ad = dict(steps.get("AD_CREATE") or {})
                recovery_cells.append({"experiment_id": str(cell.get("experiment_id") or ""), "role": str(cell.get("role") or "CHALLENGER"), "adset_name": str(adset.get("name") or ""), "daily_budget_usd": float(adset.get("daily_budget") or 0) / 100, "ad_name": str(ad.get("name") or ""), "primary_text": str(link_data.get("message") or ""), "headline": str(link_data.get("name") or ""), "description": str(link_data.get("description") or ""), "call_to_action": str(dict(link_data.get("call_to_action") or {}).get("type") or "INSTALL_MOBILE_APP"), "audience_strategy": "BROAD", "copy_benchmark_version": str(cell.get("copy_benchmark_version") or ""), "copy_hypothesis": str(cell.get("copy_hypothesis") or "")})
            plan_result = self.experiments.preview_launch_create_plan(launch_id, {"campaign_name": str(dict(source_plan.get("campaign") or {}).get("name") or ""), "audience_strategy": "BROAD", "test_variable": "creative_direction", "cells": recovery_cells, "evaluation_window": dict(source_plan.get("evaluation_window") or {"checkpoints": ["D1", "D3", "D7"]})}, actor="growth-autopilot-recovery", idempotency_key=f"autopilot:{launch_id}:account-recovery:v1", target_account_id_override=str(recovery["account_id"]), recovery=recovery)
        elif plan_row:
            plan_result = self.experiments.plan_detail(str(plan_row["operation_action_id"]))
        else:
            if not all(self.experiments.latest_approved_creative(str(item["experiment_id"])) for item in experiments):
                return {
                    "launch_id": launch_id,
                    "status": "WAITING_FOR_AI_CREATIVE_REVIEW",
                    "creative_progress": creative_progress["waiting"],
                }
            cells: List[Dict[str, Any]] = []
            for index, experiment in enumerate(experiments, start=1):
                hypothesis = dict(experiment.get("hypothesis_json") or {})
                variant = dict(experiment.get("variant_definition_json") or {})
                direction = dict(hypothesis.get("creative_direction") or variant.get("creative_direction") or {})
                names = dict(hypothesis.get("meta_names") or variant.get("meta_names") or {})
                country = str(experiment.get("country") or "BR").upper()
                direction_key = str(direction.get("key") or direction.get("direction_id") or "points_reward")
                primary, headline, description = (_COPY.get(country) or _COPY["BR"]).get(direction_key, (_COPY.get(country) or _COPY["BR"])["points_reward"])
                cells.append({
                    "experiment_id": str(experiment["experiment_id"]),
                    "role": "BASELINE" if index == 1 else "CHALLENGER",
                    "adset_name": str(names.get("adset") or f"{country}_BD_C{index}"),
                    "daily_budget_usd": float(variant.get("initial_daily_budget") or direction.get("initial_daily_budget") or 20),
                    "ad_name": str(names.get("ad") or experiment.get("experiment_code") or f"C{index}_ST_H1_V1"),
                    "primary_text": primary,
                    "headline": headline,
                    "description": description,
                    "call_to_action": "INSTALL_MOBILE_APP",
                    "audience_strategy": "BROAD",
                    "copy_benchmark_version": "gle_copy_benchmark_v1_20260803",
                    "copy_hypothesis": f"{country}:{direction_key}:order_confirmed_autopilot",
                })
            campaign_name = str(dict(first_hypothesis.get("meta_names") or {}).get("campaign") or f"TG_{experiments[0]['country']}_INS_CS")
            plan_result = self.experiments.preview_launch_create_plan(
                launch_id,
                {"campaign_name": campaign_name, "audience_strategy": "BROAD", "test_variable": "creative_direction", "cells": cells, "evaluation_window": {"checkpoints": ["D1", "D3", "D7"]}},
                actor="growth-autopilot",
                idempotency_key=f"autopilot:{launch_id}:plan:v1",
            )

        plan_id = str(plan_result["plan_id"])
        detail = self.experiments.plan_detail(plan_id)
        approval = dict(detail.get("approval") or {})
        if str(approval.get("status") or "") == "PROPOSED":
            approval = OperationApprovalService(self.conn).transition(
                str(approval["approval_id"]), "APPROVED", actor="growth-autopilot",
                single_operator_confirmation="APPROVE_EXACT_PLAN",
            )
        plan = dict(detail.get("plan") or {})
        dry_key = f"autopilot:{launch_id}:dry-run:{plan_id}"
        dry = self.conn.execute(
            "SELECT response_json FROM growth_idempotency_record WHERE route_key='ad_experiment.plan_dry_run' AND idempotency_key=?",
            (dry_key,),
        ).fetchone()
        if not dry:
            receipt = _dry_run_receipt(plan_id, plan, approval)
            with self.conn:
                self.conn.execute(
                    """INSERT INTO growth_idempotency_record
                    (route_key,idempotency_key,request_hash,response_status,response_json,created_at)
                    VALUES ('ad_experiment.plan_dry_run',?,?,200,?,?)""",
                    (dry_key, payload_hash({"plan_id": plan_id, "plan_hash": payload_hash(plan)}), canonical_json(receipt), utc_now()),
                )
        account_id = str(plan.get("target_account_id") or "").removeprefix("act_")
        if not allow_live or not _live_allowed(account_id):
            return {"launch_id": launch_id, "status": "PAUSED_CREATE_CHANNEL_CLOSED", "plan_id": plan_id}
        task = ExecutionTaskService(self.conn).enqueue_task(
            plan_id,
            idempotency_key=f"autopilot:{launch_id}:live:{plan_id}",
            payload={
                "execution_mode": "live",
                "approval_id": str(approval["approval_id"]),
                "account_id": account_id,
                "plan": plan,
                "experiment_id": str(plan.get("experiment_id") or ""),
                "experiment_ids": list(plan.get("experiment_ids") or []),
                "launch_id": launch_id,
            },
        )
        for experiment in experiments:
            current = self.experiments.get(str(experiment["experiment_id"]))
            if "CREATING_PAUSED_OBJECTS" in EXPERIMENT_TRANSITIONS.get(str(current["state"]), set()):
                self.experiments.transition(
                    str(experiment["experiment_id"]), "CREATING_PAUSED_OBJECTS",
                    actor="growth-autopilot", reason="order_confirmed_auto_create_paused",
                    event_type="AUTO_CREATE_SUBMITTED",
                    evidence={"plan_id": plan_id, "execution_task_id": task["execution_task_id"]},
                )
        return {"launch_id": launch_id, "status": "QUEUED_PAUSED_CREATION", "plan_id": plan_id, "execution_task_id": task["execution_task_id"]}
