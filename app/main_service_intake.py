from __future__ import annotations

from app.main_shared import *
from app.streamer_app_fan import reconcile_streamer_app_fans
from app.timo_guild_identity import (
    require_timo_guild_identity,
    timo_guild_contract_fields,
    timo_guild_display_name,
    timo_guild_storage_name,
)


class IntakeServiceMixin:
    def upsert_lead(self, payload: LeadUpsertRequest) -> Dict[str, Any]:
        now = utc_now()
        parser_confidence = getattr(payload, 'parser_confidence', None)
        parser_missing_fields = getattr(payload, 'parser_missing_fields', []) or []
        parser_conflicts = getattr(payload, 'parser_conflicts', []) or []
        parser_raw_text = getattr(payload, 'parser_raw_text', None)
        parser_raw_ocr_text = getattr(payload, 'parser_raw_ocr_text', None)
        parser_version = getattr(payload, 'parser_version', 'manual_cs_parser_v2')
        parser_status = getattr(payload, 'parser_status', 'unknown')
        review_reason_codes = getattr(payload, 'review_reason_codes', []) or []
        routing_decision = getattr(payload, 'routing_decision', None)
        recommended_next_action = getattr(payload, 'recommended_next_action', None)
        review_status = getattr(payload, 'review_status', 'not_needed')
        if is_external_app_id_only_phone(payload.mobile):
            normalized_mobile = str(payload.mobile or '').strip()
            normalized_area_code = int(payload.area_code or 0)
            normalized_country = str(payload.country or '').strip() or EXTERNAL_APP_ID_ONLY_COUNTRY_PLACEHOLDER
        else:
            normalized_mobile, normalized_area_code, normalized_country = normalize_phone_identity(
                mobile=payload.mobile,
                area_code=payload.area_code,
                country=payload.country,
            )
        executor_contract = guild_country_contract(self.resolve_guild_executor(payload.dept_name))
        assigned_guild_country = executor_contract['guild_country']
        cross_country_fallback = bool(
            assigned_guild_country and normalized_country
            and assigned_guild_country.casefold() != normalized_country.casefold()
            and countries_match(normalized_country, executor_contract['eligible_user_countries'])
        )
        cross_country_fallback_reason = 'eligible_country_compatibility' if cross_country_fallback else ''
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT lead_id, matched_customer_id FROM leads WHERE area_code = ? AND mobile = ?",
                (normalized_area_code, normalized_mobile),
            ).fetchone()
            if row:
                conn.execute(
                    """
                    UPDATE leads
                    SET trace_id = ?, source_platform = ?, source_campaign = ?, source_page_id = ?, country = ?,
                        assigned_guild_country = ?, cross_country_fallback = ?, cross_country_fallback_reason = ?,
                        yw_id = COALESCE(?, yw_id), app_name = COALESCE(?, app_name), dept_name = COALESCE(?, dept_name),
                        pendaftaran_group = COALESCE(?, pendaftaran_group), inviter_id = COALESCE(?, inviter_id),
                        parser_confidence = COALESCE(?, parser_confidence),
                        parser_missing_fields = ?, parser_conflicts = ?, parser_raw_text = COALESCE(?, parser_raw_text),
                        parser_raw_ocr_text = COALESCE(?, parser_raw_ocr_text), parser_version = ?, parser_status = ?,
                        review_reason_codes = ?, routing_decision = COALESCE(?, routing_decision),
                        recommended_next_action = COALESCE(?, recommended_next_action), review_status = ?, updated_at = ?
                    WHERE lead_id = ?
                    """,
                    (
                        payload.trace_id,
                        payload.source_platform,
                        payload.source_campaign,
                        payload.source_page_id,
                        normalized_country,
                        assigned_guild_country,
                        1 if cross_country_fallback else 0,
                        cross_country_fallback_reason,
                        payload.yw_id,
                        payload.app_name,
                        payload.dept_name,
                        payload.pendaftaran_group,
                        payload.inviter_id,
                        parser_confidence,
                        json.dumps(parser_missing_fields, ensure_ascii=False),
                        json.dumps(parser_conflicts, ensure_ascii=False),
                        parser_raw_text,
                        parser_raw_ocr_text,
                        parser_version,
                        parser_status,
                        json.dumps(review_reason_codes, ensure_ascii=False),
                        routing_decision,
                        recommended_next_action,
                        review_status,
                        now,
                        row["lead_id"],
                    ),
                )
                return {
                    "lead_id": row["lead_id"],
                    "matched_customer_id": row["matched_customer_id"],
                    "is_new": False,
                    "current_status": "new",
                }

            lead_id = create_id("lead")
            customer_id = create_id("cust")
            conn.execute(
                """
                INSERT INTO leads (
                    lead_id, trace_id, source_platform, source_campaign, source_page_id, country,
                    assigned_guild_country, cross_country_fallback, cross_country_fallback_reason, area_code, mobile,
                    yw_id, app_name, dept_name, pendaftaran_group, inviter_id,
                    parser_confidence, parser_missing_fields, parser_conflicts, parser_raw_text, parser_raw_ocr_text,
                    parser_version, parser_status, review_reason_codes, routing_decision, recommended_next_action, review_status,
                    current_status, matched_customer_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    lead_id,
                    payload.trace_id,
                    payload.source_platform,
                    payload.source_campaign,
                    payload.source_page_id,
                    normalized_country,
                    assigned_guild_country,
                    1 if cross_country_fallback else 0,
                    cross_country_fallback_reason,
                    normalized_area_code,
                    normalized_mobile,
                    payload.yw_id,
                    payload.app_name,
                    payload.dept_name,
                    payload.pendaftaran_group,
                    payload.inviter_id,
                    parser_confidence,
                    json.dumps(parser_missing_fields, ensure_ascii=False),
                    json.dumps(parser_conflicts, ensure_ascii=False),
                    parser_raw_text,
                    parser_raw_ocr_text,
                    parser_version,
                    parser_status,
                    json.dumps(review_reason_codes, ensure_ascii=False),
                    routing_decision,
                    recommended_next_action,
                    review_status,
                    "new",
                    customer_id,
                    now,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO customer_projection (customer_id, lead_id, mobile, area_code, yw_id, pendaftaran_group, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (customer_id, lead_id, normalized_mobile, normalized_area_code, payload.yw_id, payload.pendaftaran_group, now),
            )
            self._record_status_history(
                conn,
                lead_id=lead_id,
                from_status=None,
                to_status="new",
                trigger_type="lead_created",
                trigger_source="leads_upsert",
            )
            return {
                "lead_id": lead_id,
                "matched_customer_id": customer_id,
                "is_new": True,
                "current_status": "new",
            }

    def collect_event(self, payload: EventCollectRequest) -> Dict[str, Any]:
        event_id = create_id("evt")
        now = utc_now()
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO lead_events (
                    event_id, lead_id, trace_id, event_type, event_source, event_value, page_id, session_id,
                    operator_id, operator_name, raw_payload, happened_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    payload.lead_id,
                    payload.trace_id,
                    payload.event_type,
                    payload.event_source,
                    payload.event_value,
                    payload.page_id,
                    payload.session_id,
                    payload.operator_id,
                    payload.operator_name,
                    json.dumps(payload.raw_payload, ensure_ascii=False),
                    payload.happened_at or now,
                    now,
                ),
            )
            if payload.lead_id and payload.event_type in {"contact_clicked", "account_id_submitted", "wa_redirected"}:
                current = conn.execute("SELECT current_status FROM leads WHERE lead_id = ?", (payload.lead_id,)).fetchone()
                from_status = current["current_status"] if current else None
                conn.execute("UPDATE leads SET current_status = ?, updated_at = ? WHERE lead_id = ?", ("engaged", now, payload.lead_id))
                self._record_status_history(
                    conn,
                    lead_id=payload.lead_id,
                    from_status=from_status,
                    to_status="engaged",
                    trigger_type=payload.event_type,
                    trigger_source=payload.event_source,
                    trigger_event_id=event_id,
                    operator_id=payload.operator_id,
                    operator_name=payload.operator_name,
                )
        return {"event_id": event_id, "accepted": True}

    def create_task(self, payload: TaskCreateRequest) -> Dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute("SELECT task_id, status FROM automation_tasks WHERE dedupe_key = ?", (payload.dedupe_key,)).fetchone()
            if row:
                return {"task_id": row["task_id"], "status": row["status"]}
            task_id = create_id("task")
            conn.execute(
                """
                INSERT INTO automation_tasks (
                    task_id, lead_id, task_type, priority, payload, dedupe_key, created_by, created_at, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    payload.lead_id,
                    payload.task_type,
                    payload.priority,
                    json.dumps(payload.payload, ensure_ascii=False),
                    payload.dedupe_key,
                    payload.created_by,
                    payload.created_at,
                    "pending",
                ),
            )
            conn.execute("UPDATE leads SET current_status = ?, updated_at = ? WHERE lead_id = ?", ("processing", utc_now(), payload.lead_id))
            return {"task_id": task_id, "status": "pending"}

    def task_result(self, task_id: str, payload: TaskResultRequest) -> Dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute("SELECT lead_id FROM automation_tasks WHERE task_id = ?", (task_id,)).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="task not found")
            conn.execute(
                """
                UPDATE automation_tasks
                SET status = ?, result_code = ?, result_reason = ?, toast_text = ?, evidence_url = ?, retry_count = ?,
                    executor_type = ?, executor_id = ?, finished_at = ?, raw_result = ?
                WHERE task_id = ?
                """,
                (
                    payload.status,
                    payload.result_code,
                    payload.result_reason,
                    payload.toast_text,
                    payload.evidence_url,
                    payload.retry_count,
                    payload.executor_type,
                    payload.executor_id,
                    payload.finished_at,
                    json.dumps(payload.raw_result, ensure_ascii=False),
                    task_id,
                ),
            )
            lead_status = "success" if payload.status == "success" else "failed" if payload.status == "failed" else "manual_review"
            conn.execute("UPDATE leads SET current_status = ?, updated_at = ? WHERE lead_id = ?", (lead_status, utc_now(), row["lead_id"]))
            return {"task_id": task_id, "crm_sync_status": "pending", "next_action": "sync_customer"}

    def customer_sync(self, payload: CustomerSyncRequest) -> Dict[str, Any]:
        now = utc_now()
        normalized_mobile, normalized_area_code, _ = normalize_phone_identity(
            mobile=payload.mobile,
            area_code=int(payload.area_code or 0),
            country='',
        )
        with self.db.connect() as conn:
            lead = conn.execute("SELECT matched_customer_id FROM leads WHERE lead_id = ?", (payload.lead_id,)).fetchone()
            if not lead:
                raise HTTPException(status_code=404, detail="lead not found")
            customer_id = lead["matched_customer_id"]
            row = conn.execute("SELECT customer_id FROM customer_projection WHERE customer_id = ?", (customer_id,)).fetchone()
            action = "update" if row else "insert"
            patch = payload.crm_patch
            if row:
                conn.execute(
                    """
                    UPDATE customer_projection
                    SET yw_id = COALESCE(?, yw_id), pendaftaran_group = COALESCE(?, pendaftaran_group),
                        payment_status = COALESCE(?, payment_status), user_quality = COALESCE(?, user_quality),
                        remark = COALESCE(?, remark), join_group = COALESCE(?, join_group),
                        file_url = COALESCE(?, file_url), pz_status = COALESCE(?, pz_status), updated_at = ?
                    WHERE customer_id = ?
                    """,
                    (
                        payload.yw_id,
                        patch.get("pendaftaran_group"),
                        patch.get("payment_status"),
                        patch.get("user_quality"),
                        patch.get("remark"),
                        patch.get("join_group"),
                        patch.get("file_url"),
                        patch.get("pz_status"),
                        now,
                        customer_id,
                    ),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO customer_projection (
                        customer_id, lead_id, mobile, area_code, yw_id, pendaftaran_group, payment_status,
                        user_quality, remark, join_group, file_url, pz_status, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        customer_id,
                        payload.lead_id,
                        normalized_mobile,
                        normalized_area_code,
                        payload.yw_id,
                        patch.get("pendaftaran_group"),
                        patch.get("payment_status"),
                        patch.get("user_quality"),
                        patch.get("remark"),
                        patch.get("join_group"),
                        patch.get("file_url"),
                        patch.get("pz_status"),
                        now,
                    ),
                )
            conn.execute(
                "INSERT INTO sync_logs (sync_log_id, lead_id, task_id, sync_type, target_system, status, request_snapshot, response_snapshot, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    create_id("sync"),
                    payload.lead_id,
                    payload.task_id,
                    payload.sync_mode,
                    "crm",
                    "success",
                    json.dumps(payload.crm_patch, ensure_ascii=False),
                    json.dumps({"customer_id": customer_id, "action": action}, ensure_ascii=False),
                    now,
                ),
            )
            conn.execute("UPDATE leads SET current_status = ?, updated_at = ? WHERE lead_id = ?", ("synced", now, payload.lead_id))
            self._queue_operator_notification(
                conn,
                lead_id=payload.lead_id,
                notification_type="crm_record_success",
                mobile=normalized_mobile,
                yw_id=payload.yw_id,
                write_result="success",
            )
            return {"customer_id": customer_id, "action": action, "sync_status": "success"}

    def submit_account(self, payload: AccountSubmissionRequest) -> Dict[str, Any]:
        now = utc_now()
        submission_type = (payload.submission_type or "").strip()
        if submission_type not in {"account_id", "screenshot"}:
            raise HTTPException(status_code=400, detail="unsupported submission_type")
        with self.db.connect() as conn:
            lead = conn.execute("SELECT lead_id FROM leads WHERE lead_id = ?", (payload.lead_id,)).fetchone()
            if not lead:
                raise HTTPException(status_code=404, detail="lead not found")

            submission_id = create_id("sub")
            recognition_status = "not_needed" if submission_type == "account_id" else "pending"
            normalized_account_id = None
            next_action = "queue_account_recognition"
            task_type = "account_recognition"
            task_payload = {
                "submission_id": submission_id,
                "lead_id": payload.lead_id,
                "submission_type": submission_type,
                "file_url": payload.file_url,
                "source_channel": payload.source_channel,
                "expected_guild": payload.expected_guild,
                "source_bot_app_id": payload.source_bot_app_id,
                "source_message_id": payload.source_message_id,
                "source_chat_id": payload.source_chat_id,
            }
            current_status = "recognition_pending"

            if submission_type == "account_id":
                if not str(payload.account_id or "").isdigit():
                    raise HTTPException(status_code=400, detail="account_id must be numeric")
                normalized_account_id = str(payload.account_id)
                next_action = "queue_bind_check"
                task_type = "bind_check"
                task_payload = {
                    "submission_id": submission_id,
                    "lead_id": payload.lead_id,
                    "account_id": normalized_account_id,
                    "source_channel": payload.source_channel,
                    "expected_guild": payload.expected_guild,
                    "route_snapshot": payload.route_snapshot or {},
                    "source_bot_app_id": payload.source_bot_app_id,
                    "source_message_id": payload.source_message_id,
                    "source_chat_id": payload.source_chat_id,
                }
                current_status = "account_submitted"

            active_task = None
            if task_type == "bind_check":
                active_task = conn.execute(
                    """
                    SELECT task_id, status
                    FROM automation_tasks
                    WHERE lead_id = ? AND task_type = 'bind_check' AND status IN ('pending', 'processing')
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (payload.lead_id,),
                ).fetchone()
            reused_task_id = active_task['task_id'] if active_task else None

            conn.execute(
                """
                INSERT INTO account_submissions (
                    submission_id, lead_id, task_id, submission_type, account_id, account_id_type,
                    file_url, file_type, source_channel, submitted_by, recognition_status,
                    recognized_account_id, recognition_raw, submitted_at, remark, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    submission_id,
                    payload.lead_id,
                    reused_task_id or payload.task_id,
                    submission_type,
                    payload.account_id,
                    payload.account_id_type,
                    payload.file_url,
                    payload.file_type,
                    payload.source_channel,
                    payload.submitted_by,
                    recognition_status,
                    normalized_account_id,
                    json.dumps({}, ensure_ascii=False),
                    payload.submitted_at,
                    payload.remark,
                    now,
                    now,
                ),
            )

            if reused_task_id:
                conn.execute(
                    "UPDATE leads SET current_status = ?, updated_at = ? WHERE lead_id = ?",
                    (current_status, now, payload.lead_id),
                )
                self._record_status_history(
                    conn,
                    lead_id=payload.lead_id,
                    from_status="engaged",
                    to_status=current_status,
                    trigger_type="account_submission_reused_active_bind_task",
                    trigger_source=payload.source_channel or "account_submissions",
                    trigger_task_id=reused_task_id,
                    operator_name=payload.submitted_by,
                )
                conn.commit()
                self._notify_worker_new_work()
                return {
                    "accepted": True,
                    "submission_id": submission_id,
                    "normalized_account_id": normalized_account_id,
                    "next_action": next_action,
                    "task_type": task_type,
                    "task_id": reused_task_id,
                    "recognition_status": recognition_status,
                    "duplicate_task_reused": True,
                    "existing_task_status": active_task['status'] if active_task else None,
                }

            dedupe_key = f"{task_type}:{payload.lead_id}:{submission_id}"
            task_id = create_id("task")
            conn.execute(
                """
                INSERT INTO automation_tasks (
                    task_id, lead_id, task_type, priority, payload, dedupe_key, created_by, created_at, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    payload.lead_id,
                    task_type,
                    "P0",
                    json.dumps(task_payload, ensure_ascii=False),
                    dedupe_key,
                    payload.submitted_by or "system",
                    now,
                    "pending",
                ),
            )
            conn.execute(
                "UPDATE account_submissions SET task_id = ?, updated_at = ? WHERE submission_id = ?",
                (task_id, now, submission_id),
            )
            conn.execute(
                "UPDATE leads SET current_status = ?, updated_at = ? WHERE lead_id = ?",
                (current_status, now, payload.lead_id),
            )
            self._record_status_history(
                conn,
                lead_id=payload.lead_id,
                from_status="engaged",
                to_status=current_status,
                trigger_type="account_submission",
                trigger_source=payload.source_channel or "account_submissions",
                trigger_task_id=task_id,
                operator_name=payload.submitted_by,
            )
            conn.commit()
            self._notify_worker_new_work()
            return {
                "accepted": True,
                "submission_id": submission_id,
                "normalized_account_id": normalized_account_id,
                "next_action": next_action,
                "task_type": task_type,
                "task_id": task_id,
                "recognition_status": recognition_status,
            }

    def submit_manual_cs(self, payload: ManualCsSubmissionRequest) -> Dict[str, Any]:
        source_key = str(payload.submitted_by or payload.source_channel or 'manual_cs').strip() or 'manual_cs'
        if self.ingress_async_default:
            if not self.ingress_rate_limiter.allow(f'manual:{source_key}'):
                raise HTTPException(status_code=429, detail='manual intake rate limited')
            queued = self._enqueue_ingress_event(
                ingress_type='manual_cs_submission',
                source_key=source_key,
                payload=payload.dict(),
            )
            return {
                'accepted': True,
                'queued': True,
                'ingress_event_id': queued['event_id'],
                'duplicate': queued['duplicate'],
                'next_action': 'queued_for_processing',
            }
        return self._submit_manual_cs_sync(payload)

    def _submit_manual_cs_sync(self, payload: ManualCsSubmissionRequest) -> Dict[str, Any]:
        if not str(payload.mobile or '').strip() or not str(payload.registration_group or '').strip() or not str(payload.app_name or '').strip() or not str(payload.dept_name or '').strip() or not str(payload.submitted_by or '').strip() or not str(payload.submitted_at or '').strip():
            raise HTTPException(status_code=400, detail="mobile, registration_group, app_name, dept_name, submitted_by, submitted_at are required")
        if payload.submission_type == "account_id" and not str(payload.account_id or "").strip():
            raise HTTPException(status_code=400, detail="account_id is required when submission_type=account_id")
        if payload.submission_type == "screenshot" and not str(payload.file_url or "").strip():
            raise HTTPException(status_code=400, detail="file_url is required when submission_type=screenshot")
        if payload.submission_type == "screenshot" and not str(payload.file_type or "").strip():
            raise HTTPException(status_code=400, detail="file_type is required when submission_type=screenshot")

        payload_country_context = infer_country_context(payload.country)
        parser_text = (
            f"手机号 {payload.mobile} 应用 {payload.app_name} 公会 {payload.dept_name} 注册群组 {payload.registration_group} "
            f"ID {payload.account_id or ''} 个人邀请码 {payload.invite_code or ''} 国家 {payload_country_context or ''}"
        )
        if payload.remark:
            parser_text = f"{payload.remark}\n{parser_text}"
        parsed_payload = parse_manual_cs_message(text=parser_text, image_ocr_text=payload.image_ocr_text)

        id_only_cms_bind_input = is_external_app_id_only_phone(payload.mobile)
        if id_only_cms_bind_input:
            normalized_mobile = str(payload.mobile or '').strip()
            normalized_area_code = 0
            normalized_country = payload_country_context or str(parsed_payload.get('country') or '').strip()
        else:
            normalized_mobile, normalized_area_code, normalized_country = normalize_phone_identity(
                mobile=payload.mobile,
                area_code=0,
                country=payload_country_context or parsed_payload.get('country') or "",
            )
        explicit_fields = extract_explicit_intake_fields(payload.remark or '')
        explicit_app_name = str(explicit_fields.get('app_name') or '').strip()
        explicit_dept_name = str(explicit_fields.get('dept_name') or '').strip()
        explicit_invite_code = str(explicit_fields.get('invite_code') or '').strip().upper()
        if payload.app_name_explicit and not explicit_app_name:
            explicit_app_name = str(payload.app_name or '').strip()
        if payload.dept_name_explicit and not explicit_dept_name:
            explicit_dept_name = str(payload.dept_name or '').strip()
        if not explicit_invite_code:
            explicit_invite_code = str(payload.invite_code or '').strip().upper()

        current_default_app = str(self.lark_default_app_name or '').strip()
        current_default_dept = str(self.lark_default_dept_name or '').strip()
        prefer_payload_over_defaults = str(payload.source_channel or '').strip() in {'manual_cs_lark', 'ops_intake_workbench'}
        if id_only_cms_bind_input:
            final_mobile = normalized_mobile
            final_area_code = normalized_area_code
            final_country = normalized_country
        else:
            final_mobile = parsed_payload.get('mobile') or normalized_mobile
            final_area_code = parsed_payload.get('area_code') or normalized_area_code
            final_country = payload_country_context or parsed_payload.get('country') or normalized_country
        final_registration_group = payload.registration_group or parsed_payload.get('registration_group') or OTHER_CHANNEL_REGISTRATION_GROUP
        if prefer_payload_over_defaults:
            final_app_name = (
                explicit_app_name
                or payload.app_name
                or parsed_payload.get('app_name')
                or current_default_app
            )
            final_dept_name = (
                explicit_dept_name
                or payload.dept_name
                or parsed_payload.get('dept_name')
                or current_default_dept
            )
        else:
            final_app_name = (
                explicit_app_name
                or current_default_app
                or payload.app_name
                or parsed_payload.get('app_name')
            )
            final_dept_name = (
                explicit_dept_name
                or current_default_dept
                or payload.dept_name
                or parsed_payload.get('dept_name')
            )
        final_account_id = payload.account_id or parsed_payload.get('account_id')
        invite_code_meta = normalize_invite_code_candidate(explicit_invite_code or parsed_payload.get('evidence', {}).get('invite_code_raw_input') or str(payload.invite_code or '').strip().upper() or None)
        final_invite_code = str(invite_code_meta.get('normalized') or '').strip().upper() if invite_code_meta.get('is_valid') else None
        if not final_country and not id_only_cms_bind_input:
            executor_country = normalize_country_label((self.resolve_guild_executor(final_dept_name) or {}).get('country'))
            if executor_country:
                final_mobile, final_area_code, final_country = normalize_phone_identity(
                    mobile=final_mobile,
                    area_code=int(final_area_code or 0),
                    country=executor_country,
                )

        invite_validation_error = validate_invite_code_field(explicit_invite_code or parsed_payload.get('evidence', {}).get('invite_code_raw_input') or str(payload.invite_code or '').strip().upper() or None, invite_code_meta=invite_code_meta)
        if invite_validation_error:
            return {
                'accepted': False,
                'reason': invite_validation_error['reason'],
                'reply_phone': final_mobile or '-',
                'reply_id': final_account_id or '-',
                'reply_group': final_registration_group or '-',
                'reply_code': invite_code_meta.get('raw_input') or '-',
                'reply_error_text': invite_validation_error['reply_text'],
            }

        bypass_default_mismatch = str(payload.source_channel or '').strip() == 'ops_intake_workbench'
        if (not bypass_default_mismatch) and (
            (explicit_app_name and current_default_app and explicit_app_name.lower() != current_default_app.lower())
            or (explicit_dept_name and current_default_dept and explicit_dept_name.lower() != current_default_dept.lower())
        ):
            return {
                'accepted': False,
                'reason': 'app_agency_mismatch',
                'reply_phone': final_mobile or '-',
                'reply_id': final_account_id or '-',
                'reply_group': final_registration_group or '-',
            }

        can_bind_by_cms_id = self.guild_executor_has_platform_cms_route(final_dept_name)
        country_guard = self._guild_executor_country_guard(final_dept_name, final_country)
        if not id_only_cms_bind_input and not country_guard.get('allowed', True):
            return {
                'accepted': False,
                'reason': 'country_guild_mismatch',
                'reply_phone': final_mobile or '-',
                'reply_area_code': final_area_code,
                'reply_id': final_account_id or '-',
                'reply_group': final_registration_group or '-',
                'reply_code': final_invite_code or '-',
                'user_country': country_guard.get('user_country') or final_country or '',
                'guild_country': country_guard.get('guild_country') or '',
                'dept_name': final_dept_name,
            }
        invite_code_required = bool(self.require_invite_code and not can_bind_by_cms_id)
        classification = self._classify_manual_cs_submission(
            payload=payload,
            parsed_payload=parsed_payload,
            final_account_id=final_account_id,
            final_mobile=final_mobile,
            final_registration_group=final_registration_group,
            final_app_name=final_app_name,
            final_dept_name=final_dept_name,
            final_invite_code=final_invite_code,
            invite_code_required=invite_code_required,
        )

        parsed_result = {
            **parsed_payload,
            'mobile': final_mobile,
            'area_code': final_area_code,
            'country': final_country,
            'registration_group': final_registration_group,
            'app_name': final_app_name,
            'dept_name': final_dept_name,
            'account_id': final_account_id,
            'invite_code': final_invite_code,
        }
        with self.db.connect() as conn:
            duplicate_submission = self._find_recent_cross_channel_duplicate_submission(
                conn,
                mobile=final_mobile,
                area_code=final_area_code or 62,
                account_id=final_account_id,
                app_name=final_app_name,
                dept_name=final_dept_name,
                registration_group=final_registration_group,
                source_channel=payload.source_channel,
                submitted_at=payload.submitted_at,
            )
            if duplicate_submission:
                return self._build_duplicate_submission_response(
                    conn,
                    duplicate_submission=duplicate_submission,
                    parsed_result=parsed_result,
                )
            existing_verified_lead = self._find_recent_verified_duplicate_lead(
                conn,
                mobile=final_mobile,
                area_code=final_area_code or 62,
                account_id=final_account_id,
                app_name=final_app_name,
                dept_name=final_dept_name,
                registration_group=final_registration_group,
            )
            if existing_verified_lead:
                duplicate_semantics = str(existing_verified_lead.get('duplicate_semantics') or 'already_in_target_guild').strip() or 'already_in_target_guild'
                if duplicate_semantics == 'legacy_success_unverified':
                    return self._build_duplicate_submission_response(
                        conn,
                        duplicate_submission={
                            'submission_id': None,
                            'source_channel': 'local_verified_duplicate',
                            'lead_id': existing_verified_lead['lead_id'],
                        },
                        parsed_result=parsed_result,
                        accepted_override=False,
                        reason_override='crm_sync_failed',
                        result_reason_override='Data duplication.',
                        next_action_override='retry_crm_sync',
                    )
                return self._build_duplicate_submission_response(
                    conn,
                    duplicate_submission={
                        'submission_id': None,
                        'source_channel': 'local_verified_duplicate',
                        'lead_id': existing_verified_lead['lead_id'],
                    },
                    parsed_result=parsed_result,
                    accepted_override=False,
                    reason_override='already_in_target_guild',
                    result_code_override='already_in_target_guild',
                    result_reason_override='Previously registered in this agency',
                    bind_precheck_override='already_in_target_guild',
                    next_action_override='already_in_target_guild',
                )

        lead = self.upsert_lead(
            LeadUpsertRequest(
                trace_id=create_id("trace"),
                source_platform="manual_cs",
                source_campaign=payload.source_channel,
                source_page_id=payload.source_channel,
                country=final_country or "Indonesia",
                area_code=final_area_code or 62,
                mobile=final_mobile,
                yw_id=final_account_id,
                app_name=final_app_name,
                dept_name=final_dept_name,
                pendaftaran_group=final_registration_group,
                inviter_id=final_invite_code,
                occurred_at=payload.submitted_at,
                parser_confidence=parsed_payload.get('confidence'),
                parser_missing_fields=parsed_payload.get('missing_fields', []),
                parser_conflicts=parsed_payload.get('conflicts', []),
                parser_raw_text=parsed_payload.get('raw_text'),
                parser_raw_ocr_text=parsed_payload.get('raw_ocr_text'),
                parser_version=classification['parser_version'],
                parser_status=classification['parser_status'],
                review_reason_codes=classification['review_reason_codes'],
                routing_decision=classification['routing_decision'],
                recommended_next_action=classification['recommended_next_action'],
                review_status=classification['review_status'],
            )
        )

        if classification['routing_decision'] == 'manual_review':
            with self.db.connect() as conn:
                review_task_id = create_id('task')
                conn.execute(
                    """
                    INSERT INTO automation_tasks (
                        task_id, lead_id, task_type, priority, payload, dedupe_key, created_by, created_at, status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        review_task_id,
                        lead['lead_id'],
                        'manual_review',
                        'P0',
                        json.dumps({
                            'submission_type': payload.submission_type,
                            'file_url': payload.file_url,
                            'file_type': payload.file_type,
                            'source_channel': payload.source_channel,
                            'submitted_by': payload.submitted_by,
                            'remark': payload.remark,
                            'parsed_payload': parsed_result,
                        }, ensure_ascii=False),
                        f"manual_review:{lead['lead_id']}:{payload.submitted_at}",
                        payload.submitted_by,
                        payload.submitted_at,
                        'pending',
                    ),
                )
                current = conn.execute("SELECT current_status FROM leads WHERE lead_id = ?", (lead['lead_id'],)).fetchone()
                from_status = current['current_status'] if current else 'new'
                conn.execute(
                    "UPDATE leads SET current_status = ?, updated_at = ? WHERE lead_id = ?",
                    ('manual_review_pending', utc_now(), lead['lead_id']),
                )
                self._record_status_history(
                    conn,
                    lead_id=lead['lead_id'],
                    from_status=from_status,
                    to_status='manual_review_pending',
                    trigger_type='manual_cs_routed',
                    trigger_source='manual_cs_submission',
                    trigger_task_id=review_task_id,
                    operator_name=payload.submitted_by,
                    remark=';'.join(classification['review_reason_codes']),
                )
            return {
                "accepted": True,
                "lead_id": lead["lead_id"],
                "matched_customer_id": lead["matched_customer_id"],
                "submission_id": None,
                "task_id": review_task_id,
                "next_action": "manual_review",
                "routing_decision": classification['routing_decision'],
                "review_reason_codes": classification['review_reason_codes'],
                "parsed_payload": parsed_result,
            }

        account_submission = self.submit_account(
            AccountSubmissionRequest(
                lead_id=lead["lead_id"],
                submission_type=payload.submission_type,
                account_id=final_account_id,
                account_id_type="platform_uid" if final_account_id else None,
                file_url=payload.file_url,
                file_type=payload.file_type,
                source_channel=payload.source_channel,
                expected_guild=final_dept_name,
                source_bot_app_id=payload.source_bot_app_id,
                source_message_id=payload.source_message_id,
                submitted_by=payload.submitted_by,
                submitted_at=payload.submitted_at,
                remark=payload.remark,
            )
        )
        simulated_result = self._maybe_auto_simulate_bind_after_intake(
            lead=lead,
            payload=payload,
            parsed_result=parsed_result,
            account_submission=account_submission,
        )
        if simulated_result is not None:
            return simulated_result
        return {
            "accepted": True,
            "lead_id": lead["lead_id"],
            "matched_customer_id": lead["matched_customer_id"],
            "submission_id": account_submission["submission_id"],
            "task_id": account_submission["task_id"],
            "duplicate_task_reused": bool(account_submission.get("duplicate_task_reused")),
            "existing_task_status": account_submission.get("existing_task_status"),
            "next_action": account_submission["next_action"],
            "routing_decision": classification['routing_decision'],
            "review_reason_codes": classification['review_reason_codes'],
            "parsed_payload": parsed_result,
        }

    def _reply_lark_message(self, *, message_id: Optional[str], text: str, chat_id: Optional[str] = None, adapter: Any = None) -> None:
        active_adapter = adapter or self.lark_reply_adapter
        if active_adapter is None or not str(text or '').strip():
            return
        try:
            if message_id and hasattr(active_adapter, 'reply_text'):
                self.external_call_rate_limiter.allow('reply:lark')
                self.reply_circuit_breaker.call(lambda: active_adapter.reply_text(message_id=message_id, text=text))
                return
            if chat_id and hasattr(active_adapter, 'send_text'):
                self.external_call_rate_limiter.allow('reply:lark')
                self.reply_circuit_breaker.call(lambda: active_adapter.send_text(chat_id=chat_id, text=text))
                return
        except Exception as exc:
            if chat_id and hasattr(active_adapter, 'send_text'):
                try:
                    self.reply_circuit_breaker.call(lambda: active_adapter.send_text(chat_id=chat_id, text=text))
                    return
                except Exception as fallback_exc:
                    print(f"Lark reply failed for {message_id or 'unknown'} and fallback chat send failed for {chat_id}: {fallback_exc}")
                    return
            print(f"Lark reply failed for {message_id or chat_id or 'unknown'}: {exc}")

    def _load_profile_env_map(self, profile_name: str) -> Dict[str, str]:
        normalized_profile = str(profile_name or '').strip()
        if not normalized_profile:
            return {}
        env_path = Path.home() / '.hermes' / 'profiles' / normalized_profile / '.env'
        if not env_path.exists():
            return {}
        values: Dict[str, str] = {}
        try:
            for raw_line in env_path.read_text(encoding='utf-8', errors='ignore').splitlines():
                line = raw_line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                key, value = line.split('=', 1)
                values[str(key).strip()] = value.strip().strip('"').strip("'")
        except Exception:
            return {}
        return values

    def _expand_notify_profile_targets(self, profile_name: Optional[str], notify_robot_name: Optional[str] = None) -> List[Dict[str, Optional[str]]]:
        return expand_notify_profile_targets(profile_name, notify_robot_name)

    def _official_group_success_notification_already_sent(self, conn: sqlite3.Connection, approval_run_id: str, notify_profile_name: Optional[str] = None) -> bool:
        normalized_run_id = str(approval_run_id or '').strip()
        if not normalized_run_id:
            return False
        normalized_profile_name = str(notify_profile_name or '').strip()
        if normalized_profile_name:
            row = conn.execute(
                "SELECT 1 FROM operator_audit_log WHERE event_type = 'official_group_success_notification_sent' AND payload LIKE ? AND payload LIKE ? LIMIT 1",
                (
                    f'%\"approval_run_id\": \"{normalized_run_id}\"%',
                    f'%\"notify_profile_name\": \"{normalized_profile_name}\"%',
                ),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT 1 FROM operator_audit_log WHERE event_type = 'official_group_success_notification_sent' AND payload LIKE ? LIMIT 1",
                (f'%\"approval_run_id\": \"{normalized_run_id}\"%',),
            ).fetchone()
        return row is not None

    def _official_group_success_notification_already_sent_by_daemon(self, dedupe_key: Optional[str], notify_profile_name: Optional[str] = None) -> bool:
        normalized_dedupe_key = str(dedupe_key or '').strip()
        if not normalized_dedupe_key:
            return False
        state_path = Path(__file__).resolve().parents[1] / 'data' / 'production_ops_daemon_state.json'
        try:
            state = json.loads(state_path.read_text(encoding='utf-8'))
        except Exception:
            return False
        notifications = state.get('notifications') if isinstance(state, dict) else {}
        record = notifications.get(normalized_dedupe_key) if isinstance(notifications, dict) else None
        if not isinstance(record, dict):
            return False
        if str(record.get('last_status') or '').strip() not in {'sent', 'partial_sent'}:
            return False
        deliveries = record.get('deliveries') if isinstance(record.get('deliveries'), list) else []
        normalized_profile_name = str(notify_profile_name or '').strip()
        if normalized_profile_name:
            return any(
                isinstance(item, dict)
                and str(item.get('status') or '').strip() == 'sent'
                and str(item.get('notify_profile_name') or '').strip() == normalized_profile_name
                for item in deliveries
            )
        return bool(record.get('last_sent_at'))

    def _record_official_group_success_notification_in_daemon_state(
        self,
        *,
        dedupe_key: Optional[str],
        checked_at: Optional[str],
        status: Optional[str],
        deliveries: List[Dict[str, Any]],
    ) -> None:
        normalized_dedupe_key = str(dedupe_key or '').strip()
        if not normalized_dedupe_key:
            return
        state_path = Path(PRODUCTION_OPS_DAEMON_STATE_PATH)
        try:
            existing_state = json.loads(state_path.read_text(encoding='utf-8'))
        except Exception:
            existing_state = {}
        state = dict(existing_state) if isinstance(existing_state, dict) else {}
        notifications = state.get('notifications') if isinstance(state.get('notifications'), dict) else {}
        updated_notifications = dict(notifications)
        previous = updated_notifications.get(normalized_dedupe_key) if isinstance(updated_notifications.get(normalized_dedupe_key), dict) else {}
        sent_deliveries = [
            {
                'notify_profile_name': str(item.get('notify_profile_name') or '').strip() or None,
                'notify_robot_name': str(item.get('notify_robot_name') or '').strip() or None,
                'status': str(item.get('status') or '').strip() or None,
                'error': str(item.get('error') or '').strip() or None,
            }
            for item in list(deliveries or [])
            if isinstance(item, dict)
        ]
        updated_record = dict(previous)
        updated_record['last_status'] = str(status or '').strip() or updated_record.get('last_status') or 'sent'
        if checked_at:
            updated_record['last_sent_at'] = str(checked_at).strip()
        updated_record['deliveries'] = sent_deliveries
        updated_record['sent_count'] = sum(1 for item in sent_deliveries if str(item.get('status') or '').strip() == 'sent')
        updated_notifications[normalized_dedupe_key] = updated_record
        state['notifications'] = updated_notifications
        state_path = Path(PRODUCTION_OPS_DAEMON_STATE_PATH)
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')

    def _record_official_group_success_notification_sent(
        self,
        conn: sqlite3.Connection,
        *,
        lead_id: Optional[str],
        approval_run_id: str,
        approval_run_ids: List[str],
        notify_profile_name: Optional[str],
        notify_robot_name: Optional[str],
        message_text: str,
        dedupe_key: Optional[str],
        target_group: Optional[str],
        group_name: Optional[str],
    ) -> None:
        self._record_audit_event(
            conn,
            event_type='official_group_success_notification_sent',
            event_source='official_group_batch_runner',
            payload={
                'lead_id': lead_id,
                'approval_scope': 'official_group',
                'approval_run_id': approval_run_id,
                'approval_run_ids': [str(item).strip() for item in list(approval_run_ids or []) if str(item).strip()],
                'notify_profile_name': str(notify_profile_name or '').strip() or None,
                'notify_robot_name': str(notify_robot_name or '').strip() or None,
                'message_text': message_text,
                'dedupe_key': str(dedupe_key or '').strip() or None,
                'target_group': str(target_group or '').strip() or None,
                'group_name': str(group_name or '').strip() or None,
                'target_group_label': str(group_name or target_group or '').strip() or None,
            },
            lead_id=str(lead_id or '').strip() or None,
        )

    def _send_official_group_success_notifications(
        self,
        *,
        decided_at: str,
        ready_groups: List[Dict[str, Any]],
        results: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        success_rows: List[Dict[str, Any]] = []
        approval_run_lead_map: Dict[str, Optional[str]] = {}
        with self.db.connect() as conn:
            for item in list(results or []):
                if not isinstance(item, dict) or not item.get('executed'):
                    continue
                executor_result = item.get('executor_result') if isinstance(item.get('executor_result'), dict) else {}
                if str(executor_result.get('status') or '').strip().lower() != 'success':
                    continue
                if executor_result.get('verified') is False:
                    continue
                raw_result = executor_result.get('raw_result') if isinstance(executor_result.get('raw_result'), dict) else {}
                approval_run_id = str(raw_result.get('approval_run_id') or '').strip()
                success_rows.append(item)
                if approval_run_id:
                    approval_run_lead_map[approval_run_id] = str(item.get('lead_id') or '').strip() or None
        if not success_rows:
            return []
        checked_at = str(decided_at or '').strip() or datetime.now(timezone.utc).isoformat()
        cycle = {
            'checked_at': checked_at,
            'registration_group': str((ready_groups[0] or {}).get('registration_group') or '').strip() if ready_groups else '',
            'official_group_dispatch': {
                'triggered': True,
                'ok': True,
                'approval_type': 'manual_approval',
                'ready_groups': ready_groups,
                'result': {'results': success_rows},
            },
        }
        incidents = [
            item
            for item in build_success_notifications(cycle)
            if isinstance(item, dict) and str(item.get('code') or '').strip() == 'official_group_approval_succeeded'
        ]
        if not incidents:
            return []
        notifications: List[Dict[str, Any]] = []
        if bool(getattr(self, 'official_group_success_notifications_suppressed', False)):
            for incident in incidents:
                details = incident.get('details') if isinstance(incident.get('details'), dict) else {}
                notifications.append({
                    'code': incident.get('code'),
                    'dedupe_key': incident.get('dedupe_key'),
                    'approval_run_ids': [
                        str(item).strip()
                        for item in list(details.get('approval_run_ids') or [])
                        if str(item).strip()
                    ],
                    'notify_profile_name': str(incident.get('notify_profile_name') or '').strip() or None,
                    'notify_robot_name': str(incident.get('notify_robot_name') or '').strip() or None,
                    'status': 'skipped_daemon_owned',
                })
            return notifications
        with self.db.connect() as conn:
            for incident in incidents:
                details = incident.get('details') if isinstance(incident.get('details'), dict) else {}
                approval_run_ids = [
                    str(item).strip()
                    for item in list(details.get('approval_run_ids') or [])
                    if str(item).strip()
                ]
                notify_profile_name = str(incident.get('notify_profile_name') or '').strip()
                notify_robot_name = str(incident.get('notify_robot_name') or '').strip()
                payload = {
                    'code': incident.get('code'),
                    'dedupe_key': incident.get('dedupe_key'),
                    'approval_run_ids': approval_run_ids,
                    'notify_profile_name': notify_profile_name or None,
                    'notify_robot_name': notify_robot_name or None,
                }
                if not notify_profile_name:
                    payload['status'] = 'skipped_notify_profile_missing'
                    notifications.append(payload)
                    continue
                targets = self._expand_notify_profile_targets(notify_profile_name, notify_robot_name)
                if approval_run_ids and targets and all(
                    self._official_group_success_notification_already_sent_by_daemon(str(incident.get('dedupe_key') or '').strip(), str(target.get('profile_name') or '').strip())
                    for target in targets
                ):
                    payload['status'] = 'skipped_duplicate'
                    notifications.append(payload)
                    continue
                if approval_run_ids and targets and all(
                    all(self._official_group_success_notification_already_sent(conn, item, str(target.get('profile_name') or '').strip()) for item in approval_run_ids)
                    for target in targets
                ):
                    payload['status'] = 'skipped_duplicate'
                    notifications.append(payload)
                    continue
                if not targets:
                    payload['status'] = 'skipped_no_notifier'
                    notifications.append(payload)
                    continue
                deliveries: List[Dict[str, Any]] = []
                for target in targets:
                    target_profile_name = str(target.get('profile_name') or '').strip()
                    target_robot_name = str(target.get('robot_name') or '').strip()
                    delivery = {
                        'notify_profile_name': target_profile_name or None,
                        'notify_robot_name': target_robot_name or None,
                    }
                    if approval_run_ids and all(self._official_group_success_notification_already_sent(conn, item, target_profile_name) for item in approval_run_ids):
                        delivery['status'] = 'skipped_duplicate'
                        deliveries.append(delivery)
                        continue
                    env_values = self._load_profile_env_map(target_profile_name)
                    app_id = str(env_values.get('LARK_APP_ID') or env_values.get('FEISHU_APP_ID') or '').strip()
                    app_secret = str(env_values.get('LARK_APP_SECRET') or env_values.get('FEISHU_APP_SECRET') or '').strip()
                    chat_id = str(env_values.get('LARK_HOME_CHANNEL') or env_values.get('FEISHU_HOME_CHANNEL') or '').strip()
                    domain = str(env_values.get('LARK_DOMAIN') or env_values.get('FEISHU_DOMAIN') or 'lark').strip() or 'lark'
                    if not app_id or not app_secret or not chat_id:
                        delivery['status'] = 'skipped_no_notifier'
                        deliveries.append(delivery)
                        continue
                    adapter = LiveLarkReplyAdapter(app_id=app_id, app_secret=app_secret, domain=domain)
                    monitor_target = {
                        'notify_profile_name': target_profile_name,
                        'notify_robot_name': target_robot_name or None,
                        'group_name': details.get('group_name'),
                    }
                    effective_cycle = {**cycle, 'monitor_target': monitor_target}
                    message_text = format_lark_alert('production-ops-daemon', incident, effective_cycle)
                    if should_suppress_lark_alert(incident, effective_cycle, message_text):
                        delivery['status'] = 'skipped_suppressed_alert'
                        delivery['suppressed_reason'] = 'invalid_registration_group_invite_404'
                        deliveries.append(delivery)
                        continue
                    try:
                        self.external_call_rate_limiter.allow(f'official-group-success-notify:{target_profile_name}')
                        response = adapter.send_text(chat_id=chat_id, text=message_text)
                        delivery['status'] = 'sent'
                        delivery['response'] = response
                        for approval_run_id in approval_run_ids:
                            self._record_official_group_success_notification_sent(
                                conn,
                                lead_id=approval_run_lead_map.get(approval_run_id),
                                approval_run_id=approval_run_id,
                                approval_run_ids=approval_run_ids,
                                notify_profile_name=target_profile_name,
                                notify_robot_name=target_robot_name,
                                message_text=message_text,
                                dedupe_key=str(incident.get('dedupe_key') or '').strip() or None,
                                target_group=str(details.get('target_group') or '').strip() or None,
                                group_name=str(details.get('group_name') or '').strip() or None,
                            )
                        conn.commit()
                    except Exception as exc:
                        delivery['status'] = 'failed'
                        delivery['error'] = str(exc)
                    deliveries.append(delivery)
                payload['deliveries'] = deliveries
                delivery_statuses = {str(item.get('status') or '') for item in deliveries}
                if delivery_statuses and delivery_statuses <= {'sent', 'skipped_duplicate'} and 'sent' in delivery_statuses:
                    payload['status'] = 'sent'
                elif delivery_statuses == {'skipped_duplicate'}:
                    payload['status'] = 'skipped_duplicate'
                elif 'sent' in delivery_statuses and ('failed' in delivery_statuses or 'skipped_no_notifier' in delivery_statuses):
                    payload['status'] = 'partial_sent'
                elif 'failed' in delivery_statuses:
                    payload['status'] = 'failed'
                else:
                    payload['status'] = 'skipped_no_notifier'
                if payload['status'] in {'sent', 'partial_sent'}:
                    self._record_official_group_success_notification_in_daemon_state(
                        dedupe_key=str(incident.get('dedupe_key') or '').strip() or None,
                        checked_at=checked_at,
                        status=payload['status'],
                        deliveries=deliveries,
                    )
                notifications.append(payload)
        return notifications

    def _resolve_lark_reply_adapter(self, *, app_id: Optional[str] = None) -> Any:
        normalized_app_id = str(app_id or '').strip()
        if normalized_app_id and normalized_app_id in self._lark_reply_adapter_by_app_id:
            return self._lark_reply_adapter_by_app_id[normalized_app_id]
        if normalized_app_id and self.current_lark_app_id and normalized_app_id == str(self.current_lark_app_id).strip() and self.lark_reply_adapter is not None:
            return self.lark_reply_adapter
        if not normalized_app_id:
            return self.lark_reply_adapter
        if normalized_app_id in self._profile_reply_adapter_cache:
            return self._profile_reply_adapter_cache[normalized_app_id]
        preset = self.resolve_intake_bot_preset(app_id=normalized_app_id)
        profile_name = str(preset.get('profile_name') or '').strip()
        env_values = self._load_profile_env_map(profile_name)
        env_app_id = str(env_values.get('LARK_APP_ID') or env_values.get('FEISHU_APP_ID') or '').strip()
        env_app_secret = str(env_values.get('LARK_APP_SECRET') or env_values.get('FEISHU_APP_SECRET') or '').strip()
        env_domain = str(env_values.get('LARK_DOMAIN') or env_values.get('FEISHU_DOMAIN') or 'lark').strip() or 'lark'
        if env_app_id and env_app_secret and env_app_id == normalized_app_id:
            if isinstance(self.lark_reply_adapter, LarkCliReplyAdapter):
                adapter = self.lark_reply_adapter.with_env(env_values)
            else:
                adapter = LiveLarkReplyAdapter(app_id=env_app_id, app_secret=env_app_secret, domain=env_domain)
            self._profile_reply_adapter_cache[normalized_app_id] = adapter
            return adapter
        return self.lark_reply_adapter

    def _should_emit_lark_reply(self, result: Dict[str, Any]) -> bool:
        if not isinstance(result, dict):
            return False
        if str(result.get('next_action') or '').strip() in {'queue_bind_check', 'queue_bind_retry', 'queue_account_recognition', 'queued_for_processing', 'queue_crm_sync_retry'}:
            return False
        return True

    def _is_verified_success_result(self, result: Dict[str, Any]) -> bool:
        if not isinstance(result, dict) or not result.get('accepted'):
            return False
        if str(result.get('bind_precheck') or '').strip() == 'already_in_target_guild':
            return False
        lead_status = str(result.get('lead_status') or '').strip()
        if lead_status not in {'bind_success', 'group_join_pending', 'group_join_success', 'synced'}:
            return False
        return bool(
            result.get('crm_verified')
            or result.get('verified_after_write')
            or result.get('current_submission_crm_verified')
        )

    def _has_successful_bind_and_crm_record(self, result: Dict[str, Any]) -> bool:
        if not isinstance(result, dict):
            return False
        if str(result.get('bind_precheck') or '').strip() == 'already_in_target_guild':
            return False
        lead_status = str(result.get('lead_status') or '').strip()
        bind_succeeded = bool(result.get('accepted')) or lead_status in {'bind_success', 'group_join_pending', 'group_join_success', 'synced'}
        crm_recorded = bool(
            result.get('crm_verified')
            or result.get('verified_after_write')
            or result.get('current_submission_crm_verified')
            or result.get('crm_verified_row')
        )
        return bind_succeeded and crm_recorded

    def _format_operator_crm_failure_reason(self, *, retried: bool = False) -> str:
        return 'CRM failed after retries. Check manually.' if retried else 'CRM failed. Check manually.'

    def _registration_membership_phone_keys(self, *, mobile: Optional[str] = None, area_code: Optional[int] = None) -> set[str]:
        raw = str(mobile or '').strip()
        keys: set[str] = set()
        if not raw:
            return keys
        digits = ''.join(ch for ch in raw if ch.isdigit())
        if digits:
            keys.add(digits)
        keys.update(localized_phone_match_keys(phone=raw, area_code=int(area_code or 0), country=''))
        try:
            normalized_mobile, normalized_area_code, _ = normalize_phone_identity(mobile=raw, area_code=int(area_code or 0), country='')
        except Exception:
            normalized_mobile, normalized_area_code = '', 0
        if normalized_mobile:
            keys.add(str(normalized_mobile))
            if normalized_area_code:
                keys.add(f'{int(normalized_area_code)}{normalized_mobile}')
                keys.add(f'+{int(normalized_area_code)}{normalized_mobile}')
        for prefix in sorted(PHONE_PREFIX_COUNTRY_MAP.keys(), key=len, reverse=True):
            if digits.startswith(prefix) and len(digits) > len(prefix):
                keys.add(digits[len(prefix):])
                break
        return {key for key in keys if key}

    def _find_registration_group_memberships_for_phone(self, *, mobile: Optional[str], area_code: Optional[int] = None) -> Dict[str, Any]:
        keys = self._registration_membership_phone_keys(mobile=mobile, area_code=area_code)
        if not keys:
            return {'status': 'missing', 'matches': []}
        rows: List[Dict[str, Any]] = []
        with self.db.connect() as conn:
            candidates = [dict(row) for row in conn.execute(
                """
                SELECT member_id, approval_run_id, registration_group, registration_group_name,
                       requester_id, display_name, wa_phone_raw, wa_phone_normalized, approved_at, created_at
                FROM registration_group_approval_batch_members
                WHERE COALESCE(wa_phone_raw, '') != '' OR COALESCE(wa_phone_normalized, '') != ''
                ORDER BY approved_at DESC, created_at DESC
                LIMIT 1000
                """
            ).fetchall()]
        for row in candidates:
            row_keys = set()
            row_keys.update(self._registration_membership_phone_keys(mobile=row.get('wa_phone_normalized'), area_code=area_code))
            row_keys.update(self._registration_membership_phone_keys(mobile=row.get('wa_phone_raw'), area_code=area_code))
            if keys.intersection(row_keys):
                rows.append(row)
        groups: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            group = str(row.get('registration_group_name') or row.get('registration_group') or '').strip()
            if not group:
                continue
            groups.setdefault(group.lower(), {**row, 'resolved_registration_group': group})
        unique_rows = list(groups.values())
        if not unique_rows:
            return {'status': 'missing', 'matches': []}
        if len(unique_rows) == 1:
            return {'status': 'unique', 'match': unique_rows[0], 'matches': unique_rows}
        return {'status': 'multiple', 'matches': unique_rows}

    def _translate_customer_visible_failure_reason_to_english(self, reason_text: str) -> str:
        text = re.sub(r'\s+', ' ', str(reason_text or '').strip())
        lowered = text.lower()
        if not text:
            return ''
        country_mismatch_markers = [
            'negara anda tidak sama dengan negara agency',
            'negara anda tidak sama dengan negara agensi',
            'país e o da agência não correspondem',
            'pais e o da agencia nao correspondem',
            'país e o da agencia nao correspondem',
            'gagal bergabung ke agency',
            'falha ao entrar na agência',
            'falha ao entrar na agencia',
        ]
        if any(marker in lowered for marker in country_mismatch_markers):
            return 'Failed to join the agency. Your country does not match the agency country.'
        if (
            'the streamer was in other guild' in lowered
            or 'the streamer was in another agency' in lowered
            or 'uma conta não eliminada neste dispositivo aderiu a uma guilda' in lowered
            or 'uma conta nao eliminada neste dispositivo aderiu a uma guilda' in lowered
            or '已加入其他公会' in text
            or '其他公会' in text
            or ('currently belongs to' in lowered and 'target agency' in lowered)
        ):
            return 'The streamer was in another agency'
        if (
            'sid格式错误' in text
            or 'sid format error' in lowered
            or 'invalid sid format' in lowered
        ):
            return 'Invalid or unavailable Linky ID'
        if 'invalid arguments' in lowered:
            return 'Invalid arguments'
        if 'timed out after 3 attempts' in lowered or 'read operation timed out' in lowered or 'read timed out' in lowered:
            return 'CMS request timed out. Check manually.'
        if any(ord(ch) > 127 for ch in text):
            return 'Bind failed. Check manually.'
        return text

    def _sanitize_customer_visible_failure_reason(self, *, result_code: str, reason_text: str) -> str:
        text = str(reason_text or '').strip()
        lowered = text.lower()
        has_html = '<html' in lowered or '<body' in lowered or '<!doctype' in lowered or '<title' in lowered or '</' in lowered
        if has_html:
            plain = re.sub(r'(?is)<(script|style)[^>]*>.*?</\\1>', ' ', text)
            plain = re.sub(r'(?is)<!--.*?-->', ' ', plain)
            plain = re.sub(r'(?is)<[^>]+>', ' ', plain)
            plain = re.sub(r'\\s+', ' ', plain).strip()
            plain_lower = plain.lower()
            if '404 not found' in lowered or '404 not found' in plain_lower or 'http 404' in lowered:
                return 'Binding upstream returned HTTP 404 Not Found; check executor URL or nginx route.'
            status_match = re.search(r'http\\s*(\\d{3})|http[^0-9]{0,12}(\\d{3})', lowered)
            status = next((g for g in (status_match.groups() if status_match else []) if g), '')
            if status in {'401', '403'}:
                return f'Binding upstream returned HTTP {status}; backend session or authorization requires manual recovery.'
            if status:
                return f'Binding upstream returned HTTP {status}; check executor route.'
            return plain[:300] or 'Binding upstream returned an HTML response instead of JSON.'
        if str(result_code or '').strip() == 'bind_backend_http_error' and ('404' in lowered and 'not found' in lowered):
            return 'Binding upstream returned HTTP 404 Not Found; check executor URL or nginx route.'
        result_code_text = str(result_code or '').strip().lower()
        translated = self._translate_customer_visible_failure_reason_to_english(text)
        if result_code_text.startswith('cms_') and translated == text:
            return 'CMS rejected bind request. Check manually.'
        return translated or 'bind failed'

    def _format_lark_reply_text(self, result: Dict[str, Any]) -> str:
        parsed_payload = result.get('parsed_payload') or {}
        reply_area_code = result.get('reply_area_code')
        if reply_area_code is None and isinstance(parsed_payload, dict):
            reply_area_code = parsed_payload.get('area_code')
        phone = format_display_phone(result.get('reply_phone'), area_code=reply_area_code)
        account_id = str(result.get('reply_id') or '-').strip() or '-'
        group = str(result.get('reply_group') or '-').strip() or '-'
        code = str(
            result.get('reply_code_display')
            or result.get('reply_code')
            or result.get('invite_code')
            or (parsed_payload.get('invite_code') if isinstance(parsed_payload, dict) else '')
            or '-'
        ).strip() or '-'
        if str(result.get('bind_precheck') or '').strip() == 'already_in_target_guild':
            return (
                '**❌ Bind failed: Previously registered in this agency**\n'
                f'Phone: {phone}\n'
                f'ID: {account_id}\n'
                f'Group: {group}\n'
                f'Code: {code}'
            )
        if self._has_successful_bind_and_crm_record(result):
            return (
                '**✅ Success**\n'
                f'Phone: {phone}\n'
                f'ID: {account_id}\n'
                f'Group: {group}\n'
                f'Code: {code}'
            )
        if result.get('accepted') and str(result.get('next_action') or '').strip() == 'queue_bind_check':
            return (
                '**⏳ Processing**\n'
                f'Phone: {phone}\n'
                f'ID: {account_id}\n'
                f'Group: {group}\n'
                f'Code: {code}'
            )
        if result.get('reason') in {'app_guild_mismatch', 'app_agency_mismatch', 'bind_backend_guild_mismatch'}:
            return (
                '**🚫 I do not handle this app/agency.**\n'
                f'Phone: {phone}\n'
                f'ID: {account_id}\n'
                f'Group: {group}\n'
                f'Code: {code}'
            )
        if result.get('reason') == 'country_guild_mismatch':
            return (
                '**🚫 Country does not match this agency.**\n'
                f'Phone: {phone}\n'
                f'ID: {account_id}\n'
                f'Group: {group}\n'
                f'Code: {code}'
            )
        if result.get('reason') == 'irrelevant_message':
            return (
                '**🚫I only register host information**\n'
                '**📮Send:**\n'
                'Phone:\n'
                'ID:\n'
                'Group:\n'
                'Code:\n'
                '**📌Example:**\n'
                'Phone: +62 13800000000  ID: 123456  Group: Group-1  Code: EKVFGQ'
            )
        if result.get('reason') == 'missing_required_fields':
            missing_fields = result.get('reply_missing_fields') or []
            missing_text = ', '.join(missing_fields) if missing_fields else 'required fields'
            return (
                f'**🚫 Missing: {missing_text}**\n'
                f'Phone: {phone}\n'
                f'ID: {account_id}\n'
                f'Group: {group}\n'
                f'Code: {code}'
            )
        if result.get('reason') == 'multiple_registration_groups_found':
            return (
                '**❌ Multiple registration groups found. Please provide Group.**\n'
                f'Phone: {phone}\n'
                f'ID: {account_id}\n'
                f'Group: {group}\n'
                f'Code: {code}'
            )
        if result.get('reason') == 'invalid_phone_format':
            return (
                '**🚫 Invalid phone format. Use +<country code> <number>.**\n'
                f'Phone: {phone}\n'
                f'ID: {account_id}\n'
                f'Group: {group}\n'
                f'Code: {code}'
            )
        if result.get('reason') == 'invalid_group_format':
            return (
                '**🚫 Invalid group format. Please copy the exact registration group name.**\n'
                f'Phone: {phone}\n'
                f'ID: {account_id}\n'
                f'Group: {group}\n'
                f'Code: {code}'
            )
        if result.get('reason') == 'invalid_account_id_format':
            return (
                f"**🚫 {str(result.get('reply_error_text') or 'Invalid ID.')}**\n"
                f'Phone: {phone}\n'
                f'ID: {account_id}\n'
                f'Group: {group}\n'
                f'Code: {code}'
            )
        if result.get('reason') == 'invalid_invite_code_format':
            return (
                f"**🚫 {str(result.get('reply_error_text') or 'Invalid Code. Use a 6-character personal code: letters or letters+digits, not all digits.')}**\n"
                f'Phone: {phone}\n'
                f'ID: {account_id}\n'
                f'Group: {group}\n'
                f'Code: {code}'
            )
        if result.get('reason') in {'crm_sync_failed', 'crm_sync_retry_pending'}:
            result_reason_text = str(result.get('result_reason') or '').strip()
            result_code_text = str(result.get('result_code') or '').strip().lower()
            duplicate_crm_failure = (
                result_code_text in {'duplicate_sid', 'duplicate_submission_after_verified_success', 'duplicate_sid_existing_crm'}
                or 'data duplication' in result_reason_text.lower()
                or 'duplicate_sid' in result_reason_text.lower()
                or 'sid already exists' in result_reason_text.lower()
            )
            if duplicate_crm_failure and str(result.get('bind_precheck') or '').strip() != 'already_in_target_guild':
                return (
                    '**❌ Bind failed: Previously registered in this agency**\n'
                    f'Phone: {phone}\n'
                    f'ID: {account_id}\n'
                    f'Group: {group}\n'
                    f'Code: {code}'
                )
            if str(result.get('bind_precheck') or '').strip() == 'already_in_target_guild':
                return (
                    '**❌ Bind failed: Previously registered in this agency**\n'
                    f'Phone: {phone}\n'
                    f'ID: {account_id}\n'
                    f'Group: {group}\n'
                    f'Code: {code}'
                )
            return (
                '**❌ Bind Success, CRM Failed**\n'
                f'Phone: {phone}\n'
                f'ID: {account_id}\n'
                f'Group: {group}\n'
                f'Code: {code}'
            )
        if result.get('reason') in {'app_guild_mismatch', 'app_agency_mismatch', 'bind_backend_guild_mismatch'}:
            return (
                '**🚫 I do not handle this app/agency.**\n'
                f'Phone: {phone}\n'
                f'ID: {account_id}\n'
                f'Group: {group}\n'
                f'Code: {code}'
            )
        if result.get('reason') in {'simulated_bind_failed', 'bind_check_failed'}:
            original_reason_text = str(result.get('result_reason') or 'bind failed').strip() or 'bind failed'
            result_code = str(result.get('result_code') or '').strip()
            reason_text = self._sanitize_customer_visible_failure_reason(
                result_code=result_code,
                reason_text=original_reason_text,
            )
            lowered_reason = reason_text.lower()
            original_lowered_reason = original_reason_text.lower()
            failure_category = str(result.get('bind_failure_category') or '').strip()
            raw_result = result.get('raw_result') if isinstance(result.get('raw_result'), dict) else {}
            cms_submit_failure_text = ' '.join(
                str(item.get('reason') or item.get('message') or item.get('msg') or '')
                for item in (raw_result.get('cms_submit_fail_items') or [])
                if isinstance(item, dict)
            ).strip()
            cms_primary_failure_text = ' '.join(
                value for value in [
                    str(raw_result.get('cms_submit_error_category') or ''),
                    cms_submit_failure_text,
                    original_reason_text,
                ] if value
            )
            cms_primary_failure_lower = cms_primary_failure_text.lower()
            if result_code == 'bind_executor_unavailable' or 'bind executor unavailable' in lowered_reason:
                return (
                    '**❌ Bind failed: bind executor unavailable. Check backend runtime.**\n'
                    f'Phone: {phone}\n'
                    f'ID: {account_id}\n'
                    f'Group: {group}\n'
                    f'Code: {code}'
                )
            if (
                'sid格式错误' in cms_primary_failure_lower
                or 'sid 格式错误' in cms_primary_failure_lower
                or 'sid format' in cms_primary_failure_lower
                or 'invalid sid' in cms_primary_failure_lower
            ):
                return (
                    '**❌ Bind failed: Invalid or unavailable Linky ID**\n'
                    f'Phone: {phone}\n'
                    f'ID: {account_id}\n'
                    f'Group: {group}\n'
                    f'Code: {code}'
                )
            if '403' in original_reason_text or '403' in reason_text:
                return (
                    '**❌ Bind failed: CMS authorization rejected with HTTP 403**\n'
                    f'Phone: {phone}\n'
                    f'ID: {account_id}\n'
                    f'Group: {group}\n'
                    f'Code: {code}'
                )
            if result_code == 'cms_authorization_scope_denied':
                return (
                    '**❌ Bind failed: CMS authorization does not allow adding this SID to the target guild**\n'
                    f'Phone: {phone}\n'
                    f'ID: {account_id}\n'
                    f'Group: {group}\n'
                    f'Code: {code}'
                )
            if '401' in original_reason_text or '401' in reason_text or result_code in {'bind_unauthorized', 'cms_authorization_invalid'}:
                return (
                    '**❌ Bind failed: backend login or authorization expired. Check manually.**\n'
                    f'Phone: {phone}\n'
                    f'ID: {account_id}\n'
                    f'Group: {group}\n'
                    f'Code: {code}'
                )
            if (
                result.get('result_code') == 'bind_executor_profile_not_configured'
                or 'no chrome profile mapping configured' in lowered_reason
            ):
                return (
                    '**🚫 I do not handle this app/agency.**\n'
                    f'Phone: {phone}\n'
                    f'ID: {account_id}\n'
                    f'Group: {group}\n'
                    f'Code: {code}'
                )
            if 'batas maksimum guild' in lowered_reason or 'maximum guild' in lowered_reason:
                return (
                    '**❌ Device Duplicate Registration**\n'
                    f'Phone: {phone}\n'
                    f'ID: {account_id}\n'
                    f'Group: {group}\n'
                    f'Code: {code}'
                )
            result_code = str(result.get('result_code') or '').strip()
            if result_code in {'already_in_other_guild', 'other_agency', 'already_joined_other_guild'}:
                return (
                    '**❌ Bind failed: The streamer was in another agency**\n'
                    f'Phone: {phone}\n'
                    f'ID: {account_id}\n'
                    f'Group: {group}\n'
                    f'Code: {code}'
                )
            if result_code in {'cms_add_anchor_invalid_arguments_manual_check'}:
                return (
                    '**❌ Bind failed: CMS rejected bind request, manual check required**\n'
                    f'Phone: {phone}\n'
                    f'ID: {account_id}\n'
                    f'Group: {group}\n'
                    f'Code: {code}'
                )
            if result_code in {'cms_precheck_untrusted', 'cms_target_guild_ambiguous', 'cms_target_guild_not_visible', 'cms_target_guild_mismatch', 'cms_target_guild_lock_missing', 'cms_postcheck_timeout', 'cms_postcheck_mismatch'}:
                return (
                    '**❌ Bind failed: CMS verification requires manual check**\n'
                    f'Phone: {phone}\n'
                    f'ID: {account_id}\n'
                    f'Group: {group}\n'
                    f'Code: {code}'
                )
            if (not result_code or result_code in {'bind_backend_http_error', 'bind_failed'}) and ('the streamer was in other guild' in lowered_reason or 'another agency' in lowered_reason):
                return (
                    '**❌ Bind failed: The streamer was in another agency**\n'
                    f'Phone: {phone}\n'
                    f'ID: {account_id}\n'
                    f'Group: {group}\n'
                    f'Code: {code}'
                )
            if result_code == 'cms_bind_runtime_error' and 'timed out' in lowered_reason:
                return (
                    '**❌ Bind failed: CMS request timed out. Check manually.**\n'
                    f'Phone: {phone}\n'
                    f'ID: {account_id}\n'
                    f'Group: {group}\n'
                    f'Code: {code}'
                )
            if (
                result.get('result_code') in {'cms_add_anchor_invalid_arguments', 'cms_sid_not_found', 'sid_not_found_or_not_anchor', 'cms_bind_invalid_sid'}
                or 'invalid arguments' in lowered_reason
            ):
                return (
                    '**❌ Bind failed: Invalid or unavailable Linky ID**\n'
                    f'Phone: {phone}\n'
                    f'ID: {account_id}\n'
                    f'Group: {group}\n'
                    f'Code: {code}'
                )
            if failure_category == 'invalid_personal_code':
                return (
                    '**❌ Bind failed: Invalid personal code**\n'
                    f'Phone: {phone}\n'
                    f'ID: {account_id}\n'
                    f'Group: {group}\n'
                    f'Code: {code}'
                )
            if failure_category in {'auth_required', 'session_expired', 'captcha_required', 'manual_continue_required'}:
                return (
                    '**❌ Bind failed: Backend session requires manual recovery**\n'
                    f'Phone: {phone}\n'
                    f'ID: {account_id}\n'
                    f'Group: {group}\n'
                    f'Code: {code}'
                )
            return (
                f'**❌ Bind failed: {reason_text}**\n'
                f'Phone: {phone}\n'
                f'ID: {account_id}\n'
                f'Group: {group}\n'
                f'Code: {code}'
            )
        return (
            '**❌ Failed**\n'
            f'Phone: {phone}\n'
            f'ID: {account_id}\n'
            f'Group: {group}\n'
            f'Code: {code}'
        )

    def handle_lark_event(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if payload.get('type') == 'url_verification':
            return {'challenge': payload.get('challenge', '')}
        header = payload.get('header') or {}
        event_type = header.get('event_type') or payload.get('event_type')
        event = payload.get('event') or {}
        message = event.get('message') or {}
        message_id = str(message.get('message_id') or '')
        sender = event.get('sender') or {}
        sender_id = (sender.get('sender_id') or {}).get('open_id') or 'lark_unknown'
        gateway_direct = bool(payload.get('_gateway_direct'))
        if self.ingress_async_default and not gateway_direct and event_type == 'im.message.receive_v1' and message_id:
            if not self.ingress_rate_limiter.allow(f'lark:{sender_id}'):
                raise HTTPException(status_code=429, detail='lark ingress rate limited')
            queued = self._enqueue_ingress_event(
                ingress_type='lark_event',
                source_key=f'lark:{sender_id}',
                payload=payload,
            )
            return {
                'accepted': True,
                'queued': True,
                'ingress_event_id': queued['event_id'],
                'duplicate': queued['duplicate'],
                'message_id': message_id,
                'next_action': 'queued_for_processing',
            }
        return self._handle_lark_event_sync(payload)

    def _handle_lark_event_sync(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if payload.get('type') == 'url_verification':
            return {'challenge': payload.get('challenge', '')}

        gateway_direct = bool(payload.get('_gateway_direct'))

        def _finalize(message_id: Optional[str], result: Dict[str, Any]) -> Dict[str, Any]:
            if self._should_emit_lark_reply(result):
                reply_text = self._format_lark_reply_text(result)
                result['reply_text'] = reply_text
                if not gateway_direct:
                    self._reply_lark_message(message_id=message_id, text=reply_text)
            else:
                result['reply_text'] = ''
            return result

        header = payload.get('header') or {}
        event_type = header.get('event_type') or payload.get('event_type')
        if event_type != 'im.message.receive_v1':
            return {'accepted': False, 'ignored': True, 'reason': 'unsupported_event_type', 'event_type': event_type}

        bot_app_id = str(payload.get('_bot_app_id') or header.get('app_id') or '').strip()
        active_preset = self.resolve_intake_bot_preset(app_id=bot_app_id or None)
        active_default_app = str(payload.get('_default_app_override') or active_preset.get('default_app') or self.lark_default_app_name or '').strip() or None
        active_default_dept = str(payload.get('_default_dept_override') or active_preset.get('default_guild') or self.lark_default_dept_name or '').strip() or None

        event = payload.get('event') or {}
        message = event.get('message') or {}
        message_id = message.get('message_id')
        chat_type = message.get('chat_type') or 'p2p'
        mentions = message.get('mentions') or []
        if chat_type == 'group' and not mentions:
            result = {
                'accepted': False,
                'ignored': True,
                'reason': 'group_message_without_mention',
                'reply_phone': '-',
                'reply_id': '-',
                'reply_group': '-',
            }
            return _finalize(message_id, result)

        content = message.get('content') or '{}'
        try:
            content_obj = json.loads(content)
        except Exception:
            raw_content = str(content)
            malformed_text_match = re.match(r'^\{"text":"(.*)"\}$', raw_content, flags=re.S)
            content_obj = {'text': malformed_text_match.group(1) if malformed_text_match else raw_content}
        sender = event.get('sender') or {}
        sender_id = (sender.get('sender_id') or {}).get('open_id') or 'lark_unknown'
        message_type = (message.get('message_type') or 'text')

        if message_type == 'image':
            if self.lark_media_adapter is None:
                raise HTTPException(status_code=503, detail='lark media adapter not configured')
            message_id = message.get('message_id') or create_id('msg')
            file_key = content_obj.get('image_key') or content_obj.get('file_key')
            if not file_key:
                result = {
                    'accepted': False,
                    'ignored': True,
                    'reason': 'missing_image_key',
                    'reply_phone': '-',
                    'reply_id': '-',
                    'reply_group': '-',
                }
                self._reply_lark_message(message_id=message_id, text=self._format_lark_reply_text(result))
                return result
            suffix = '.bin'
            cache_path = self.media_cache_dir / f"{message_id}_{file_key}{suffix}"
            downloaded = False
            if not cache_path.exists():
                image_bytes = self.lark_media_adapter.download_image(message_id, file_key)
                cache_path.write_bytes(image_bytes)
                downloaded = True
            result = {
                'accepted': True,
                'source': 'lark_event_bridge',
                'chat_type': chat_type,
                'message_type': 'image',
                'message_id': message_id,
                'file_key': file_key,
                'cached': True,
                'downloaded': downloaded,
                'cached_file_url': str(cache_path),
                'next_action': 'await_text_context',
                'reply_phone': '-',
                'reply_id': '-',
                'reply_group': '-',
            }
            return _finalize(message_id, result)

        if message_type != 'text':
            result = {
                'accepted': False,
                'ignored': True,
                'reason': 'unsupported_message_type',
                'reply_phone': '-',
                'reply_id': '-',
                'reply_group': '-',
            }
            return _finalize(message_id, result)

        text = str(content_obj.get('text') or '').strip()
        media_urls = content_obj.get('media_urls') or []
        image_ocr_text = None
        if media_urls and self.ocr_adapter is not None:
            first_media = str(media_urls[0] or '').strip()
            if first_media:
                try:
                    extracted = self.ocr_adapter.extract_text(first_media)
                    image_ocr_text = str((extracted or {}).get('raw_text') or '').strip() or None
                except Exception:
                    image_ocr_text = None
        if not text:
            result = {
                'accepted': False,
                'ignored': True,
                'reason': 'empty_text',
                'reply_phone': '-',
                'reply_id': '-',
                'reply_group': '-',
            }
            return _finalize(message_id, result)
        cleaned_text = re.sub(r'@[^\s]+\s*', '', text).strip()
        cleaned_text = (
            cleaned_text
            .replace('\\\\+', '+')
            .replace('\\\\-', '-')
            .replace('\\\\[', '[')
            .replace('\\\\]', ']')
            .replace('\\+', '+')
            .replace('\\-', '-')
            .replace('\\[', '[')
            .replace('\\]', ']')
        )
        parsed_text = parse_manual_cs_message(text=cleaned_text, image_ocr_text=image_ocr_text)
        bare_candidates = extract_bare_multiline_candidates(cleaned_text)
        explicit_fields = extract_explicit_intake_fields(cleaned_text)

        mobile_match = PHONE_CANDIDATE_PATTERN.search(cleaned_text)
        registration_group_match = REGISTRATION_GROUP_LABEL_PATTERN.search(cleaned_text)
        app_match = re.search(r'\b(Linky|FUMI)\b', cleaned_text, flags=re.IGNORECASE)
        dept_match = re.search(r'(?:公会|guild|dept)\s*[:：]?\s*([A-Za-z]+)', cleaned_text, flags=re.IGNORECASE)
        account_match = re.search(r'(?:^|\b)(?:id|uid|ywid|用户id|用户ID)\s*[:：是]?\s*(\d{6,})', cleaned_text, flags=re.IGNORECASE)
        invite_match = INVITE_CODE_CAPTURE_PATTERN.search(cleaned_text)
        invite_match_value = str(invite_match.group(1) or '').strip() if invite_match else None

        resolved_phone = (
            str(explicit_fields.get('mobile') or '').strip()
            or str(bare_candidates.get('mobile_line') or '').strip()
            or (mobile_match.group(1) if mobile_match else '-')
        )
        if resolved_phone != '-' and ('*' in resolved_phone or re.search(r'[^\d\s+\-().]', resolved_phone)):
            normalized_reply_phone = resolved_phone
        else:
            normalized_reply_phone = format_display_phone(resolved_phone if resolved_phone != '-' else None, area_code=(parsed_text.get('area_code') if isinstance(parsed_text, dict) else None))
        resolved_group = normalize_registration_group_candidate(registration_group_match.group(1)) if registration_group_match else (bare_candidates.get('registration_group_line') or parsed_text.get('registration_group') or None)
        resolved_account_id = account_match.group(1) if account_match else (bare_candidates.get('account_id_line') or parsed_text.get('account_id') or None)
        invite_code_meta = normalize_invite_code_candidate(
            invite_match_value
            or explicit_fields.get('invite_code')
            or parsed_text.get('evidence', {}).get('invite_code_raw_input')
            or parsed_text.get('invite_code')
            or None
        )
        resolved_invite_code = str(invite_code_meta.get('normalized') or '').strip().upper() if invite_code_meta.get('is_valid') else None
        resolved_app_name = app_match.group(1) if app_match else (parsed_text.get('app_name') or active_default_app or None)
        resolved_dept_name = (
            str(explicit_fields.get('dept_name') or '').strip()
            or (dept_match.group(1) if dept_match else '')
            or parsed_text.get('dept_name')
            or active_default_dept
            or None
        )
        if not resolved_group and normalized_reply_phone != '-':
            membership_lookup = self._find_registration_group_memberships_for_phone(
                mobile=normalized_reply_phone,
                area_code=(parsed_text.get('area_code') if isinstance(parsed_text, dict) else None),
            )
            if membership_lookup.get('status') == 'unique':
                resolved_group = str((membership_lookup.get('match') or {}).get('resolved_registration_group') or '').strip() or None
            elif membership_lookup.get('status') == 'multiple':
                result = {
                    'accepted': False,
                    'ignored': True,
                    'reason': 'multiple_registration_groups_found',
                    'reply_phone': normalized_reply_phone,
                    'reply_id': resolved_account_id or '-',
                    'reply_group': '-',
                    'reply_code': resolved_invite_code or invite_code_meta.get('raw_input') or '-',
                    'reply_missing_fields': ['Group'],
                    'registration_group_candidates': [
                        str((item or {}).get('resolved_registration_group') or '').strip()
                        for item in (membership_lookup.get('matches') or [])
                        if str((item or {}).get('resolved_registration_group') or '').strip()
                    ],
                }
                return _finalize(message_id, result)
        invalid_group_candidate = extract_invalid_group_candidate(cleaned_text)
        explicit_group_label_present = bool(REGISTRATION_GROUP_LABEL_PATTERN.search(cleaned_text))
        if invalid_group_candidate and explicit_group_label_present:
            resolved_group = None

        explicit_app_name = str(explicit_fields.get('app_name') or (app_match.group(1) if app_match else '')).strip() or None
        explicit_dept_name = str(explicit_fields.get('dept_name') or (dept_match.group(1) if dept_match else '')).strip() or None
        inferred_group_guild = self._infer_executor_guild_from_registration_group(resolved_group)
        if (
            (explicit_app_name and active_default_app and explicit_app_name.lower() != active_default_app.lower())
            or (explicit_dept_name and active_default_dept and explicit_dept_name.lower() != active_default_dept.lower())
            or (
                inferred_group_guild
                and active_default_dept
                and inferred_group_guild.strip().lower() != str(active_default_dept).strip().lower()
            )
        ):
            result = {
                'accepted': False,
                'ignored': True,
                'reason': 'app_guild_mismatch',
                'reply_phone': normalized_reply_phone,
                'reply_id': resolved_account_id or '-',
                'reply_group': resolved_group or '-',
                'reply_code': resolved_invite_code or invite_code_meta.get('raw_input') or '-',
            }
            return _finalize(message_id, result)

        cms_route_guild = resolved_dept_name or inferred_group_guild or active_default_dept
        can_bind_by_cms_id = self.guild_executor_has_platform_cms_route(cms_route_guild)
        id_only_cms_bind = is_external_app_id_only_phone(normalized_reply_phone) and can_bind_by_cms_id

        explicit_mobile_value = str(explicit_fields.get('mobile') or '').strip()
        explicit_invite_value = str(explicit_fields.get('invite_code') or '').strip()
        has_phone_input = bool(
            (explicit_mobile_value and not is_blank_intake_field_value(explicit_mobile_value))
            or bare_candidates.get('mobile_line')
            or mobile_match
        )
        explicit_code_input = bool(
            (explicit_invite_value and not is_blank_intake_field_value(explicit_invite_value))
            or invite_match_value
        )
        has_only_bare_invite_code = bool(
            resolved_invite_code
            and not explicit_code_input
            and not has_phone_input
            and not resolved_group
            and not resolved_account_id
        )

        if not has_phone_input and not resolved_group and not resolved_account_id and (not resolved_invite_code or has_only_bare_invite_code):
            result = {
                'accepted': False,
                'ignored': True,
                'reason': 'irrelevant_message',
                'reply_phone': normalized_reply_phone,
                'reply_id': resolved_account_id or '-',
                'reply_group': resolved_group or '-',
                'reply_code': resolved_invite_code or invite_code_meta.get('raw_input') or '-',
            }
            return _finalize(message_id, result)

        if invalid_group_candidate and not resolved_group and explicit_group_label_present:
            result = {
                'accepted': False,
                'ignored': True,
                'reason': 'invalid_group_format',
                'reply_phone': normalized_reply_phone,
                'reply_id': resolved_account_id or '-',
                'reply_group': invalid_group_candidate,
            }
            return _finalize(message_id, result)

        if not resolved_group:
            resolved_group = OTHER_CHANNEL_REGISTRATION_GROUP

        missing_labels = []
        if not resolved_group:
            missing_labels.append('Group')
        if not resolved_account_id:
            missing_labels.append('ID')
        if not has_phone_input and not id_only_cms_bind:
            missing_labels.append('Phone')

        if missing_labels:
            result = {
                'accepted': False,
                'ignored': True,
                'reason': 'missing_required_fields',
                'reply_phone': normalized_reply_phone,
                'reply_id': resolved_account_id or '-',
                'reply_group': resolved_group or '-',
                'reply_missing_fields': missing_labels,
            }
            return _finalize(message_id, result)

        fast_validation_error = validate_fast_intake_fields(
            mobile=None if id_only_cms_bind else (normalized_reply_phone if normalized_reply_phone != '-' else None),
            app_name=resolved_app_name,
            account_id=resolved_account_id,
        )
        if fast_validation_error:
            result = {
                'accepted': False,
                'ignored': True,
                'reason': fast_validation_error['reason'],
                'reply_phone': normalized_reply_phone,
                'reply_id': resolved_account_id or '-',
                'reply_group': resolved_group or '-',
                'reply_error_text': fast_validation_error['reply_text'],
            }
            return _finalize(message_id, result)
        invite_validation_error = validate_invite_code_field(invite_match_value or explicit_fields.get('invite_code') or parsed_text.get('evidence', {}).get('invite_code_raw_input') or None, invite_code_meta=invite_code_meta)
        if invite_validation_error:
            result = {
                'accepted': False,
                'ignored': True,
                'reason': invite_validation_error['reason'],
                'reply_phone': normalized_reply_phone,
                'reply_id': resolved_account_id or '-',
                'reply_group': resolved_group or '-',
                'reply_code': invite_code_meta.get('raw_input') or '-',
                'reply_error_text': invite_validation_error['reply_text'],
            }
            return _finalize(message_id, result)
        if not resolved_invite_code and self.require_invite_code and not can_bind_by_cms_id:
            result = {
                'accepted': False,
                'ignored': True,
                'reason': 'missing_required_fields',
                'reply_phone': normalized_reply_phone,
                'reply_id': resolved_account_id or '-',
                'reply_group': resolved_group or '-',
                'reply_missing_fields': ['Code'],
            }
            return _finalize(message_id, result)

        intake_response = self._submit_manual_cs_sync(
            ManualCsSubmissionRequest(
                mobile=normalized_reply_phone,
                registration_group=resolved_group,
                app_name=resolved_app_name,
                dept_name=resolved_dept_name,
                country=str(parsed_text.get('country') or '').strip() or None,
                invite_code=resolved_invite_code,
                app_name_explicit=bool(explicit_app_name),
                dept_name_explicit=bool(explicit_dept_name),
                submission_type='account_id' if resolved_account_id else 'screenshot',
                account_id=resolved_account_id,
                file_url='https://placeholder.lark.local/pending-image' if not resolved_account_id else None,
                file_type='text/plain' if not resolved_account_id else None,
                submitted_by=f'lark:{sender_id}',
                source_channel='ops_intake_workbench' if payload.get('_default_dept_override') else 'manual_cs_lark',
                source_bot_app_id=bot_app_id or None,
                source_message_id=message_id,
                source_chat_id=str(message.get('chat_id') or '') or None,
                remark=cleaned_text,
                submitted_at=utc_now(),
            )
        )
        intake_response['source'] = 'lark_event_bridge'
        intake_response['chat_type'] = chat_type
        intake_response['reply_phone'] = normalized_reply_phone
        intake_response['reply_id'] = resolved_account_id or '-'
        intake_response['reply_group'] = resolved_group or '-'
        return _finalize(message_id, intake_response)

    def submit_ops_intake_text(
        self,
        *,
        text: str,
        profile_name: Optional[str],
        submitted_by: str,
        default_app_override: Optional[str] = None,
        default_dept_override: Optional[str] = None,
    ) -> Dict[str, Any]:
        cleaned_text = str(text or '').strip()
        if not cleaned_text:
            raise HTTPException(status_code=400, detail='text_required')
        active_preset = self.resolve_intake_bot_preset(profile_name=profile_name or None)
        bot_app_id = str(active_preset.get('app_id') or self.current_lark_app_id or '').strip()
        synthetic_message_id = create_id('ops_msg')
        synthetic_sender = re.sub(r'[^A-Za-z0-9_.:-]+', '_', str(submitted_by or 'ops_user').strip())[:80] or 'ops_user'
        payload = {
            '_gateway_direct': True,
            '_bot_app_id': bot_app_id,
            '_default_app_override': str(default_app_override or '').strip(),
            '_default_dept_override': str(default_dept_override or '').strip(),
            'schema': '2.0',
            'header': {'event_type': 'im.message.receive_v1', 'app_id': bot_app_id},
            'event': {
                'sender': {'sender_id': {'open_id': f'ops:{synthetic_sender}'}},
                'message': {
                    'message_id': synthetic_message_id,
                    'message_type': 'text',
                    'chat_type': 'p2p',
                    'chat_id': 'ops_intake_submit',
                    'content': json.dumps({'text': cleaned_text}, ensure_ascii=False),
                },
            },
        }
        result = self._handle_lark_event_sync(payload)
        result['source'] = 'ops_intake_submit'
        result['profile_name'] = str(active_preset.get('profile_name') or profile_name or '').strip()
        result['submitted_by'] = submitted_by
        result['submitted_text'] = cleaned_text
        result.setdefault('reply_text', self._format_lark_reply_text(result))
        return result

    def _ops_intake_user_can_access_guild(self, user: Optional[Dict[str, Any]], guild_name: str) -> bool:
        role = str((user or {}).get('role') or '').strip().lower()
        if role in {OPS_AUTH_ROLE_SUPER_ADMIN, OPS_AUTH_ROLE_ADMIN, OPS_AUTH_ROLE_INTERNAL} or not role:
            return True
        if not ops_role_is_business(role):
            return False
        user_id = str((user or {}).get('user_id') or '').strip()
        if not user_id:
            return False
        with self.db.connect() as conn:
            assigned_total = conn.execute(
                'SELECT COUNT(*) AS total FROM intake_guild_assignees WHERE guild_name = ?',
                (str(guild_name or '').strip(),),
            ).fetchone()
            # No assignment means hidden from customer_service; admins can configure it first.
            if not assigned_total or int(assigned_total['total'] or 0) <= 0:
                return False
            row = conn.execute(
                'SELECT 1 FROM intake_guild_assignees WHERE guild_name = ? AND user_id = ? LIMIT 1',
                (str(guild_name or '').strip(), user_id),
            ).fetchone()
        return row is not None

    def _ops_intake_assignees_for_guild(self, guild_name: str) -> List[Dict[str, Any]]:
        with self.db.connect() as conn:
            rows = [dict(r) for r in conn.execute(
                """
                SELECT u.user_id, u.username, u.display_name, u.role, u.enabled
                FROM intake_guild_assignees a
                JOIN ops_users u ON u.user_id = a.user_id
                WHERE a.guild_name = ?
                ORDER BY COALESCE(u.display_name, u.username), u.username
                """,
                (str(guild_name or '').strip(),),
            ).fetchall()]
        for row in rows:
            row['enabled'] = bool(row.get('enabled'))
        return rows

    def _default_timo_intake_guild_name(self) -> str:
        return str(os.getenv('TIMO_GUILD_NAME') or 'Timo').strip() or 'Timo'

    def _default_timo_intake_guild_display_name(self) -> str:
        configured = (
            str(os.getenv('TIMO_GUILD_DISPLAY_NAME') or '').strip()
            or str(os.getenv('TIMO_INTAKE_GUILD_DISPLAY_NAME') or '').strip()
        )
        if configured:
            return configured
        guild_name = str(os.getenv('TIMO_GUILD_NAME') or '').strip()
        if guild_name and guild_name.lower() != 'timo':
            return guild_name
        return 'Royal Latam'

    def _timo_intake_guild_display_name(self, guild_name: str, executor: Optional[Dict[str, Any]] = None) -> str:
        normalized = str(guild_name or '').strip()
        if executor:
            return timo_guild_display_name(
                normalized,
                guild_id=executor.get('cms_guild_id'),
                guild_sid=executor.get('cms_guild_sid'),
            ) or self._default_timo_intake_guild_display_name()
        if normalized == self._default_timo_intake_guild_name():
            return timo_guild_display_name(self._default_timo_intake_guild_display_name())
        return timo_guild_display_name(normalized) or self._default_timo_intake_guild_display_name()

    def _is_virtual_timo_intake_guild_name(self, guild_name: str) -> bool:
        if self._find_fallback_timo_guild_executor_config():
            return False
        return str(guild_name or '').strip() == self._default_timo_intake_guild_name()

    def _auto_assign_default_mafubo_for_guild(self, guild_name: str, *, assigned_by: str) -> Dict[str, Any]:
        normalized_guild = str(guild_name or '').strip()
        if not normalized_guild:
            return {'ok': False, 'status': 'skipped', 'reason': 'empty_guild_name'}
        now = utc_now()
        with self.db.connect() as conn:
            user = conn.execute(
                """
                SELECT user_id, username, display_name, role, enabled
                FROM ops_users
                WHERE LOWER(username) = 'mafubo' AND enabled = 1
                ORDER BY updated_at DESC
                LIMIT 1
                """
            ).fetchone()
            if not user:
                return {'ok': False, 'status': 'skipped', 'reason': 'mafubo_user_not_found'}
            user_row = dict(user)
            role = str(user_row.get('role') or '').strip().lower()
            if role in {OPS_AUTH_ROLE_SUPER_ADMIN, OPS_AUTH_ROLE_ADMIN, OPS_AUTH_ROLE_INTERNAL}:
                return {
                    'ok': True,
                    'status': 'global_access',
                    'guild_name': normalized_guild,
                    'user_id': str(user_row.get('user_id') or ''),
                    'username': str(user_row.get('username') or 'mafubo'),
                    'role': role,
                }
            if role not in OPS_AUTH_BUSINESS_ROLES:
                return {
                    'ok': False,
                    'status': 'skipped',
                    'reason': 'mafubo_role_not_assignable',
                    'guild_name': normalized_guild,
                    'user_id': str(user_row.get('user_id') or ''),
                    'username': str(user_row.get('username') or 'mafubo'),
                    'role': role,
                }
            conn.execute(
                """
                INSERT INTO intake_guild_assignees (guild_name, user_id, assigned_by, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(guild_name, user_id)
                DO UPDATE SET assigned_by = excluded.assigned_by,
                              updated_at = excluded.updated_at
                """,
                (normalized_guild, str(user_row.get('user_id') or ''), str(assigned_by or '').strip(), now),
            )
            conn.commit()
        return {
            'ok': True,
            'status': 'assigned',
            'guild_name': normalized_guild,
            'user_id': str(user_row.get('user_id') or ''),
            'username': str(user_row.get('username') or 'mafubo'),
            'role': role,
        }

    def set_ops_intake_guild_assignees(self, *, guild_name: str, user_ids: List[str], assigned_by: str) -> Dict[str, Any]:
        normalized_guild = str(guild_name or '').strip()
        if not normalized_guild:
            raise HTTPException(status_code=400, detail='guild_name_required')
        clean_ids = []
        for user_id in user_ids or []:
            value = str(user_id or '').strip()
            if value and value not in clean_ids:
                clean_ids.append(value)
        now = utc_now()
        with self.db.connect() as conn:
            existing = conn.execute('SELECT 1 FROM guild_executors WHERE guild_name = ? LIMIT 1', (normalized_guild,)).fetchone()
            if not existing and not self._is_virtual_timo_intake_guild_name(normalized_guild):
                raise HTTPException(status_code=404, detail='guild_executor_not_found')
            business_roles = tuple(sorted(OPS_AUTH_BUSINESS_ROLES))
            role_placeholders = ','.join('?' for _ in business_roles)
            user_placeholders = ','.join('?' for _ in clean_ids)
            valid_rows = [dict(r) for r in conn.execute(
                f"SELECT user_id FROM ops_users WHERE role IN ({role_placeholders}) AND enabled = 1 AND user_id IN ({user_placeholders})",
                (*business_roles, *clean_ids),
            ).fetchall()] if clean_ids else []
            valid_ids = {str(r['user_id']) for r in valid_rows}
            invalid_ids = [user_id for user_id in clean_ids if user_id not in valid_ids]
            if invalid_ids:
                raise HTTPException(status_code=400, detail='invalid_customer_service_user')
            conn.execute('DELETE FROM intake_guild_assignees WHERE guild_name = ?', (normalized_guild,))
            for user_id in clean_ids:
                conn.execute(
                    'INSERT INTO intake_guild_assignees (guild_name, user_id, assigned_by, updated_at) VALUES (?, ?, ?, ?)',
                    (normalized_guild, user_id, str(assigned_by or '').strip(), now),
                )
            conn.commit()
        return {'ok': True, 'guild_name': normalized_guild, 'assignees': self._ops_intake_assignees_for_guild(normalized_guild)}

    def _merged_mcn_region_options(self, *, include_disabled: bool = False) -> list[dict[str, Any]]:
        overrides: Dict[str, Dict[str, Any]] = {}
        with self.db.connect() as conn:
            for row in conn.execute('SELECT code, enabled, sort_order, updated_at FROM mcn_region_options').fetchall():
                overrides[str(row['code'] or '').strip().upper()] = {
                    'enabled': bool(row['enabled']),
                    'sort_order': row['sort_order'],
                    'updated_at': row['updated_at'],
                }
        rows: list[dict[str, Any]] = []
        for item in _mcn_region_options(include_disabled=True):
            row = dict(item)
            code = str(row.get('code') or '').strip().upper()
            override = overrides.get(code) or {}
            if override:
                row['enabled'] = bool(override.get('enabled'))
                if override.get('sort_order') is not None:
                    row['sort_order'] = int(override.get('sort_order') or row.get('sort_order') or 999)
                row['updated_at'] = override.get('updated_at')
            if include_disabled or bool(row.get('enabled')):
                rows.append(row)
        rows.sort(key=lambda row: (int(row.get('sort_order') or 999), str(row.get('label_zh') or row.get('label') or '')))
        return rows

    def list_mcn_region_options(self, *, include_disabled: bool = False) -> Dict[str, Any]:
        options = self._merged_mcn_region_options(include_disabled=include_disabled)
        return {
            'options': options,
            'enabled_options': [row for row in options if bool(row.get('enabled'))],
            'source': 'mcn_region_options',
            'editable': True,
        }

    def update_mcn_region_options(self, payload: McnRegionOptionsUpdateRequest) -> Dict[str, Any]:
        known_codes = {str(item.get('code') or '').strip().upper() for item in _mcn_region_options(include_disabled=True)}
        updates: list[tuple[int, int, str, str]] = []
        for index, item in enumerate(payload.options or []):
            code = str(item.code or '').strip().upper()
            if not code or code not in known_codes:
                raise HTTPException(status_code=400, detail='invalid_region_code')
            sort_order = int(item.sort_order if item.sort_order is not None else (index + 1) * 10)
            updates.append((1 if bool(item.enabled) else 0, sort_order, utc_now(), code))
        if not updates:
            raise HTTPException(status_code=400, detail='region_options_required')
        with self.db.connect() as conn:
            for enabled, sort_order, updated_at, code in updates:
                conn.execute(
                    """
                    INSERT INTO mcn_region_options (code, enabled, sort_order, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(code) DO UPDATE SET enabled=excluded.enabled, sort_order=excluded.sort_order, updated_at=excluded.updated_at
                    """,
                    (code, enabled, sort_order, updated_at),
                )
            conn.commit()
        result = self.list_mcn_region_options(include_disabled=True)
        result['saved'] = True
        return result

    def _ops_intake_visible_guild_names(self, *, user: Optional[Dict[str, Any]]) -> List[str]:
        role = str((user or {}).get('role') or '').strip().lower()
        normalized_names: List[str] = []
        seen: set[str] = set()

        def _append_name(value: Any) -> None:
            name = str(value or '').strip()
            if not name or name in seen:
                return
            seen.add(name)
            normalized_names.append(name)

        with self.db.connect() as conn:
            if role in {OPS_AUTH_ROLE_SUPER_ADMIN, OPS_AUTH_ROLE_ADMIN, OPS_AUTH_ROLE_INTERNAL} or not role:
                rows = conn.execute(
                    """
                    SELECT guild_name
                    FROM guild_executors
                    WHERE enabled = 1 AND TRIM(COALESCE(guild_name, '')) != ''
                      AND LOWER(COALESCE(app_name, 'linky')) = 'linky'
                    ORDER BY LOWER(guild_name), guild_name
                    """
                ).fetchall()
            elif ops_role_is_business(role):
                user_id = str((user or {}).get('user_id') or '').strip()
                if not user_id:
                    return []
                rows = conn.execute(
                    """
                    SELECT DISTINCT ge.guild_name
                    FROM guild_executors ge
                    JOIN intake_guild_assignees iga ON iga.guild_name = ge.guild_name
                    WHERE ge.enabled = 1
                      AND TRIM(COALESCE(ge.guild_name, '')) != ''
                      AND LOWER(COALESCE(ge.app_name, 'linky')) = 'linky'
                      AND iga.user_id = ?
                    ORDER BY LOWER(ge.guild_name), ge.guild_name
                    """,
                    (user_id,),
                ).fetchall()
            else:
                return []
        for row in rows:
            _append_name(row['guild_name'] if isinstance(row, sqlite3.Row) else row[0])
        return normalized_names

    def list_ops_intake_filter_guilds(self, *, user: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        return {'rows': [{'guild_name': name} for name in self._ops_intake_visible_guild_names(user=user)]}

    def list_ops_intake_guilds(self, *, user: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        health_rows = {str(row.get('guild_name') or '').strip(): row for row in self.guild_executor_health().get('rows', [])}
        executors = self.list_guild_executors().get('rows', [])
        rows: List[Dict[str, Any]] = []
        for executor in executors:
            guild_name = str(executor.get('guild_name') or '').strip()
            if not guild_name or not bool(executor.get('enabled')):
                continue
            health = health_rows.get(guild_name, {})
            if not self._ops_intake_user_can_access_guild(user, guild_name):
                continue
            assignees = self._ops_intake_assignees_for_guild(guild_name)
            effective_proxy_url = self._resolve_executor_proxy_url(executor)
            effective_status = str(health.get('effective_status') or 'active').strip() or 'active'
            cms_configured = bool(executor.get('platform_authorization_configured'))
            cms_refresh_configured = bool(executor.get('cms_refresh_token_configured'))
            oauth_configured = bool(executor.get('oauth_configured'))
            cms_live_status = str(health.get('cms_live_status') or ('not_configured' if not (cms_configured or cms_refresh_configured) else 'unknown')).strip() or 'unknown'
            cms_channel_status = 'not_configured' if not (cms_configured or cms_refresh_configured) else ('valid' if cms_live_status == 'active' else ('invalid' if cms_live_status == 'inactive' else 'unknown'))
            code_channel_status = 'valid' if oauth_configured else 'not_configured'
            rows.append({
                'guild_name': guild_name,
                'effective_status': effective_status,
                'effective_reason': health.get('effective_reason') or '',
                'submission_enabled': effective_status == 'active',
                'route_type': 'cms_id' if cms_configured else 'invite_code',
                'code_required': not cms_configured,
                'default_app': self.lark_default_app_name or 'Linky',
                'default_agency': guild_name,
                'assignees': assignees,
                'cms_channel_status': cms_channel_status,
                'code_channel_status': code_channel_status,
                'cms_token_configured': cms_configured,
                'cms_refresh_token_configured': cms_refresh_configured,
                'oauth_configured': oauth_configured,
                'proxy_region': str(executor.get('proxy_region') or ''),
                'proxy_effective_configured': bool(effective_proxy_url),
                'proxy_region_mapping_configured': bool(str(executor.get('proxy_region') or '').strip() and self.guild_executor_proxy_region_urls.get(str(executor.get('proxy_region') or '').strip())),
            })
        rows.sort(key=lambda row: str(row.get('guild_name') or '').lower())
        return {'rows': rows, 'region_options': self.list_mcn_region_options(include_disabled=False).get('enabled_options', [])}

    def list_timo_intake_guilds(self, *, user: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        executors = self.list_timo_guild_executors(user=user, include_reward_tracks=False).get('rows', [])
        if executors:
            rows: List[Dict[str, Any]] = []
            cache_statuses: List[Dict[str, Any]] = []
            for executor in executors:
                guild_name = str(executor.get('guild_name') or '').strip()
                if not guild_name:
                    continue
                enabled = bool(executor.get('enabled'))
                platform_authorization_configured = bool(executor.get('platform_authorization_configured'))
                export_cache_status = self._timo_export_cache_status_for_executor(executor)
                timo_live_status = str(executor.get('timo_live_status') or ('not_configured' if not platform_authorization_configured else 'unknown')).strip() or 'unknown'
                cache_statuses.append(export_cache_status)
                rows.append({
                    'guild_name': guild_name,
                    'guild_id': str(executor.get('guild_id') or executor.get('cms_guild_id') or ''),
                    'guild_sid': str(executor.get('guild_sid') or executor.get('cms_guild_sid') or ''),
                    'guild_display_name': self._timo_intake_guild_display_name(guild_name, executor),
                    'effective_status': 'active' if enabled else 'inactive',
                    'effective_reason': '',
                    'submission_enabled': enabled and timo_live_status != 'inactive',
                    'route_type': 'timo_verify',
                    'code_required': False,
                    'default_app': 'Timo',
                    'default_agency': guild_name,
                    'assignees': executor.get('assignees') or self._ops_intake_assignees_for_guild(guild_name),
                    'cms_channel_status': 'valid' if timo_live_status == 'active' else ('invalid' if timo_live_status == 'inactive' else ('not_configured' if not platform_authorization_configured else 'unknown')),
                    'code_channel_status': 'not_configured',
                    'cms_token_configured': platform_authorization_configured,
                    'cms_refresh_token_configured': False,
                    'oauth_configured': False,
                    'platform_backend_url': str(executor.get('platform_backend_url') or TIMO_DEFAULT_API_BASE_URL).strip() or TIMO_DEFAULT_API_BASE_URL,
                    'cms_guild_id': str(executor.get('cms_guild_id') or ''),
                    'cms_guild_sid': str(executor.get('cms_guild_sid') or ''),
                    'bind_concurrency': int(executor.get('bind_concurrency') or 3),
                    'request_timeout_seconds': int(executor.get('request_timeout_seconds') or 15),
                    'proxy_region': '',
                    'proxy_effective_configured': False,
                    'proxy_region_mapping_configured': False,
                    'export_cache_status': export_cache_status,
                    'timo_live_status': timo_live_status,
                    'timo_live_checked_at': executor.get('timo_live_checked_at'),
                    'timo_live_reason': executor.get('timo_live_reason') or '',
                    'timo_live_capability': executor.get('timo_live_capability') or '',
                    'timo_live_is_stale': bool(executor.get('timo_live_is_stale')),
                    'virtual': False,
                })
            self._maybe_trigger_timo_export_cache_catchup(cache_statuses)
            return {'rows': rows}
        executor = self._find_fallback_timo_guild_executor_config() or {}
        guild_name = str(executor.get('guild_name') or self._default_timo_intake_guild_name()).strip() or self._default_timo_intake_guild_name()
        if not self._ops_intake_user_can_access_guild(user, guild_name):
            return {'rows': []}
        enabled = bool(executor.get('enabled')) if executor else True
        platform_authorization_configured = bool(str(executor.get('platform_authorization') or os.getenv('TIMO_TICKET') or os.getenv('TIMO_PLATFORM_AUTHORIZATION') or '').strip())
        rows = [{
            'guild_name': guild_name,
            'guild_id': str(executor.get('cms_guild_id') or ''),
            'guild_sid': str(executor.get('cms_guild_sid') or os.getenv('TIMO_USER_UUID') or os.getenv('TIMO_GUILD_UUID') or ''),
            'guild_display_name': self._timo_intake_guild_display_name(guild_name, executor if executor else None),
            'effective_status': 'active' if enabled else 'inactive',
            'effective_reason': '',
            'submission_enabled': enabled,
            'route_type': 'timo_verify',
            'code_required': False,
            'default_app': 'Timo',
            'default_agency': guild_name,
            'assignees': self._ops_intake_assignees_for_guild(guild_name),
            'cms_channel_status': 'valid' if platform_authorization_configured else 'not_configured',
            'code_channel_status': 'not_configured',
            'cms_token_configured': platform_authorization_configured,
            'cms_refresh_token_configured': False,
            'oauth_configured': False,
            'platform_backend_url': str(executor.get('platform_backend_url') or os.getenv('TIMO_API_BASE_URL') or TIMO_DEFAULT_API_BASE_URL).strip() or TIMO_DEFAULT_API_BASE_URL,
            'cms_guild_id': str(executor.get('cms_guild_id') or ''),
            'cms_guild_sid': str(executor.get('cms_guild_sid') or os.getenv('TIMO_USER_UUID') or os.getenv('TIMO_GUILD_UUID') or ''),
            'bind_concurrency': int(executor.get('bind_concurrency') or 3),
            'request_timeout_seconds': int(executor.get('request_timeout_seconds') or 15),
            'proxy_region': str(executor.get('proxy_region') or ''),
            'proxy_effective_configured': False,
            'proxy_region_mapping_configured': False,
            'export_cache_status': self._timo_export_cache_status_for_executor({**executor, 'app_name': 'timo', 'guild_name': guild_name}),
            'virtual': not bool(executor),
        }]
        self._maybe_trigger_timo_export_cache_catchup([rows[0]['export_cache_status']])
        return {'rows': rows}

    def list_sogo_intake_guilds(self, *, user: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        executors = self.list_sogo_guild_executors(user=user).get('rows', [])
        rows: List[Dict[str, Any]] = []
        for executor in executors:
            guild_name = str(executor.get('guild_name') or '').strip()
            if not guild_name:
                continue
            enabled = bool(executor.get('enabled'))
            platform_authorization_configured = bool(executor.get('platform_authorization_configured'))
            rows.append({
                'guild_name': guild_name,
                'guild_display_name': guild_name,
                'effective_status': 'active' if enabled else 'inactive',
                'effective_reason': '',
                'submission_enabled': enabled and platform_authorization_configured,
                'route_type': 'sogo_member_lookup',
                'code_required': False,
                'default_app': 'Sugo',
                'default_agency': guild_name,
                'assignees': executor.get('assignees') or self._ops_intake_assignees_for_guild(guild_name),
                'cms_channel_status': 'valid' if platform_authorization_configured else 'not_configured',
                'code_channel_status': 'not_configured',
                'cms_token_configured': platform_authorization_configured,
                'cms_refresh_token_configured': bool(executor.get('cms_refresh_token_configured')),
                'oauth_configured': False,
                'platform_backend_url': str(executor.get('platform_backend_url') or SUGO_DEFAULT_API_BASE_URL).strip() or SUGO_DEFAULT_API_BASE_URL,
                'cms_guild_id': str(executor.get('cms_guild_id') or ''),
                'cms_guild_sid': str(executor.get('cms_guild_sid') or ''),
                'bind_concurrency': int(executor.get('bind_concurrency') or 1),
                'request_timeout_seconds': int(executor.get('request_timeout_seconds') or 30),
                'proxy_region': '',
                'proxy_effective_configured': False,
                'proxy_region_mapping_configured': False,
                'virtual': False,
            })
        return {'rows': rows}

    def parse_ops_intake_text(self, *, guild_name: str, text: str, fields: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        normalized_guild = str(guild_name or '').strip()
        cleaned_text = str(text or '').strip()
        parsed = parse_manual_cs_message(text=cleaned_text, image_ocr_text=None)
        explicit_fields = extract_explicit_intake_fields(cleaned_text)
        bare_candidates = extract_bare_multiline_candidates(cleaned_text)
        fields = fields or {}
        executor = self.resolve_guild_executor(normalized_guild) or {}
        guild_country_context = normalize_country_label((executor or {}).get('country'))
        field_country_context = infer_country_context(
            fields.get('country'),
            fields.get('region'),
            fields.get('area'),
            fields.get('country_region'),
        )
        raw_phone_input = str(fields.get('phone') or explicit_fields.get('mobile') or bare_candidates.get('mobile_line') or '').strip()
        parsed_mobile = str(parsed.get('mobile') or '').strip()
        mobile_raw = raw_phone_input or parsed_mobile
        group_from_fields = str(fields.get('group') or '').strip()
        group_from_text = str(explicit_fields.get('registration_group') or bare_candidates.get('registration_group_line') or parsed.get('registration_group') or '').strip()
        group_value = str(group_from_fields or group_from_text).strip()
        phone_country_context = field_country_context or str(parsed.get('country') or '').strip() or guild_country_context
        normalized_phone = format_display_phone(mobile_raw, area_code=parsed.get('area_code'), country=phone_country_context) if mobile_raw else ''
        code_required = not bool(str(executor.get('platform_authorization') or '').strip())
        id_only_cms_bind = is_external_app_id_only_phone(normalized_phone or mobile_raw) and not code_required
        try:
            if id_only_cms_bind:
                normalized_phone = str(normalized_phone or mobile_raw or '').strip()
                normalized_phone_area_code = 0
            else:
                _, normalized_phone_area_code, _ = normalize_phone_identity(
                    mobile=normalized_phone,
                    area_code=int(parsed.get('area_code') or 0),
                    country=phone_country_context,
                )
        except Exception:
            normalized_phone_area_code = int(parsed.get('area_code') or 0)
        digits_only_phone = ''.join(ch for ch in str(mobile_raw or '') if ch.isdigit())
        indonesia_context = '🇮🇩' in group_value or 'indonesia' in group_value.lower() or normalized_guild.lower() in {'carote', 'permata', 'piso', 'sampanye'}
        if mobile_raw and not phone_country_context and not str(mobile_raw).strip().startswith('+') and digits_only_phone.startswith('0') and indonesia_context:
            normalized_phone = format_display_phone(digits_only_phone[1:], area_code=62)
        group_auto_filled = False
        group_auto_fill_source = ''
        group_auto_fill_confidence = ''
        if not group_value and normalized_phone:
            membership_lookup = self._find_registration_group_memberships_for_phone(
                mobile=normalized_phone,
                area_code=(normalized_phone_area_code or (62 if normalized_phone.startswith('+62 ') else None)),
            )
            if membership_lookup.get('status') == 'unique':
                group_value = str((membership_lookup.get('match') or {}).get('resolved_registration_group') or '').strip()
                if group_value:
                    group_auto_filled = True
                    group_auto_fill_source = 'registration_group_approval_history'
                    group_auto_fill_confidence = 'unique'
            elif membership_lookup.get('status') == 'missing':
                group_value = OTHER_CHANNEL_REGISTRATION_GROUP
                group_auto_filled = True
                group_auto_fill_source = 'no_registration_group_history'
                group_auto_fill_confidence = 'fallback'
        elif group_from_fields and not group_from_text and normalized_phone:
            membership_lookup = self._find_registration_group_memberships_for_phone(
                mobile=normalized_phone,
                area_code=(normalized_phone_area_code or (62 if normalized_phone.startswith('+62 ') else None)),
            )
            matched_group = str((membership_lookup.get('match') or {}).get('resolved_registration_group') or '').strip() if membership_lookup.get('status') == 'unique' else ''
            if matched_group and matched_group == group_value:
                group_auto_filled = True
                group_auto_fill_source = 'registration_group_approval_history'
                group_auto_fill_confidence = 'unique'
        raw_account_label_match = re.search(r'(?:^|\n|\b)(?:id|uid|account_id)\s*[:：]?\s*([^\n]+)', cleaned_text, flags=re.IGNORECASE)
        raw_account_label = str(raw_account_label_match.group(1) or '').strip() if raw_account_label_match else ''
        raw_account_input = str(fields.get('account_id') or explicit_fields.get('account_id') or raw_account_label or bare_candidates.get('account_id_line') or parsed.get('account_id') or '').strip()
        account_id = raw_account_input if raw_account_input.isdigit() else ''
        invite_raw = str(fields.get('code') or explicit_fields.get('invite_code') or parsed.get('invite_code') or parsed.get('evidence', {}).get('invite_code_raw_input') or '').strip()
        invite_meta = normalize_invite_code_candidate(invite_raw or None)
        code = str(invite_meta.get('normalized') or '').strip().upper() if invite_meta.get('is_valid') else invite_raw
        display_code = (code if code_required else '-')
        code_missing_for_required_route = code_required and (not code or invite_raw == '-')
        field_values = {
            'phone': normalized_phone,
            'account_id': account_id,
            'group': group_value,
            'code': display_code or ('-' if not code_required else ''),
            'app': str(fields.get('app') or self.lark_default_app_name or 'Linky').strip() or 'Linky',
            'agency': normalized_guild,
            'country': phone_country_context,
        }
        fast_validation_error = validate_fast_intake_fields(
            mobile=None if id_only_cms_bind else (normalized_phone if normalized_phone else None),
            app_name=field_values['app'],
            account_id=raw_account_input if raw_account_input else None,
            country=phone_country_context,
        )
        validation = {
            'phone': bool(normalized_phone),
            'account_id': bool(account_id),
            'group': bool(group_value),
            'code': (not code_missing_for_required_route if code_required else True),
        }
        errors = []
        if not normalized_phone:
            errors.append('missing_phone')
        if not account_id:
            errors.append('missing_id')
        if not group_value:
            errors.append('missing_group')
        if code_missing_for_required_route:
            errors.append('missing_code')
        if fast_validation_error:
            if fast_validation_error.get('reason') == 'invalid_phone_format':
                validation['phone'] = False
                if 'missing_phone' in errors:
                    errors.remove('missing_phone')
                errors.append('invalid_phone')
            elif fast_validation_error.get('reason') == 'invalid_account_id_format':
                validation['account_id'] = False
                if 'missing_id' in errors and raw_account_input:
                    errors.remove('missing_id')
                errors.append('invalid_id')
        if code_required and invite_raw and invite_meta and not invite_meta.get('is_valid') and invite_raw != '-':
            validation['code'] = False
            if invite_meta.get('has_confusable_characters'):
                errors.append('invalid_code_confusable_characters')
            else:
                errors.append('invalid_code')
        return {
            'guild_name': normalized_guild,
            'fields': field_values,
            'validation': validation,
            'errors': errors,
            'can_submit': not errors,
            'code_required': code_required,
            'group_auto_filled': group_auto_filled,
            'group_auto_fill_source': group_auto_fill_source,
            'group_auto_fill_confidence': group_auto_fill_confidence,
            'group_auto_fill_confirmed': bool(fields.get('group_auto_fill_confirmed')),
            'raw_text': cleaned_text,
        }

    def _classify_ops_intake_result_status(self, result: Dict[str, Any]) -> str:
        reason = str(result.get('reason') or '').strip()
        next_action = str(result.get('next_action') or '').strip()
        reply_text = str(result.get('reply_text') or '')
        crm_verified = bool(result.get('crm_verified') or result.get('current_submission_crm_verified'))
        bind_success = result.get('accepted') and ('✅ Success' in reply_text or self._is_verified_success_result(result) or crm_verified)
        result_reason_text = str(result.get('result_reason') or '').strip().lower()
        result_code_text = str(result.get('result_code') or '').strip().lower()
        duplicate_crm_failure = (
            result_code_text in {'duplicate_sid', 'duplicate_submission_after_verified_success', 'duplicate_sid_existing_crm'}
            or 'duplicate submission' in reply_text.lower()
            or 'data duplication' in result_reason_text
            or 'duplicate_sid' in result_reason_text
            or 'sid already exists' in result_reason_text
        )
        if duplicate_crm_failure:
            return 'bind_failed'
        if str(result.get('bind_precheck') or '').strip() == 'already_in_target_guild' or 'Previously registered in this agency' in reply_text:
            return 'bind_failed'
        if self._has_successful_bind_and_crm_record(result):
            return 'fully_success'
        if reason == 'crm_sync_failed' or 'Bind Success, CRM Failed' in reply_text:
            return 'partial_success_crm_failed'
        if result.get('lead_status') == 'bind_success' and not crm_verified and reason in {'crm_sync_failed', 'crm_sync_retry_pending'}:
            return 'partial_success_crm_failed'
        if bind_success and crm_verified:
            return 'fully_success'
        if next_action == 'queue_bind_check':
            return 'bind_queued'
        if next_action == 'manual_review':
            return 'manual_required'
        return 'bind_failed'

    def _ops_intake_feedback_status_for_system_status(self, system_status: str) -> str:
        return 'pending_feedback' if str(system_status or '') == 'fully_success' else 'not_feedbackable'

    def _ops_intake_dedupe_route(self, *, code_required: bool) -> str:
        return 'guild_invite_code' if code_required else 'cms_id'

    def _build_ops_intake_idempotency_key(self, *, guild_name: str, phone: str, account_id: str, group: str, code: str, code_required: bool) -> str:
        parts = [str(guild_name or '').strip().lower(), str(phone or '').strip(), str(account_id or '').strip(), str(group or '').strip().lower()]
        if code_required:
            parts.append(str(code or '').strip().upper())
        raw = '\u001f'.join(parts)
        return hashlib.sha256(raw.encode('utf-8')).hexdigest()

    def _build_ops_intake_route_snapshot(self, *, guild_name: str, executor: Optional[Dict[str, Any]], code_required: bool, idempotency_key: str, dedupe_route: str) -> Dict[str, Any]:
        executor = executor or {}
        version_source = json.dumps({
            'guild_name': guild_name,
            'enabled': bool(executor.get('enabled')),
            'platform_backend_url_configured': bool(str(executor.get('platform_backend_url') or '').strip()),
            'platform_authorization_configured': bool(str(executor.get('platform_authorization') or '').strip()),
            'cms_guild_id': str(executor.get('cms_guild_id') or ''),
            'cms_guild_sid': str(executor.get('cms_guild_sid') or ''),
            'backend_url': str(executor.get('backend_url') or ''),
        }, ensure_ascii=False, sort_keys=True)
        return {
            'bind_route': dedupe_route,
            'expected_guild': str(guild_name or '').strip(),
            'executor_config_version': hashlib.sha256(version_source.encode('utf-8')).hexdigest()[:16],
            'cms_enabled_snapshot': not bool(code_required),
            'invite_code_required_snapshot': bool(code_required),
            'code_included_in_dedupe': bool(code_required),
            'idempotency_key': idempotency_key,
        }

    def _is_ops_intake_final(self, *, system_status: str, feedback_status: str) -> bool:
        return str(system_status or '') in {'fully_success', 'partial_success_crm_failed', 'bind_failed', 'validation_failed', 'manual_required', 'route_mismatch'} and str(feedback_status or '') in {'feedback_done', 'cleared'}

    def _find_ops_intake_duplicate_pending(self, conn: sqlite3.Connection, *, guild_name: str, phone: str, account_id: str, submitted_by_username: str = '') -> Optional[sqlite3.Row]:
        return conn.execute(
            """
            SELECT item_id, submitted_by_username, system_status, feedback_status
            FROM ops_intake_items
            WHERE guild_name = ?
              AND parsed_phone = ?
              AND parsed_account_id = ?
              AND COALESCE(submitted_by_username, '') != ?
              AND COALESCE(feedback_status, '') NOT IN ('feedback_done', 'cleared')
              AND COALESCE(system_status, '') NOT IN ('fully_success', 'partial_success_crm_failed', 'bind_failed', 'validation_failed', 'manual_required', 'route_mismatch')
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (guild_name, phone, account_id, submitted_by_username),
        ).fetchone()

    def _update_ops_intake_task_route_snapshot(self, *, task_id: str, route_snapshot: Dict[str, Any]) -> None:
        normalized_task_id = str(task_id or '').strip()
        if not normalized_task_id or not route_snapshot:
            return
        with self.db.connect() as conn:
            row = conn.execute('SELECT payload FROM automation_tasks WHERE task_id = ?', (normalized_task_id,)).fetchone()
            if not row:
                return
            try:
                payload = json.loads(row['payload'] or '{}')
            except Exception:
                payload = {}
            payload['route_snapshot'] = route_snapshot
            payload['expected_guild'] = route_snapshot.get('expected_guild') or payload.get('expected_guild')
            conn.execute('UPDATE automation_tasks SET payload = ? WHERE task_id = ?', (json.dumps(payload, ensure_ascii=False), normalized_task_id))
            conn.commit()

    def _find_ops_intake_items_for_bind_update(
        self,
        conn: sqlite3.Connection,
        *,
        task_id: str,
        lead_id: str,
        submission_id: str,
    ) -> List[Dict[str, Any]]:
        normalized_task_id = str(task_id or '').strip()
        normalized_lead_id = str(lead_id or '').strip()
        normalized_submission_id = str(submission_id or '').strip()
        candidate_task_ids: List[str] = []
        if normalized_task_id:
            candidate_task_ids.append(normalized_task_id)
        if normalized_lead_id and normalized_submission_id:
            related_rows = conn.execute(
                """
                SELECT task_id
                FROM automation_tasks
                WHERE lead_id = ?
                  AND task_type = 'bind_check'
                  AND payload LIKE ?
                ORDER BY created_at ASC, task_id ASC
                """,
                (normalized_lead_id, f'%"submission_id": "{normalized_submission_id}"%'),
            ).fetchall()
            for row in related_rows:
                candidate = str(row['task_id'] or '').strip()
                if candidate and candidate not in candidate_task_ids:
                    candidate_task_ids.append(candidate)
        like_patterns: List[str] = []
        for candidate_task_id in candidate_task_ids:
            like_patterns.append(f'%"task_id": "{candidate_task_id}"%')
            like_patterns.append(f'%"retry_task_id": "{candidate_task_id}"%')
        if normalized_submission_id:
            like_patterns.append(f'%"submission_id": "{normalized_submission_id}"%')
        if not like_patterns:
            return []
        where_clause = ' OR '.join('result_snapshot LIKE ?' for _ in like_patterns)
        rows = conn.execute(
            f"SELECT * FROM ops_intake_items WHERE {where_clause} ORDER BY created_at ASC, item_id ASC",
            tuple(like_patterns),
        ).fetchall()
        items: List[Dict[str, Any]] = [dict(row) for row in rows]
        seen_item_ids = {str(item.get('item_id') or '') for item in items}
        active_statuses = ('queued', 'processing', 'bind_queued', 'binding', 'crm_verifying')
        if normalized_lead_id:
            lead_rows = conn.execute(
                f"""
                SELECT * FROM ops_intake_items
                WHERE COALESCE(feedback_status, '') NOT IN ('feedback_done', 'cleared')
                  AND system_status IN ({','.join('?' for _ in active_statuses)})
                  AND result_snapshot LIKE ?
                ORDER BY created_at ASC, item_id ASC
                """,
                (*active_statuses, f'%"lead_id": "{normalized_lead_id}"%'),
            ).fetchall()
            for row in lead_rows:
                item = dict(row)
                item_id = str(item.get('item_id') or '')
                if item_id and item_id not in seen_item_ids:
                    items.append(item)
                    seen_item_ids.add(item_id)
        task_payload: Dict[str, Any] = {}
        if normalized_task_id:
            task_row = conn.execute("SELECT payload FROM automation_tasks WHERE task_id = ?", (normalized_task_id,)).fetchone()
            if task_row:
                try:
                    task_payload = json.loads(task_row['payload'] or '{}')
                except Exception:
                    task_payload = {}
                task_payload = task_payload if isinstance(task_payload, dict) else {}
        account_id = str(task_payload.get('account_id') or '').strip()
        expected_guild = str(task_payload.get('expected_guild') or '').strip()
        if not expected_guild and normalized_lead_id:
            lead_row = conn.execute("SELECT dept_name FROM leads WHERE lead_id = ?", (normalized_lead_id,)).fetchone()
            expected_guild = str((lead_row['dept_name'] if lead_row else '') or '').strip()
        if account_id and expected_guild:
            sibling_rows = conn.execute(
                f"""
                SELECT * FROM ops_intake_items
                WHERE COALESCE(feedback_status, '') NOT IN ('feedback_done', 'cleared')
                  AND system_status IN ({','.join('?' for _ in active_statuses)})
                  AND parsed_account_id = ?
                  AND guild_name = ?
                ORDER BY created_at ASC, item_id ASC
                """,
                (*active_statuses, account_id, expected_guild),
            ).fetchall()
            for row in sibling_rows:
                item = dict(row)
                item_id = str(item.get('item_id') or '')
                if item_id and item_id not in seen_item_ids:
                    items.append(item)
                    seen_item_ids.add(item_id)
        return items

    def _update_ops_intake_items_after_bind_result(
        self,
        conn: sqlite3.Connection,
        *,
        task_id: str,
        lead_id: str,
        submission_id: str,
        result: Dict[str, Any],
        reply_envelope: Dict[str, Any],
        reply_text: str,
    ) -> List[Dict[str, Any]]:
        items = self._find_ops_intake_items_for_bind_update(
            conn,
            task_id=task_id,
            lead_id=lead_id,
            submission_id=submission_id,
        )
        if not items:
            return []
        updated_items: List[Dict[str, Any]] = []
        active_task_id = str(result.get('retry_task_id') or task_id or '').strip() or str(task_id or '').strip()
        for item in items:
            try:
                existing_snapshot = json.loads(item.get('result_snapshot') or '{}')
            except Exception:
                existing_snapshot = {}
            merged_snapshot = dict(existing_snapshot) if isinstance(existing_snapshot, dict) else {}
            merged_snapshot.update(result or {})
            merged_snapshot.update(reply_envelope or {})
            merged_snapshot['reply_text'] = reply_text
            if active_task_id:
                merged_snapshot['task_id'] = active_task_id
            if task_id and task_id != active_task_id:
                merged_snapshot['source_task_id'] = str(task_id)
            if lead_id and not merged_snapshot.get('lead_id'):
                merged_snapshot['lead_id'] = str(lead_id)
            if submission_id and not merged_snapshot.get('submission_id'):
                merged_snapshot['submission_id'] = str(submission_id)
            merged_snapshot.setdefault('result_code', str(result.get('result_code') or ''))
            merged_snapshot.setdefault('result_reason', str(result.get('result_reason') or result.get('reason') or ''))
            updated_system_status = self._classify_ops_intake_result_status(merged_snapshot)
            updated_feedback_status = self._ops_intake_feedback_status_for_system_status(updated_system_status)
            processed_at = utc_now()
            conn.execute(
                """
                UPDATE ops_intake_items
                SET system_status = ?, feedback_status = ?, reply_text = ?, result_code = ?, result_reason = ?, result_snapshot = ?, processed_at = ?
                WHERE item_id = ?
                """,
                (
                    updated_system_status,
                    updated_feedback_status,
                    reply_text,
                    str(result.get('result_code') or ''),
                    str(result.get('result_reason') or result.get('reason') or ''),
                    json.dumps(merged_snapshot, ensure_ascii=False, default=str),
                    processed_at,
                    str(item.get('item_id') or ''),
                ),
            )
            refreshed = dict(item)
            refreshed.update({
                'system_status': updated_system_status,
                'feedback_status': updated_feedback_status,
                'reply_text': reply_text,
                'result_code': str(result.get('result_code') or ''),
                'result_reason': str(result.get('result_reason') or result.get('reason') or ''),
                'result_snapshot': json.dumps(merged_snapshot, ensure_ascii=False, default=str),
                'processed_at': processed_at,
            })
            updated_items.append(refreshed)
        return updated_items

    def submit_ops_intake_guild_item(self, *, guild_name: str, text: str, fields: Optional[Dict[str, Any]], user: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        normalized_guild = str(guild_name or '').strip()
        if not self._ops_intake_user_can_access_guild(user, normalized_guild):
            raise HTTPException(status_code=403, detail='ops_guild_intake_forbidden')
        parsed = self.parse_ops_intake_text(guild_name=normalized_guild, text=text, fields=fields)
        if not parsed.get('can_submit'):
            raise HTTPException(status_code=400, detail={'reason': 'parse_validation_failed', 'errors': parsed.get('errors', []), 'parsed': parsed})
        field_values = parsed['fields']
        submitted_by = str((user or {}).get('username') or (user or {}).get('display_name') or (user or {}).get('user_id') or 'ops_user').strip()
        raw_code_value = str(field_values.get('code') or '').strip()
        submit_code_value = '' if not parsed.get('code_required') else raw_code_value
        code_required = bool(parsed.get('code_required'))
        dedupe_route = self._ops_intake_dedupe_route(code_required=code_required)
        idempotency_key = self._build_ops_intake_idempotency_key(
            guild_name=normalized_guild,
            phone=str(field_values.get('phone') or ''),
            account_id=str(field_values.get('account_id') or ''),
            group=str(field_values.get('group') or ''),
            code=submit_code_value,
            code_required=code_required,
        )
        route_snapshot = self._build_ops_intake_route_snapshot(
            guild_name=normalized_guild,
            executor=self.resolve_guild_executor(normalized_guild) or {},
            code_required=code_required,
            idempotency_key=idempotency_key,
            dedupe_route=dedupe_route,
        )
        with self.db.connect() as conn:
            duplicate_pending = self._find_ops_intake_duplicate_pending(
                conn,
                guild_name=normalized_guild,
                phone=str(field_values.get('phone') or ''),
                account_id=str(field_values.get('account_id') or ''),
                submitted_by_username=submitted_by,
            )
            if duplicate_pending:
                raise HTTPException(status_code=409, detail={
                    'reason': 'duplicate_pending',
                    'existing_item_id': duplicate_pending['item_id'],
                    'existing_status': duplicate_pending['system_status'],
                    'existing_feedback_status': duplicate_pending['feedback_status'],
                    'existing_owner': duplicate_pending['submitted_by_username'],
                })
            duplicate_row = conn.execute(
                """
                SELECT item_id, result_snapshot FROM ops_intake_items
                WHERE guild_name = ?
                  AND submitted_by_username = ?
                  AND parsed_phone = ?
                  AND parsed_account_id = ?
                  AND parsed_group = ?
                  AND COALESCE(parsed_code, '') = ?
                  AND COALESCE(feedback_status, '') NOT IN ('cleared', 'feedback_done')
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (
                    normalized_guild,
                    submitted_by,
                    field_values.get('phone') or '',
                    field_values.get('account_id') or '',
                    field_values.get('group') or '',
                    submit_code_value,
                ),
            ).fetchone()
        if duplicate_row:
            try:
                duplicate_result = json.loads(duplicate_row['result_snapshot'] or '{}')
            except Exception:
                duplicate_result = {}
            return {
                'ok': True,
                'duplicate': True,
                'item': self._get_ops_intake_item(duplicate_row['item_id']),
                'result': duplicate_result,
                'parsed': parsed,
            }
        submit_lines = [
            f"App: {field_values.get('app') or self.lark_default_app_name or 'Linky'}",
            f"Agency: {field_values.get('agency') or normalized_guild}",
            f"Phone: {field_values.get('phone') or ''}",
            f"ID: {field_values.get('account_id') or ''}",
            f"Group: {field_values.get('group') or ''}",
        ]
        if str(field_values.get('country') or '').strip():
            submit_lines.append(f"Country: {str(field_values.get('country') or '').strip()}")
        if parsed.get('code_required') or submit_code_value:
            submit_lines.append(f"Code: {submit_code_value}")
        submit_text = '\n'.join(submit_lines)
        result = self.submit_ops_intake_text(
            text=submit_text,
            profile_name=None,
            submitted_by=submitted_by,
            default_app_override=str(field_values.get('app') or self.lark_default_app_name or 'Linky'),
            default_dept_override=str(field_values.get('agency') or normalized_guild),
        )
        if result.get('task_id'):
            self._update_ops_intake_task_route_snapshot(task_id=str(result.get('task_id') or ''), route_snapshot=route_snapshot)
        system_status = self._classify_ops_intake_result_status(result)
        feedback_status = self._ops_intake_feedback_status_for_system_status(system_status)
        now = utc_now()
        item_id = create_id('intake_item')
        reply_text = str(result.get('reply_text') or self._format_lark_reply_text(result) or '')
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO ops_intake_items (
                    item_id, guild_name, submitted_by_user_id, submitted_by_username, raw_text,
                    parsed_phone, parsed_account_id, parsed_group, parsed_code, parsed_app, parsed_agency,
                    system_status, feedback_status, reply_text, result_code, result_reason, result_snapshot,
                    created_at, processed_at, idempotency_key, dedupe_route, route_snapshot,
                    group_auto_filled, group_auto_fill_source, group_auto_fill_confidence,
                    group_auto_fill_confirmed, group_auto_fill_confirmed_by, group_auto_fill_confirmed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item_id,
                    normalized_guild,
                    str((user or {}).get('user_id') or '').strip(),
                    submitted_by,
                    str(text or '').strip(),
                    field_values.get('phone') or '',
                    field_values.get('account_id') or '',
                    field_values.get('group') or '',
                    submit_code_value,
                    field_values.get('app') or '',
                    field_values.get('agency') or '',
                    system_status,
                    feedback_status,
                    reply_text,
                    str(result.get('result_code') or ''),
                    str(result.get('result_reason') or result.get('reason') or ''),
                    json.dumps(result, ensure_ascii=False, default=str),
                    now,
                    now,
                    idempotency_key,
                    dedupe_route,
                    json.dumps(route_snapshot, ensure_ascii=False, sort_keys=True),
                    1 if parsed.get('group_auto_filled') else 0,
                    str(parsed.get('group_auto_fill_source') or ''),
                    str(parsed.get('group_auto_fill_confidence') or ''),
                    1 if parsed.get('group_auto_fill_confirmed') else 0,
                    submitted_by if parsed.get('group_auto_fill_confirmed') else None,
                    now if parsed.get('group_auto_fill_confirmed') else None,
                ),
            )
            conn.commit()
        item = self._get_ops_intake_item(item_id)
        self._upsert_binding_current_truth_snapshot(item, result)
        item = self._get_ops_intake_item(item_id)
        return {'ok': True, 'item': item, 'result': result, 'parsed': parsed}

    def _external_app_item_response(self, item: Dict[str, Any], *, has_submission: Optional[bool] = None, duplicate: bool = False) -> Dict[str, Any]:
        internal_system_status = str(item.get('system_status') or '').strip()
        internal_feedback_status = str(item.get('feedback_status') or '').strip()
        crm_partial_success = internal_system_status == 'partial_success_crm_failed'
        system_status = 'fully_success' if crm_partial_success else internal_system_status
        feedback_status = internal_feedback_status
        if crm_partial_success and feedback_status not in {'feedback_done', 'cleared'}:
            feedback_status = 'pending_feedback'
        raw_reply_template = str(item.get('reply_text') or '').strip()
        try:
            external_payload = json.loads(str(item.get('external_payload') or '{}'))
        except Exception:
            external_payload = {}
        external_payload = external_payload if isinstance(external_payload, dict) else {}
        identity_mode = str(external_payload.get('identity_mode') or '').strip()
        if not identity_mode:
            identity_mode = 'id_only_cms_bind' if is_external_app_id_only_phone(item.get('parsed_phone')) else 'phone'
        phone_backfill_status = str(external_payload.get('phone_backfill_status') or '').strip()
        if not phone_backfill_status:
            phone_backfill_status = 'missing' if identity_mode == 'id_only_cms_bind' else 'not_needed'
        reply_template_statuses = {
            'fully_success', 'partial_success_crm_failed', 'bind_failed', 'failed',
            'crm_failed', 'validation_failed', 'manual_required', 'route_mismatch', 'already_registered',
        }
        reply_template_source = raw_reply_template
        if crm_partial_success:
            reply_template_source = self._external_app_success_reply_text_for_crm_partial(item, raw_reply_template)
        reply_template = self._external_app_reply_template_zh(reply_template_source) if reply_template_source and system_status in reply_template_statuses else None
        raw_reason = ''
        if reply_template_source and system_status in reply_template_statuses:
            raw_reason = str(reply_template_source.split('\n', 1)[0]).replace('**', '').strip()
        response = {
            'ok': True,
            'app': str(item.get('parsed_app') or 'Linky').strip() or 'Linky',
            'submission_id': str(item.get('item_id') or '').strip(),
            'external_user_id': str(item.get('external_user_id') or '').strip(),
            'initiator': self._ops_intake_display_initiator(item),
            'system_status': system_status,
            'feedback_status': feedback_status,
            'message': self._external_app_status_message(system_status=system_status, feedback_status=feedback_status),
            'identity_mode': identity_mode,
            'phone_backfill_status': phone_backfill_status,
            'reply_template': reply_template,
            'reply_template_language': 'zh-CN' if reply_template else None,
            'updated_at': item.get('processed_at') or item.get('created_at') or utc_now(),
        }
        if has_submission is not None:
            response['has_submission'] = bool(has_submission)
        if duplicate:
            response['duplicate'] = True
        if raw_reason:
            response['reason'] = raw_reason
        if crm_partial_success:
            response.update({
                'internal_system_status': internal_system_status,
                'internal_feedback_status': internal_feedback_status,
                'crm_sync_status': 'pending_internal_compensation',
                'crm_sync_pending_internal': True,
            })
        return response

    def _external_app_success_reply_text_for_crm_partial(self, item: Dict[str, Any], raw_reply_template: str) -> str:
        text = str(raw_reply_template or '').strip()
        if text:
            lines = text.split('\n')
            lines[0] = '**✅ Success**'
            return '\n'.join(lines)
        phone = str(item.get('parsed_phone') or '-').strip() or '-'
        account_id = str(item.get('parsed_account_id') or '-').strip() or '-'
        group = str(item.get('parsed_group') or item.get('guild_name') or '-').strip() or '-'
        code = str(item.get('parsed_code') or 'N/A (CMS ID bind)').strip() or 'N/A (CMS ID bind)'
        return f'**✅ Success**\nPhone: {phone}\nID: {account_id}\nGroup: {group}\nCode: {code}'

    def _external_app_status_message(self, *, system_status: str, feedback_status: str) -> str:
        if system_status == 'fully_success' and feedback_status == 'pending_feedback':
            return '绑定成功，可以反馈用户'
        if system_status == 'fully_success' and feedback_status == 'feedback_done':
            return '已反馈用户'
        if system_status == 'partial_success_crm_failed':
            return '后台已完成绑定，资料同步中，请勿反馈最终成功'
        if system_status in {'bind_failed', 'failed', 'crm_failed', 'validation_failed'}:
            return '绑定失败，请核对资料后重新提交'
        if system_status == 'manual_required':
            return '需要人工处理'
        return '已提交，系统处理中，请勿重复提交'

    def _external_app_reply_template_zh(self, reply_text: Any) -> str:
        raw = str(reply_text or '').replace('**', '').strip()
        if not raw:
            return ''
        headline_map = [
            ('✅ Success', '✅ 成功'),
            ('⏳ Processing', '⏳ 处理中'),
            ('❌ Previously registered in this agency', '❌ 绑定失败：该用户此前已在本公会注册。'),
            ('❌ Bind failed: Previously registered in this agency', '❌ 绑定失败：该用户此前已在本公会注册。'),
            ('❌ Bind failed: Failed to join the agency. Your country does not match the agency country.', '❌ 绑定失败：用户国家/地区与公会国家/地区不一致。'),
            ('🚫 Country does not match this agency.', '🚫 国家/地区与当前公会不匹配，未发起绑定。'),
            ('❌ Bind failed: CMS rejected bind request. Check manually.', '❌ 绑定失败：CMS 拒绝绑定请求，请人工检查。'),
            ('❌ Bind failed: CMS rejected bind request, manual check required', '❌ 绑定失败：CMS 拒绝绑定请求，需要人工检查。'),
            ('❌ Bind failed: Invalid or unavailable Linky ID', '❌ 绑定失败：Linky ID 无效或暂不可用。'),
            ('❌ Bind failed: CMS verification requires manual check', '❌ 绑定失败：CMS 核验需要人工检查。'),
            ('❌ Bind failed: Invalid personal code', '❌ 绑定失败：个人 Code 无效。'),
            ('❌ Bind failed: Backend session requires manual recovery', '❌ 绑定失败：后台登录态需要人工恢复。'),
            ('❌ Bind failed: bind executor unavailable. Check backend runtime.', '❌ 绑定失败：绑定执行器不可用，请检查后台运行状态。'),
            ('❌ Bind failed: backend login or authorization expired. Check manually.', '❌ 绑定失败：绑定后台登录态或授权异常，请人工检查。'),
            ('❌ Bind failed: The streamer was in another agency', '❌ 绑定失败：该用户已在其他公会。'),
            ('❌ Bind failed: Bind failed. Check manually.', '❌ 绑定失败：请人工检查。'),
            ('❌ Already registered in another agency', '❌ 该用户已在其他公会注册。'),
            ('❌ Bind failed: CMS authorization rejected with HTTP 403', '❌ 绑定失败：CMS 授权已失效或无权限，请人工检查。'),
            ('❌ Bind failed: CMS authorization does not allow adding this SID to the target guild', '❌ 绑定失败：CMS 授权不允许添加该 SID 到目标公会，请人工检查。'),
            ('❌ Bind failed: Falha ao entrar na Agência: seu país e o da Agência não correspondem.', '❌ 绑定失败：用户国家/地区与公会国家/地区不一致。'),
            ('❌ Bind failed: Gagal bergabung ke agency. Negara Anda tidak sama dengan negara agency tersebut', '❌ 绑定失败：用户国家/地区与公会国家/地区不一致。'),
            ('🚫 Invalid Code. Use a 6-character personal code: letters or letters+digits, not all digits.', '🚫 Code 无效。请使用 6 位个人 Code：字母或字母+数字，不能全数字。'),
            ('🚫 Missing: Code', '🚫 缺少 Code。请补充 6 位个人 Code。'),
            ('❌ Failed：Error Code Unable to Bind', '❌ 绑定失败：绑定后台登录态或授权异常，请人工检查。'),
            ('❌ Duplicate submission: user already joined this agency', '❌ 绑定失败：该用户此前已在本公会注册。'),
            ('❌ Duplicate submission', '❌ 绑定失败：该用户此前已在本公会注册。'),
            ('❌ Bind Success, CRM Failed', '✅ 绑定成功'),
            ('🚫 I do not handle this app/agency.', '🚫 当前客服不处理这个 App/公会。'),
            ('🚫 Invalid group format. Please copy the exact registration group name.', '🚫 群组格式无效，请复制准确的注册群名称。'),
            ('❌ Multiple registration groups found. Please provide Group.', '❌ 找到多个注册群，请补充 Group。'),
            ('❌ Failed', '❌ 失败'),
        ]
        translated = raw
        for en, zh in headline_map:
            if translated.startswith(en):
                translated = zh + translated[len(en):]
                break
        lines = []
        for line in translated.split('\n'):
            line = re.sub(r'^Phone:\s*', '手机号：', line)
            line = re.sub(r'^ID:\s*', 'ID：', line)
            line = re.sub(r'^Group:\s*', '群组：', line)
            line = re.sub(r'^Code:\s*', 'Code：', line)
            lines.append(line)
        return '\n'.join(lines)

    @staticmethod
    def _normalize_external_product_app(value: Optional[str]) -> str:
        raw = str(value or '').strip().lower()
        if not raw:
            raise HTTPException(status_code=400, detail={'reason': 'missing_app', 'message': '请填写 app，取值为 linky 或 timo'})
        compact = re.sub(r'[^a-z0-9]+', '', raw)
        if compact in {'linky', 'linkyapp', 'linkylive'}:
            return 'linky'
        if compact in {'timo', 'timoapp', 'touchchat'}:
            return 'timo'
        raise HTTPException(status_code=400, detail={'reason': 'unsupported_app', 'message': 'app 只支持 Linky 或 Timo'})

    @staticmethod
    def _external_product_app_display(app_slug: str) -> str:
        return 'Timo' if str(app_slug or '').strip().lower() == 'timo' else 'Linky'

    @staticmethod
    def _external_guild_match_key(value: Optional[str]) -> str:
        return ' '.join(str(value or '').strip().split()).casefold()

    def _validate_external_app_guild_match(self, *, app_slug: str, guild_name: str, source_config: Dict[str, Any]) -> None:
        canonical_guild = timo_guild_storage_name(guild_name) if app_slug == 'timo' else guild_name
        guild_key = self._external_guild_match_key(canonical_guild)
        app_guilds_raw = source_config.get('app_guilds') if isinstance(source_config, dict) else None
        app_guilds = app_guilds_raw if isinstance(app_guilds_raw, dict) else {}
        configured_guilds = app_guilds.get(app_slug) or []
        if configured_guilds:
            configured_keys = {
                self._external_guild_match_key(timo_guild_storage_name(value) if app_slug == 'timo' else value)
                for value in list(configured_guilds or [])
                if self._external_guild_match_key(value)
            }
            if guild_key not in configured_keys:
                raise HTTPException(status_code=403, detail={
                    'reason': 'app_guild_mismatch',
                    'message': f"{guild_name} 不属于 {self._external_product_app_display(app_slug)}，请检查 app 和公会是否选错",
                })
            return
        known_app = EXTERNAL_APP_KNOWN_GUILD_APP_MAP.get(guild_key)
        if known_app and known_app != app_slug:
            raise HTTPException(status_code=403, detail={
                'reason': 'app_guild_mismatch',
                'message': f"{guild_name} 不属于 {self._external_product_app_display(app_slug)}，请检查 app 和公会是否选错",
            })

    @staticmethod
    def _normalize_external_registration_group(value: Optional[str], *, guild_name: Optional[str] = None) -> str:
        group = str(value or '').strip()
        guild = str(guild_name or '').strip()
        if not group:
            return OTHER_CHANNEL_REGISTRATION_GROUP
        if guild and group.casefold() == guild.casefold():
            return OTHER_CHANNEL_REGISTRATION_GROUP
        return group

    @staticmethod
    def _humanize_timo_failure_reason(reason: Any, code: Any = '') -> str:
        raw = str(reason or code or '').strip()
        text = f"{reason or ''} {code or ''}".strip().lower()
        if not raw:
            return ''
        if 'crm app mapping is missing' in text or 'crm_mapping_failed' in text:
            return 'CRM 应用配置缺失，请联系管理员处理'
        if 'crm adapter is not configured' in text or 'crm_not_configured' in text:
            return 'CRM 写入通道未配置，请联系管理员处理'
        if 'please retry once' in text or '502' in text or 'non-json' in text or 'get_apps' in text:
            return 'CRM 临时不可用，请稍后重试'
        if (
            'data duplication' in text
            or 'duplication' in text
            or 'duplicate' in text
            or 'duplicate_sid' in text
            or 'sid already exists' in text
            or 'already exists' in text
        ):
            return 'CRM 已有重复资料，请检查历史记录'
        if 'crm_sync_failed' in text or 'crm write was rejected' in text or 'crm write could not be verified' in text:
            return 'CRM 写入失败，请稍后重试'
        if 'authorization' in text or 'ticket' in text or 'unauthorized' in text or '401' in text or '403' in text:
            return 'Timo 后台授权已失效，请更新 Ticket / Authorization'
        if 'not found in the guild member list' in text or 'timo_member_not_found' in text:
            return 'Timo 公会未查询到此成员，请确认邀请码和入会状态后重试'
        if 'timeout' in text or 'timed out' in text:
            return 'Timo 后台请求超时，请稍后重试'
        return raw

    def _external_app_timo_status_message(self, *, system_status: str, feedback_status: str, crm_sync_status: str, timo_result_code: str = '') -> str:
        if system_status in {'crm_success', 'verified_success'} and feedback_status == 'feedback_done':
            return '已反馈用户'
        if system_status in {'crm_success', 'verified_success'}:
            return 'Timo 资料已写入 CRM，可以反馈用户'
        if system_status == 'crm_failed' or crm_sync_status == 'failed':
            return '已验证在 Timo 公会中，但本次 CRM 写入失败，请检查历史记录'
        if str(timo_result_code or '').strip() == 'timo_ticket_expired':
            return '当前ticket已失效，无法进行成员列表查询'
        if system_status == 'verify_failed':
            return 'Timo 公会未查询到此成员，请确认邀请码和入会状态后重试'
        return '已提交，等待 CRM 写入，请勿重复提交'

    def _external_app_timo_reply_template(self, item: Dict[str, Any]) -> Optional[str]:
        system_status = str(item.get('system_status') or '').strip()
        if system_status not in {'crm_success', 'verified_success', 'verify_failed', 'crm_failed'}:
            return None
        timo_result_code = str(item.get('timo_result_code') or '').strip()
        if system_status in {'crm_success', 'verified_success'}:
            headline = '✅ Timo 资料已写入 CRM'
        elif system_status == 'crm_failed':
            headline = '⚠️ 已验证在 Timo 公会中，但 CRM 写入失败'
        elif timo_result_code == 'timo_ticket_expired':
            headline = '❌ Timo公会成员查询失败'
        else:
            headline = '❌ Timo 公会未查询到此成员'
        timo_id = str(item.get('timo_id') or '-')
        guild_name = str(item.get('guild_display_name') or timo_guild_display_name(item.get('guild_name')) or '-')
        return '\n'.join([
            headline,
            f"Phone: Timo:{timo_id}",
            f"ID: {timo_id}",
            f"Guild: {guild_name}",
            f"Group: {str(item.get('group_name') or '-')}",
            "Code: -",
        ])

    def _external_app_timo_initiator(self, item: Dict[str, Any]) -> str:
        return str(
            item.get('external_customer_service_id')
            or item.get('external_customer_service_name')
            or item.get('submitted_by_username')
            or item.get('submitted_by_user_id')
            or '-'
        ).strip() or '-'

    def _external_app_timo_item_response(self, item: Dict[str, Any], *, has_submission: Optional[bool] = None, duplicate: bool = False) -> Dict[str, Any]:
        item = self._public_timo_intake_row(item)
        system_status = str(item.get('system_status') or '').strip()
        feedback_status = str(item.get('feedback_status') or '').strip() or ('pending_feedback' if system_status in {'crm_success', 'verified_success'} else 'not_feedbackable')
        crm_sync_status = str(item.get('crm_sync_status') or '').strip()
        reply_template = self._external_app_timo_reply_template(item)
        if system_status == 'crm_failed':
            reason = str(item.get('crm_result_reason_display') or item.get('crm_result_reason') or '').strip()
        else:
            reason = str(item.get('timo_result_reason_display') or item.get('timo_result_reason') or '').strip()
        guild_contract = timo_guild_contract_fields(item.get('guild_name'))
        response = {
            'ok': True,
            'app': 'Timo',
            **guild_contract,
            'submission_id': str(item.get('item_id') or '').strip(),
            'external_user_id': str(item.get('external_user_id') or '').strip(),
            'initiator': self._external_app_timo_initiator(item),
            'system_status': system_status,
            'feedback_status': feedback_status,
            'message': self._external_app_timo_status_message(
                system_status=system_status,
                feedback_status=feedback_status,
                crm_sync_status=crm_sync_status,
                timo_result_code=str(item.get('timo_result_code') or '').strip(),
            ),
            'reply_template': reply_template,
            'reply_template_language': 'zh-CN' if reply_template else None,
            'updated_at': item.get('updated_at') or item.get('created_at') or utc_now(),
        }
        if has_submission is not None:
            response['has_submission'] = bool(has_submission)
        if duplicate:
            response['duplicate'] = True
        if reason:
            response['reason'] = reason
        return response

    def _get_timo_intake_item(self, item_id: str) -> Dict[str, Any]:
        normalized_item_id = str(item_id or '').strip()
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM ops_timo_intake_items WHERE item_id = ?", (normalized_item_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail='timo_intake_item_not_found')
        return self._public_timo_intake_row(dict(row))

    def _submit_external_timo_intake(self, *, payload: ExternalAppIntakeSubmissionRequest, source_config: Dict[str, Any], guild_name: str) -> Dict[str, Any]:
        source = str(payload.source or '').strip()
        external_user_id = str(payload.external_user_id or '').strip()
        if not external_user_id:
            raise HTTPException(status_code=400, detail={'reason': 'missing_external_user_id', 'message': '缺少 Tugao 用户 ID'})
        mobile = str(payload.phone or '').strip()
        timo_id = self._normalize_timo_id(payload.timo_id or payload.linky_account_id)
        if not timo_id:
            raise HTTPException(status_code=400, detail={'reason': 'missing_timo_id', 'message': '请填写 Timo ID'})
        group_name = self._normalize_external_registration_group(payload.group, guild_name=guild_name)
        cs_id = str(payload.customer_service_id or '').strip()
        cs_name = str(payload.customer_service_name or cs_id or 'Tugao客服').strip()
        raw_text = str(payload.raw_text or '').strip()
        if not raw_text:
            raw_text = '\n'.join([
                f'App: Timo',
                f'Phone: {mobile}',
                f'Timo ID: {timo_id}',
                f'Group: {group_name}',
            ])
        user = {
            'user_id': f'external:{source}:{cs_id or cs_name}',
            'username': cs_id or cs_name,
            'display_name': cs_name,
            'role': OPS_AUTH_ROLE_INTERNAL,
            'enabled': True,
        }
        submitted = self.submit_timo_intake_item(
            payload=OpsTimoIntakeSubmitRequest(
                guild_name=guild_name,
                mobile=mobile,
                timo_id=timo_id,
                group_name=group_name,
                app_name='Timo',
                source_text=raw_text,
                source_channel=source,
                profile_name=cs_name,
                auto_verify=True,
            ),
            user=user,
        )
        item = submitted.get('item') or {}
        external_payload = payload.model_dump() if hasattr(payload, 'model_dump') else payload.dict()
        external_payload['app'] = 'Timo'
        with self.db.connect() as conn:
            conn.execute(
                """
                UPDATE ops_timo_intake_items
                SET source=?, external_user_id=?, external_session_id=?, external_message_id=?,
                    external_customer_service_id=?, external_customer_service_name=?, external_payload=?
                WHERE item_id=?
                """,
                (
                    source,
                    external_user_id,
                    str(payload.external_session_id or '').strip(),
                    str(payload.external_message_id or '').strip(),
                    cs_id,
                    cs_name,
                    json.dumps(external_payload, ensure_ascii=False, default=str),
                    item.get('item_id'),
                ),
            )
            reconcile_streamer_app_fans(conn, app_names=('timo',))
            conn.commit()
        return self._external_app_timo_item_response(self._get_timo_intake_item(str(item.get('item_id') or '')))

    def submit_external_app_intake(self, *, payload: ExternalAppIntakeSubmissionRequest, source_config: Dict[str, Any]) -> Dict[str, Any]:
        source = str(payload.source or '').strip()
        if source != str(source_config.get('source') or '').strip():
            raise HTTPException(status_code=403, detail='source_mismatch')
        app_slug = self._normalize_external_product_app(payload.app)
        app_display = self._external_product_app_display(app_slug)
        external_user_id = str(payload.external_user_id or '').strip()
        if not external_user_id:
            raise HTTPException(status_code=400, detail={'reason': 'missing_external_user_id', 'message': '缺少 Tugao 用户 ID'})
        guild = str(payload.guild or '').strip()
        guild_id = str(getattr(payload, 'guild_id', '') or '').strip()
        guild_sid = str(getattr(payload, 'guild_sid', '') or '').strip()
        if not guild and not guild_id and not guild_sid:
            raise HTTPException(status_code=400, detail={
                'reason': 'missing_guild_identity',
                'message': 'Timo 请传 guild_id；兼容模式可传 guild 或 guild_sid',
            })
        if app_slug == 'timo':
            # Preserve the existing product/guild mismatch diagnosis for a known
            # non-Timo guild before applying the stricter Timo identity contract.
            raw_guild_key = self._external_guild_match_key(guild)
            if guild and EXTERNAL_APP_KNOWN_GUILD_APP_MAP.get(raw_guild_key) not in {None, 'timo'}:
                self._validate_external_app_guild_match(
                    app_slug=app_slug,
                    guild_name=guild,
                    source_config=source_config,
                )
            try:
                guild_identity = require_timo_guild_identity(
                    guild,
                    guild_id=guild_id,
                    guild_sid=guild_sid,
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail={
                    'reason': str(exc),
                    'message': 'Timo 公会身份无法确认，或 guild_id 与名称/SID 不一致',
                }) from exc
            guild = guild_identity.storage_name
        elif not guild:
            raise HTTPException(status_code=400, detail={'reason': 'missing_guild', 'message': '请填写明确的公会名'})
        allowed_guilds = {
            timo_guild_storage_name(v) if app_slug == 'timo' else str(v or '').strip()
            for v in list(source_config.get('allowed_guilds') or [])
            if str(v or '').strip()
        }
        if allowed_guilds and guild not in allowed_guilds:
            raise HTTPException(status_code=403, detail='guild_not_allowed')
        self._validate_external_app_guild_match(app_slug=app_slug, guild_name=guild, source_config=source_config)
        country_context = infer_country_context(payload.country) or normalize_country_label((self.resolve_guild_executor(guild) or {}).get('country'))
        if app_slug == 'timo':
            return self._submit_external_timo_intake(payload=payload, source_config=source_config, guild_name=guild)
        linky_account_id = str(payload.linky_account_id or '').strip()
        if not linky_account_id:
            raise HTTPException(status_code=400, detail={'reason': 'missing_account_id', 'message': '请填写 Linky ID'})
        phone_from_payload = str(payload.phone or '').strip()
        id_only_cms_bind = False
        phone_for_submission = phone_from_payload
        if not phone_for_submission:
            if source != 'tugao_app':
                raise HTTPException(status_code=400, detail={'reason': 'missing_phone', 'message': '请填写用户手机号'})
            if not self.guild_executor_has_platform_cms_route(guild):
                raise HTTPException(status_code=400, detail={
                    'reason': 'cms_id_route_required',
                    'message': '该公会未配置 CMS ID 绑定通道，不能提交无手机号绑定',
                })
            id_only_cms_bind = True
            phone_for_submission = make_external_app_id_only_phone(linky_account_id)
        group_name = self._normalize_external_registration_group(payload.group, guild_name=guild)
        text_lines = [
            f"App: {app_display}",
            f"Phone: {phone_for_submission}",
            f"ID: {linky_account_id}",
        ]
        if group_name:
            text_lines.append(f"Group: {group_name}")
        if country_context:
            text_lines.append(f"Country: {country_context}")
        if str(payload.code or '').strip():
            text_lines.append(f"Code: {str(payload.code or '').strip()}")
        raw_text = str(payload.raw_text or '').strip()
        submit_text = raw_text if raw_text else '\n'.join(text_lines)
        fields = {
            'app': app_display,
            'phone': phone_for_submission,
            'account_id': linky_account_id,
            'group': group_name,
            'code': str(payload.code or '').strip(),
            'country': country_context,
        }
        cs_id = str(payload.customer_service_id or '').strip()
        cs_name = str(payload.customer_service_name or cs_id or 'Tugao客服').strip()
        user = {
            'user_id': f'external:{source}:{cs_id or cs_name}',
            'username': cs_id or cs_name,
            'display_name': cs_name,
            'role': OPS_AUTH_ROLE_INTERNAL,
            'enabled': True,
        }
        try:
            submitted = self.submit_ops_intake_guild_item(guild_name=guild, text=submit_text, fields=fields, user=user)
        except HTTPException as exc:
            detail = exc.detail
            if exc.status_code == 409 and isinstance(detail, dict) and detail.get('reason') == 'duplicate_pending':
                raise HTTPException(status_code=409, detail={
                    'ok': False,
                    'reason': 'duplicate_pending',
                    'existing_submission_id': detail.get('existing_item_id'),
                    'system_status': detail.get('existing_status') or 'bind_queued',
                    'feedback_status': detail.get('existing_feedback_status') or 'not_feedbackable',
                    'message': '该用户已有绑定记录处理中，请勿重复提交',
                }) from exc
            raise
        item = submitted.get('item') or {}
        external_payload = payload.model_dump() if hasattr(payload, 'model_dump') else payload.dict()
        external_payload['app'] = app_display
        external_payload['country'] = country_context
        external_payload['identity_mode'] = 'id_only_cms_bind' if id_only_cms_bind else 'phone'
        external_payload['phone_backfill_status'] = 'missing' if id_only_cms_bind else 'not_needed'
        if id_only_cms_bind:
            external_payload['local_phone_placeholder'] = phone_for_submission
            external_payload['crm_mobile_placeholder'] = phone_for_submission
        with self.db.connect() as conn:
            conn.execute(
                """
                UPDATE ops_intake_items
                SET source=?, external_user_id=?, external_session_id=?, external_message_id=?,
                    external_customer_service_id=?, external_customer_service_name=?, external_payload=?
                WHERE item_id=?
                """,
                (
                    source,
                    external_user_id,
                    str(payload.external_session_id or '').strip(),
                    str(payload.external_message_id or '').strip(),
                    cs_id,
                    cs_name,
                    json.dumps(external_payload, ensure_ascii=False, default=str),
                    item.get('item_id'),
                ),
            )
            reconcile_streamer_app_fans(conn, app_names=('linky',))
            conn.commit()
        item = self._get_ops_intake_item(str(item.get('item_id') or ''))
        response = self._external_app_item_response(item, duplicate=bool(submitted.get('duplicate')))
        if submitted.get('duplicate'):
            response['message'] = '该用户已提交，系统处理中，请勿重复提交'
        return response

    def record_external_app_phone_backfill_request(self, *, payload: ExternalAppPhoneBackfillRequest, source_config: Dict[str, Any]) -> str:
        payload_dict = payload.model_dump() if hasattr(payload, 'model_dump') else payload.dict()
        source = str(payload.source or source_config.get('source') or '').strip()
        request_id = create_id('phone_backfill')
        now = utc_now()
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO external_app_phone_backfill_requests (
                    request_id, source, app, guild_name, linky_account_id, phone,
                    external_user_id, external_session_id, external_message_id,
                    external_customer_service_id, external_customer_service_name,
                    request_payload, status, result_code, result_reason, submission_id,
                    result_snapshot, created_at, updated_at, processed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request_id,
                    source,
                    str(payload.app or '').strip(),
                    str(payload.guild or '').strip(),
                    str(payload.linky_account_id or '').strip(),
                    str(payload.phone or '').strip(),
                    str(payload.external_user_id or '').strip(),
                    str(payload.external_session_id or '').strip(),
                    str(payload.external_message_id or '').strip(),
                    str(payload.customer_service_id or '').strip(),
                    str(payload.customer_service_name or '').strip(),
                    json.dumps(payload_dict, ensure_ascii=False, default=str),
                    'received',
                    '',
                    '',
                    '',
                    '{}',
                    now,
                    now,
                    None,
                ),
            )
            conn.commit()
        return request_id

    def update_external_app_phone_backfill_request(
        self,
        *,
        request_id: str,
        status: str,
        result_code: str = '',
        result_reason: str = '',
        submission_id: str = '',
        result_snapshot: Optional[Dict[str, Any]] = None,
    ) -> None:
        normalized_request_id = str(request_id or '').strip()
        if not normalized_request_id:
            return
        now = utc_now()
        try:
            with self.db.connect() as conn:
                conn.execute(
                    """
                    UPDATE external_app_phone_backfill_requests
                    SET status=?, result_code=?, result_reason=?, submission_id=?,
                        result_snapshot=?, updated_at=?, processed_at=?
                    WHERE request_id=?
                    """,
                    (
                        str(status or '').strip() or 'unknown',
                        str(result_code or '').strip(),
                        str(result_reason or '').strip(),
                        str(submission_id or '').strip(),
                        json.dumps(result_snapshot or {}, ensure_ascii=False, default=str),
                        now,
                        now,
                        normalized_request_id,
                    ),
                )
                conn.commit()
        except Exception:
            logger.exception('failed to update external app phone backfill request audit: request_id=%s', normalized_request_id)

    def backfill_external_app_phone(self, *, payload: ExternalAppPhoneBackfillRequest, source_config: Dict[str, Any]) -> Dict[str, Any]:
        source = str(payload.source or '').strip()
        if source != str(source_config.get('source') or '').strip():
            raise HTTPException(status_code=403, detail='source_mismatch')
        app_slug = self._normalize_external_product_app(payload.app or 'linky')
        if app_slug != 'linky':
            raise HTTPException(status_code=400, detail={'reason': 'unsupported_backfill_app', 'message': '手机号回补当前只支持 Linky'})
        guild = str(payload.guild or '').strip()
        if not guild:
            raise HTTPException(status_code=400, detail={'reason': 'missing_guild', 'message': '请填写明确的公会名'})
        allowed_guilds = {str(v or '').strip() for v in list(source_config.get('allowed_guilds') or []) if str(v or '').strip()}
        if allowed_guilds and guild not in allowed_guilds:
            raise HTTPException(status_code=403, detail='guild_not_allowed')
        self._validate_external_app_guild_match(app_slug=app_slug, guild_name=guild, source_config=source_config)
        account_id = str(payload.linky_account_id or '').strip()
        if not account_id:
            raise HTTPException(status_code=400, detail={'reason': 'missing_account_id', 'message': '请填写 Linky ID'})
        executor_country = normalize_country_label((self.resolve_guild_executor(guild) or {}).get('country'))
        display_phone = format_display_phone(payload.phone, country=executor_country)
        fast_validation_error = validate_fast_intake_fields(mobile=display_phone, app_name='Linky', account_id=account_id, country=executor_country)
        if fast_validation_error:
            raise HTTPException(status_code=400, detail={
                'reason': fast_validation_error['reason'],
                'message': fast_validation_error['reply_text'],
            })
        mobile_body, area_code, country = normalize_phone_identity(mobile=display_phone, area_code=0, country=executor_country)
        if not mobile_body or not area_code:
            raise HTTPException(status_code=400, detail={'reason': 'invalid_phone_format', 'message': '手机号必须能解析出国家码和号码主体'})
        display_phone = format_display_phone(display_phone, area_code=area_code, country=country)
        external_user_id = str(payload.external_user_id or '').strip()
        now = utc_now()
        with self.db.connect() as conn:
            rows = [dict(row) for row in conn.execute(
                """
                SELECT * FROM ops_intake_items
                WHERE source=? AND guild_name=? AND parsed_account_id=?
                  AND LOWER(COALESCE(parsed_app, 'linky')) = 'linky'
                ORDER BY created_at DESC, item_id DESC
                LIMIT 20
                """,
                (source, guild, account_id),
            ).fetchall()]
            if external_user_id:
                rows = [row for row in rows if str(row.get('external_user_id') or '').strip() == external_user_id]
            if not rows:
                raise HTTPException(status_code=404, detail={'reason': 'id_only_submission_not_found', 'message': '没有找到可回补的 Tugao ID-only 绑定记录'})
            item = next((row for row in rows if is_external_app_id_only_phone(row.get('parsed_phone'))), rows[0])
            previous_phone = str(item.get('parsed_phone') or '').strip()
            if previous_phone and not is_external_app_id_only_phone(previous_phone) and previous_phone != display_phone:
                raise HTTPException(status_code=409, detail={
                    'reason': 'phone_already_backfilled_with_different_value',
                    'message': '该记录已回补过不同手机号，请人工确认',
                    'existing_phone': previous_phone,
                })
            try:
                snapshot = json.loads(item.get('result_snapshot') or '{}')
            except Exception:
                snapshot = {}
            snapshot = snapshot if isinstance(snapshot, dict) else {}
            lead_id = str(snapshot.get('lead_id') or '').strip()
            if not lead_id:
                lead_row = conn.execute(
                    """
                    SELECT lead_id FROM leads
                    WHERE COALESCE(yw_id, '')=? AND mobile=?
                    ORDER BY updated_at DESC
                    LIMIT 1
                    """,
                    (account_id, previous_phone),
                ).fetchone()
                lead_id = str(lead_row['lead_id'] or '').strip() if lead_row else ''
            if not lead_id:
                raise HTTPException(status_code=404, detail={'reason': 'lead_not_found_for_backfill', 'message': '没有找到该绑定记录对应的本地 lead'})
            conflict = conn.execute(
                "SELECT lead_id FROM leads WHERE area_code=? AND mobile=? AND lead_id<>? LIMIT 1",
                (area_code, mobile_body, lead_id),
            ).fetchone()
            if conflict:
                raise HTTPException(status_code=409, detail={'reason': 'local_phone_conflict', 'message': '该手机号已被其他本地记录占用'})
            lead_row = conn.execute('SELECT * FROM leads WHERE lead_id=?', (lead_id,)).fetchone()
            if not lead_row:
                raise HTTPException(status_code=404, detail={'reason': 'lead_not_found_for_backfill', 'message': '没有找到该绑定记录对应的本地 lead'})
            lead_dict = dict(lead_row)
            resolved_app = self._resolve_crm_app_mapping(lead_dict.get('app_name') or 'Linky')
            resolved_dept = self._resolve_crm_dept_mapping(lead_dict.get('dept_name') or guild, None)
            group_name = str(lead_dict.get('pendaftaran_group') or item.get('parsed_group') or OTHER_CHANNEL_REGISTRATION_GROUP).strip()
            crm_status = 'skipped_not_configured'
            crm_response = None
            verified_after_update = None
            if self.crm_adapter is not None:
                existing_by_phone = None
                try:
                    existing_by_phone = self.crm_adapter.find_customer(yw_id=None, mobile=mobile_body)
                except Exception:
                    existing_by_phone = None
                if existing_by_phone and str(existing_by_phone.get('ywId') or '').strip() not in {'', account_id}:
                    raise HTTPException(status_code=409, detail={'reason': 'crm_phone_conflict', 'message': 'CRM 中该手机号已属于其他 Linky ID'})
                crm_placeholder_mobile = previous_phone if is_external_app_id_only_phone(previous_phone) else make_external_app_id_only_phone(account_id)
                crm_lookup_mobile = crm_placeholder_mobile if is_external_app_id_only_phone(previous_phone) else mobile_body
                existing_customer = self._find_existing_customer_with_fallback(
                    yw_id=account_id,
                    mobile=crm_lookup_mobile,
                    app_name=resolved_app['appName'],
                    dept_name=resolved_dept['deptName'],
                    registration_group=group_name,
                    allow_empty_mobile_match=is_external_app_id_only_phone(previous_phone),
                )
                if existing_customer and hasattr(self.crm_adapter, 'update_customer'):
                    crm_payload = dict(existing_customer)
                    crm_payload.update({
                        'mobile': mobile_body,
                        'phoneRaw': display_phone,
                        'phoneE164': f'+{area_code}{mobile_body}',
                        'areaCode': str(area_code),
                        'ywId': account_id,
                        'appName': resolved_app['appName'],
                        'appId': existing_customer.get('appId') or resolved_app['appId'],
                        'deptName': resolved_dept['deptName'],
                        'deptId': existing_customer.get('deptId') or resolved_dept['deptId'],
                        'pendaftaranGroup': group_name,
                    })
                    crm_response = self.crm_adapter.update_customer(crm_payload)
                    if not isinstance(crm_response, dict) or crm_response.get('code') != 0:
                        raise HTTPException(status_code=502, detail={'reason': 'crm_phone_backfill_failed', 'message': 'CRM 手机号回补失败', 'crm_response': crm_response})
                    verified_after_update = self._find_existing_customer_with_fallback(
                        yw_id=account_id,
                        mobile=mobile_body,
                        app_name=resolved_app['appName'],
                        dept_name=resolved_dept['deptName'],
                        registration_group=group_name,
                    )
                    crm_status = 'updated_verified' if verified_after_update else 'updated_unverified'
                elif existing_customer:
                    crm_status = 'update_not_supported'
                else:
                    crm_payload = {
                        'mobile': mobile_body,
                        'phoneRaw': display_phone,
                        'phoneE164': f'+{area_code}{mobile_body}',
                        'ywId': account_id,
                        'name': '',
                        'remark': 'Phone backfill after ID-only CMS bind',
                        'dept': '',
                        'wa': '',
                        'areaCode': str(area_code),
                        'inviterId': lead_dict.get('inviter_id'),
                        'appName': resolved_app['appName'],
                        'appId': resolved_app['appId'],
                        'pendaftaranGroup': group_name,
                        'paymentStatus': '',
                        'pzStatus': 0,
                        'userQuality': '',
                        'fileUrl': '',
                        'deptName': resolved_dept['deptName'],
                        'deptId': resolved_dept['deptId'],
                        'submissionId': str(item.get('item_id') or ''),
                        'sourceChannel': str(item.get('source') or source or ''),
                        'creatorName': str(item.get('external_customer_service_name') or item.get('external_customer_service_id') or source or '').strip(),
                        'bindStatus': 'bind_success',
                        'officialGroupStatus': 'pending',
                    }
                    crm_response = self.crm_adapter.create_customer(crm_payload)
                    if not isinstance(crm_response, dict) or crm_response.get('code') != 0:
                        verified_after_update = self._find_existing_customer_with_fallback(
                            yw_id=account_id,
                            mobile=mobile_body,
                            app_name=resolved_app['appName'],
                            dept_name=resolved_dept['deptName'],
                            registration_group=group_name,
                        )
                        if not verified_after_update:
                            raise HTTPException(status_code=502, detail={'reason': 'crm_phone_backfill_create_failed', 'message': 'CRM 手机号回补创建失败', 'crm_response': crm_response})
                        crm_status = 'already_verified'
                    else:
                        verified_after_update = self._find_existing_customer_with_fallback(
                            yw_id=account_id,
                            mobile=mobile_body,
                            app_name=resolved_app['appName'],
                            dept_name=resolved_dept['deptName'],
                            registration_group=group_name,
                        )
                        crm_status = 'created_verified' if verified_after_update else 'created_unverified'
            try:
                external_payload = json.loads(item.get('external_payload') or '{}')
            except Exception:
                external_payload = {}
            external_payload = external_payload if isinstance(external_payload, dict) else {}
            external_payload.update({
                'phone': display_phone,
                'identity_mode': 'phone_backfilled',
                'phone_backfill_status': 'backfilled',
                'phone_backfilled_at': now,
                'phone_backfilled_by': str(payload.customer_service_id or payload.customer_service_name or source or '').strip(),
                'previous_local_phone_placeholder': previous_phone,
            })
            snapshot.update({
                'phone_backfill_status': 'backfilled',
                'phone_backfilled_at': now,
                'phone_backfill_previous_phone': previous_phone,
                'phone_backfill_display_phone': display_phone,
                'phone_backfill_crm_status': crm_status,
                'phone_backfill_crm_response': crm_response,
            })
            conn.execute(
                "UPDATE leads SET mobile=?, area_code=?, country=?, updated_at=? WHERE lead_id=?",
                (mobile_body, area_code, country or executor_country or str(lead_dict.get('country') or ''), now, lead_id),
            )
            conn.execute(
                "UPDATE customer_projection SET mobile=?, area_code=?, updated_at=? WHERE lead_id=?",
                (mobile_body, area_code, now, lead_id),
            )
            if verified_after_update:
                verified_payload = dict(verified_after_update)
                verified_payload.update({
                    'mobile': mobile_body,
                    'phoneRaw': display_phone,
                    'phoneE164': f'+{area_code}{mobile_body}',
                    'areaCode': str(area_code),
                    'appName': resolved_app['appName'],
                    'deptName': resolved_dept['deptName'],
                    'pendaftaranGroup': group_name,
                })
                self._record_verified_crm_state(conn, lead_id=lead_id, crm_payload=verified_payload)
            conn.execute(
                """
                UPDATE ops_intake_items
                SET parsed_phone=?, external_payload=?, result_snapshot=?, processed_at=?
                WHERE item_id=?
                """,
                (
                    display_phone,
                    json.dumps(external_payload, ensure_ascii=False, default=str),
                    json.dumps(snapshot, ensure_ascii=False, default=str),
                    now,
                    item['item_id'],
                ),
            )
            conn.commit()
        refreshed = self._get_ops_intake_item(str(item.get('item_id') or ''))
        response = self._external_app_item_response(refreshed)
        response.update({
            'phone': display_phone,
            'phone_backfill_status': 'backfilled',
            'crm_backfill_status': crm_status,
            'crm_verified': bool(verified_after_update),
            'message': '手机号已回补',
        })
        return response

    def get_external_app_intake_submission(self, *, source: str, submission_id: str, app_name: Optional[str] = None) -> Dict[str, Any]:
        app_slug = self._normalize_external_product_app(app_name)
        if app_slug == 'timo':
            item = self._get_timo_intake_item(submission_id)
            if str(item.get('source') or '') != str(source or '').strip():
                raise HTTPException(status_code=404, detail='submission_not_found')
            return self._external_app_timo_item_response(item)
        item = self._get_ops_intake_item(submission_id)
        if str(item.get('source') or '') != str(source or '').strip():
            raise HTTPException(status_code=404, detail='submission_not_found')
        return self._external_app_item_response(item)

    def get_external_app_latest_submission(self, *, source: str, external_user_id: str, app_name: Optional[str] = None) -> Dict[str, Any]:
        normalized_source = str(source or '').strip()
        normalized_user = str(external_user_id or '').strip()
        app_slug = self._normalize_external_product_app(app_name)
        if app_slug == 'timo':
            with self.db.connect() as conn:
                row = conn.execute(
                    """
                    SELECT item_id FROM ops_timo_intake_items
                    WHERE source=? AND external_user_id=?
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (normalized_source, normalized_user),
                ).fetchone()
            if not row:
                return {'ok': True, 'app': 'Timo', 'has_submission': False}
            item = self._get_timo_intake_item(row['item_id'])
            return self._external_app_timo_item_response(item, has_submission=True)
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT item_id FROM ops_intake_items
                WHERE source=? AND external_user_id=?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (normalized_source, normalized_user),
            ).fetchone()
        if not row:
            return {'ok': True, 'has_submission': False}
        item = self._get_ops_intake_item(row['item_id'])
        return self._external_app_item_response(item, has_submission=True)

    def mark_external_app_template_copied(self, *, source: str, item_id: str, customer_service_id: str, customer_service_name: Optional[str], app_name: Optional[str] = None) -> Dict[str, Any]:
        app_slug = self._normalize_external_product_app(app_name)
        if app_slug == 'timo':
            item = self._get_timo_intake_item(item_id)
            if str(item.get('source') or '') != str(source or '').strip():
                raise HTTPException(status_code=404, detail='submission_not_found')
            if str(item.get('system_status') or '') not in {'crm_success', 'verified_success'} or str(item.get('feedback_status') or '') != 'pending_feedback':
                raise HTTPException(status_code=400, detail={'reason': 'not_feedbackable', 'message': '当前状态不可复制成功模板'})
            copied_by = str(customer_service_id or customer_service_name or '').strip()
            now = utc_now()
            with self.db.connect() as conn:
                conn.execute(
                    "UPDATE ops_timo_intake_items SET template_copied_at=?, template_copied_by=?, updated_at=? WHERE item_id=?",
                    (now, copied_by, now, item_id),
                )
                conn.commit()
            item = self._get_timo_intake_item(item_id)
            return {'ok': True, 'app': 'Timo', 'submission_id': item_id, 'feedback_status': item.get('feedback_status'), 'template_copied_at': item.get('template_copied_at'), 'message': '已记录复制模板'}
        item = self._get_ops_intake_item(item_id)
        if str(item.get('source') or '') != str(source or '').strip():
            raise HTTPException(status_code=404, detail='submission_not_found')
        if str(item.get('system_status') or '') == 'partial_success_crm_failed':
            copied_by = str(customer_service_id or customer_service_name or '').strip()
            now = utc_now()
            with self.db.connect() as conn:
                conn.execute(
                    "UPDATE ops_intake_items SET template_copied_at=?, template_copied_by=? WHERE item_id=?",
                    (now, copied_by, item_id),
                )
                conn.commit()
            item = self._get_ops_intake_item(item_id)
            return {
                'ok': True,
                'submission_id': item_id,
                'feedback_status': 'pending_feedback',
                'internal_feedback_status': item.get('feedback_status'),
                'template_copied_at': item.get('template_copied_at'),
                'crm_sync_status': 'pending_internal_compensation',
                'message': '已记录复制模板',
            }
        if str(item.get('system_status') or '') != 'fully_success' or str(item.get('feedback_status') or '') != 'pending_feedback':
            raise HTTPException(status_code=400, detail={'reason': 'not_feedbackable', 'message': '当前状态不可复制成功模板'})
        user = {
            'user_id': str(customer_service_id or '').strip(),
            'username': str(customer_service_id or '').strip(),
            'display_name': str(customer_service_name or customer_service_id or '').strip(),
            'role': OPS_AUTH_ROLE_INTERNAL,
        }
        self.mark_ops_intake_template_copied(item_id=item_id, user=user)
        item = self._get_ops_intake_item(item_id)
        return {'ok': True, 'submission_id': item_id, 'feedback_status': item.get('feedback_status'), 'template_copied_at': item.get('template_copied_at'), 'message': '已记录复制模板'}

    def mark_external_app_feedback_done(self, *, source: str, item_id: str, customer_service_id: str, customer_service_name: Optional[str], app_name: Optional[str] = None) -> Dict[str, Any]:
        app_slug = self._normalize_external_product_app(app_name)
        if app_slug == 'timo':
            item = self._get_timo_intake_item(item_id)
            if str(item.get('source') or '') != str(source or '').strip():
                raise HTTPException(status_code=404, detail='submission_not_found')
            if str(item.get('system_status') or '') not in {'crm_success', 'verified_success'}:
                raise HTTPException(status_code=400, detail={'reason': 'not_feedbackable', 'message': '当前状态不可标记已反馈用户'})
            done_by = str(customer_service_id or customer_service_name or '').strip()
            now = utc_now()
            with self.db.connect() as conn:
                conn.execute(
                    "UPDATE ops_timo_intake_items SET feedback_status='feedback_done', feedback_done_at=?, feedback_done_by=?, updated_at=? WHERE item_id=?",
                    (now, done_by, now, item_id),
                )
                conn.commit()
            item = self._get_timo_intake_item(item_id)
            return {'ok': True, 'app': 'Timo', 'submission_id': item_id, 'feedback_status': item.get('feedback_status'), 'feedback_done_at': item.get('feedback_done_at'), 'message': '已标记客服完成反馈'}
        item = self._get_ops_intake_item(item_id)
        if str(item.get('source') or '') != str(source or '').strip():
            raise HTTPException(status_code=404, detail='submission_not_found')
        if str(item.get('system_status') or '') == 'partial_success_crm_failed':
            done_by = str(customer_service_id or customer_service_name or '').strip()
            now = utc_now()
            with self.db.connect() as conn:
                conn.execute(
                    "UPDATE ops_intake_items SET feedback_done_at=COALESCE(feedback_done_at, ?), feedback_done_by=COALESCE(feedback_done_by, ?) WHERE item_id=?",
                    (now, done_by, item_id),
                )
                conn.commit()
            item = self._get_ops_intake_item(item_id)
            return {
                'ok': True,
                'submission_id': item_id,
                'feedback_status': 'feedback_done',
                'internal_feedback_status': item.get('feedback_status'),
                'feedback_done_at': item.get('feedback_done_at'),
                'crm_sync_status': 'pending_internal_compensation',
                'message': '已标记客服完成反馈；CRM 同步由系统内部补偿',
            }
        user = {
            'user_id': str(customer_service_id or '').strip(),
            'username': str(customer_service_id or '').strip(),
            'display_name': str(customer_service_name or customer_service_id or '').strip(),
            'role': OPS_AUTH_ROLE_INTERNAL,
        }
        try:
            self.mark_ops_intake_feedback_done(item_id=item_id, user=user)
        except HTTPException as exc:
            if isinstance(exc.detail, str):
                raise HTTPException(status_code=exc.status_code, detail={'reason': exc.detail, 'message': '请先复制成功模板，再标记已反馈用户'}) from exc
            raise
        item = self._get_ops_intake_item(item_id)
        return {'ok': True, 'submission_id': item_id, 'feedback_status': item.get('feedback_status'), 'feedback_done_at': item.get('feedback_done_at'), 'message': '已标记客服完成反馈'}

    def _get_ops_intake_item(self, item_id: str) -> Dict[str, Any]:
        normalized_item_id = str(item_id or '').strip()
        with self.db.connect() as conn:
            row = conn.execute('SELECT * FROM ops_intake_items WHERE item_id = ?', (normalized_item_id,)).fetchone()
            if row:
                item = self._enhance_ops_intake_item_display(dict(row))
                item.setdefault('source_type', 'ops_intake_item')
                return item
            lead_row = conn.execute(
                """
                SELECT l.*, t.task_id, t.payload AS task_payload, t.result_code, t.result_reason, t.raw_result,
                       t.created_at AS task_created_at, t.finished_at
                FROM leads l
                LEFT JOIN automation_tasks t ON t.task_id = (
                    SELECT t2.task_id FROM automation_tasks t2
                    WHERE t2.lead_id = l.lead_id AND t2.task_type = 'bind_check'
                    ORDER BY COALESCE(t2.finished_at, t2.created_at) DESC LIMIT 1
                )
                WHERE l.lead_id = ? AND l.current_status = 'bind_failed'
                """,
                (normalized_item_id,),
            ).fetchone()
        if lead_row:
            return self._ops_intake_bind_failed_lead_item_from_row(dict(lead_row))
        raise HTTPException(status_code=404, detail='intake_item_not_found')

    def list_ops_intake_items(self, *, guild_name: Optional[str], user: Optional[Dict[str, Any]], limit: int = 100, include_done: bool = False) -> Dict[str, Any]:
        visible_guilds = set(self._ops_intake_visible_guild_names(user=user))
        requested_guild = str(guild_name or '').strip()
        params: List[Any] = []
        conditions: List[str] = []
        if requested_guild:
            role = str((user or {}).get('role') or '').strip().lower()
            if requested_guild not in visible_guilds and role not in {OPS_AUTH_ROLE_SUPER_ADMIN, OPS_AUTH_ROLE_ADMIN, OPS_AUTH_ROLE_INTERNAL}:
                return {'rows': [], 'total_count': 0, 'loaded_count': 0, 'summary': {'pending_feedback': 0, 'processing': 0, 'feedback_done_today': 0}}
            conditions.append('guild_name = ?')
            params.append(requested_guild)
        elif visible_guilds:
            placeholders = ','.join('?' for _ in visible_guilds)
            conditions.append(f'guild_name IN ({placeholders})')
            params.extend(sorted(visible_guilds))
        else:
            return {'rows': [], 'total_count': 0, 'loaded_count': 0, 'summary': {'pending_feedback': 0, 'processing': 0, 'feedback_done_today': 0}}
        summary_conditions = list(conditions)
        summary_params = list(params)
        if not include_done:
            conditions.append("COALESCE(feedback_status, '') NOT IN ('feedback_done', 'cleared')")
        where = ' WHERE ' + ' AND '.join(conditions)
        summary_where = ' WHERE ' + ' AND '.join(summary_conditions)
        normalized_limit = max(1, min(int(limit or 100), 1000))
        today_prefix = utc_now()[:10]
        with self.db.connect() as conn:
            rows = [dict(r) for r in conn.execute(
                f'SELECT * FROM ops_intake_items{where} ORDER BY created_at DESC LIMIT ?',
                (*params, normalized_limit),
            ).fetchall()]
            total_count = int(conn.execute(
                f'SELECT COUNT(*) FROM ops_intake_items{where}',
                tuple(params),
            ).fetchone()[0] or 0)
            summary = conn.execute(
                f"""
                SELECT
                    COALESCE(SUM(CASE WHEN feedback_status = 'pending_feedback' THEN 1 ELSE 0 END), 0) AS pending_feedback,
                    COALESCE(SUM(CASE
                        WHEN COALESCE(feedback_status, '') NOT IN ('feedback_done', 'cleared')
                         AND system_status IN ('queued', 'processing', 'bind_queued', 'binding', 'crm_verifying')
                        THEN 1 ELSE 0 END), 0) AS processing,
                    COALESCE(SUM(CASE
                        WHEN feedback_status = 'feedback_done'
                         AND SUBSTR(COALESCE(feedback_done_at, processed_at, created_at, ''), 1, 10) = ?
                        THEN 1 ELSE 0 END), 0) AS feedback_done_today
                FROM ops_intake_items{summary_where}
                """,
                (today_prefix, *summary_params),
            ).fetchone()
            truth_map = self._load_binding_current_truth_snapshot_map(
                conn,
                [str(row.get('item_id') or '').strip() for row in rows],
            )
        rows = [
            self._enhance_ops_intake_item_display(
                row,
                current_truth=truth_map.get(str(row.get('item_id') or '').strip()),
                load_current_truth=False,
            )
            for row in rows
        ]
        pending = int(summary['pending_feedback'] or 0) if summary else 0
        processing = int(summary['processing'] or 0) if summary else 0
        done = int(summary['feedback_done_today'] or 0) if summary else 0
        return {
            'rows': rows,
            'total_count': total_count,
            'loaded_count': len(rows),
            'summary': {'pending_feedback': pending, 'processing': processing, 'feedback_done_today': done},
        }

    def clear_ops_intake_stale_feedback_items(self, *, guild_name: str, user: Optional[Dict[str, Any]], threshold_minutes: int = 120) -> Dict[str, Any]:
        normalized_guild = str(guild_name or '').strip()
        if not normalized_guild:
            raise HTTPException(status_code=400, detail='guild_name_required')
        if not self._ops_intake_user_can_access_guild(user, normalized_guild):
            raise HTTPException(status_code=403, detail='ops_guild_intake_forbidden')
        threshold = max(1, int(threshold_minutes or 120))
        cutoff_dt = datetime.now(timezone.utc) - timedelta(minutes=threshold)
        cleared_by = str((user or {}).get('display_name') or (user or {}).get('username') or (user or {}).get('user_id') or 'ops_user').strip()
        now = utc_now()
        cleared_ids: List[str] = []
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT item_id, system_status, feedback_status, processed_at, created_at
                FROM ops_intake_items
                WHERE guild_name = ?
                  AND COALESCE(feedback_status, '') IN ('pending_feedback', 'not_feedbackable')
                """,
                (normalized_guild,),
            ).fetchall()
            processing_statuses = {'queued', 'processing', 'bind_queued', 'binding', 'crm_verifying'}
            for row in rows:
                item = dict(row)
                feedback_status = str(item.get('feedback_status') or '').strip()
                system_status = str(item.get('system_status') or '').strip()
                if feedback_status == 'not_feedbackable' and system_status in processing_statuses:
                    continue
                age_source = str(item.get('processed_at') or item.get('created_at') or '').strip()
                if not age_source:
                    continue
                try:
                    age_dt = parse_iso_datetime(age_source)
                except Exception:
                    continue
                if age_dt <= cutoff_dt:
                    item_id = str(item.get('item_id') or '').strip()
                    if item_id:
                        cleared_ids.append(item_id)
            if cleared_ids:
                placeholders = ','.join('?' for _ in cleared_ids)
                conn.execute(
                    f"UPDATE ops_intake_items SET feedback_status='cleared', feedback_done_at=COALESCE(feedback_done_at, ?), feedback_done_by=COALESCE(feedback_done_by, ?) WHERE item_id IN ({placeholders})",
                    (now, cleared_by, *cleared_ids),
                )
                conn.commit()
        return {
            'ok': True,
            'guild_name': normalized_guild,
            'threshold_minutes': threshold,
            'cutoff_at': cutoff_dt.isoformat(),
            'cleared_count': len(cleared_ids),
            'cleared_item_ids': cleared_ids,
        }

    def clear_ops_intake_previous_day_stale_feedback_items(
        self,
        *,
        now: Optional[datetime] = None,
        threshold_minutes: Optional[int] = None,
        cleared_by: str = '系统自动清理',
    ) -> Dict[str, Any]:
        threshold = max(1, int(threshold_minutes or self.ops_intake_auto_clear_stale_feedback_threshold_minutes or 120))
        now_dt = now or datetime.now(timezone.utc)
        if now_dt.tzinfo is None:
            now_dt = now_dt.replace(tzinfo=timezone.utc)
        now_utc_dt = now_dt.astimezone(timezone.utc)
        beijing_tz = ZoneInfo('Asia/Shanghai')
        now_bj = now_utc_dt.astimezone(beijing_tz)
        today_start_bj = datetime.combine(now_bj.date(), datetime.min.time(), tzinfo=beijing_tz)
        cutoff_dt = now_utc_dt - timedelta(minutes=threshold)
        now_iso = now_utc_dt.isoformat()
        cleared_item_ids: List[str] = []
        timo_cleared_item_ids: List[str] = []
        cleared_by_text = str(cleared_by or '').strip() or '系统自动清理'
        processing_statuses = {'queued', 'processing', 'bind_queued', 'binding', 'crm_verifying'}
        with self.db.connect() as conn:
            rows = [dict(row) for row in conn.execute(
                """
                SELECT item_id, guild_name, system_status, feedback_status, processed_at, created_at
                FROM ops_intake_items
                WHERE COALESCE(feedback_status, '') IN ('pending_feedback', 'not_feedbackable')
                """
            ).fetchall()]
            for item in rows:
                feedback_status = str(item.get('feedback_status') or '').strip()
                system_status = str(item.get('system_status') or '').strip()
                if feedback_status == 'not_feedbackable' and system_status in processing_statuses:
                    continue
                age_source = str(item.get('processed_at') or item.get('created_at') or '').strip()
                if not age_source:
                    continue
                try:
                    age_dt = parse_iso_datetime(age_source)
                except Exception:
                    continue
                if age_dt > cutoff_dt:
                    continue
                if age_dt.astimezone(beijing_tz) >= today_start_bj:
                    continue
                item_id = str(item.get('item_id') or '').strip()
                if item_id:
                    cleared_item_ids.append(item_id)
            if cleared_item_ids:
                placeholders = ','.join('?' for _ in cleared_item_ids)
                conn.execute(
                    f"UPDATE ops_intake_items SET feedback_status='cleared', feedback_done_at=COALESCE(feedback_done_at, ?), feedback_done_by=COALESCE(feedback_done_by, ?) WHERE item_id IN ({placeholders})",
                    (now_iso, cleared_by_text, *cleared_item_ids),
                )
            timo_rows = [dict(row) for row in conn.execute(
                """
                SELECT item_id, system_status, feedback_status, timo_verified_at, updated_at, created_at
                FROM ops_timo_intake_items
                WHERE COALESCE(feedback_status, '') IN ('pending_feedback', 'not_feedbackable')
                """
            ).fetchall()]
            for item in timo_rows:
                if str(item.get('system_status') or '').strip() == 'pending_verification':
                    continue
                age_source = str(item.get('timo_verified_at') or item.get('updated_at') or item.get('created_at') or '').strip()
                if not age_source:
                    continue
                try:
                    age_dt = parse_iso_datetime(age_source)
                except Exception:
                    continue
                if age_dt > cutoff_dt:
                    continue
                if age_dt.astimezone(beijing_tz) >= today_start_bj:
                    continue
                item_id = str(item.get('item_id') or '').strip()
                if item_id:
                    timo_cleared_item_ids.append(item_id)
            if timo_cleared_item_ids:
                placeholders = ','.join('?' for _ in timo_cleared_item_ids)
                conn.execute(
                    f"UPDATE ops_timo_intake_items SET feedback_status='cleared', feedback_done_at=COALESCE(feedback_done_at, ?), feedback_done_by=COALESCE(feedback_done_by, ?), updated_at=? WHERE item_id IN ({placeholders})",
                    (now_iso, cleared_by_text, now_iso, *timo_cleared_item_ids),
                )
            if cleared_item_ids or timo_cleared_item_ids:
                conn.commit()
        all_cleared_item_ids = cleared_item_ids + timo_cleared_item_ids
        return {
            'ok': True,
            'scope': 'ops_intake_previous_day_stale_feedback',
            'timezone': 'Asia/Shanghai',
            'cleanup_date_bj': now_bj.date().isoformat(),
            'clear_before_local_date_bj': now_bj.date().isoformat(),
            'threshold_minutes': threshold,
            'cutoff_at': cutoff_dt.isoformat(),
            'cleared_count': len(all_cleared_item_ids),
            'cleared_item_ids': all_cleared_item_ids,
            'linky_cleared_item_ids': cleared_item_ids,
            'timo_cleared_item_ids': timo_cleared_item_ids,
        }

    def run_ops_intake_midnight_feedback_cleanup(self, *, now: Optional[datetime] = None, force: bool = False) -> Dict[str, Any]:
        now_dt = now or datetime.now(timezone.utc)
        if now_dt.tzinfo is None:
            now_dt = now_dt.replace(tzinfo=timezone.utc)
        cleanup_date_bj = now_dt.astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat()
        with self._ops_intake_stale_feedback_cleanup_lock:
            if not force and self._ops_intake_stale_feedback_last_cleanup_date_bj == cleanup_date_bj:
                return {
                    'ok': True,
                    'skipped': True,
                    'reason': 'already_cleaned_for_beijing_date',
                    'cleanup_date_bj': cleanup_date_bj,
                }
            result = self.clear_ops_intake_previous_day_stale_feedback_items(now=now_dt, cleared_by='系统自动清理')
            self._ops_intake_stale_feedback_last_cleanup_date_bj = cleanup_date_bj
            return result

    def _start_ops_intake_stale_feedback_cleanup_worker(self) -> None:
        if self._ops_intake_stale_feedback_cleanup_thread and self._ops_intake_stale_feedback_cleanup_thread.is_alive():
            return
        thread = threading.Thread(
            target=self._ops_intake_stale_feedback_cleanup_loop,
            name='ops-intake-stale-feedback-cleanup',
            daemon=True,
        )
        thread.start()
        self._ops_intake_stale_feedback_cleanup_thread = thread

    def _ops_intake_stale_feedback_cleanup_loop(self) -> None:
        beijing_tz = ZoneInfo('Asia/Shanghai')
        while not self._worker_stop.is_set():
            try:
                self.run_ops_intake_midnight_feedback_cleanup()
            except Exception as exc:
                print(f'Ops intake stale feedback cleanup degraded: {exc}')
            now_bj = datetime.now(timezone.utc).astimezone(beijing_tz)
            next_midnight_bj = datetime.combine(now_bj.date() + timedelta(days=1), datetime.min.time(), tzinfo=beijing_tz)
            seconds_until_midnight = max(60.0, (next_midnight_bj - now_bj).total_seconds())
            wait_seconds = min(self.ops_intake_auto_clear_stale_feedback_poll_interval_seconds, seconds_until_midnight)
            self._worker_stop.wait(wait_seconds)

    def _start_guild_anchor_daily_stats_worker(self) -> None:
        if self._guild_anchor_daily_stats_thread and self._guild_anchor_daily_stats_thread.is_alive():
            return
        thread = threading.Thread(
            target=self._guild_anchor_daily_stats_loop,
            name='guild-anchor-daily-stats',
            daemon=True,
        )
        thread.start()
        self._guild_anchor_daily_stats_thread = thread

    def _guild_anchor_daily_stats_loop(self) -> None:
        beijing_tz = ZoneInfo('Asia/Shanghai')
        while not self._worker_stop.is_set():
            now_bj = datetime.now(timezone.utc).astimezone(beijing_tz)
            run_at_bj = datetime.combine(
                now_bj.date(),
                datetime.min.time(),
                tzinfo=beijing_tz,
            ) + timedelta(hours=self.guild_anchor_daily_stats_hour_bj, minutes=self.guild_anchor_daily_stats_minute_bj)
            if now_bj >= run_at_bj and self._guild_anchor_daily_stats_last_run_date_bj != now_bj.date().isoformat():
                with self._guild_anchor_daily_stats_lock:
                    if self._guild_anchor_daily_stats_last_run_date_bj != now_bj.date().isoformat():
                        try:
                            target_dates = [
                                (now_bj.date() - timedelta(days=offset)).isoformat()
                                for offset in range(1, self.guild_anchor_daily_stats_backfill_days + 1)
                            ]
                            self.enqueue_guild_anchor_daily_stat_jobs(stat_dates=target_dates, source='schedule', force=False)
                            self._guild_anchor_daily_stats_last_run_date_bj = now_bj.date().isoformat()
                        except Exception as exc:
                            print(f'Guild anchor daily stats enqueue degraded: {exc}')
            try:
                self.run_due_guild_anchor_daily_stat_jobs(limit=1)
            except Exception as exc:
                print(f'Guild anchor daily stats job degraded: {exc}')
            now_bj = datetime.now(timezone.utc).astimezone(beijing_tz)
            next_run_bj = datetime.combine(
                now_bj.date(),
                datetime.min.time(),
                tzinfo=beijing_tz,
            ) + timedelta(hours=self.guild_anchor_daily_stats_hour_bj, minutes=self.guild_anchor_daily_stats_minute_bj)
            if now_bj >= next_run_bj:
                next_run_bj += timedelta(days=1)
            wait_seconds = max(10.0, min(60.0, (next_run_bj - now_bj).total_seconds()))
            self._worker_stop.wait(wait_seconds)

    def _binding_truth_status_from_item(self, item: Dict[str, Any], result: Optional[Dict[str, Any]] = None) -> tuple[str, str, str]:
        result = dict(result or {})
        system_status = str(item.get('system_status') or '').strip()
        result_code = str(result.get('result_code') or item.get('result_code') or '').strip().lower()
        result_reason = str(result.get('result_reason') or result.get('reason') or item.get('result_reason') or '').strip().lower()
        crm_verified = bool(result.get('crm_verified') or result.get('current_submission_crm_verified'))
        if system_status == 'fully_success' and crm_verified:
            return 'verified_success', 'verified', 'cms_and_crm_verified'
        if system_status == 'fully_success':
            return 'success_unverified', 'unverified', 'success_without_run_scoped_crm_verification'
        if system_status == 'partial_success_crm_failed':
            return 'cms_bound_crm_failed', 'partial', 'cms_success_crm_failed'
        if result_code == 'already_in_target_guild' or 'previously registered in this agency' in result_reason:
            return 'previously_registered', 'current_fact', 'already_in_target_guild'
        if 'another_guild' in result_code or 'another guild' in result_reason:
            return 'already_in_other_guild', 'verified', 'already_joined_another_guild'
        if system_status in {'queued', 'processing', 'bind_queued', 'binding', 'crm_verifying'}:
            return 'processing', 'unverified', system_status
        if system_status in {'manual_required', 'route_mismatch', 'validation_failed'}:
            return 'needs_review', 'unverified', system_status
        return 'failed', 'failed', result_code or system_status or 'binding_failed'

    def _operation_task_row(self, task_id: str) -> Dict[str, Any]:
        normalized_task_id = str(task_id or '').strip()
        with self.db.connect() as conn:
            row = conn.execute('SELECT * FROM mcn_operation_tasks WHERE task_id = ?', (normalized_task_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail='operation_task_not_found')
        return parse_operation_task_row(row)

    def get_operation_task(self, task_id: str) -> Dict[str, Any]:
        return self._operation_task_row(task_id)

    @staticmethod
    def _whatsapp_approval_task_specs() -> Dict[str, Dict[str, Any]]:
        return whatsapp_approval_task_specs()

    @classmethod
    def _whatsapp_approval_operation_from_task_type(cls, task_type: str) -> str:
        return whatsapp_approval_operation_from_task_type(task_type)

    @classmethod
    def _is_whatsapp_approval_operation_task_type(cls, task_type: str) -> bool:
        return is_whatsapp_approval_operation_task_type(task_type)

    @classmethod
    def _operation_task_is_manual_approve_task_type(cls, task_type: str) -> bool:
        return cls._whatsapp_approval_operation_from_task_type(task_type) == 'manual_approve'

    @classmethod
    def _whatsapp_approval_operation_task_types(cls) -> Tuple[str, ...]:
        return tuple(
            sorted({
                str(spec.get('task_type') or '').strip()
                for spec in cls._whatsapp_approval_task_specs().values()
                if str(spec.get('task_type') or '').strip()
            })
        )

    @staticmethod
    def _operation_task_is_terminal_status(status: str) -> bool:
        return operation_task_is_terminal_status(status)

    @classmethod
    def _effective_whatsapp_approval_task_wait_timeout(
        cls,
        *,
        operation: str,
        requested_wait_timeout: Optional[float],
        task_timeout_seconds: Optional[Any] = None,
        task_status: str = '',
        task_deduped: bool = False,
    ) -> float:
        return effective_whatsapp_approval_task_wait_timeout(
            operation=operation,
            requested_wait_timeout=requested_wait_timeout,
            task_timeout_seconds=task_timeout_seconds,
            task_status=task_status,
            task_deduped=task_deduped,
        )

    def _whatsapp_approval_task_object_keys(self, account_key: str, binding_index: int) -> List[str]:
        normalized_key = str(account_key or '').strip()
        keys: List[str] = []
        binding_id = ''
        try:
            binding_runtime = self._get_whatsapp_approval_binding_runtime_snapshot(normalized_key, int(binding_index)) or {}
        except Exception:
            binding_runtime = {}
        if isinstance(binding_runtime, dict):
            binding_id = str(binding_runtime.get('binding_id') or '').strip()
        if normalized_key and binding_id:
            keys.append(f'{normalized_key}:binding:{binding_id}')
        if normalized_key:
            legacy_key = f'{normalized_key}:{int(binding_index)}'
            if legacy_key not in keys:
                keys.append(legacy_key)
        return keys

    def _whatsapp_approval_task_object_key(self, account_key: str, binding_index: int) -> str:
        keys = self._whatsapp_approval_task_object_keys(account_key, binding_index)
        return keys[0] if keys else ''

    def enqueue_whatsapp_approval_task(
        self,
        *,
        account_key: str,
        binding_index: int,
        operation: str,
        input_payload: Optional[Dict[str, Any]] = None,
        priority: Optional[int] = None,
        timeout_seconds: Optional[int] = None,
        max_retries: Optional[int] = None,
        created_by: str = '',
    ) -> Dict[str, Any]:
        normalized_account_key = str(account_key or '').strip()
        normalized_operation = str(operation or '').strip()
        if not normalized_account_key:
            raise HTTPException(status_code=400, detail='account_key is required')
        specs = self._whatsapp_approval_task_specs()
        if normalized_operation not in specs:
            raise HTTPException(status_code=400, detail='unsupported_whatsapp_approval_operation')
        spec = specs[normalized_operation]
        now = utc_now()
        object_keys = self._whatsapp_approval_task_object_keys(normalized_account_key, binding_index)
        object_key = object_keys[0] if object_keys else self._whatsapp_approval_task_object_key(normalized_account_key, binding_index)
        task_id = create_id('wa_task')
        task_envelope = build_whatsapp_approval_task_envelope(
            account_key=normalized_account_key,
            binding_index=int(binding_index),
            operation=normalized_operation,
            object_key=object_key,
            spec=spec,
            input_payload=input_payload,
            priority=priority,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
            created_by=created_by,
        )
        task_type = task_envelope['task_type']
        idempotency_key = task_envelope['idempotency_key']
        if normalized_operation == 'manual_approve':
            with self.db.connect() as conn:
                active_row = conn.execute(
                    """
                    SELECT task_id, task_type, object_key, status, stage, retry_count, max_retries, idempotency_key
                    FROM mcn_operation_tasks
                    WHERE task_type=? AND object_key=? AND status IN ('pending', 'running')
                    ORDER BY CASE status WHEN 'running' THEN 0 ELSE 1 END,
                             COALESCE(started_at, available_at, created_at) DESC
                    LIMIT 1
                    """,
                    (task_type, task_envelope['object_key']),
                ).fetchone()
            if active_row is not None:
                result = dict(active_row)
                result['deduped'] = True
                result['object_key'] = str(result.get('object_key') or task_envelope['object_key'])
                result['idempotency_key'] = str(result.get('idempotency_key') or idempotency_key)
                result['operation'] = normalized_operation
                if self.task_engine_enabled:
                    self._operation_task_worker_wakeup.set()
                    result = self.kick_whatsapp_approval_operation_task(result, force=False)
                return result
            input_payload_dict = dict(input_payload or {})
            request_payload = input_payload_dict.get('request') if isinstance(input_payload_dict.get('request'), dict) else {}
            request_id = str(
                input_payload_dict.get('request_id')
                or request_payload.get('request_id')
                or ''
            ).strip()
            idempotency_nonce = request_id or task_id
            idempotency_key = f'{task_type}:{task_envelope["object_key"]}:{idempotency_nonce}'
            task_envelope['idempotency_key'] = idempotency_key
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO mcn_operation_tasks (
                    task_id, task_type, object_type, object_key, idempotency_key,
                    status, stage, priority, retry_count, max_retries, input_json, result_json,
                    error_code, error_message, created_by, created_at, available_at, lease_owner,
                    lease_until, timeout_seconds
                ) VALUES (?, ?, 'registration_group_binding', ?, ?, 'pending', 'queued', ?, 0, ?, ?, '{}', '', '', ?, ?, ?, '', '', ?)
                ON CONFLICT(task_type, idempotency_key)
                DO UPDATE SET status = CASE WHEN mcn_operation_tasks.status IN ('pending', 'running') THEN mcn_operation_tasks.status ELSE 'pending' END,
                              stage = CASE
                                  WHEN mcn_operation_tasks.status = 'running' THEN mcn_operation_tasks.stage
                                  WHEN mcn_operation_tasks.status = 'pending' AND mcn_operation_tasks.stage IN ('retry_waiting', 'lease_expired') THEN mcn_operation_tasks.stage
                                  ELSE 'queued'
                              END,
                              priority = MIN(mcn_operation_tasks.priority, excluded.priority),
                              retry_count = CASE WHEN mcn_operation_tasks.status IN ('pending', 'running') THEN mcn_operation_tasks.retry_count ELSE 0 END,
                              max_retries = excluded.max_retries,
                              input_json = CASE WHEN mcn_operation_tasks.status = 'running' THEN mcn_operation_tasks.input_json ELSE excluded.input_json END,
                              result_json = CASE
                                  WHEN mcn_operation_tasks.status = 'running' OR (mcn_operation_tasks.status = 'pending' AND mcn_operation_tasks.stage IN ('retry_waiting', 'lease_expired')) THEN mcn_operation_tasks.result_json
                                  ELSE '{}'
                              END,
                              error_code = CASE
                                  WHEN mcn_operation_tasks.status = 'running' OR (mcn_operation_tasks.status = 'pending' AND mcn_operation_tasks.stage IN ('retry_waiting', 'lease_expired')) THEN mcn_operation_tasks.error_code
                                  ELSE ''
                              END,
                              error_message = CASE
                                  WHEN mcn_operation_tasks.status = 'running' OR (mcn_operation_tasks.status = 'pending' AND mcn_operation_tasks.stage IN ('retry_waiting', 'lease_expired')) THEN mcn_operation_tasks.error_message
                                  ELSE ''
                              END,
                              available_at = CASE
                                  WHEN mcn_operation_tasks.status = 'running' OR (mcn_operation_tasks.status = 'pending' AND mcn_operation_tasks.stage IN ('retry_waiting', 'lease_expired')) THEN mcn_operation_tasks.available_at
                                  ELSE excluded.available_at
                              END,
                              lease_owner = CASE WHEN mcn_operation_tasks.status = 'running' THEN mcn_operation_tasks.lease_owner ELSE '' END,
                              lease_until = CASE WHEN mcn_operation_tasks.status = 'running' THEN mcn_operation_tasks.lease_until ELSE '' END,
                              timeout_seconds = excluded.timeout_seconds,
                              started_at = CASE WHEN mcn_operation_tasks.status = 'running' THEN mcn_operation_tasks.started_at ELSE NULL END,
                              finished_at = CASE WHEN mcn_operation_tasks.status = 'running' THEN mcn_operation_tasks.finished_at ELSE NULL END
                """,
                (
                    task_id,
                    task_type,
                    task_envelope['object_key'],
                    idempotency_key,
                    task_envelope['priority'],
                    task_envelope['max_retries'],
                    task_envelope['input_json'],
                    task_envelope['created_by'],
                    now,
                    now,
                    task_envelope['timeout_seconds'],
                ),
            )
            row = conn.execute(
                "SELECT task_id, task_type, object_key, status, stage, retry_count, max_retries FROM mcn_operation_tasks WHERE task_type=? AND idempotency_key=?",
                (task_type, idempotency_key),
            ).fetchone()
            conn.commit()
        result = dict(row) if row is not None else {'task_id': task_id, 'task_type': task_type, 'status': 'pending', 'stage': 'queued'}
        result['deduped'] = str(result.get('task_id') or '') != task_id
        result['object_key'] = str(result.get('object_key') or task_envelope['object_key'])
        result['idempotency_key'] = idempotency_key
        result['operation'] = normalized_operation
        if self.task_engine_enabled:
            self._operation_task_worker_wakeup.set()
            if normalized_operation == 'manual_approve':
                result = self.kick_whatsapp_approval_operation_task(result, force=False)
        return result

    def kick_whatsapp_approval_operation_task(self, task: Dict[str, Any], *, force: bool = False) -> Dict[str, Any]:
        result = dict(task or {})
        task_id = str(result.get('task_id') or '').strip()
        operation = str(result.get('operation') or '').strip()
        task_type = str(result.get('task_type') or '').strip()
        if operation != 'manual_approve' and self._whatsapp_approval_operation_from_task_type(task_type) != 'manual_approve':
            return result
        if not task_id:
            return result
        status = str(result.get('status') or result.get('task_status') or '').strip()
        if status and status not in {'pending', 'queued'}:
            return result
        if not force and not self.task_engine_enabled:
            return result
        kick_claimed = False
        try:
            kick_claimed = bool(self._claim_operation_task(task_id, stage='claimed'))
        except Exception:
            kick_claimed = False
        if not kick_claimed:
            return result

        if self.db.db_path == ':memory:':
            try:
                self._execute_operation_task(task_id, user={'role': OPS_AUTH_ROLE_INTERNAL, 'username': 'task_engine'})
            except Exception as exc:
                try:
                    current = self.get_operation_task(task_id)
                    self._requeue_or_fail_operation_task(
                        current,
                        error_code='manual_approve_kick_failed',
                        error_message=str(exc),
                    )
                except Exception:
                    pass
            current = self.get_operation_task(task_id)
            current_result = current.get('result') if isinstance(current.get('result'), dict) else {}
            if current_result:
                result.update(current_result)
            result['status'] = str(current.get('status') or result.get('status') or '')
            result['stage'] = str(current.get('stage') or result.get('stage') or '')
            result['task_status'] = result['status']
            result['error_code'] = str(current.get('error_code') or '')
            result['error_message'] = str(current.get('error_message') or '')
            return result

        def _manual_approve_kick() -> None:
            try:
                self._execute_operation_task(task_id, user={'role': OPS_AUTH_ROLE_INTERNAL, 'username': 'task_engine'})
            except Exception as exc:
                try:
                    current = self.get_operation_task(task_id)
                    self._requeue_or_fail_operation_task(
                        current,
                        error_code='manual_approve_kick_failed',
                        error_message=str(exc),
                    )
                except Exception:
                    pass

        result['status'] = 'running'
        result['stage'] = 'claimed'
        result['task_status'] = 'running'
        threading.Thread(
            target=_manual_approve_kick,
            name='mcn-operation-task-manual-approve-kick',
            daemon=True,
        ).start()
        return result

    @staticmethod
    def _operation_task_latest_activity_iso(task: Dict[str, Any]) -> str:
        if not isinstance(task, dict):
            return ''
        for key in ('finished_at', 'available_at', 'started_at', 'created_at'):
            value = str(task.get(key) or '').strip()
            if value:
                return value
        return ''

    @staticmethod
    def _operation_task_is_background_approval_refresh_task(task: Dict[str, Any]) -> bool:
        task_type = str((task or {}).get('task_type') or '').strip()
        if task_type not in {'whatsapp_full_sync', 'whatsapp_truth_refresh'}:
            return False
        input_payload = (task or {}).get('input') if isinstance((task or {}).get('input'), dict) else None
        if input_payload is None:
            try:
                input_payload = json.loads(str((task or {}).get('input_json') or '{}'))
            except Exception:
                input_payload = {}
        source = str((input_payload or {}).get('source') or '').strip()
        reason = str((input_payload or {}).get('reason') or '').strip()
        return (
            source in {'lightweight_probe_escalation', 'scheduled_full_sync', 'manual_truth_refresh'}
            or reason in {'expired_truth_self_heal', 'auto_refresh_truth_reconciliation', 'official_group_truth_refresh_fallback'}
        )

    def _background_approval_refresh_task_is_stale_for_self_heal(self, task: Dict[str, Any], *, now_dt: datetime) -> bool:
        if not self._operation_task_is_background_approval_refresh_task(task):
            return False
        status = str((task or {}).get('status') or '').strip()
        if status not in {'pending', 'running'}:
            return False
        try:
            timeout_seconds = max(1, int((task or {}).get('timeout_seconds') or 45))
        except Exception:
            timeout_seconds = 45
        stale_after_seconds = max(APPROVAL_TRUTH_PENDING_TTL_SECONDS * 3, timeout_seconds * 2, 90)
        if status == 'running':
            lease_until = str((task or {}).get('lease_until') or '').strip()
            if lease_until:
                try:
                    lease_until_dt = parse_iso_datetime(lease_until)
                    if lease_until_dt.tzinfo is None:
                        lease_until_dt = lease_until_dt.replace(tzinfo=timezone.utc)
                    if lease_until_dt <= now_dt:
                        return True
                except Exception:
                    pass
            anchor = str((task or {}).get('started_at') or (task or {}).get('created_at') or '').strip()
        else:
            available_at = str((task or {}).get('available_at') or '').strip()
            if available_at:
                try:
                    available_dt = parse_iso_datetime(available_at)
                    if available_dt.tzinfo is None:
                        available_dt = available_dt.replace(tzinfo=timezone.utc)
                    if available_dt > now_dt:
                        return False
                except Exception:
                    pass
            anchor = str((task or {}).get('created_at') or (task or {}).get('available_at') or '').strip()
        if not anchor:
            return False
        try:
            anchor_dt = parse_iso_datetime(anchor)
            if anchor_dt.tzinfo is None:
                anchor_dt = anchor_dt.replace(tzinfo=timezone.utc)
            return (now_dt - anchor_dt).total_seconds() >= stale_after_seconds
        except Exception:
            return False

    def _recycle_stale_background_approval_refresh_task(self, task: Dict[str, Any], *, reason: str) -> None:
        task_id = str((task or {}).get('task_id') or '').strip()
        if not task_id:
            return
        self._set_operation_task_status(
            task_id,
            status='dead_letter',
            stage='stale_background_refresh_requeued',
            result={
                'recycled_for': reason,
                'previous_status': str((task or {}).get('status') or '').strip(),
                'previous_stage': str((task or {}).get('stage') or '').strip(),
            },
            error_code='stale_background_refresh_task',
            error_message='stale background approval refresh task recycled by truth self-heal',
        )

    def _latest_whatsapp_approval_task_for_binding(self, *, account_key: str, binding_index: int, operation: str) -> Dict[str, Any]:
        normalized_operation = str(operation or '').strip()
        spec = self._whatsapp_approval_task_specs().get(normalized_operation) or {}
        task_type = str(spec.get('task_type') or '').strip()
        if not task_type:
            return {}
        object_keys = self._whatsapp_approval_task_object_keys(account_key, binding_index)
        primary_object_key = object_keys[0] if object_keys else self._whatsapp_approval_task_object_key(account_key, binding_index)
        if not primary_object_key:
            return {}
        query_keys = object_keys or [primary_object_key]
        placeholders = ','.join('?' for _ in query_keys)
        with self.db.connect() as conn:
            row = conn.execute(
                f"""
                SELECT task_id, task_type, status, stage, created_at, started_at, finished_at, available_at,
                       lease_until, timeout_seconds, retry_count, max_retries, created_by, input_json,
                       result_json, error_code
                FROM mcn_operation_tasks
                WHERE task_type = ? AND object_key IN ({placeholders})
                ORDER BY CASE WHEN object_key = ? THEN 0 ELSE 1 END, COALESCE(finished_at, started_at, created_at) DESC
                LIMIT 1
                """,
                (task_type, *query_keys, primary_object_key),
            ).fetchone()
        if not row:
            return {}
        task = dict(row)
        try:
            task['input'] = json.loads(task.get('input_json') or '{}')
        except Exception:
            task['input'] = {}
        try:
            task['result'] = json.loads(task.get('result_json') or '{}')
        except Exception:
            task['result'] = {}
        return task

    @staticmethod
    def _background_approval_refresh_cooldown_seconds(
        task: Dict[str, Any],
        *,
        configured_seconds: int,
    ) -> int:
        # Realtime snapshots are read paths. A failed provider probe must not turn
        # every page poll into another live full-sync attempt that competes with
        # an operator's one-click approval.
        cooldown_floor = 60
        cooldown = max(int(configured_seconds or 0), cooldown_floor)
        if not task:
            return cooldown
        status = str(task.get('status') or '').strip().lower()
        result = dict(task.get('result') or {}) if isinstance(task.get('result'), dict) else {}
        final_state = str(result.get('final_state') or '').strip()
        failure_class = str(result.get('failure_class') or '').strip()
        failed_refresh = bool(
            status in {'failed', 'dead_letter', 'cancelled'}
            or final_state == 'TRUTH_ACQUISITION_FAILED'
            or failure_class not in {'', 'NONE'}
            or result.get('ok') is False
        )
        if final_state == 'COMMIT_PERMISSION_STATE':
            return max(cooldown, 300)
        if failed_refresh:
            return max(cooldown, 300)
        return cooldown

    def get_latest_whatsapp_approval_operation_task(self, account_key: str, binding_index: int, *, operation: str) -> Dict[str, Any]:
        return self._latest_whatsapp_approval_task_for_binding(
            account_key=account_key,
            binding_index=binding_index,
            operation=operation,
        )

    def maybe_enqueue_expired_approval_queue_self_heal(
        self,
        rows: List[Dict[str, Any]],
        *,
        created_by: str = 'lightweight_snapshot_refresh',
        cooldown_seconds: int = APPROVAL_TRUTH_PENDING_TTL_SECONDS,
    ) -> Dict[str, Any]:
        try:
            self._recover_operation_task_leases()
        except Exception:
            pass
        try:
            self.reconcile_task_residue()
        except Exception:
            pass

        def _auto_refresh_trigger(truth: Dict[str, Any]) -> Optional[Dict[str, Any]]:
            current_truth = dict(truth.get('current_truth') or {}) if isinstance(truth.get('current_truth'), dict) else {}
            reason_code = str(current_truth.get('reason_code') or '').strip().lower()
            verified_at = str(current_truth.get('verified_at') or current_truth.get('source_ts') or current_truth.get('checked_at') or '').strip()
            expires_at = str(current_truth.get('expires_at') or '').strip()

            if not current_truth:
                return {
                    'source': 'scheduled_full_sync',
                    'reason': 'auto_refresh_truth_reconciliation',
                    'queue_reason': 'enqueued_scheduled_full_sync',
                }

            if bool(current_truth.get('stale')):
                return {
                    'source': 'scheduled_full_sync',
                    'reason': 'auto_refresh_truth_reconciliation',
                    'queue_reason': 'enqueued_scheduled_full_sync',
                }

            if reason_code == 'historical_polluted_empty_downgraded':
                return {
                    'source': 'scheduled_full_sync',
                    'reason': 'auto_refresh_truth_reconciliation',
                    'queue_reason': 'enqueued_scheduled_full_sync',
                }

            if expires_at:
                try:
                    expiry_dt = parse_iso_datetime(expires_at)
                    if expiry_dt.tzinfo is None:
                        expiry_dt = expiry_dt.replace(tzinfo=timezone.utc)
                    if now_utc() >= expiry_dt:
                        return {
                            'source': 'lightweight_probe_escalation',
                            'reason': 'expired_truth_self_heal',
                            'queue_reason': 'enqueued_lightweight_probe_escalation',
                        }
                    return None
                except Exception:
                    pass
            if verified_at:
                try:
                    verified_dt = parse_iso_datetime(verified_at)
                    if verified_dt.tzinfo is None:
                        verified_dt = verified_dt.replace(tzinfo=timezone.utc)
                    if (now_utc() - verified_dt).total_seconds() > APPROVAL_TRUTH_PENDING_TTL_SECONDS:
                        return {
                            'source': 'lightweight_probe_escalation',
                            'reason': 'expired_truth_self_heal',
                            'queue_reason': 'enqueued_lightweight_probe_escalation',
                        }
                except Exception:
                    pass
            return None

        now_dt = now_utc()
        results: List[Dict[str, Any]] = []
        queued_count = 0
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            responsible_type = str(row.get('responsible_type') or '').strip()
            if responsible_type not in {'registration_group', 'official_group'}:
                continue
            account_key = str(row.get('account_key') or '').strip()
            runtime_state = dict(row.get('runtime_state') or {}) if isinstance(row.get('runtime_state'), dict) else {}
            session_state = dict(row.get('session_state') or {}) if isinstance(row.get('session_state'), dict) else {}
            for binding_index, binding in enumerate(list(row.get('group_binding_runtimes') or [])):
                if not isinstance(binding, dict):
                    continue
                outcome = {
                    'account_key': account_key,
                    'binding_index': int(binding_index),
                    'queued': False,
                    'reason': '',
                }
                truth = dict(binding.get('approval_queue_truth') or {}) if isinstance(binding.get('approval_queue_truth'), dict) else {}
                auto_refresh = _auto_refresh_trigger(truth)
                if not auto_refresh:
                    outcome['reason'] = 'truth_auto_refresh_not_needed'
                    results.append(outcome)
                    continue
                if not bool(row.get('enabled')):
                    outcome['reason'] = 'account_disabled'
                    results.append(outcome)
                    continue
                if not bool(binding.get('enabled', True)):
                    outcome['reason'] = 'binding_disabled'
                    results.append(outcome)
                    continue
                if not bool(binding.get('monitoring_effective')):
                    outcome['reason'] = 'monitoring_not_effective'
                    results.append(outcome)
                    continue
                if not bool(runtime_state.get('active')):
                    outcome['reason'] = 'runtime_inactive'
                    results.append(outcome)
                    continue
                if not bool(session_state.get('can_probe')):
                    outcome['reason'] = 'session_not_probe_ready'
                    results.append(outcome)
                    continue
                operation_state = dict(binding.get('operation_state') or {}) if isinstance(binding.get('operation_state'), dict) else {}
                if bool(operation_state.get('active')):
                    outcome['reason'] = 'binding_operation_in_progress'
                    results.append(outcome)
                    continue
                refresh_operation = 'truth_refresh' if responsible_type == 'official_group' else 'full_sync'
                refresh_source = str(auto_refresh.get('source') or 'scheduled_full_sync').strip() or 'scheduled_full_sync'
                queue_reason = str(auto_refresh.get('queue_reason') or 'enqueued_scheduled_full_sync').strip() or 'enqueued_scheduled_full_sync'
                input_timeout_seconds = 5.0 if responsible_type == 'official_group' else 30.0
                task_timeout_seconds = 8 if responsible_type == 'official_group' else 45
                task_priority = 80 if responsible_type == 'official_group' else 30
                latest_task = self._latest_whatsapp_approval_task_for_binding(
                    account_key=account_key,
                    binding_index=binding_index,
                    operation=refresh_operation,
                )
                latest_status = str(latest_task.get('status') or '').strip()
                if latest_status in {'pending', 'running'}:
                    if self._background_approval_refresh_task_is_stale_for_self_heal(latest_task, now_dt=now_dt):
                        recycled_task_id = str(latest_task.get('task_id') or '').strip()
                        try:
                            self._recycle_stale_background_approval_refresh_task(
                                latest_task,
                                reason=str(auto_refresh.get('reason') or 'auto_refresh_truth_reconciliation'),
                            )
                        except Exception as exc:
                            outcome['reason'] = f'{refresh_operation}_task_recycle_failed'
                            outcome['error'] = str(exc)
                            results.append(outcome)
                            continue
                        outcome['recycled_stale_task_id'] = recycled_task_id or None
                        latest_task = {}
                    else:
                        outcome['reason'] = f'{refresh_operation}_task_in_flight'
                        results.append(outcome)
                        continue
                latest_activity_iso = self._operation_task_latest_activity_iso(latest_task)
                if latest_activity_iso:
                    try:
                        latest_activity_dt = parse_iso_datetime(latest_activity_iso)
                        if latest_activity_dt.tzinfo is None:
                            latest_activity_dt = latest_activity_dt.replace(tzinfo=timezone.utc)
                        cooldown_window = self._background_approval_refresh_cooldown_seconds(
                            latest_task,
                            configured_seconds=int(cooldown_seconds or 0),
                        )
                        if (now_dt - latest_activity_dt).total_seconds() < cooldown_window:
                            outcome['reason'] = f'recent_{refresh_operation}_cooldown'
                            outcome['cooldown_until'] = (latest_activity_dt + timedelta(seconds=cooldown_window)).isoformat()
                            outcome['cooldown_seconds'] = cooldown_window
                            results.append(outcome)
                            continue
                    except Exception:
                        pass
                queued = self.enqueue_whatsapp_approval_task(
                    account_key=account_key,
                    binding_index=binding_index,
                    operation=refresh_operation,
                    input_payload={
                        'source': refresh_source,
                        'timeout_seconds': input_timeout_seconds,
                        'reason': str(auto_refresh.get('reason') or 'auto_refresh_truth_reconciliation'),
                    },
                    priority=task_priority,
                    timeout_seconds=task_timeout_seconds,
                    max_retries=2,
                    created_by=created_by,
                )
                outcome.update({
                    'queued': True,
                    'reason': queue_reason,
                    'task_id': str(queued.get('task_id') or '').strip() or None,
                    'deduped': bool(queued.get('deduped')),
                })
                queued_count += 1
                try:
                    self.write_event_ledger(
                        event_type='approval_truth_self_heal_enqueued',
                        object_type='registration_group_binding',
                        object_key=self._approval_binding_truth_object_key(account_key, binding),
                        status='success',
                        evidence_level='queue',
                        payload={
                            'account_key': account_key,
                            'binding_id': str(binding.get('binding_id') or '').strip() or None,
                            'binding_index': int(binding_index),
                            'operation': refresh_operation,
                            'trigger': refresh_source,
                            'reason': str(auto_refresh.get('reason') or 'auto_refresh_truth_reconciliation'),
                            'task_id': outcome.get('task_id'),
                        },
                    )
                except Exception:
                    pass
                results.append(outcome)
        return {
            'queued_count': queued_count,
            'results': results,
        }

    def run_whatsapp_approval_task_sync(
        self,
        *,
        account_key: str,
        binding_index: int,
        operation: str,
        input_payload: Optional[Dict[str, Any]] = None,
        priority: Optional[int] = None,
        timeout_seconds: Optional[int] = None,
        max_retries: Optional[int] = None,
        created_by: str = '',
        wait_timeout_seconds: float = 120.0,
    ) -> Dict[str, Any]:
        normalized_operation = str(operation or '').strip()
        if normalized_operation != 'manual_approve':
            existing_operation = self._get_whatsapp_binding_operation_state(account_key, binding_index)
            if isinstance(existing_operation, dict):
                existing_name = str(existing_operation.get('operation') or '').strip()
                if existing_name == 'manual_approve':
                    raise HTTPException(
                        status_code=409,
                        detail={
                            'reason': 'binding_operation_in_progress',
                            'account_key': str(account_key or '').strip() or None,
                            'binding_index': int(binding_index),
                            'active_operation': existing_name or None,
                            'active_operation_label': str(existing_operation.get('operation_label') or '').strip() or self._whatsapp_binding_operation_label(existing_name),
                            'active_detail': str(existing_operation.get('detail') or '').strip() or None,
                            'active_stage_code': str(existing_operation.get('stage_code') or '').strip() or None,
                            'active_stage_label': str(existing_operation.get('stage_label') or '').strip() or None,
                            'request_id': str(existing_operation.get('request_id') or '').strip() or None,
                            'started_at': existing_operation.get('started_at'),
                        },
                    )
        if self.db.db_path == ':memory:':
            return self._run_whatsapp_approval_operation_inline(
                account_key=account_key,
                binding_index=binding_index,
                operation=normalized_operation,
                input_payload=input_payload,
            )
        queued = self.enqueue_whatsapp_approval_task(
            account_key=account_key,
            binding_index=binding_index,
            operation=normalized_operation,
            input_payload=input_payload,
            priority=priority,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
            created_by=created_by,
        )
        wait_started = time.monotonic()
        task_id = str(queued.get('task_id') or '').strip()
        deadline = wait_started + self._effective_whatsapp_approval_task_wait_timeout(
            operation=normalized_operation,
            requested_wait_timeout=wait_timeout_seconds,
            task_timeout_seconds=queued.get('timeout_seconds') or timeout_seconds,
            task_status=str(queued.get('status') or '').strip(),
            task_deduped=bool(queued.get('deduped')),
        )
        while True:
            task = self.get_operation_task(task_id)
            status = str(task.get('status') or '').strip()
            effective_wait_timeout = self._effective_whatsapp_approval_task_wait_timeout(
                operation=normalized_operation,
                requested_wait_timeout=wait_timeout_seconds,
                task_timeout_seconds=task.get('timeout_seconds') or queued.get('timeout_seconds') or timeout_seconds,
                task_status=status,
                task_deduped=bool(queued.get('deduped')),
            )
            deadline = max(deadline, wait_started + effective_wait_timeout)
            if self._operation_task_is_terminal_status(status):
                if status == 'success':
                    result = dict(task.get('result') or {})
                    if result:
                        return result
                    return {'task_id': task_id, 'status': status}
                task_result = dict(task.get('result') or {})
                task_detail = task_result.get('detail') if isinstance(task_result.get('detail'), (dict, str)) else None
                if isinstance(task_detail, dict):
                    detail_payload = dict(task_detail)
                elif isinstance(task_detail, str) and task_detail.strip():
                    detail_payload = {'reason': task_detail.strip()}
                else:
                    detail_payload = {
                        'reason': str(task.get('error_code') or 'whatsapp_approval_task_failed'),
                        'task_id': task_id,
                        'status': status,
                        'stage': task.get('stage'),
                        'error_message': task.get('error_message'),
                    }
                detail_payload.setdefault('task_id', task_id)
                detail_payload.setdefault('status', status)
                detail_payload.setdefault('stage', task.get('stage'))
                if not detail_payload.get('error_message') and task.get('error_message'):
                    detail_payload['error_message'] = task.get('error_message')
                status_code = int(task_result.get('http_status') or detail_payload.get('http_status') or (409 if status == 'dead_letter' else 500))
                raise HTTPException(status_code=status_code, detail=detail_payload)
            if not self.task_engine_enabled or self.db.db_path == ':memory:':
                self.process_operation_tasks_once(limit=5)
            else:
                worker_alive = bool(
                    self._operation_task_worker_thread
                    and self._operation_task_worker_thread.is_alive()
                )
                if not worker_alive:
                    self._start_operation_task_worker()
                    self.process_operation_tasks_once(limit=5)
                self._operation_task_worker_wakeup.set()
                time.sleep(0.2)
            if time.monotonic() > deadline:
                raise HTTPException(
                    status_code=504,
                    detail={
                        'reason': 'whatsapp_approval_task_wait_timeout',
                        'task_id': task_id,
                        'status': status,
                        'operation': operation,
                    },
                )

    @staticmethod
    def _normalize_whatsapp_probe_refresh_mode(value: Any) -> str:
        normalized = str(value or '').strip().lower()
        if normalized in {'fast', 'quick', 'manual_fast'}:
            return 'fast'
        return 'strict'

    def _run_whatsapp_approval_operation_inline(
        self,
        *,
        account_key: str,
        binding_index: int,
        operation: str,
        input_payload: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        payload = dict(input_payload or {})
        normalized_operation = str(operation or '').strip()
        if not account_key or binding_index < 0 or not normalized_operation:
            raise ValueError('invalid whatsapp approval task payload')
        if normalized_operation == 'manual_approve':
            return self.manual_approve_whatsapp_approval_binding(
                account_key,
                binding_index,
                audit_context={
                    'operator': dict(payload.get('operator') or {}) or {'role': OPS_AUTH_ROLE_INTERNAL, 'username': 'task_engine'},
                    'request': dict(payload.get('request') or {}),
                },
            )
        if normalized_operation == 'full_sync':
            return self.full_sync_whatsapp_approval_binding(
                account_key,
                binding_index,
                source=str(payload.get('source') or 'manual_full_sync'),
                timeout_seconds=float(payload.get('timeout_seconds') or 45.0),
                request_id=str(payload.get('request_id') or '').strip() or None,
            )
        if normalized_operation == 'truth_refresh':
            return self.refresh_whatsapp_approval_binding_truth(
                account_key,
                binding_index,
                source=str(payload.get('source') or 'manual_truth_refresh'),
                timeout_seconds=float(payload.get('timeout_seconds') or 45.0),
                request_id=str(payload.get('request_id') or '').strip() or None,
            )
        if normalized_operation == 'probe_refresh':
            probe_result = self.refresh_whatsapp_approval_binding_probe(
                account_key,
                binding_index,
                probe_mode=self._normalize_whatsapp_probe_refresh_mode(payload.get('probe_mode')),
            )
            if not bool(payload.get('followup_truth_refresh')):
                return probe_result
            truth_result = self.refresh_whatsapp_approval_binding_truth(
                account_key,
                binding_index,
                source=str(payload.get('followup_source') or 'background_identity_probe_recovery'),
                timeout_seconds=float(payload.get('followup_timeout_seconds') or 30.0),
                request_id=str(payload.get('request_id') or '').strip() or None,
            )
            return {
                **dict(truth_result or {}),
                'recovery_probe': dict(probe_result or {}),
                'recovery_followup_attempted': True,
            }
        if normalized_operation == 'rebuild_identity':
            return self.rebuild_whatsapp_approval_binding_identity(
                account_key,
                binding_index,
                current_user=dict(payload.get('current_user') or {}) or {'role': OPS_AUTH_ROLE_INTERNAL, 'username': 'task_engine'},
                request_context=dict(payload.get('request_context') or {}),
            )
        raise ValueError(f'unsupported whatsapp approval operation: {normalized_operation}')

    def _set_operation_task_status(
        self,
        task_id: str,
        *,
        status: str,
        stage: str = '',
        result: Optional[Dict[str, Any]] = None,
        error_code: str = '',
        error_message: str = '',
    ) -> None:
        now = utc_now()
        with self.db.connect() as conn:
            if status == 'running':
                row = conn.execute("SELECT timeout_seconds FROM mcn_operation_tasks WHERE task_id=?", (task_id,)).fetchone()
                timeout_seconds = int((dict(row).get('timeout_seconds') if row is not None else 60) or 60)
                lease_until = (parse_iso_datetime(now) + timedelta(seconds=max(1, timeout_seconds))).isoformat()
                conn.execute(
                    "UPDATE mcn_operation_tasks SET status=?, stage=?, started_at=?, finished_at=NULL, error_code='', error_message='', lease_owner=?, lease_until=? WHERE task_id=?",
                    (status, stage, now, self._worker_id, lease_until, task_id),
                )
            elif status in {'success', 'failed', 'dead_letter'}:
                conn.execute(
                    "UPDATE mcn_operation_tasks SET status=?, stage=?, result_json=?, error_code=?, error_message=?, finished_at=?, lease_owner='', lease_until='' WHERE task_id=?",
                    (status, stage, json.dumps(result or {}, ensure_ascii=False, default=str), error_code, error_message, now, task_id),
                )
            else:
                conn.execute("UPDATE mcn_operation_tasks SET status=?, stage=?, lease_owner='', lease_until='' WHERE task_id=?", (status, stage, task_id))
            conn.commit()

    def _claim_operation_task(self, task_id: str, *, stage: str = 'claimed') -> bool:
        normalized_task_id = str(task_id or '').strip()
        if not normalized_task_id:
            return False
        now = utc_now()
        task_types = self._whatsapp_approval_operation_task_types()
        with self.db.connect() as conn:
            try:
                conn.execute('BEGIN IMMEDIATE')
                row = conn.execute(
                    "SELECT task_id, task_type, object_key, timeout_seconds FROM mcn_operation_tasks WHERE task_id=? AND status='pending'",
                    (normalized_task_id,),
                ).fetchone()
                if row is None:
                    conn.rollback()
                    return False
                task = dict(row)
                task_type = str(task.get('task_type') or '').strip()
                object_key = str(task.get('object_key') or '').strip()
                if task_types and self._is_whatsapp_approval_operation_task_type(task_type):
                    account_key = operation_task_account_key_from_object_key(object_key)
                    if account_key:
                        current_is_manual_approve = self._operation_task_is_manual_approve_task_type(task_type)
                        placeholders = ','.join('?' for _ in task_types)
                        running_rows = conn.execute(
                            f"""
                            SELECT task_id, task_type, object_key
                            FROM mcn_operation_tasks
                            WHERE status='running'
                              AND task_type IN ({placeholders})
                              AND (lease_until = '' OR lease_until > ?)
                            """,
                            (*task_types, now),
                        ).fetchall()
                        for running_row in running_rows:
                            running_task = dict(running_row)
                            if str(running_task.get('task_id') or '').strip() == normalized_task_id:
                                continue
                            running_task_type = str(running_task.get('task_type') or '').strip()
                            running_account = operation_task_account_key_from_object_key(str(running_task.get('object_key') or '').strip())
                            if running_account == account_key:
                                if current_is_manual_approve and not self._operation_task_is_manual_approve_task_type(running_task_type):
                                    continue
                                conn.rollback()
                                return False
                timeout_seconds = int(task.get('timeout_seconds') or 60)
                lease_until = (parse_iso_datetime(now) + timedelta(seconds=max(1, timeout_seconds))).isoformat()
                cursor = conn.execute(
                    """
                    UPDATE mcn_operation_tasks
                    SET status='running', stage=?, started_at=?, finished_at=NULL,
                        error_code='', error_message='', lease_owner=?, lease_until=?
                    WHERE task_id=? AND status='pending'
                    """,
                    (stage, now, self._worker_id, lease_until, normalized_task_id),
                )
                if cursor.rowcount != 1:
                    conn.rollback()
                    return False
                conn.commit()
                return True
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass
                raise

    @staticmethod
    def _operation_task_is_background_full_sync_task(task: Dict[str, Any]) -> bool:
        if str((task or {}).get('task_type') or '').strip() not in {'whatsapp_full_sync', 'whatsapp_truth_refresh'}:
            return False
        created_by = str((task or {}).get('created_by') or '').strip()
        input_payload = (task or {}).get('input') if isinstance((task or {}).get('input'), dict) else None
        if input_payload is None:
            try:
                input_payload = json.loads(str((task or {}).get('input_json') or '{}'))
            except Exception:
                input_payload = {}
        source = str((input_payload or {}).get('source') or '').strip()
        reason = str((input_payload or {}).get('reason') or '').strip()
        return (
            created_by == 'realtime_snapshot_refresh'
            or created_by == 'official_manual_approve_post_verify'
            or source in {'lightweight_probe_escalation', 'scheduled_full_sync'}
            or reason in {'expired_truth_self_heal', 'auto_refresh_truth_reconciliation', 'official_manual_approve_post_verify_deferred'}
        )

    @staticmethod
    def _approval_refresh_result_is_provider_locked(result: Any) -> bool:
        try:
            serialized = json.dumps(result or {}, ensure_ascii=False, default=str).lower()
        except Exception:
            serialized = str(result or '').lower()
        return any(marker in serialized for marker in (
            '"probe_error": "locked"',
            '"probe_error":"locked"',
            '"reason": "locked"',
            '"reason":"locked"',
            '"error": "locked"',
            '"error":"locked"',
            'temporarily_locked',
        ))

    def _requeue_or_fail_operation_task(self, task: Dict[str, Any], *, error_code: str, error_message: str, result: Optional[Dict[str, Any]] = None) -> None:
        task_id = str(task.get('task_id') or '').strip()
        task_type = str(task.get('task_type') or '').strip()
        try:
            retry_count = int(task.get('retry_count') or 0) + 1
        except Exception:
            retry_count = 1
        try:
            max_retries = max(1, int(task.get('max_retries') or 3))
        except Exception:
            max_retries = 3
        now = parse_iso_datetime(utc_now())
        result_payload = dict(result or {})
        result_json = json.dumps(result_payload, ensure_ascii=False, default=str)
        persistent_background_refresh = self._operation_task_is_background_full_sync_task(task)
        provider_locked = bool(
            persistent_background_refresh
            and self._approval_refresh_result_is_provider_locked(result_payload)
        )
        retry_priority = 30 if persistent_background_refresh else None
        with self.db.connect() as conn:
            if persistent_background_refresh or operation_task_should_retry(retry_count=retry_count, max_retries=max_retries):
                failure_class = str(result_payload.get('failure_class') or '').strip().upper()
                reason_code = str(result_payload.get('reason_code') or '').strip()
                independent_verify_unavailable = bool(
                    failure_class == 'INDEPENDENT_VERIFY_UNAVAILABLE'
                    or reason_code == 'executor_group_state_fallback_disabled_for_single_truth'
                )
                if provider_locked:
                    locked_backoff_seconds = (300, 900, 1800, 3600, 7200, 21600)
                    backoff_seconds = locked_backoff_seconds[min(max(retry_count - 1, 0), len(locked_backoff_seconds) - 1)]
                elif persistent_background_refresh and independent_verify_unavailable:
                    backoff_seconds = min(1800, max(300, retry_count * 60))
                elif persistent_background_refresh:
                    backoff_seconds = min(300, max(30, retry_count * 15))
                else:
                    backoff_seconds = 0
                available_at = (now + timedelta(seconds=backoff_seconds)).isoformat()
                conn.execute(
                    """
                    UPDATE mcn_operation_tasks
                    SET status='pending', stage='retry_waiting', retry_count=?, error_code=?, error_message=?,
                        result_json=?, available_at=?, priority=CASE WHEN ? IS NULL THEN priority ELSE ? END,
                        started_at=NULL, finished_at=NULL, lease_owner='', lease_until=''
                    WHERE task_id=?
                    """,
                    (retry_count, error_code, str(error_message or '')[:500], result_json, available_at, retry_priority, retry_priority, task_id),
                )
            else:
                terminal_status = operation_task_terminal_failure_status(task_type)
                conn.execute(
                    """
                    UPDATE mcn_operation_tasks
                    SET status=?, stage='failed', retry_count=?, error_code=?, error_message=?, result_json=?, finished_at=?,
                        lease_owner='', lease_until=''
                    WHERE task_id=?
                    """,
                    (terminal_status, retry_count, error_code, str(error_message or '')[:500], result_json, now.isoformat(), task_id),
                )
            conn.commit()

    def _defer_whatsapp_approval_operation_task_for_contention(
        self,
        task: Dict[str, Any],
        *,
        error_code: str,
        error_message: str,
        result: Optional[Dict[str, Any]] = None,
    ) -> bool:
        task_id = str((task or {}).get('task_id') or '').strip()
        task_type = str((task or {}).get('task_type') or '').strip()
        if not task_id or task_type not in {'whatsapp_full_sync', 'whatsapp_truth_refresh', 'whatsapp_probe_refresh'}:
            return False
        normalized_error = str(error_code or '').strip()
        if normalized_error not in {'truth_acquisition_in_progress', 'runtime_actor_busy', 'binding_operation_in_progress'}:
            return False
        now_dt = parse_iso_datetime(utc_now())
        delay_seconds = 15
        detail = (result or {}).get('detail') if isinstance((result or {}).get('detail'), dict) else {}
        wait_timeout = normalize_int_or_none(detail.get('wait_timeout_seconds')) if isinstance(detail, dict) else None
        if wait_timeout is not None:
            delay_seconds = max(delay_seconds, min(60, int(wait_timeout) + 5))
        available_at = (now_dt + timedelta(seconds=delay_seconds)).isoformat()
        stage = 'waiting_for_truth_acquisition' if normalized_error == 'truth_acquisition_in_progress' else 'waiting_for_runtime_actor'
        with self.db.connect() as conn:
            conn.execute(
                """
                UPDATE mcn_operation_tasks
                SET status='pending', stage=?, result_json=?, error_code=?, error_message=?,
                    available_at=?, started_at=NULL, finished_at=NULL, lease_owner='', lease_until=''
                WHERE task_id=?
                """,
                (
                    stage,
                    json.dumps(result or {}, ensure_ascii=False, default=str),
                    normalized_error,
                    str(error_message or '')[:500],
                    available_at,
                    task_id,
                ),
            )
            conn.commit()
        return True

    def _classify_registration_group_probe_result(self, result: Dict[str, Any]) -> Dict[str, Any]:
        binding_runtime = dict((result or {}).get('binding_runtime') or {})
        probe = dict((result or {}).get('probe') or {})
        verifier = dict(binding_runtime.get('membership_verifier') or {})
        verifier_probe = dict(verifier.get('probe') or {})
        approval_truth = dict(binding_runtime.get('approval_queue_truth') or {})
        current_truth = dict(approval_truth.get('current_truth') or {})
        runtime_group_id = str(binding_runtime.get('runtime_probe_group_id') or probe.get('runtime_probe_group_id') or probe.get('group_id') or '').strip()
        runtime_group_name = str(binding_runtime.get('runtime_probe_group_name') or probe.get('runtime_probe_group_name') or probe.get('group_name') or '').strip()
        resolved = bool(runtime_group_id and runtime_group_name)
        pending_count_raw = current_truth.get('pending_count', current_truth.get('pendingCount'))
        try:
            pending_count = int(pending_count_raw) if pending_count_raw is not None else None
        except Exception:
            pending_count = None
        requester_ids = probe.get('requester_ids', binding_runtime.get('requester_ids'))
        if not isinstance(requester_ids, list):
            requester_ids = []
        self_participant_found = probe.get('self_participant_found', binding_runtime.get('self_participant_found'))
        self_is_admin = probe.get('self_is_admin', binding_runtime.get('self_is_admin'))
        can_manage = probe.get('can_manage_membership_requests', binding_runtime.get('can_manage_membership_requests'))
        review_surface_ready = bool(probe.get('review_surface_ready') or binding_runtime.get('review_surface_ready'))
        empty_queue_visible = bool(probe.get('empty_queue_visible') or binding_runtime.get('empty_queue_visible'))
        zero_verified_by = str(probe.get('zero_pending_verified_by') or binding_runtime.get('zero_pending_verified_by') or '').strip()
        permission_status = ''
        for value in (
            probe.get('permission_status'),
            probe.get('reason_code'),
            verifier.get('status'),
            verifier_probe.get('permission_status'),
            verifier_probe.get('reason_code'),
            binding_runtime.get('last_probe_status'),
            binding_runtime.get('last_probe_reason'),
        ):
            normalized = str(value or '').strip().lower()
            if normalized in {'not_group_member', 'not_group_admin'}:
                permission_status = normalized
                break
        probe_is_stale_or_failed = bool(
            current_truth.get('stale')
            or probe.get('stale')
            or probe.get('error')
            or probe.get('probe_error')
            or str(probe.get('source') or '').strip().lower().endswith('_after_error')
        )
        outcome = {
            'identity_status': 'resolved' if resolved else 'unresolved',
            'queue_status': 'unknown',
            'stage': 'probe_completed_verified',
            'reason': 'verified',
            'runtime_probe_group_id': runtime_group_id,
            'runtime_probe_group_name': runtime_group_name,
            'pending_count': pending_count,
            'self_participant_found': self_participant_found,
            'self_is_admin': self_is_admin,
            'can_manage_membership_requests': can_manage,
            'review_surface_ready': review_surface_ready,
            'empty_queue_visible': empty_queue_visible,
            'zero_pending_verified_by': zero_verified_by,
            'terminal_result': None,
        }
        if not resolved:
            outcome.update({'stage': 'probe_failed', 'reason': 'identity_unresolved', 'queue_status': 'unavailable', 'terminal_result': 'probe_failed'})
            return outcome
        if permission_status == 'not_group_member' or (self_participant_found is False and not probe_is_stale_or_failed):
            outcome.update({'identity_status': 'resolved', 'stage': 'probe_completed_not_group_member', 'reason': 'not_group_member', 'queue_status': 'unavailable', 'terminal_result': 'not_group_member'})
            return outcome
        if permission_status == 'not_group_admin' or (
            self_participant_found is True
            and (self_is_admin is False or can_manage is False)
            and not probe_is_stale_or_failed
        ):
            outcome.update({'identity_status': 'resolved', 'stage': 'probe_completed_not_group_admin', 'reason': 'not_group_admin', 'queue_status': 'unavailable', 'terminal_result': 'not_group_admin'})
            return outcome
        if probe_is_stale_or_failed:
            outcome.update({'stage': 'probe_failed', 'reason': 'live_probe_failed', 'queue_status': 'unavailable', 'terminal_result': 'probe_failed'})
            return outcome
        if pending_count is not None and pending_count > 0:
            outcome.update({'queue_status': 'confirmed_pending', 'stage': 'probe_completed_verified', 'reason': 'confirmed_pending', 'terminal_result': 'pending_count'})
            return outcome
        if pending_count == 0:
            strong_empty = bool(empty_queue_visible or (requester_ids == [] and review_surface_ready) or (zero_verified_by and review_surface_ready))
            if strong_empty:
                outcome.update({'queue_status': 'confirmed_empty', 'stage': 'probe_completed_verified', 'reason': 'confirmed_empty', 'terminal_result': 'pending_count'})
            else:
                outcome.update({'queue_status': 'empty_unverified', 'stage': 'probe_failed', 'reason': 'zero_without_current_truth_evidence', 'terminal_result': 'probe_failed'})
            return outcome
        outcome.update({'stage': 'probe_failed', 'reason': 'current_truth_missing', 'queue_status': 'unknown', 'terminal_result': 'probe_failed'})
        return outcome

    def _execute_probe_registration_group_truth_task(self, task: Dict[str, Any]) -> None:
        task_id = str(task.get('task_id') or '').strip()
        payload = dict(task.get('input') or {})
        account_key = str(payload.get('account_key') or '').strip()
        try:
            binding_index = int(payload.get('binding_index'))
        except Exception:
            binding_index = -1
        self._set_operation_task_status(task_id, status='running', stage='probing')
        try:
            if not account_key or binding_index < 0:
                raise ValueError('invalid registration probe task payload')
            result = self.refresh_whatsapp_approval_binding_probe(account_key, binding_index)
            result_payload = result if isinstance(result, dict) else {'result': result}
            probe_outcome = self._classify_registration_group_probe_result(result_payload)
            result_payload = {**result_payload, 'probe_outcome': probe_outcome}
            if str(probe_outcome.get('terminal_result') or '').strip() == 'probe_failed':
                self._requeue_or_fail_operation_task(
                    task,
                    error_code='probe_registration_group_truth_unverified',
                    error_message=str(probe_outcome.get('reason') or 'probe failed without terminal truth'),
                    result=result_payload,
                )
                updated_task = self.get_operation_task(task_id)
                if str(updated_task.get('status') or '').strip() not in {'pending', 'running'}:
                    self._notify_approval_operation_realtime_update(
                        account_key=account_key,
                        binding_index=binding_index,
                        operation='probe_refresh',
                        task_id=task_id,
                        result=result_payload,
                    )
                return
            self._set_operation_task_status(task_id, status='success', stage=str(probe_outcome.get('stage') or 'probe_completed_verified'), result=result_payload)
            self._notify_approval_operation_realtime_update(
                account_key=account_key,
                binding_index=binding_index,
                operation='probe_refresh',
                task_id=task_id,
                result=result_payload,
            )
        except Exception as exc:
            self._requeue_or_fail_operation_task(
                task,
                error_code='probe_registration_group_truth_failed',
                error_message=str(exc),
            )
            updated_task = self.get_operation_task(task_id)
            if str(updated_task.get('status') or '').strip() not in {'pending', 'running'}:
                self._notify_approval_operation_realtime_update(
                    account_key=account_key,
                    binding_index=binding_index,
                    operation='probe_refresh',
                    task_id=task_id,
                    result={'probe_outcome': {'terminal_result': 'probe_failed', 'reason': 'probe_exception'}},
                )

    def _execute_whatsapp_approval_operation_task(self, task: Dict[str, Any]) -> None:
        task_id = str(task.get('task_id') or '').strip()
        payload = dict(task.get('input') or {})
        account_key = str(payload.get('account_key') or '').strip()
        try:
            binding_index = int(payload.get('binding_index'))
        except Exception:
            binding_index = -1
        operation = str(payload.get('operation') or self._whatsapp_approval_operation_from_task_type(task.get('task_type') or '')).strip()
        try:
            result = self._run_whatsapp_approval_operation_inline(
                account_key=account_key,
                binding_index=binding_index,
                operation=operation,
                input_payload=payload,
            )
            result_payload = result if isinstance(result, dict) else {'result': result}
            if (
                operation in {'truth_refresh', 'full_sync'}
                and not bool(result_payload.get('current_truth_written'))
                and self._approval_truth_failure_class(result_payload) in {'BUDGET_EXHAUSTED', 'IDENTITY_UNRESOLVED'}
            ):
                recovery_task = self.enqueue_whatsapp_approval_task(
                    account_key=account_key,
                    binding_index=binding_index,
                    operation='probe_refresh',
                    input_payload={
                        'probe_mode': 'strict',
                        'followup_truth_refresh': True,
                        'followup_source': 'background_identity_probe_recovery',
                        'followup_timeout_seconds': 30.0,
                        'request_id': str(payload.get('request_id') or '').strip(),
                        'reason': str(result_payload.get('failure_class') or '').strip().lower(),
                    },
                    timeout_seconds=75,
                    max_retries=2,
                    created_by=str(task.get('created_by') or 'truth_refresh_recovery').strip(),
                )
                result_payload.update({
                    'ok': False,
                    'refresh_pending_background': True,
                    'queued_refresh': recovery_task,
                    'recovery_mode': 'identity_probe_then_truth_refresh',
                })
            if operation == 'manual_approve' and str(result_payload.get('status') or '').strip().lower() in {'failed', 'skipped'}:
                error_code = str(result_payload.get('result_code') or 'manual_approval_not_executed').strip()
                error_message = str(result_payload.get('result_reason') or 'manual approval did not execute').strip()
                self._set_operation_task_status(
                    task_id,
                    status='dead_letter',
                    stage='business_failed',
                    result=result_payload,
                    error_code=error_code,
                    error_message=error_message,
                )
                self._notify_approval_operation_realtime_update(
                    account_key=account_key,
                    binding_index=binding_index,
                    operation=operation,
                    task_id=task_id,
                    result=result_payload,
                )
                return
            if operation in {'truth_refresh', 'full_sync'} and str(result_payload.get('final_state') or '').strip() == 'COMMIT_PERMISSION_STATE':
                self._set_operation_task_status(
                    task_id,
                    status='success',
                    stage='permission_state_confirmed',
                    result=result_payload,
                    error_code='',
                    error_message='',
                )
                self._notify_approval_operation_realtime_update(
                    account_key=account_key,
                    binding_index=binding_index,
                    operation=operation,
                    task_id=task_id,
                    result=result_payload,
                )
                return
            if operation in {'truth_refresh', 'full_sync'} and not bool(result_payload.get('current_truth_written')):
                failure_class = self._approval_truth_failure_class(result_payload)
                if failure_class != 'NONE':
                    error_code = str(result_payload.get('reason_code') or 'approval_truth_refresh_unverified').strip()
                    error_message = str(
                        result_payload.get('recommended_action')
                        or result_payload.get('failure_class')
                        or result_payload.get('reason_code')
                        or 'approval truth refresh did not commit current truth'
                    ).strip()
                    if bool(result_payload.get('refresh_pending_background')) and result_payload.get('queued_refresh'):
                        self._set_operation_task_status(
                            task_id,
                            status='dead_letter',
                            stage='recovery_queued',
                            result=result_payload,
                            error_code=error_code,
                            error_message=error_message,
                        )
                        self._notify_approval_operation_realtime_update(
                            account_key=account_key,
                            binding_index=binding_index,
                            operation=operation,
                            task_id=task_id,
                            result=result_payload,
                        )
                    elif failure_class == 'PERMISSION_DENIED':
                        self._set_operation_task_status(
                            task_id,
                            status='dead_letter',
                            stage='business_blocked',
                            result=result_payload,
                            error_code=error_code,
                            error_message=error_message,
                        )
                        self._notify_approval_operation_realtime_update(
                            account_key=account_key,
                            binding_index=binding_index,
                            operation=operation,
                            task_id=task_id,
                            result=result_payload,
                        )
                    else:
                        self._requeue_or_fail_operation_task(
                            task,
                            error_code=error_code,
                            error_message=error_message,
                            result=result_payload,
                        )
                        updated_task = self.get_operation_task(task_id)
                        if str(updated_task.get('status') or '').strip() not in {'pending', 'running'}:
                            self._notify_approval_operation_realtime_update(
                                account_key=account_key,
                                binding_index=binding_index,
                                operation=operation,
                                task_id=task_id,
                                result=result_payload,
                            )
                    return
            self._set_operation_task_status(task_id, status='success', stage='completed', result=result_payload)
            self._notify_approval_operation_realtime_update(
                account_key=account_key,
                binding_index=binding_index,
                operation=operation,
                task_id=task_id,
                result=result_payload,
            )
        except HTTPException as exc:
            detail = exc.detail
            error_code = str(detail if isinstance(detail, str) else (detail or {}).get('reason') or 'http_error')
            result_payload = {'detail': detail, 'http_status': int(getattr(exc, 'status_code', 500) or 500)}
            if self._defer_whatsapp_approval_operation_task_for_contention(
                task,
                error_code=error_code,
                error_message=str(detail),
                result=result_payload,
            ):
                return
            self._requeue_or_fail_operation_task(
                task,
                error_code=error_code,
                error_message=str(detail),
                result=result_payload,
            )
        except Exception as exc:
            self._requeue_or_fail_operation_task(task, error_code='whatsapp_approval_task_failed', error_message=str(exc))

    def _notify_approval_operation_realtime_update(
        self,
        *,
        account_key: str,
        binding_index: int,
        operation: str,
        task_id: str,
        result: Dict[str, Any],
    ) -> None:
        callback = getattr(self, 'approval_operation_realtime_callback', None)
        if not callable(callback):
            return
        normalized_operation = str(operation or '').strip()
        if normalized_operation not in {'truth_refresh', 'full_sync', 'manual_approve', 'probe_refresh', 'rebuild_identity'}:
            return
        try:
            callback(
                account_key=str(account_key or '').strip(),
                binding_index=int(binding_index),
                operation=normalized_operation,
                task_id=str(task_id or '').strip(),
                result=dict(result or {}),
            )
        except Exception:
            pass

    def _recover_operation_task_leases(self) -> None:
        now_iso = utc_now()
        with self.db.connect() as conn:
            rows = [dict(row) for row in conn.execute(
                "SELECT task_id, task_type, retry_count, max_retries, input_json, created_by FROM mcn_operation_tasks WHERE status='running' AND lease_until != '' AND lease_until <= ?",
                (now_iso,),
            ).fetchall()]
            for row in rows:
                lease_status = operation_task_lease_expiry_status(
                    task_type=row.get('task_type') or '',
                    retry_count=int(row.get('retry_count') or 0),
                    max_retries=int(row.get('max_retries') or 1),
                )
                if lease_status == 'dead_letter':
                    conn.execute(
                        "UPDATE mcn_operation_tasks SET status='dead_letter', stage='lease_expired', finished_at=?, error_code='lease_expired', error_message='operation task lease expired', lease_owner='', lease_until='' WHERE task_id=?",
                        (now_iso, row['task_id']),
                    )
                else:
                    retry_priority = 30 if self._operation_task_is_background_full_sync_task(row) else None
                    conn.execute(
                        "UPDATE mcn_operation_tasks SET status='pending', stage='lease_expired', available_at=?, priority=CASE WHEN ? IS NULL THEN priority ELSE ? END, started_at=NULL, finished_at=NULL, lease_owner='', lease_until='' WHERE task_id=?",
                        (now_iso, retry_priority, retry_priority, row['task_id']),
                    )
            conn.commit()

    def _active_whatsapp_approval_running_account_task_types(self, *, now_iso: str) -> Dict[str, set[str]]:
        task_types = self._whatsapp_approval_operation_task_types()
        if not task_types:
            return {}
        placeholders = ','.join('?' for _ in task_types)
        with self.db.connect() as conn:
            rows = conn.execute(
                f"SELECT object_key, task_type FROM mcn_operation_tasks WHERE status='running' AND task_type IN ({placeholders}) AND (lease_until = '' OR lease_until > ?)",
                (*task_types, now_iso),
            ).fetchall()
        accounts: Dict[str, set[str]] = {}
        for row in rows:
            data = dict(row)
            object_key = str(data.get('object_key') or '').strip()
            account_key = operation_task_account_key_from_object_key(object_key)
            if account_key:
                accounts.setdefault(account_key, set()).add(str(data.get('task_type') or '').strip())
        return accounts

    def _active_whatsapp_approval_running_accounts(self, *, now_iso: str) -> set[str]:
        return set(self._active_whatsapp_approval_running_account_task_types(now_iso=now_iso).keys())

    def _execute_operation_task(self, task_id: str, *, user: Optional[Dict[str, Any]] = None) -> None:
        task = self._operation_task_row(task_id)
        task_type = str(task.get('task_type') or '').strip()
        if task_type == 'verify_binding_current_truth':
            self._execute_verify_binding_current_truth_task(task, user=user)
            return
        if task_type == 'probe_registration_group_truth':
            self._execute_probe_registration_group_truth_task(task)
            return
        if self._is_whatsapp_approval_operation_task_type(task_type):
            self._execute_whatsapp_approval_operation_task(task)
            return
        self._set_operation_task_status(task_id, status='failed', stage='unsupported_task', error_code='unsupported_task_type', error_message=task_type)

    def process_operation_tasks_once(self, *, limit: int = 5) -> Dict[str, Any]:
        normalized_limit = max(1, min(50, int(limit or 5)))
        self._recover_operation_task_leases()
        now_iso = utc_now()
        running_account_task_types = self._active_whatsapp_approval_running_account_task_types(now_iso=now_iso)
        with self.db.connect() as conn:
            rows = [dict(row) for row in conn.execute(
                """
                SELECT task_id, task_type, object_key FROM mcn_operation_tasks
                WHERE status = 'pending'
                  AND (available_at = '' OR available_at <= ?)
                ORDER BY
                  priority ASC,
                  CASE
                    WHEN task_type = 'whatsapp_manual_approve' THEN 0
                    WHEN task_type IN ('whatsapp_full_sync', 'whatsapp_truth_refresh') THEN 2
                    ELSE 1
                  END ASC,
                  COALESCE(NULLIF(available_at, ''), created_at) ASC,
                  created_at ASC
                LIMIT ?
                """,
                (now_iso, normalized_limit * 5),
            ).fetchall()]
        processed = 0
        task_ids: List[str] = []
        for row in rows:
            task_id = str(row.get('task_id') or '').strip()
            if not task_id:
                continue
            task_type = str(row.get('task_type') or '').strip()
            if self._is_whatsapp_approval_operation_task_type(task_type):
                object_key = str(row.get('object_key') or '').strip()
                account_key = operation_task_account_key_from_object_key(object_key)
                if account_key:
                    active_task_types = running_account_task_types.get(account_key, set())
                    if self._operation_task_is_manual_approve_task_type(task_type):
                        if any(self._operation_task_is_manual_approve_task_type(active_type) for active_type in active_task_types):
                            continue
                    elif active_task_types:
                        continue
                if account_key:
                    running_account_task_types.setdefault(account_key, set()).add(task_type)
            if not self._claim_operation_task(task_id, stage='claimed'):
                continue
            self._execute_operation_task(task_id, user={'role': OPS_AUTH_ROLE_INTERNAL, 'username': 'task_engine'})
            processed += 1
            task_ids.append(task_id)
            if processed >= normalized_limit:
                break
        with self.db.connect() as conn:
            remaining = conn.execute("SELECT COUNT(*) FROM mcn_operation_tasks WHERE status = 'pending'").fetchone()[0]
        return {'processed': processed, 'task_ids': task_ids, 'remaining_pending': int(remaining or 0)}

    def _start_operation_task_worker(self) -> None:
        if self._operation_task_worker_thread and self._operation_task_worker_thread.is_alive():
            return
        self._operation_task_worker_stop.clear()

        def _loop() -> None:
            while not self._operation_task_worker_stop.is_set():
                try:
                    result = self.process_operation_tasks_once(limit=3)
                    if not result.get('processed'):
                        self._operation_task_worker_wakeup.wait(self._operation_task_worker_poll_interval)
                        self._operation_task_worker_wakeup.clear()
                except Exception:
                    self._operation_task_worker_wakeup.wait(self._operation_task_worker_poll_interval)
                    self._operation_task_worker_wakeup.clear()

        thread = threading.Thread(target=_loop, name='mcn-operation-task-worker', daemon=True)
        thread.start()
        self._operation_task_worker_thread = thread

    def _ops_intake_display_initiator(self, item: Dict[str, Any]) -> str:
        return str(
            item.get('external_customer_service_id')
            or item.get('external_customer_service_name')
            or item.get('submitted_by_username')
            or item.get('submitted_by_user_id')
            or '-'
        ).strip() or '-'

    def _enhance_ops_intake_item_display(
        self,
        item: Dict[str, Any],
        *,
        current_truth: Optional[Dict[str, Any]] = None,
        load_current_truth: bool = True,
    ) -> Dict[str, Any]:
        item['display_initiator'] = self._ops_intake_display_initiator(item)
        if load_current_truth:
            current_truth = self._load_binding_current_truth_snapshot(str(item.get('item_id') or ''))
        if current_truth:
            item['current_truth'] = current_truth
        reply = str(item.get('reply_text') or '').strip()
        if reply.startswith('**❌ Bind failed:') or reply.startswith('**❌ Already registered in another agency**'):
            result_code = str(item.get('result_code') or '').strip()
            result_reason = str(item.get('result_reason') or '').strip()
            translated_reason = self._translate_customer_visible_failure_reason_to_english(result_reason)
            if translated_reason and translated_reason not in reply:
                item['reply_text'] = self._format_lark_reply_text({
                    'accepted': False,
                    'reason': 'bind_check_failed',
                    'result_code': result_code,
                    'result_reason': result_reason,
                    'reply_phone': item.get('parsed_phone') or '-',
                    'reply_id': item.get('parsed_account_id') or '-',
                    'reply_group': item.get('parsed_group') or '-',
                    'reply_code_display': item.get('parsed_code') or '-',
                })
                reply = str(item.get('reply_text') or '').strip()
        if 'Invalid Code. Use 6 English letters or letters+digits only.' in reply:
            item['reply_text'] = self._format_lark_reply_text({
                'accepted': False,
                'reason': 'invalid_invite_code_format',
                'reply_error_text': 'Invalid Code. Use a 6-character personal code: letters or letters+digits, not all digits.',
                'reply_phone': item.get('parsed_phone') or '-',
                'reply_id': item.get('parsed_account_id') or '-',
                'reply_group': item.get('parsed_group') or '-',
                'reply_code_display': item.get('parsed_code') or '-',
            })
            reply = str(item.get('reply_text') or '').strip()
        if 'Failed：Error Code Unable to Bind' in reply:
            item['reply_text'] = self._format_lark_reply_text({
                'accepted': False,
                'reason': 'bind_check_failed',
                'result_code': str(item.get('result_code') or '').strip(),
                'result_reason': str(item.get('result_reason') or '').strip(),
                'reply_phone': item.get('parsed_phone') or '-',
                'reply_id': item.get('parsed_account_id') or '-',
                'reply_group': item.get('parsed_group') or '-',
                'reply_code_display': item.get('parsed_code') or '-',
            })
            reply = str(item.get('reply_text') or '').strip()
        if not reply.startswith('**❌ Failed**'):
            return item
        if str(item.get('system_status') or '').strip() in {'queued', 'processing', 'bind_queued', 'binding', 'crm_verifying'}:
            item['reply_text'] = self._format_lark_reply_text({
                'accepted': True,
                'next_action': 'queue_bind_check',
                'lead_status': 'bind_check_pending',
                'reply_phone': item.get('parsed_phone') or '-',
                'reply_id': item.get('parsed_account_id') or '-',
                'reply_group': item.get('parsed_group') or '-',
                'reply_code_display': item.get('parsed_code') or '-',
            })
            return item
        snapshot: Dict[str, Any] = {}
        try:
            snapshot = json.loads(str(item.get('result_snapshot') or '{}'))
        except Exception:
            snapshot = {}
        nested = snapshot.get('result') if isinstance(snapshot.get('result'), dict) else {}
        result_code = str(item.get('result_code') or nested.get('result_code') or snapshot.get('result_code') or '').strip()
        result_reason = str(item.get('result_reason') or nested.get('result_reason') or snapshot.get('result_reason') or '').strip()
        lead_id = str(item.get('lead_id') or nested.get('lead_id') or snapshot.get('lead_id') or '').strip()
        if (not result_code and not result_reason) and lead_id:
            try:
                with self.db.connect() as conn:
                    task = conn.execute(
                        """
                        SELECT result_code, result_reason
                        FROM automation_tasks
                        WHERE lead_id = ? AND task_type = 'bind_check'
                        ORDER BY COALESCE(finished_at, started_at, created_at) DESC
                        LIMIT 1
                        """,
                        (lead_id,),
                    ).fetchone()
                if task:
                    result_code = str(task['result_code'] or '').strip()
                    result_reason = str(task['result_reason'] or '').strip()
            except Exception:
                pass
        if result_code or result_reason:
            item['reply_text'] = self._format_lark_reply_text({
                'accepted': False,
                'reason': 'bind_check_failed',
                'result_code': result_code,
                'result_reason': result_reason,
                'reply_phone': item.get('parsed_phone') or '-',
                'reply_id': item.get('parsed_account_id') or '-',
                'reply_group': item.get('parsed_group') or '-',
                'reply_code_display': item.get('parsed_code') or '-',
            })
        elif str(item.get('system_status') or '') == 'manual_required':
            item['reply_text'] = (
                '**⚠️ Needs review: information was not queued for binding**\n'
                f"Phone: {item.get('parsed_phone') or '-'}\n"
                f"ID: {item.get('parsed_account_id') or '-'}\n"
                f"Group: {item.get('parsed_group') or '-'}\n"
                f"Code: {item.get('parsed_code') or '-'}"
            )
        return item

    def _ops_intake_item_editable_fields(self, item: Dict[str, Any]) -> Dict[str, str]:
        def clean_placeholder(value: Any) -> str:
            text = str(value or '').strip()
            return '' if text.lower() in {'code', '-', '—', 'n/a', 'na', 'none', 'null', '无'} else text
        external_payload: Dict[str, Any] = {}
        try:
            external_payload = json.loads(str(item.get('external_payload') or '{}'))
        except Exception:
            external_payload = {}
        return {
            'phone': clean_placeholder(item.get('parsed_phone')),
            'account_id': clean_placeholder(item.get('parsed_account_id')),
            'group': clean_placeholder(item.get('parsed_group')),
            'code': clean_placeholder(item.get('parsed_code')),
            'app': clean_placeholder(item.get('parsed_app')),
            'agency': clean_placeholder(item.get('parsed_agency')),
            'country': clean_placeholder(external_payload.get('country')),
        }

    def _ops_intake_bind_failed_lead_item_from_row(self, row: Dict[str, Any]) -> Dict[str, Any]:
        payload: Dict[str, Any] = {}
        try:
            payload = json.loads(str(row.get('task_payload') or '{}'))
        except Exception:
            payload = {}
        phone = format_display_phone(str(row.get('mobile') or ''), area_code=int(row.get('area_code') or 0))
        account_id = str(row.get('yw_id') or payload.get('account_id') or payload.get('sid') or '').strip()
        code = str(payload.get('invite_code') or payload.get('code') or payload.get('user_code') or '').strip()
        item = {
            'item_id': str(row.get('lead_id') or '').strip(),
            'source_type': 'lead_bind_failed',
            'lead_id': str(row.get('lead_id') or '').strip(),
            'task_id': str(row.get('task_id') or '').strip(),
            'guild_name': str(row.get('dept_name') or row.get('guild_name') or '').strip(),
            'submitted_by_user_id': '',
            'submitted_by_username': str(row.get('submitted_by_username') or row.get('submitted_by') or row.get('created_by') or '').strip(),
            'raw_text': str(row.get('parser_raw_text') or '').strip(),
            'parsed_phone': phone,
            'parsed_account_id': account_id,
            'parsed_group': str(row.get('pendaftaran_group') or '').strip(),
            'parsed_code': code,
            'parsed_app': str(row.get('app_name') or '').strip(),
            'parsed_agency': str(row.get('dept_name') or row.get('guild_name') or '').strip(),
            'system_status': 'failed',
            'feedback_status': 'pending_feedback',
            'reply_text': '',
            'result_code': str(row.get('result_code') or '').strip(),
            'result_reason': str(row.get('result_reason') or '').strip(),
            'result_snapshot': str(row.get('raw_result') or '{}'),
            'created_at': str(row.get('finished_at') or row.get('task_created_at') or row.get('updated_at') or row.get('created_at') or '').strip(),
            'processed_at': str(row.get('finished_at') or '').strip(),
        }
        item['editable_fields'] = self._ops_intake_item_editable_fields(item)
        return item

    def _ensure_ops_intake_bind_failed_clears_table(self, conn: sqlite3.Connection) -> None:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ops_intake_bind_failed_clears (
                clear_id TEXT PRIMARY KEY,
                source_type TEXT NOT NULL,
                source_id TEXT NOT NULL,
                cleared_by TEXT,
                cleared_at TEXT NOT NULL,
                action TEXT,
                reason TEXT,
                note TEXT,
                UNIQUE(source_type, source_id)
            )
        """)
        existing_cols = {str(row[1]) for row in conn.execute("PRAGMA table_info(ops_intake_bind_failed_clears)").fetchall()}
        for col_name in ('action', 'reason', 'note'):
            if col_name not in existing_cols:
                conn.execute(f"ALTER TABLE ops_intake_bind_failed_clears ADD COLUMN {col_name} TEXT")

    def _ops_intake_closed_feedback_statuses(self) -> set[str]:
        return {'feedback_done', 'cleared', 'resolved', 'ignored', 'no_followup', 'duplicate_closed'}

    def _ops_intake_failure_statuses(self) -> set[str]:
        return {'failed', 'crm_failed', 'bind_failed', 'partial_success_crm_failed', 'validation_failed', 'route_mismatch'}

    def _binding_history_processing_statuses(self) -> set[str]:
        return {'queued', 'processing', 'bind_queued', 'binding', 'crm_verifying'}

    def _binding_history_attempt_is_finalized_snapshot(self, attempt: Dict[str, Any]) -> bool:
        if not isinstance(attempt, dict):
            return False
        system_status = str(attempt.get('system_status') or '').strip().lower()
        if system_status in self._binding_history_processing_statuses():
            return False
        truth = self._binding_history_parse_current_truth_payload(attempt.get('current_truth_json'))
        truth_status = str(truth.get('truth_status') or '').strip().lower()
        if truth_status == 'processing':
            return False
        if not str(attempt.get('parsed_phone') or '').strip():
            return False
        if not str(attempt.get('parsed_account_id') or '').strip():
            return False
        return True

    def _binding_history_authoritative_attempt(
        self,
        latest_attempt: Dict[str, Any],
        attempts: Sequence[Dict[str, Any]],
    ) -> tuple[Dict[str, Any], bool, str]:
        ordered_attempts = [dict(a) for a in (attempts or []) if isinstance(a, dict)]
        latest = dict(latest_attempt or {})
        if not ordered_attempts:
            return latest, False, ''
        if self._binding_history_attempt_is_finalized_snapshot(latest):
            return latest, False, ''
        for candidate in ordered_attempts:
            if self._binding_history_attempt_is_finalized_snapshot(candidate):
                stale_reason = str(latest.get('result_reason') or latest.get('result_code') or latest.get('system_status') or '').strip()
                return dict(candidate), True, stale_reason
        return latest, False, ''

    def _binding_history_response_snapshot(
        self,
        *,
        rows: Sequence[Dict[str, Any]],
        summary: Dict[str, Any],
        pagination: Dict[str, Any],
    ) -> Dict[str, Any]:
        binding_users = [dict(row) for row in (rows or []) if isinstance(row, dict)]
        display_count = len(binding_users)
        stale = any(bool(row.get('stale')) for row in binding_users)
        if binding_users:
            status = 'stale_ready' if stale else 'ready'
        elif int(summary.get('history_count') or 0) > 0:
            status = 'initial_loading'
        else:
            status = 'empty_ready'
        verified_at = ''
        for row in binding_users:
            candidate = str(row.get('verified_at') or row.get('processed_at') or row.get('created_at') or '').strip()
            if candidate and candidate > verified_at:
                verified_at = candidate
        return {
            'status': status,
            'stale': stale,
            'bindingUsers': binding_users,
            'display_count': display_count,
            'verifiedAt': verified_at,
            'source': 'ops_intake_binding_history_projection',
            'pagination': dict(pagination or {}),
            'summary': dict(summary or {}),
        }

    def _normalize_binding_history_phone_keys(self, phone: str) -> tuple[str, str]:
        phone_display = format_display_phone(str(phone or ''))
        digits = ''.join(ch for ch in phone_display if ch.isdigit())
        normalized = digits
        if phone_display.startswith('+') and digits:
            normalized = '+' + digits
        return normalized, digits

    def _binding_history_created_date_bj(self, value: Any) -> str:
        raw = str(value or '').strip()
        if not raw:
            return ''
        try:
            dt = parse_iso_datetime(raw)
            return dt.astimezone(timezone(timedelta(hours=8))).date().isoformat()
        except Exception:
            return raw[:10]

    def _ensure_binding_history_projection_tables(self, conn: sqlite3.Connection) -> None:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ops_intake_binding_history_attempts (
                attempt_id TEXT PRIMARY KEY,
                source_type TEXT NOT NULL,
                source_id TEXT NOT NULL,
                item_id TEXT NOT NULL DEFAULT '',
                lead_id TEXT NOT NULL DEFAULT '',
                task_id TEXT NOT NULL DEFAULT '',
                dedupe_key TEXT NOT NULL,
                normalized_phone TEXT NOT NULL DEFAULT '',
                normalized_phone_digits TEXT NOT NULL DEFAULT '',
                created_date_bj TEXT NOT NULL DEFAULT '',
                guild_name TEXT NOT NULL DEFAULT '',
                submitted_by_user_id TEXT NOT NULL DEFAULT '',
                submitted_by_username TEXT NOT NULL DEFAULT '',
                external_customer_service_id TEXT NOT NULL DEFAULT '',
                external_customer_service_name TEXT NOT NULL DEFAULT '',
                display_initiator TEXT NOT NULL DEFAULT '',
                parsed_phone TEXT NOT NULL DEFAULT '',
                parsed_account_id TEXT NOT NULL DEFAULT '',
                parsed_group TEXT NOT NULL DEFAULT '',
                parsed_code TEXT NOT NULL DEFAULT '',
                parsed_app TEXT NOT NULL DEFAULT '',
                parsed_agency TEXT NOT NULL DEFAULT '',
                system_status TEXT NOT NULL DEFAULT '',
                feedback_status TEXT NOT NULL DEFAULT '',
                result_code TEXT NOT NULL DEFAULT '',
                result_reason TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                processed_at TEXT NOT NULL DEFAULT '',
                closure_status TEXT NOT NULL DEFAULT '',
                closure_reason TEXT NOT NULL DEFAULT '',
                closure_note TEXT NOT NULL DEFAULT '',
                current_exception INTEGER NOT NULL DEFAULT 0,
                is_failure INTEGER NOT NULL DEFAULT 0,
                is_duplicate INTEGER NOT NULL DEFAULT 0,
                is_success INTEGER NOT NULL DEFAULT 0,
                is_closed INTEGER NOT NULL DEFAULT 0,
                current_truth_json TEXT NOT NULL DEFAULT '{}'
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ops_intake_binding_history_projection_meta (
                projection_key TEXT PRIMARY KEY,
                signature TEXT NOT NULL DEFAULT '',
                refreshed_at TEXT NOT NULL DEFAULT ''
            )
        """)

    def _binding_history_projection_signature(self, conn: sqlite3.Connection) -> str:
        def scalar(query: str, params: Sequence[Any] = ()) -> Any:
            row = conn.execute(query, tuple(params)).fetchone()
            return row[0] if row else None

        payload = {
            'ops_count': int(scalar("SELECT COUNT(*) FROM ops_intake_items WHERE COALESCE(parsed_phone, '') != '' AND COALESCE(parsed_account_id, '') != ''") or 0),
            'ops_created_max': scalar("SELECT MAX(created_at) FROM ops_intake_items"),
            'ops_processed_max': scalar("SELECT MAX(processed_at) FROM ops_intake_items"),
            'lead_failed_count': int(scalar("SELECT COUNT(*) FROM leads WHERE current_status = 'bind_failed' AND COALESCE(mobile, '') != '' AND COALESCE(yw_id, '') != ''") or 0),
            'lead_updated_max': scalar("SELECT MAX(updated_at) FROM leads WHERE current_status = 'bind_failed'"),
            'task_bind_count': int(scalar("SELECT COUNT(*) FROM automation_tasks WHERE task_type = 'bind_check'") or 0),
            'task_bind_max': scalar("SELECT MAX(COALESCE(finished_at, created_at)) FROM automation_tasks WHERE task_type = 'bind_check'"),
            'closure_count': int(scalar("SELECT COUNT(*) FROM ops_intake_bind_failed_clears") or 0),
            'closure_max': scalar("SELECT MAX(cleared_at) FROM ops_intake_bind_failed_clears"),
            'truth_count': int(scalar("SELECT COUNT(*) FROM mcn_truth_snapshots WHERE object_type = 'binding_submission' AND snapshot_type = 'binding_current_truth'") or 0),
            'truth_max': scalar("SELECT MAX(updated_at) FROM mcn_truth_snapshots WHERE object_type = 'binding_submission' AND snapshot_type = 'binding_current_truth'"),
        }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)

    def _build_binding_history_projection_attempt_rows(self, conn: sqlite3.Connection) -> List[Dict[str, Any]]:
        raw_rows = [dict(r) for r in conn.execute(
            "SELECT * FROM ops_intake_items WHERE COALESCE(parsed_phone, '') != '' AND COALESCE(parsed_account_id, '') != '' ORDER BY created_at DESC"
        ).fetchall()]
        truth_map = self._load_binding_current_truth_snapshot_map(conn, [str(row.get('item_id') or '').strip() for row in raw_rows])
        lead_rows = [dict(r) for r in conn.execute(
            """
            SELECT l.*, t.task_id, t.payload AS task_payload, t.result_code, t.result_reason, t.raw_result,
                   t.created_at AS task_created_at, t.finished_at, t.created_by AS submitted_by_username
            FROM leads l
            LEFT JOIN automation_tasks t ON t.task_id = (
                SELECT t2.task_id FROM automation_tasks t2
                WHERE t2.lead_id = l.lead_id AND t2.task_type = 'bind_check'
                ORDER BY COALESCE(t2.finished_at, t2.created_at) DESC LIMIT 1
            )
            WHERE l.current_status = 'bind_failed'
              AND COALESCE(l.mobile, '') != ''
              AND COALESCE(l.yw_id, '') != ''
            ORDER BY COALESCE(t.finished_at, l.updated_at, l.created_at) DESC
            """
        ).fetchall()]
        closure_rows = [dict(r) for r in conn.execute(
            "SELECT source_type, source_id, action, reason, note, cleared_by, cleared_at FROM ops_intake_bind_failed_clears"
        ).fetchall()]
        closures = {
            f"{str(r.get('source_type') or '').strip()}:{str(r.get('source_id') or '').strip()}": r
            for r in closure_rows
        }
        failure_statuses = self._ops_intake_failure_statuses()
        closed_statuses = self._ops_intake_closed_feedback_statuses()
        attempt_rows: List[Dict[str, Any]] = []

        def append_attempt(item: Dict[str, Any], *, source_type: str, source_id: str, lead_id: str = '', task_id: str = '', current_truth: Optional[Dict[str, Any]] = None) -> None:
            normalized_phone, normalized_digits = self._normalize_binding_history_phone_keys(str(item.get('parsed_phone') or ''))
            account_id = str(item.get('parsed_account_id') or '').strip()
            if not normalized_phone or not account_id:
                return
            result_code = str(item.get('result_code') or '').strip()
            result_reason = str(item.get('result_reason') or '').strip()
            feedback_status = str(item.get('feedback_status') or '').strip()
            system_status = str(item.get('system_status') or '').strip()
            closure_status = str(item.get('closure_status') or '').strip()
            current_exception = int(
                system_status in failure_statuses
                and feedback_status not in closed_statuses
                and closure_status not in closed_statuses
            )
            result_code_lower = result_code.lower()
            result_reason_lower = result_reason.lower()
            is_duplicate = int(
                'duplicate' in result_code_lower
                or 'data duplication' in result_reason_lower
                or 'duplicate_sid' in result_reason_lower
                or 'sid already exists' in result_reason_lower
            )
            is_failure = int(system_status in failure_statuses or result_code_lower.startswith(('cms_', 'crm_')))
            is_success = int(system_status in {'fully_success', 'success'} and not is_duplicate)
            is_closed = int((closure_status or feedback_status).lower() in closed_statuses)
            dedupe_key = f'{normalized_phone}|{account_id}'
            attempt_rows.append({
                'attempt_id': f'{source_type}:{source_id}',
                'source_type': source_type,
                'source_id': source_id,
                'item_id': str(item.get('item_id') or source_id).strip(),
                'lead_id': lead_id,
                'task_id': task_id,
                'dedupe_key': dedupe_key,
                'normalized_phone': normalized_phone,
                'normalized_phone_digits': normalized_digits,
                'created_date_bj': self._binding_history_created_date_bj(item.get('created_at')),
                'guild_name': str(item.get('guild_name') or '').strip(),
                'submitted_by_user_id': str(item.get('submitted_by_user_id') or '').strip(),
                'submitted_by_username': str(item.get('submitted_by_username') or '').strip(),
                'external_customer_service_id': str(item.get('external_customer_service_id') or '').strip(),
                'external_customer_service_name': str(item.get('external_customer_service_name') or '').strip(),
                'display_initiator': self._ops_intake_display_initiator(item),
                'parsed_phone': str(item.get('parsed_phone') or '').strip(),
                'parsed_account_id': account_id,
                'parsed_group': str(item.get('parsed_group') or '').strip(),
                'parsed_code': str(item.get('parsed_code') or '').strip(),
                'parsed_app': str(item.get('parsed_app') or '').strip(),
                'parsed_agency': str(item.get('parsed_agency') or item.get('guild_name') or '').strip(),
                'system_status': system_status,
                'feedback_status': feedback_status,
                'result_code': result_code,
                'result_reason': result_reason,
                'created_at': str(item.get('created_at') or '').strip(),
                'processed_at': str(item.get('processed_at') or '').strip(),
                'closure_status': closure_status,
                'closure_reason': str(item.get('closure_reason') or '').strip(),
                'closure_note': str(item.get('closure_note') or '').strip(),
                'current_exception': current_exception,
                'is_failure': is_failure,
                'is_duplicate': is_duplicate,
                'is_success': is_success,
                'is_closed': is_closed,
                'current_truth_json': json.dumps(current_truth or {}, ensure_ascii=False, sort_keys=True),
            })

        for raw in raw_rows:
            item = dict(raw)
            item['display_initiator'] = self._ops_intake_display_initiator(item)
            current_truth = truth_map.get(str(item.get('item_id') or '').strip())
            if current_truth:
                item['current_truth'] = current_truth
            closure = closures.get(f"ops_intake_item:{str(item.get('item_id') or '').strip()}")
            if closure:
                item['closure_status'] = str(closure.get('action') or item.get('feedback_status') or '')
                item['closure_reason'] = str(closure.get('reason') or '')
                item['closure_note'] = str(closure.get('note') or '')
            append_attempt(
                item,
                source_type='ops_intake_item',
                source_id=str(item.get('item_id') or '').strip(),
                lead_id=str(item.get('lead_id') or '').strip(),
                task_id=str(item.get('task_id') or '').strip(),
                current_truth=current_truth,
            )

        for lead_row in lead_rows:
            item = self._ops_intake_bind_failed_lead_item_from_row(lead_row)
            closure = closures.get(f"lead:{str(item.get('lead_id') or item.get('item_id') or '').strip()}") or closures.get(f"lead_bind_failed:{str(item.get('lead_id') or item.get('item_id') or '').strip()}")
            if closure:
                item['closure_status'] = str(closure.get('action') or item.get('feedback_status') or '')
                item['closure_reason'] = str(closure.get('reason') or '')
                item['closure_note'] = str(closure.get('note') or '')
                item['feedback_status'] = str(closure.get('action') or item.get('feedback_status') or '')
            append_attempt(
                item,
                source_type='lead_bind_failed',
                source_id=str(item.get('lead_id') or item.get('item_id') or '').strip(),
                lead_id=str(item.get('lead_id') or item.get('item_id') or '').strip(),
                task_id=str(item.get('task_id') or '').strip(),
                current_truth=None,
            )
        return attempt_rows

    def _refresh_binding_history_projection(self, conn: sqlite3.Connection) -> None:
        self._ensure_binding_history_projection_tables(conn)
        attempt_rows = self._build_binding_history_projection_attempt_rows(conn)
        conn.execute("DELETE FROM ops_intake_binding_history_attempts")
        if attempt_rows:
            conn.executemany(
                """
                INSERT INTO ops_intake_binding_history_attempts (
                    attempt_id, source_type, source_id, item_id, lead_id, task_id, dedupe_key,
                    normalized_phone, normalized_phone_digits, created_date_bj, guild_name,
                    submitted_by_user_id, submitted_by_username, external_customer_service_id,
                    external_customer_service_name, display_initiator, parsed_phone,
                    parsed_account_id, parsed_group, parsed_code, parsed_app, parsed_agency,
                    system_status, feedback_status, result_code, result_reason, created_at,
                    processed_at, closure_status, closure_reason, closure_note, current_exception,
                    is_failure, is_duplicate, is_success, is_closed, current_truth_json
                ) VALUES (
                    :attempt_id, :source_type, :source_id, :item_id, :lead_id, :task_id, :dedupe_key,
                    :normalized_phone, :normalized_phone_digits, :created_date_bj, :guild_name,
                    :submitted_by_user_id, :submitted_by_username, :external_customer_service_id,
                    :external_customer_service_name, :display_initiator, :parsed_phone,
                    :parsed_account_id, :parsed_group, :parsed_code, :parsed_app, :parsed_agency,
                    :system_status, :feedback_status, :result_code, :result_reason, :created_at,
                    :processed_at, :closure_status, :closure_reason, :closure_note, :current_exception,
                    :is_failure, :is_duplicate, :is_success, :is_closed, :current_truth_json
                )
                """,
                attempt_rows,
            )
        signature = self._binding_history_projection_signature(conn)
        conn.execute(
            """
            INSERT INTO ops_intake_binding_history_projection_meta (projection_key, signature, refreshed_at)
            VALUES ('default', ?, ?)
            ON CONFLICT(projection_key) DO UPDATE SET signature=excluded.signature, refreshed_at=excluded.refreshed_at
            """,
            (signature, utc_now()),
        )

    def _ensure_binding_history_projection_current(self, conn: sqlite3.Connection) -> None:
        self._ensure_ops_intake_bind_failed_clears_table(conn)
        self._ensure_binding_history_projection_tables(conn)
        current_signature = self._binding_history_projection_signature(conn)
        meta = conn.execute(
            "SELECT signature FROM ops_intake_binding_history_projection_meta WHERE projection_key='default' LIMIT 1"
        ).fetchone()
        stored_signature = str(meta['signature'] or '').strip() if meta else ''
        row_count = conn.execute("SELECT COUNT(*) FROM ops_intake_binding_history_attempts").fetchone()[0]
        if stored_signature != current_signature or not row_count:
            self._refresh_binding_history_projection(conn)

    def list_ops_intake_binding_history_items(
        self,
        *,
        user: Optional[Dict[str, Any]],
        limit: int = 100,
        offset: int = 0,
        guild_name: Optional[str] = None,
        date: Optional[str] = None,
        submitted_by: Optional[str] = None,
        view: str = 'all',
        q: Optional[str] = None,
        status: Optional[str] = None,
    ) -> Dict[str, Any]:
        visible_guild_names = self._ops_intake_visible_guild_names(user=user)
        visible_guilds = set(visible_guild_names)
        role = str((user or {}).get('role') or '').strip().lower()
        is_admin_role = role in {OPS_AUTH_ROLE_SUPER_ADMIN, OPS_AUTH_ROLE_ADMIN, OPS_AUTH_ROLE_INTERNAL}
        requested_guild = str(guild_name or '').strip()
        requested_date = str(date or '').strip()
        requested_operator = str(submitted_by or '').strip()
        requested_query = str(q or '').strip()
        requested_status = str(status or '').strip().lower()
        requested_view = str(view or 'all').strip().lower()
        if requested_view not in {'all', 'current'}:
            requested_view = 'all'
        normalized_limit = max(1, min(int(limit or 100), 200))
        normalized_offset = max(0, int(offset or 0))

        base_conditions: List[str] = []
        base_params: List[Any] = []
        if requested_guild:
            if not is_admin_role and requested_guild not in visible_guilds:
                return {
                    'rows': [],
                    'summary': {'history_count': 0, 'submission_count': 0, 'duplicate_group_count': 0, 'current_exception_count': 0, 'view': requested_view},
                    'pagination': {'limit': normalized_limit, 'offset': normalized_offset, 'has_more': False, 'total_count': 0},
                    'filter_options': {'guild_names': visible_guild_names},
                }
            base_conditions.append('guild_name = ?')
            base_params.append(requested_guild)
        elif visible_guilds and not is_admin_role:
            placeholders = ','.join('?' for _ in visible_guilds)
            base_conditions.append(f'guild_name IN ({placeholders})')
            base_params.extend(sorted(visible_guilds))
        elif not visible_guilds and not is_admin_role:
            return {
                'rows': [],
                'summary': {'history_count': 0, 'submission_count': 0, 'duplicate_group_count': 0, 'current_exception_count': 0, 'view': requested_view},
                'pagination': {'limit': normalized_limit, 'offset': normalized_offset, 'has_more': False, 'total_count': 0},
                'filter_options': {'guild_names': visible_guild_names},
            }
        if requested_date:
            base_conditions.append('created_date_bj = ?')
            base_params.append(requested_date)
        if requested_operator:
            base_conditions.append('(submitted_by_user_id = ? OR submitted_by_username = ? OR external_customer_service_id = ? OR external_customer_service_name = ? OR display_initiator = ?)')
            base_params.extend([requested_operator, requested_operator, requested_operator, requested_operator, requested_operator])
        if requested_query:
            like_query = f"%{requested_query.lower()}%"
            digits_query = ''.join(ch for ch in requested_query if ch.isdigit())
            query_clauses = [
                'LOWER(parsed_phone) LIKE ?',
                'LOWER(parsed_account_id) LIKE ?',
                'LOWER(parsed_group) LIKE ?',
                'LOWER(parsed_code) LIKE ?',
                'LOWER(guild_name) LIKE ?',
                'LOWER(submitted_by_username) LIKE ?',
                'LOWER(external_customer_service_id) LIKE ?',
                'LOWER(external_customer_service_name) LIKE ?',
                'LOWER(display_initiator) LIKE ?',
                'LOWER(result_code) LIKE ?',
                'LOWER(result_reason) LIKE ?',
            ]
            base_params.extend([like_query] * len(query_clauses))
            if digits_query:
                query_clauses.append('normalized_phone_digits LIKE ?')
                base_params.append(f'%{digits_query}%')
            base_conditions.append('(' + ' OR '.join(query_clauses) + ')')
        base_where = (' WHERE ' + ' AND '.join(base_conditions)) if base_conditions else ''

        matched_conditions: List[str] = []
        if requested_view == 'current':
            matched_conditions.append('current_exception = 1')
        if requested_status and requested_status != 'all':
            if requested_status == 'duplicate':
                matched_conditions.append('is_duplicate = 1')
            elif requested_status == 'success':
                matched_conditions.append('is_success = 1')
            elif requested_status == 'closed':
                matched_conditions.append('is_closed = 1')
            elif requested_status == 'exception':
                matched_conditions.append('current_exception = 1')
            elif requested_status == 'failed':
                matched_conditions.append('is_failure = 1')
        matched_where = (' WHERE ' + ' AND '.join(matched_conditions)) if matched_conditions else ''

        base_cte = f"""
            WITH filtered AS (
                SELECT *
                FROM ops_intake_binding_history_attempts
                {base_where}
            ),
            ranked AS (
                SELECT
                    filtered.*,
                    ROW_NUMBER() OVER (PARTITION BY dedupe_key ORDER BY created_at DESC, attempt_id DESC) AS rn,
                    COUNT(*) OVER (PARTITION BY dedupe_key) AS attempt_count,
                    SUM(is_failure) OVER (PARTITION BY dedupe_key) AS failure_attempt_count
                FROM filtered
            ),
            latest AS (
                SELECT *
                FROM ranked
                WHERE rn = 1
            ),
            matched AS (
                SELECT *
                FROM latest
                {matched_where}
            )
        """

        with self.db.connect() as conn:
            self._ensure_binding_history_projection_current(conn)
            summary_row = conn.execute(
                base_cte + """
                SELECT
                    COUNT(*) AS history_count,
                    COALESCE(SUM(attempt_count), 0) AS submission_count,
                    COALESCE(SUM(CASE WHEN attempt_count > 1 THEN 1 ELSE 0 END), 0) AS duplicate_group_count,
                    COALESCE(SUM(CASE WHEN current_exception = 1 THEN 1 ELSE 0 END), 0) AS current_exception_count
                FROM matched
                """,
                tuple(base_params),
            ).fetchone()
            page_rows = [dict(r) for r in conn.execute(
                base_cte + """
                SELECT *
                FROM matched
                ORDER BY created_at DESC, dedupe_key DESC
                LIMIT ? OFFSET ?
                """,
                (*base_params, normalized_limit, normalized_offset),
            ).fetchall()]

            dedupe_keys = [str(row.get('dedupe_key') or '').strip() for row in page_rows if str(row.get('dedupe_key') or '').strip()]
            attempts_by_key: Dict[str, List[Dict[str, Any]]] = {key: [] for key in dedupe_keys}
            if dedupe_keys:
                placeholders = ','.join('?' for _ in dedupe_keys)
                attempt_rows = [dict(r) for r in conn.execute(
                    f"""
                    WITH filtered AS (
                        SELECT *
                        FROM ops_intake_binding_history_attempts
                        {base_where}
                    )
                    SELECT *
                    FROM filtered
                    WHERE dedupe_key IN ({placeholders})
                    ORDER BY dedupe_key, created_at DESC, attempt_id DESC
                    """,
                    (*base_params, *dedupe_keys),
                ).fetchall()]
                for attempt in attempt_rows:
                    attempts_by_key.setdefault(str(attempt.get('dedupe_key') or '').strip(), []).append({
                        'item_id': str(attempt.get('item_id') or '').strip(),
                        'lead_id': str(attempt.get('lead_id') or '').strip(),
                        'task_id': str(attempt.get('task_id') or '').strip(),
                        'source_type': str(attempt.get('source_type') or '').strip(),
                        'guild_name': str(attempt.get('guild_name') or '').strip(),
                        'submitted_by_user_id': str(attempt.get('submitted_by_user_id') or '').strip(),
                        'submitted_by_username': str(attempt.get('submitted_by_username') or '').strip(),
                        'external_customer_service_id': str(attempt.get('external_customer_service_id') or '').strip(),
                        'external_customer_service_name': str(attempt.get('external_customer_service_name') or '').strip(),
                        'display_initiator': str(attempt.get('display_initiator') or '').strip(),
                        'parsed_phone': str(attempt.get('parsed_phone') or '').strip(),
                        'parsed_account_id': str(attempt.get('parsed_account_id') or '').strip(),
                        'system_status': str(attempt.get('system_status') or '').strip(),
                        'feedback_status': str(attempt.get('feedback_status') or '').strip(),
                        'result_code': str(attempt.get('result_code') or '').strip(),
                        'result_reason': str(attempt.get('result_reason') or '').strip(),
                        'created_at': str(attempt.get('created_at') or '').strip(),
                        'processed_at': str(attempt.get('processed_at') or '').strip(),
                        'parsed_group': str(attempt.get('parsed_group') or '').strip(),
                        'parsed_code': str(attempt.get('parsed_code') or '').strip(),
                        'parsed_app': str(attempt.get('parsed_app') or '').strip(),
                        'parsed_agency': str(attempt.get('parsed_agency') or '').strip(),
                        'closure_status': str(attempt.get('closure_status') or '').strip(),
                        'closure_reason': str(attempt.get('closure_reason') or '').strip(),
                        'closure_note': str(attempt.get('closure_note') or '').strip(),
                        'current_exception': bool(int(attempt.get('current_exception') or 0)),
                        'current_truth_json': str(attempt.get('current_truth_json') or '').strip(),
                    })

        rows: List[Dict[str, Any]] = []
        for row in page_rows:
            dedupe_key = str(row.get('dedupe_key') or '').strip()
            attempts = attempts_by_key.get(dedupe_key, [])
            authoritative_row, row_stale, stale_reason = self._binding_history_authoritative_attempt(row, attempts)
            built = {
                'item_id': str(authoritative_row.get('item_id') or '').strip(),
                'lead_id': str(authoritative_row.get('lead_id') or '').strip(),
                'task_id': str(authoritative_row.get('task_id') or '').strip(),
                'source_type': 'ops_intake_history',
                'guild_name': str(authoritative_row.get('guild_name') or '').strip(),
                'submitted_by_user_id': str(authoritative_row.get('submitted_by_user_id') or '').strip(),
                'submitted_by_username': str(authoritative_row.get('submitted_by_username') or '').strip(),
                'external_customer_service_id': str(authoritative_row.get('external_customer_service_id') or '').strip(),
                'external_customer_service_name': str(authoritative_row.get('external_customer_service_name') or '').strip(),
                'display_initiator': str(authoritative_row.get('display_initiator') or '').strip(),
                'parsed_phone': str(authoritative_row.get('parsed_phone') or '').strip(),
                'parsed_account_id': str(authoritative_row.get('parsed_account_id') or '').strip(),
                'parsed_group': str(authoritative_row.get('parsed_group') or '').strip(),
                'parsed_code': str(authoritative_row.get('parsed_code') or '').strip(),
                'parsed_app': str(authoritative_row.get('parsed_app') or '').strip(),
                'parsed_agency': str(authoritative_row.get('parsed_agency') or '').strip(),
                'system_status': str(authoritative_row.get('system_status') or '').strip(),
                'feedback_status': str(authoritative_row.get('feedback_status') or '').strip(),
                'result_code': str(authoritative_row.get('result_code') or '').strip(),
                'result_reason': str(authoritative_row.get('result_reason') or '').strip(),
                'created_at': str(authoritative_row.get('created_at') or '').strip(),
                'processed_at': str(authoritative_row.get('processed_at') or '').strip(),
                'dedupe_key': dedupe_key,
                'normalized_phone': str(authoritative_row.get('normalized_phone') or row.get('normalized_phone') or '').strip(),
                'latest_result_code': str(row.get('result_code') or '').strip(),
                'latest_result_reason': str(row.get('result_reason') or '').strip(),
                'current_exception': bool(int(authoritative_row.get('current_exception') or 0)),
                'closure_status': str(authoritative_row.get('closure_status') or '').strip(),
                'closure_reason': str(authoritative_row.get('closure_reason') or '').strip(),
                'closure_note': str(authoritative_row.get('closure_note') or '').strip(),
                'attempt_count': int(row.get('attempt_count') or 0),
                'failure_attempt_count': int(row.get('failure_attempt_count') or 0),
                'editable_fields': self._ops_intake_item_editable_fields({
                    'parsed_phone': authoritative_row.get('parsed_phone'),
                    'parsed_account_id': authoritative_row.get('parsed_account_id'),
                    'parsed_group': authoritative_row.get('parsed_group'),
                    'parsed_code': authoritative_row.get('parsed_code'),
                    'parsed_app': authoritative_row.get('parsed_app'),
                    'parsed_agency': authoritative_row.get('parsed_agency'),
                }),
                'attempts': attempts,
                'stale': row_stale,
                'verified_at': str(authoritative_row.get('processed_at') or authoritative_row.get('created_at') or '').strip(),
                'display_count': 1,
            }
            if row_stale:
                built['stale_reason'] = stale_reason
                built['stale_source_item_id'] = str(row.get('item_id') or '').strip()
            truth = self._binding_history_parse_current_truth_payload(authoritative_row.get('current_truth_json'))
            if truth:
                built['current_truth'] = truth
            rows.append(built)

        history_count = int(summary_row['history_count'] or 0) if summary_row else 0
        submission_count = int(summary_row['submission_count'] or 0) if summary_row else 0
        duplicate_group_count = int(summary_row['duplicate_group_count'] or 0) if summary_row else 0
        current_exception_count = int(summary_row['current_exception_count'] or 0) if summary_row else 0
        summary = {
            'history_count': history_count,
            'submission_count': submission_count,
            'duplicate_group_count': duplicate_group_count,
            'current_exception_count': current_exception_count,
            'view': requested_view,
        }
        pagination = {
            'limit': normalized_limit,
            'offset': normalized_offset,
            'has_more': normalized_offset + len(rows) < history_count,
            'total_count': history_count,
        }
        finalized_snapshot = self._binding_history_response_snapshot(rows=rows, summary=summary, pagination=pagination)
        return {
            'rows': rows,
            'current_truth': finalized_snapshot,
            'finalized_snapshot': finalized_snapshot,
            'summary': summary,
            'pagination': pagination,
            'filter_options': {
                'guild_names': visible_guild_names,
            },
        }

    def list_external_fan_conversions(
        self,
        *,
        updated_since: str = '',
        limit: int = 500,
        offset: int = 0,
    ) -> Dict[str, Any]:
        normalized_since = str(updated_since or '').strip()
        if normalized_since:
            try:
                normalized_since = parse_iso_datetime(normalized_since).astimezone(
                    timezone.utc
                ).isoformat()
            except Exception as exc:
                raise HTTPException(status_code=400, detail='invalid_updated_since') from exc
        normalized_limit = max(1, min(int(limit or 500), 1000))
        normalized_offset = max(0, int(offset or 0))
        updated_expression = "COALESCE(NULLIF(processed_at, ''), created_at)"
        duplicate_expression = """
            LOWER(COALESCE(result_code, '')) LIKE '%duplicate%'
            OR LOWER(COALESCE(result_reason, '')) LIKE '%data duplication%'
            OR LOWER(COALESCE(result_reason, '')) LIKE '%duplicate_sid%'
            OR LOWER(COALESCE(result_reason, '')) LIKE '%sid already exists%'
        """
        conditions = [
            "LOWER(TRIM(system_status)) IN ('fully_success','success')",
            "TRIM(COALESCE(item_id, '')) != ''",
            "TRIM(COALESCE(parsed_phone, '')) != ''",
            "TRIM(COALESCE(parsed_account_id, '')) != ''",
            "LOWER(TRIM(COALESCE(parsed_app, ''))) IN ('linky','timo')",
            f'NOT ({duplicate_expression})',
        ]
        params: List[Any] = []
        if normalized_since:
            conditions.append(f'julianday({updated_expression}) >= julianday(?)')
            params.append(normalized_since)
        where_clause = ' AND '.join(conditions)
        with self.db.connect() as conn:
            total_row = conn.execute(
                f'SELECT COUNT(*) AS total FROM ops_intake_items WHERE {where_clause}',
                tuple(params),
            ).fetchone()
            source_rows = [dict(row) for row in conn.execute(
                f"""
                SELECT item_id,guild_name,submitted_by_user_id,submitted_by_username,
                       external_customer_service_id,external_customer_service_name,
                       parsed_phone,parsed_account_id,parsed_app,parsed_agency,
                       created_at,processed_at,{updated_expression} AS source_updated_at
                FROM ops_intake_items
                WHERE {where_clause}
                ORDER BY julianday({updated_expression}) ASC,item_id ASC
                LIMIT ? OFFSET ?
                """,
                (*params, normalized_limit, normalized_offset),
            ).fetchall()]
        rows = []
        for row in source_rows:
            normalized_phone, _ = self._normalize_binding_history_phone_keys(
                str(row.get('parsed_phone') or '')
            )
            item_id = str(row.get('item_id') or '').strip()
            operator_name = str(
                row.get('external_customer_service_name')
                or row.get('submitted_by_username')
                or row.get('external_customer_service_id')
                or row.get('submitted_by_user_id')
                or ''
            ).strip()
            operator_account_key = str(
                row.get('external_customer_service_id')
                or row.get('submitted_by_user_id')
                or row.get('submitted_by_username')
                or ''
            ).strip()
            source_record_key = f'ops_intake_item:{item_id}'
            observed_at = str(row.get('processed_at') or row.get('created_at') or '').strip()
            rows.append({
                'sourceRecordKey': source_record_key,
                'idempotencyKey': source_record_key,
                'platform': str(row.get('parsed_app') or '').strip().upper(),
                'subjectId': str(row.get('parsed_account_id') or '').strip(),
                'whatsappId': normalized_phone,
                'operatorName': operator_name,
                'operatorAccountKey': operator_account_key,
                'guildName': str(
                    row.get('guild_name') or row.get('parsed_agency') or ''
                ).strip(),
                'observedAt': observed_at,
                'sourceUpdatedAt': str(row.get('source_updated_at') or observed_at).strip(),
            })
        total = int(total_row['total'] or 0) if total_row else 0
        return {
            'ok': True,
            'data': {
                'schemaVersion': 1,
                'sourceContract': 'ops_intake_success_v1',
                'updatedSince': normalized_since,
                'total': total,
                'limit': normalized_limit,
                'offset': normalized_offset,
                'hasMore': normalized_offset + len(rows) < total,
                'rows': rows,
            },
        }

    def list_ops_intake_bind_failed_items(
        self,
        *,
        user: Optional[Dict[str, Any]],
        limit: int = 100,
        guild_name: Optional[str] = None,
        date: Optional[str] = None,
        submitted_by: Optional[str] = None,
    ) -> Dict[str, Any]:
        visible_guilds = set(self._ops_intake_visible_guild_names(user=user))
        role = str((user or {}).get('role') or '').strip().lower()
        is_admin_role = role in {OPS_AUTH_ROLE_SUPER_ADMIN, OPS_AUTH_ROLE_ADMIN, OPS_AUTH_ROLE_INTERNAL}
        requested_guild = str(guild_name or '').strip()
        requested_date = str(date or '').strip()
        requested_operator = str(submitted_by or '').strip()
        max_limit = max(1, min(int(limit or 50), 500))
        fetch_limit = max_limit
        params: List[Any] = []
        conditions = ["system_status IN ('failed', 'crm_failed', 'bind_failed', 'partial_success_crm_failed')", "COALESCE(feedback_status, '') != 'cleared'", "NOT EXISTS (SELECT 1 FROM ops_intake_bind_failed_clears c WHERE c.source_type='ops_intake_item' AND c.source_id=ops_intake_items.item_id)"]
        if requested_date:
            start_dt = datetime.fromisoformat(requested_date).replace(tzinfo=timezone(timedelta(hours=8))).astimezone(timezone.utc)
            end_dt = start_dt + timedelta(days=1)
            conditions.append('created_at >= ? AND created_at < ?')
            params.extend([start_dt.isoformat(), end_dt.isoformat()])
        if requested_operator:
            conditions.append('(submitted_by_user_id = ? OR submitted_by_username = ? OR external_customer_service_id = ? OR external_customer_service_name = ?)')
            params.extend([requested_operator, requested_operator, requested_operator, requested_operator])
        if requested_guild:
            if not is_admin_role and requested_guild not in visible_guilds:
                return {'rows': [], 'summary': {'bind_failed_count': 0}}
            conditions.append('guild_name = ?')
            params.append(requested_guild)
        elif visible_guilds and not is_admin_role:
            placeholders = ','.join('?' for _ in visible_guilds)
            conditions.append(f'guild_name IN ({placeholders})')
            params.extend(sorted(visible_guilds))
        elif not visible_guilds and not is_admin_role:
            return {'rows': [], 'summary': {'bind_failed_count': 0}}
        where = ' WHERE ' + ' AND '.join(conditions)
        with self.db.connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS ops_intake_bind_failed_clears (
                    clear_id TEXT PRIMARY KEY,
                    source_type TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    cleared_by TEXT,
                    cleared_at TEXT NOT NULL,
                    UNIQUE(source_type, source_id)
                )
            """)
            ops_rows = [dict(r) for r in conn.execute(
                f'SELECT * FROM ops_intake_items{where} ORDER BY created_at DESC LIMIT ?',
                (*params, fetch_limit),
            ).fetchall()]
            lead_params: List[Any] = []
            lead_conditions = ["l.current_status = 'bind_failed'", "NOT EXISTS (SELECT 1 FROM ops_intake_bind_failed_clears c WHERE c.source_type IN ('lead', 'lead_bind_failed') AND c.source_id=l.lead_id)"]
            if requested_date:
                lead_conditions.append('COALESCE(t.finished_at, l.updated_at, l.created_at) >= ? AND COALESCE(t.finished_at, l.updated_at, l.created_at) < ?')
                lead_params.extend([start_dt.isoformat(), end_dt.isoformat()])
            if requested_operator:
                lead_conditions.append('(t.created_by = ?)')
                lead_params.append(requested_operator)
            if requested_guild:
                if not is_admin_role and requested_guild not in visible_guilds:
                    return {'rows': [], 'summary': {'bind_failed_count': 0}}
                lead_conditions.append("COALESCE(l.dept_name, '') = ?")
                lead_params.append(requested_guild)
            elif visible_guilds and not is_admin_role:
                placeholders = ','.join('?' for _ in visible_guilds)
                lead_conditions.append(f'COALESCE(l.dept_name, \'\') IN ({placeholders})')
                lead_params.extend(sorted(visible_guilds))
            lead_where = ' WHERE ' + ' AND '.join(lead_conditions)
            lead_rows = [dict(r) for r in conn.execute(
                f"""
                SELECT l.*, t.task_id, t.payload AS task_payload, t.result_code, t.result_reason, t.raw_result,
                       t.created_at AS task_created_at, t.finished_at, t.created_by AS submitted_by_username
                FROM leads l
                LEFT JOIN automation_tasks t ON t.task_id = (
                    SELECT t2.task_id FROM automation_tasks t2
                    WHERE t2.lead_id = l.lead_id AND t2.task_type = 'bind_check'
                    ORDER BY COALESCE(t2.finished_at, t2.created_at) DESC LIMIT 1
                )
                {lead_where}
                ORDER BY COALESCE(t.finished_at, l.updated_at, l.created_at) DESC
                LIMIT ?
                """,
                (*lead_params, fetch_limit),
            ).fetchall()]
        rows: List[Dict[str, Any]] = []
        for row in ops_rows:
            row['source_type'] = 'ops_intake_item'
            row['editable_fields'] = self._ops_intake_item_editable_fields(row)
            rows.append(row)
        existing_item_ids = {str(row.get('item_id') or '') for row in rows}
        for lead_row in lead_rows:
            item = self._ops_intake_bind_failed_lead_item_from_row(lead_row)
            if item['item_id'] not in existing_item_ids:
                rows.append(item)

        beijing_tz = timezone(timedelta(hours=8))

        def beijing_day(value: Any) -> str:
            raw = str(value or '').strip()
            if not raw:
                return ''
            try:
                dt = datetime.fromisoformat(raw.replace('Z', '+00:00'))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(beijing_tz).date().isoformat()
            except Exception:
                return raw[:10]

        def operator_matches(row: Dict[str, Any]) -> bool:
            if not requested_operator:
                return True
            candidates = {
                str(row.get('submitted_by_user_id') or '').strip(),
                str(row.get('submitted_by_username') or '').strip(),
                str(row.get('external_customer_service_id') or '').strip(),
                str(row.get('external_customer_service_name') or '').strip(),
                str(row.get('submitted_by') or '').strip(),
                str(row.get('created_by') or '').strip(),
            }
            return requested_operator in candidates

        if requested_date:
            rows = [row for row in rows if beijing_day(row.get('created_at')) == requested_date]
        if requested_operator:
            rows = [row for row in rows if operator_matches(row)]
        rows.sort(key=lambda r: str(r.get('created_at') or ''), reverse=True)
        rows = rows[:max_limit]
        return {'rows': rows, 'summary': {'bind_failed_count': len(rows)}}

    def clear_ops_intake_bind_failed_items(
        self,
        *,
        user: Optional[Dict[str, Any]],
        guild_name: Optional[str] = None,
        date: Optional[str] = None,
        submitted_by: Optional[str] = None,
        item_ids: Optional[list[str]] = None,
        limit: int = 500,
    ) -> Dict[str, Any]:
        role = str((user or {}).get('role') or '').strip().lower()
        if role not in {OPS_AUTH_ROLE_SUPER_ADMIN, OPS_AUTH_ROLE_ADMIN, OPS_AUTH_ROLE_INTERNAL}:
            raise HTTPException(status_code=403, detail='ops_admin_required')
        payload = self.list_ops_intake_bind_failed_items(
            user=user,
            limit=max(1, min(int(limit or 500), 2000)),
            guild_name=guild_name,
            date=date,
            submitted_by=submitted_by,
        )
        rows = list(payload.get('rows') or [])
        selected_ids = {str(item_id or '').strip() for item_id in (item_ids or []) if str(item_id or '').strip()}
        if selected_ids:
            rows = [row for row in rows if str(row.get('item_id') or row.get('lead_id') or '').strip() in selected_ids]
        now = utc_now()
        cleared_by = str((user or {}).get('display_name') or (user or {}).get('username') or (user or {}).get('user_id') or 'ops_admin').strip()
        cleared_count = 0
        with self.db.connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS ops_intake_bind_failed_clears (
                    clear_id TEXT PRIMARY KEY,
                    source_type TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    cleared_by TEXT,
                    cleared_at TEXT NOT NULL,
                    UNIQUE(source_type, source_id)
                )
            """)
            for row in rows:
                source_type = str(row.get('source_type') or 'ops_intake_item').strip()
                source_id = str(row.get('lead_id') if source_type in {'lead', 'lead_bind_failed'} else row.get('item_id') or '').strip()
                clear_source_type = 'lead' if source_type in {'lead', 'lead_bind_failed'} else source_type
                if not source_id:
                    continue
                conn.execute(
                    "INSERT OR IGNORE INTO ops_intake_bind_failed_clears (clear_id, source_type, source_id, cleared_by, cleared_at) VALUES (?, ?, ?, ?, ?)",
                    (create_id('bfclr'), clear_source_type, source_id, cleared_by, now),
                )
                if clear_source_type == 'ops_intake_item':
                    conn.execute(
                        "UPDATE ops_intake_items SET feedback_status='cleared', processed_at=? WHERE item_id=?",
                        (now, source_id),
                    )
                cleared_count += 1
            conn.commit()
        return {'ok': True, 'cleared_count': cleared_count, 'cleared_at': now}

    def resolve_ops_intake_history_item(self, *, item_id: str, action: str, reason: str, note: Optional[str], user: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        source_id = str(item_id or '').strip()
        if not source_id:
            raise HTTPException(status_code=400, detail='missing_item_id')
        action_key = str(action or 'resolved').strip().lower()
        allowed_actions = {'resolved', 'ignored', 'no_followup', 'duplicate_closed', 'manual_review'}
        if action_key not in allowed_actions:
            raise HTTPException(status_code=400, detail='invalid_resolution_action')
        reason_text = str(reason or '').strip()
        if not reason_text:
            raise HTTPException(status_code=400, detail='resolution_reason_required')
        note_text = str(note or '').strip()
        actor = str((user or {}).get('display_name') or (user or {}).get('username') or (user or {}).get('user_id') or 'ops_user').strip()
        now = utc_now()
        with self.db.connect() as conn:
            self._ensure_ops_intake_bind_failed_clears_table(conn)
            ops_row = conn.execute("SELECT * FROM ops_intake_items WHERE item_id=?", (source_id,)).fetchone()
            lead_row = None if ops_row else conn.execute("SELECT * FROM leads WHERE lead_id=?", (source_id,)).fetchone()
            if not ops_row and not lead_row:
                raise HTTPException(status_code=404, detail='ops_intake_history_item_not_found')
            if ops_row:
                row = dict(ops_row)
                if not self._ops_intake_user_can_access_guild(user, str(row.get('guild_name') or '')):
                    raise HTTPException(status_code=403, detail='ops_guild_intake_forbidden')
                feedback_status = 'manual_review' if action_key == 'manual_review' else action_key
                system_status = 'manual_required' if action_key == 'manual_review' else str(row.get('system_status') or '')
                conn.execute(
                    "UPDATE ops_intake_items SET feedback_status=?, system_status=?, result_reason=COALESCE(NULLIF(result_reason,''), ?), processed_at=? WHERE item_id=?",
                    (feedback_status, system_status, reason_text, now, source_id),
                )
                source_type = 'ops_intake_item'
            else:
                row = dict(lead_row)
                guild_name = str(row.get('dept_name') or '')
                if not self._ops_intake_user_can_access_guild(user, guild_name):
                    raise HTTPException(status_code=403, detail='ops_guild_intake_forbidden')
                source_type = 'lead'
                if action_key == 'manual_review':
                    conn.execute("UPDATE leads SET current_status='manual_review_pending', updated_at=? WHERE lead_id=?", (now, source_id))
            conn.execute(
                """
                INSERT INTO ops_intake_bind_failed_clears (clear_id, source_type, source_id, cleared_by, cleared_at, action, reason, note)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_type, source_id) DO UPDATE SET
                    cleared_by=excluded.cleared_by,
                    cleared_at=excluded.cleared_at,
                    action=excluded.action,
                    reason=excluded.reason,
                    note=excluded.note
                """,
                (create_id('bfres'), source_type, source_id, actor, now, action_key, reason_text, note_text),
            )
            conn.commit()
        item = self._get_ops_intake_item(source_id) if source_type == 'ops_intake_item' else self._ops_intake_bind_failed_lead_item_from_row(dict(lead_row))
        item['feedback_status'] = 'manual_review' if action_key == 'manual_review' else action_key
        item['closure_status'] = action_key
        item['closure_reason'] = reason_text
        item['closure_note'] = note_text
        return {'ok': True, 'item_id': source_id, 'action': action_key, 'resolved_at': now, 'item': item}

    def update_ops_intake_item_fields(self, *, item_id: str, fields: Optional[Dict[str, Any]], user: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        item = self._get_ops_intake_item(item_id)
        guild_name = str(item.get('guild_name') or '').strip()
        if not self._ops_intake_user_can_access_guild(user, guild_name):
            raise HTTPException(status_code=403, detail='ops_guild_intake_forbidden')
        base_fields = self._ops_intake_item_editable_fields(item)
        requested_fields = {str(k): v for k, v in dict(fields or {}).items() if str(k) in {'phone', 'account_id', 'group', 'code', 'country'}}
        merged_fields = {**base_fields, **requested_fields}
        # 资料更正允许 Phone / ID / Group / Code。处理中记录原位更新并同步待执行任务；终态记录重新提交链路。
        normalized_fields = {
            key: ('' if is_blank_intake_field_value(value) else str(value or '').strip())
            for key, value in merged_fields.items()
        }
        parsed = self.parse_ops_intake_text(guild_name=guild_name, text='', fields=normalized_fields)
        if not parsed.get('can_submit'):
            raise HTTPException(status_code=400, detail={'reason': 'parse_validation_failed', 'errors': parsed.get('errors', []), 'parsed': parsed})
        parsed_fields = parsed.get('fields') or normalized_fields
        phone = str(parsed_fields.get('phone') or '').strip()
        account_id = str(parsed_fields.get('account_id') or '').strip()
        group = str(parsed_fields.get('group') or '').strip()
        raw_code = str(parsed_fields.get('code') or '').strip()
        requested_code = str(normalized_fields.get('code') or '').strip()
        if requested_code and not is_blank_intake_field_value(requested_code):
            requested_code_meta = normalize_invite_code_candidate(requested_code)
            raw_code = str(requested_code_meta.get('normalized') or requested_code).strip().upper() if requested_code_meta.get('is_valid') else requested_code
        code = '' if not parsed.get('code_required') and is_blank_intake_field_value(raw_code) else raw_code
        now = utc_now()
        task_id = ''
        try:
            snapshot = json.loads(str(item.get('result_snapshot') or '{}'))
        except Exception:
            snapshot = {}
        nested = snapshot.get('result') if isinstance(snapshot.get('result'), dict) else {}
        task_id = str(snapshot.get('task_id') or nested.get('task_id') or '').strip()
        active_statuses = {'queued', 'processing', 'bind_queued', 'binding', 'crm_verifying'}
        in_place = str(item.get('system_status') or '') in active_statuses
        if not in_place:
            result = self.resubmit_ops_intake_item(item_id=item_id, text='', fields=parsed_fields, user=user)
            return {'ok': True, 'item_id': str(item_id or '').strip(), 'correction_mode': 'resubmitted_for_crm_sync', 'resubmitted_item': result.get('item'), 'item': self._get_ops_intake_item(item_id)}
        with self.db.connect() as conn:
            conn.execute(
                """
                UPDATE ops_intake_items
                SET parsed_phone=?, parsed_account_id=?, parsed_group=?, parsed_code=?, parsed_app=?, parsed_agency=?,
                    raw_text=?, result_reason=COALESCE(NULLIF(result_reason,''), '资料已更正'), processed_at=?
                WHERE item_id=?
                """,
                (
                    phone,
                    account_id,
                    group,
                    code,
                    str(parsed_fields.get('app') or item.get('parsed_app') or ''),
                    str(parsed_fields.get('agency') or item.get('parsed_agency') or guild_name),
                    '\n'.join([f"Phone: {phone}", f"ID: {account_id}", f"Group: {group}"] + ([f"Code: {code}"] if code else [])),
                    now,
                    str(item_id or '').strip(),
                ),
            )
            if task_id:
                row = conn.execute('SELECT payload FROM automation_tasks WHERE task_id=?', (task_id,)).fetchone()
                if row:
                    try:
                        payload = json.loads(row['payload'] or '{}')
                    except Exception:
                        payload = {}
                    old_values = {
                        str(item.get('parsed_phone') or ''): phone,
                        str(item.get('parsed_account_id') or ''): account_id,
                        str(item.get('parsed_group') or ''): group,
                        str(item.get('parsed_code') or ''): code,
                    }
                    def replace_values(value):
                        if isinstance(value, str):
                            updated = value
                            for old, new in old_values.items():
                                if old and old != new:
                                    updated = updated.replace(old, new)
                            return updated
                        if isinstance(value, list):
                            return [replace_values(v) for v in value]
                        if isinstance(value, dict):
                            return {k: replace_values(v) for k, v in value.items()}
                        return value
                    payload = replace_values(payload)
                    payload.update({'mobile': phone, 'phone': phone, 'account_id': account_id, 'sid': account_id, 'registration_group': group, 'group': group, 'invite_code': code, 'code': code})
                    conn.execute('UPDATE automation_tasks SET payload=? WHERE task_id=?', (json.dumps(payload, ensure_ascii=False), task_id))
            conn.commit()
        return {'ok': True, 'item_id': str(item_id or '').strip(), 'correction_mode': 'in_place', 'item': self._get_ops_intake_item(item_id)}

    def recheck_ops_intake_bind_failed_item(self, *, item_id: str, fields: Optional[Dict[str, Any]], user: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        item = self._get_ops_intake_item(item_id)
        guild_name = str(item.get('guild_name') or '').strip()
        if not self._ops_intake_user_can_access_guild(user, guild_name):
            raise HTTPException(status_code=403, detail='ops_guild_intake_forbidden')
        executor = self.resolve_guild_executor(guild_name) or {}
        if not str(executor.get('platform_authorization') or '').strip():
            raise HTTPException(status_code=400, detail='cms_authorization_missing')
        probe = self.real_bind_executor
        required_methods = ('_cms_find_target_guild', '_cms_query_sid', '_cms_match_target_guild')
        if not probe or not all(hasattr(probe, name) for name in required_methods):
            raise HTTPException(status_code=400, detail='cms_probe_unavailable')
        merged_fields = {**self._ops_intake_item_editable_fields(item), **{str(k): v for k, v in dict(fields or {}).items()}}
        sid = str(merged_fields.get('account_id') or '').strip()
        if not sid or not sid.isdigit():
            raise HTTPException(status_code=400, detail='invalid_sid')
        base_url = str(executor.get('platform_backend_url') or 'https://cms.linke.ai/').strip().rstrip('/') or 'https://cms.linke.ai'
        authorization = str(executor.get('platform_authorization') or '').strip()
        configured_guild_id = str(executor.get('cms_guild_id') or '').strip()
        configured_guild_sid = str(executor.get('cms_guild_sid') or '').strip()
        default_locks = {'carote': ('3432', '43536425'), 'permata': ('413', '25400979'), 'nova': ('1423', '31350499')}
        default_lock = default_locks.get(guild_name.lower())
        if (not configured_guild_id or not configured_guild_sid) and default_lock:
            configured_guild_id = configured_guild_id or default_lock[0]
            configured_guild_sid = configured_guild_sid or default_lock[1]
        if not configured_guild_id or not configured_guild_sid:
            result = {'status': 'failed', 'result_code': 'cms_target_guild_lock_missing', 'result_reason': 'CMS guild ID/SID lock is required'}
        else:
            try:
                timeout_seconds = min(8.0, max(2.0, float(executor.get('request_timeout_seconds') or 8.0)))
                proxy_url = self._resolve_executor_proxy_url(executor)
                guild = probe._cms_find_target_guild(base_url=base_url, authorization=authorization, proxy_url=proxy_url, target_guild=guild_name, configured_guild_id=configured_guild_id, configured_guild_sid=configured_guild_sid, timeout_seconds=timeout_seconds)
                rows = probe._cms_query_sid(base_url=base_url, authorization=authorization, proxy_url=proxy_url, sid=sid, timeout_seconds=timeout_seconds)
                match = probe._cms_match_target_guild(rows, guild)
                safe_rows = [{k: row.get(k) for k in ('sid', 'user_id', 'guild_id', 'guild_name', 'nickname', 'admin_name')} for row in rows[:3]]
                if not rows:
                    result = {'status': 'failed', 'result_code': 'cms_sid_not_found', 'result_reason': 'SID not found in current CMS probe', 'cms_rows': safe_rows}
                elif match == 'target':
                    result = {'status': 'success', 'result_code': 'cms_recheck_already_in_target_guild', 'result_reason': 'CMS now verifies SID in target guild; resubmit to continue CRM verification', 'cms_rows': safe_rows, 'next_action': 'resubmit_for_crm'}
                elif match == 'other':
                    result = {'status': 'failed', 'result_code': 'already_in_other_guild', 'result_reason': 'The streamer was in another agency', 'cms_rows': safe_rows}
                else:
                    result = {'status': 'pending', 'result_code': 'cms_recheck_sid_found_without_guild', 'result_reason': 'SID exists without guild; resubmit can retry CMS bind', 'cms_rows': safe_rows, 'next_action': 'resubmit_for_bind'}
            except Exception as exc:
                result = {'status': 'failed', 'result_code': 'cms_recheck_failed', 'result_reason': str(exc)}
        now = utc_now()
        with self.db.connect() as conn:
            conn.execute("UPDATE ops_intake_items SET result_code=?, result_reason=?, result_snapshot=?, processed_at=? WHERE item_id=?", (str(result.get('result_code') or ''), str(result.get('result_reason') or ''), json.dumps({'recheck': result}, ensure_ascii=False, default=str), now, str(item_id or '').strip()))
            conn.commit()
        return {'ok': True, 'item_id': str(item_id or '').strip(), 'recheck': result, 'item': self._get_ops_intake_item(item_id)}

    def resubmit_ops_intake_item(self, *, item_id: str, text: str, fields: Optional[Dict[str, Any]], user: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        source = self._get_ops_intake_item(item_id)
        guild_name = str(source.get('guild_name') or '').strip()
        if not self._ops_intake_user_can_access_guild(user, guild_name):
            raise HTTPException(status_code=403, detail='ops_guild_intake_forbidden')
        base_fields = self._ops_intake_item_editable_fields(source)
        merged_fields = {**base_fields, **{str(k): v for k, v in dict(fields or {}).items()}}
        normalized_fields = {
            key: ('' if is_blank_intake_field_value(value) else str(value or '').strip())
            for key, value in merged_fields.items()
        }
        submit_lines = [
            f"Phone: {normalized_fields.get('phone') or ''}",
            f"ID: {normalized_fields.get('account_id') or ''}",
            f"Group: {normalized_fields.get('group') or OTHER_CHANNEL_REGISTRATION_GROUP}",
        ]
        if normalized_fields.get('country'):
            submit_lines.append(f"Country: {normalized_fields.get('country')}")
        if normalized_fields.get('code'):
            submit_lines.append(f"Code: {normalized_fields.get('code')}")
        submit_text = str(text or '').strip() or '\n'.join(submit_lines)
        submitted_by = str((user or {}).get('username') or (user or {}).get('display_name') or (user or {}).get('user_id') or 'ops_user').strip()
        result = self.submit_ops_intake_text(
            text=submit_text,
            profile_name=None,
            submitted_by=submitted_by,
            default_app_override=normalized_fields.get('app') or None,
            default_dept_override=normalized_fields.get('agency') or guild_name or None,
        )
        system_status = self._classify_ops_intake_result_status(result)
        now = utc_now()
        new_item_id = create_id('intake_item')
        reply_text = str(result.get('reply_text') or self._format_lark_reply_text(result) or '')
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO ops_intake_items (
                    item_id, guild_name, submitted_by_user_id, submitted_by_username, raw_text,
                    parsed_phone, parsed_account_id, parsed_group, parsed_code, parsed_app, parsed_agency,
                    system_status, feedback_status, reply_text, result_code, result_reason, result_snapshot,
                    created_at, processed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_item_id,
                    guild_name,
                    str((user or {}).get('user_id') or ''),
                    submitted_by,
                    submit_text,
                    str(normalized_fields.get('phone') or ''),
                    str(normalized_fields.get('account_id') or result.get('reply_id') or ''),
                    str(normalized_fields.get('group') or result.get('reply_group') or ''),
                    str(normalized_fields.get('code') or result.get('reply_code') or ''),
                    str(normalized_fields.get('app') or ''),
                    str(normalized_fields.get('agency') or guild_name),
                    system_status,
                    'pending_feedback',
                    reply_text,
                    str(result.get('result_code') or result.get('reason') or ''),
                    str(result.get('result_reason') or result.get('reason') or ''),
                    json.dumps({'source_item_id': str(item_id or '').strip(), 'result': result}, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            conn.commit()
        return {'ok': True, 'source_item_id': str(item_id or '').strip(), 'item': self._get_ops_intake_item(new_item_id), 'result': result}

    def clear_ops_intake_item_card(self, *, item_id: str, user: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        item = self._get_ops_intake_item(item_id)
        if not self._ops_intake_user_can_access_guild(user, str(item.get('guild_name') or '')):
            raise HTTPException(status_code=403, detail='ops_guild_intake_forbidden')
        if str(item.get('feedback_status') or '') == 'cleared':
            return {'ok': True, 'item': item}
        if str(item.get('system_status') or '') == 'fully_success' and str(item.get('feedback_status') or '') == 'pending_feedback':
            raise HTTPException(status_code=400, detail='successful_item_requires_feedback_done')
        now = utc_now()
        done_by = str((user or {}).get('display_name') or (user or {}).get('username') or (user or {}).get('user_id') or 'ops_user').strip()
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE ops_intake_items SET feedback_status='cleared', feedback_done_at = COALESCE(feedback_done_at, ?), feedback_done_by = COALESCE(feedback_done_by, ?) WHERE item_id = ?",
                (now, done_by, str(item_id or '').strip()),
            )
            conn.commit()
        return {'ok': True, 'item': self._get_ops_intake_item(item_id)}

    def mark_ops_intake_template_copied(self, *, item_id: str, user: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        item = self._get_ops_intake_item(item_id)
        if not self._ops_intake_user_can_access_guild(user, str(item.get('guild_name') or '')):
            raise HTTPException(status_code=403, detail='ops_guild_intake_forbidden')
        now = utc_now()
        copied_by = str((user or {}).get('display_name') or (user or {}).get('username') or (user or {}).get('user_id') or 'ops_user').strip()
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE ops_intake_items SET template_copied_at = ?, template_copied_by = ? WHERE item_id = ?",
                (now, copied_by, str(item_id or '').strip()),
            )
            conn.commit()
        return {'ok': True, 'item': self._get_ops_intake_item(item_id)}

    def mark_ops_intake_feedback_done(self, *, item_id: str, user: Optional[Dict[str, Any]], force: bool = False, reason: Optional[str] = None) -> Dict[str, Any]:
        item = self._get_ops_intake_item(item_id)
        if not self._ops_intake_user_can_access_guild(user, str(item.get('guild_name') or '')):
            raise HTTPException(status_code=403, detail='ops_guild_intake_forbidden')
        role = str((user or {}).get('role') or '').strip().lower()
        is_admin_role = role in {OPS_AUTH_ROLE_SUPER_ADMIN, OPS_AUTH_ROLE_ADMIN, OPS_AUTH_ROLE_INTERNAL}
        if str(item.get('system_status') or '') != 'fully_success':
            raise HTTPException(status_code=400, detail='item_not_fully_success')
        force_reason = str(reason or '').strip()
        if is_admin_role and force:
            if not force_reason:
                raise HTTPException(status_code=400, detail='force_feedback_reason_required')
        now = utc_now()
        done_by = str((user or {}).get('display_name') or (user or {}).get('username') or (user or {}).get('user_id') or 'ops_user').strip()
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE ops_intake_items SET feedback_status = 'feedback_done', feedback_done_at = ?, feedback_done_by = ?, force_feedback_reason = COALESCE(NULLIF(?, ''), force_feedback_reason) WHERE item_id = ?",
                (now, done_by, force_reason, str(item_id or '').strip()),
            )
            conn.commit()
        return {'ok': True, 'item': self._get_ops_intake_item(item_id)}

    def recognition_result(self, task_id: str, payload: RecognitionResultRequest) -> Dict[str, Any]:
        now = utc_now()
        with self.db.connect() as conn:
            task = conn.execute("SELECT lead_id, payload FROM automation_tasks WHERE task_id = ?", (task_id,)).fetchone()
            if not task:
                raise HTTPException(status_code=404, detail="task not found")
            task_payload = json.loads(task["payload"] or "{}")
            submission_id = task_payload.get("submission_id")
            if not submission_id:
                raise HTTPException(status_code=400, detail="submission_id missing from task payload")
            recognized_account_id = payload.recognized_account_id if payload.status == "success" else None
            recognition_status = "success" if payload.status == "success" else "failed"
            conn.execute(
                """
                UPDATE account_submissions
                SET recognition_status = ?, recognized_account_id = ?, recognition_raw = ?, updated_at = ?
                WHERE submission_id = ?
                """,
                (
                    recognition_status,
                    recognized_account_id,
                    json.dumps(payload.raw_result, ensure_ascii=False),
                    now,
                    submission_id,
                ),
            )
            conn.execute(
                """
                UPDATE automation_tasks
                SET status = ?, result_code = ?, result_reason = ?, finished_at = ?, raw_result = ?, lease_until = '', heartbeat_at = ''
                WHERE task_id = ?
                """,
                (
                    payload.status,
                    payload.result_code,
                    payload.result_reason,
                    payload.finished_at,
                    json.dumps(payload.raw_result, ensure_ascii=False),
                    task_id,
                ),
            )
            if payload.status == "success" and recognized_account_id and str(recognized_account_id).isdigit():
                bind_task_id = create_id("task")
                bind_payload = {
                    "submission_id": submission_id,
                    "lead_id": task["lead_id"],
                    "account_id": str(recognized_account_id),
                    "source_bot_app_id": task_payload.get("source_bot_app_id"),
                    "source_message_id": task_payload.get("source_message_id"),
                }
                conn.execute(
                    """
                    INSERT INTO automation_tasks (
                        task_id, lead_id, task_type, priority, payload, dedupe_key, created_by, created_at, status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        bind_task_id,
                        task["lead_id"],
                        "bind_check",
                        "P0",
                        json.dumps(bind_payload, ensure_ascii=False),
                        f"bind_check:{task['lead_id']}:{submission_id}",
                        "system",
                        payload.finished_at,
                        "pending",
                    ),
                )
                conn.execute("UPDATE leads SET current_status = ?, updated_at = ? WHERE lead_id = ?", ("account_submitted", now, task["lead_id"]))
                self._record_status_history(
                    conn,
                    lead_id=task["lead_id"],
                    from_status="recognition_pending",
                    to_status="account_submitted",
                    trigger_type="recognition_success",
                    trigger_source="recognition_result",
                    trigger_task_id=task_id,
                    remark=str(recognized_account_id),
                )
                return {
                    "task_id": task_id,
                    "lead_status": "account_submitted",
                    "next_action": "queue_bind_check",
                    "bind_task_type": "bind_check",
                    "recognized_account_id": str(recognized_account_id),
                }
            conn.execute("UPDATE leads SET current_status = ?, updated_at = ? WHERE lead_id = ?", ("re_engage_pending", now, task["lead_id"]))
            self._record_status_history(
                conn,
                lead_id=task["lead_id"],
                from_status="recognition_pending",
                to_status="re_engage_pending",
                trigger_type="recognition_failed",
                trigger_source="recognition_result",
                trigger_task_id=task_id,
            )
            return {
                "task_id": task_id,
                "lead_status": "re_engage_pending",
                "next_action": "manual_recovery",
                "recognized_account_id": recognized_account_id,
            }

    def run_native_ocr(self, task_id: str) -> Dict[str, Any]:
        if self.ocr_adapter is None:
            raise HTTPException(status_code=503, detail='ocr adapter not configured')
        with self.db.connect() as conn:
            task = conn.execute("SELECT task_id, lead_id, task_type, payload, status FROM automation_tasks WHERE task_id = ?", (task_id,)).fetchone()
            if not task:
                raise HTTPException(status_code=404, detail='task not found')
            if task['task_type'] != 'account_recognition':
                raise HTTPException(status_code=400, detail='task is not account_recognition')
            task_payload = json.loads(task['payload'] or '{}')
            file_url = task_payload.get('file_url')
            if not file_url:
                raise HTTPException(status_code=400, detail='file_url missing from task payload')
            conn.execute("UPDATE automation_tasks SET status = ? WHERE task_id = ?", ('running', task_id))

        extracted = self.ocr_adapter.extract_text(file_url)
        raw_text = str((extracted or {}).get('raw_text') or '').strip()
        normalized = normalize_native_ocr_fields(raw_text)
        recognized_account_id = normalized.get('account_id')
        status = 'success' if str(recognized_account_id or '').isdigit() else 'failed'
        result_code = 'recognized' if status == 'success' else 'ocr_no_account_id'
        result_reason = 'native ocr success' if status == 'success' else 'native ocr failed to extract account id'
        result = self.recognition_result(
            task_id,
            RecognitionResultRequest(
                status=status,
                recognized_account_id=str(recognized_account_id) if recognized_account_id else None,
                result_code=result_code,
                result_reason=result_reason,
                finished_at=utc_now(),
                raw_result={
                    'ocr_engine': (extracted or {}).get('engine'),
                    'ocr_raw_text': raw_text,
                    'normalized': normalized,
                    'person_code': normalized.get('person_code'),
                    'guild_invite_code': normalized.get('guild_invite_code'),
                    'invite_code': normalized.get('invite_code'),
                },
            ),
        )
        return {
            'task_id': task_id,
            'status': status,
            'recognized_account_id': str(recognized_account_id) if recognized_account_id else None,
            'person_code': normalized.get('person_code'),
            'guild_invite_code': normalized.get('guild_invite_code'),
            'invite_code': normalized.get('invite_code'),
            **result,
        }

    def _infer_executor_guild_from_registration_group(self, registration_group: Optional[str]) -> Optional[str]:
        normalized_group = str(registration_group or '').strip()
        if not normalized_group:
            return None
        with self.db.connect() as conn:
            rows = [dict(r) for r in conn.execute("SELECT guild_name, enabled FROM guild_executors").fetchall()]
        lowered_group = normalized_group.lower()
        for row in rows:
            guild_name = str(row.get('guild_name') or '').strip()
            if not guild_name or not bool(row.get('enabled', 1)):
                continue
            if lowered_group == guild_name.lower() or lowered_group.startswith(f"{guild_name.lower()}-"):
                return guild_name
        return None

    def _resolve_expected_bind_guild(self, *, task_payload: Dict[str, Any], lead_row: Optional[sqlite3.Row]) -> Optional[str]:
        registration_group = ''
        lead_guild = ''
        crm_verified_guild = ''
        if lead_row:
            registration_group = str(lead_row['pendaftaran_group'] or '').strip()
            lead_guild = str(lead_row['dept_name'] or '').strip()
            crm_verified_guild = str(lead_row['crm_verified_dept_name'] or '').strip()
        inferred_executor_guild = self._infer_executor_guild_from_registration_group(registration_group)
        route_snapshot = task_payload.get('route_snapshot') if isinstance(task_payload.get('route_snapshot'), dict) else {}
        snapshot_expected_guild = str(route_snapshot.get('expected_guild') or '').strip()
        if snapshot_expected_guild:
            return snapshot_expected_guild
        explicit_expected_guild = str(task_payload.get('expected_guild') or '').strip()
        if explicit_expected_guild:
            return explicit_expected_guild
        if crm_verified_guild:
            return crm_verified_guild
        bot_app_id = str(task_payload.get('source_bot_app_id') or '').strip()
        if bot_app_id:
            preset = self.resolve_intake_bot_preset(app_id=bot_app_id)
            preset_guild = str(preset.get('default_guild') or '').strip()
            if preset_guild:
                if inferred_executor_guild and inferred_executor_guild.strip().lower() != preset_guild.strip().lower():
                    preset_executor = self.resolve_guild_executor(preset_guild)
                    if not preset_executor:
                        return inferred_executor_guild
                return preset_guild
        if inferred_executor_guild:
            return inferred_executor_guild
        if lead_guild:
            return lead_guild
        return None

    def _extract_backend_bind_guild(self, raw_result: Optional[Dict[str, Any]]) -> Optional[str]:
        if not isinstance(raw_result, dict):
            return None
        for key in ('deptName', 'guild_code', 'guildName', 'guild'):
            value = str(raw_result.get(key) or '').strip()
            if value:
                return value
        return None

    def _detect_bind_backend_guild_mismatch(self, *, task_payload: Dict[str, Any], lead_row: Optional[sqlite3.Row], raw_result: Optional[Dict[str, Any]]) -> Optional[Dict[str, str]]:
        expected_guild = self._resolve_expected_bind_guild(task_payload=task_payload, lead_row=lead_row)
        backend_guild = self._extract_backend_bind_guild(raw_result)
        if not expected_guild or not backend_guild:
            return None
        if expected_guild.strip().lower() == backend_guild.strip().lower():
            return None
        return {
            'expected_guild': expected_guild,
            'backend_guild': backend_guild,
            'result_reason': f'Configured guild {expected_guild} does not match backend guild {backend_guild}.',
        }

    def _classify_bind_human_action(self, *, result_code: Optional[str], result_reason: Optional[str], raw_result: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        normalized_code = str(result_code or '').strip().lower()
        normalized_reason = str(result_reason or '').strip().lower()
        raw = raw_result or {}
        if raw.get('captcha_required'):
            return {'requires_human_action': True, 'human_action_type': 'captcha_required'}
        if raw.get('manual_continue_required'):
            return {'requires_human_action': True, 'human_action_type': 'manual_continue_required'}
        if raw.get('session_expired'):
            return {'requires_human_action': True, 'human_action_type': 'session_expired'}
        if raw.get('auth_required'):
            return {'requires_human_action': True, 'human_action_type': 'auth_required'}
        if normalized_code in {'bind_unauthorized', 'auth_required'}:
            return {'requires_human_action': True, 'human_action_type': 'auth_required'}
        if normalized_code in {'session_expired', 'bind_session_expired'}:
            return {'requires_human_action': True, 'human_action_type': 'session_expired'}
        if normalized_code in {'captcha_required', 'bind_captcha_required'}:
            return {'requires_human_action': True, 'human_action_type': 'captcha_required'}
        if normalized_code in {'manual_continue_required', 'bind_manual_continue_required'}:
            return {'requires_human_action': True, 'human_action_type': 'manual_continue_required'}
        if 'please re-login' in normalized_reason or 're-login' in normalized_reason:
            return {'requires_human_action': True, 'human_action_type': 'session_expired'}
        if 'captcha' in normalized_reason:
            return {'requires_human_action': True, 'human_action_type': 'captcha_required'}
        if 'status code 401' in normalized_reason or 'unauthorized' in normalized_reason or 'forbidden' in normalized_reason:
            return {'requires_human_action': True, 'human_action_type': 'auth_required'}
        return {'requires_human_action': False, 'human_action_type': None}

    def _classify_bind_failure(self, *, result_code: Optional[str], result_reason: Optional[str], raw_result: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        normalized_code = str(result_code or '').strip().lower()
        reason_text = str(result_reason or '').strip()
        normalized_reason = reason_text.lower()
        human = self._classify_bind_human_action(
            result_code=result_code,
            result_reason=result_reason,
            raw_result=raw_result,
        )
        if normalized_code == 'bind_backend_guild_mismatch':
            return {
                'failure_category': 'routing_mismatch',
                'failure_stage': 'bind',
                'retryable': False,
                'requires_human_action': False,
                'human_action_type': None,
                'operator_reason': 'Bot/guild routing mismatch. Check app/agency mapping.',
            }
        if normalized_code == 'bind_executor_unavailable' or 'bind executor unavailable' in normalized_reason:
            return {
                'failure_category': 'bind_executor_unavailable',
                'failure_stage': 'bind',
                'retryable': True,
                'requires_human_action': False,
                'human_action_type': None,
                'operator_reason': 'Bind executor unavailable. Check backend runtime.',
            }
        if normalized_code == 'bind_executor_profile_not_configured' or 'no chrome profile mapping configured' in normalized_reason:
            return {
                'failure_category': 'routing_mismatch',
                'failure_stage': 'bind',
                'retryable': False,
                'requires_human_action': False,
                'human_action_type': None,
                'operator_reason': 'No guild executor profile mapping. Check app/agency routing.',
            }
        if normalized_code in {'already_in_other_guild', 'other_agency', 'already_joined_other_guild'} or (normalized_code in {'', 'bind_backend_http_error', 'bind_failed'} and ('the streamer was in other guild' in normalized_reason or 'another agency' in normalized_reason)):
            return {
                'failure_category': 'already_in_other_agency',
                'failure_stage': 'bind',
                'retryable': False,
                'requires_human_action': False,
                'human_action_type': None,
                'operator_reason': 'Account is already in another agency.',
            }
        if normalized_code in {'cms_precheck_untrusted', 'cms_target_guild_ambiguous', 'cms_target_guild_not_visible', 'cms_target_guild_mismatch', 'cms_target_guild_lock_missing', 'cms_postcheck_timeout', 'cms_postcheck_mismatch', 'cms_add_anchor_invalid_arguments_manual_check', 'cms_authorization_scope_denied', 'cms_add_anchor_unexpected_error', 'cms_add_anchor_temporary_error'}:
            return {
                'failure_category': normalized_code,
                'failure_stage': 'bind',
                'retryable': normalized_code in {'cms_postcheck_timeout', 'cms_add_anchor_temporary_error'},
                'requires_human_action': normalized_code not in {'cms_postcheck_timeout', 'cms_add_anchor_temporary_error'},
                'human_action_type': 'cms_manual_check' if normalized_code not in {'cms_postcheck_timeout', 'cms_add_anchor_temporary_error'} else None,
                'operator_reason': 'CMS bind verification is not trusted. Check guild executor and CMS result manually.',
            }
        if normalized_code in {'cms_add_anchor_invalid_arguments', 'cms_sid_not_found', 'sid_not_found_or_not_anchor', 'cms_bind_invalid_sid'}:
            return {
                'failure_category': 'invalid_or_unavailable_linky_id',
                'failure_stage': 'bind',
                'retryable': False,
                'requires_human_action': False,
                'human_action_type': None,
                'operator_reason': 'Invalid or unavailable Linky ID.',
            }
        if 'batas maksimum guild' in normalized_reason or 'maximum guild' in normalized_reason:
            return {
                'failure_category': 'device_duplicate_registration',
                'failure_stage': 'bind',
                'retryable': False,
                'requires_human_action': False,
                'human_action_type': None,
                'operator_reason': 'Device/account has reached the guild join limit.',
            }
        if 'invalid person code' in normalized_reason or 'invalid invite code' in normalized_reason:
            return {
                'failure_category': 'invalid_personal_code',
                'failure_stage': 'bind',
                'retryable': False,
                'requires_human_action': False,
                'human_action_type': None,
                'operator_reason': 'Personal bind code is invalid for this agency.',
            }
        if human.get('requires_human_action'):
            human_action_type = human.get('human_action_type')
            human_reason_map = {
                'auth_required': 'Bind backend authorization expired. Re-login required.',
                'session_expired': 'Bind backend session expired. Re-login required.',
                'captcha_required': 'Bind backend requires captcha/manual confirmation.',
                'manual_continue_required': 'Bind backend requires manual confirmation to continue.',
            }
            return {
                'failure_category': human_action_type or 'manual_intervention_required',
                'failure_stage': 'bind',
                'retryable': False,
                'requires_human_action': True,
                'human_action_type': human_action_type,
                'operator_reason': human_reason_map.get(str(human_action_type or '').strip(), reason_text or 'Bind requires manual intervention.'),
            }
        retryable_keywords = (
            'timeout',
            'timed out',
            'gateway',
            'temporarily',
            'unavailable',
            'connection',
            'reset',
            'broken pipe',
            'empty response',
            'non-json response',
            'econnreset',
            'net::err',
            'chrome-error://',
        )
        retryable_codes = {
            'bind_execution_error',
            'bind_backend_http_500',
            'bind_backend_http_502',
            'bind_backend_http_503',
            'bind_backend_http_504',
            'bind_backend_timeout',
            'bind_transport_error',
        }
        if normalized_code in retryable_codes or any(keyword in normalized_reason for keyword in retryable_keywords):
            return {
                'failure_category': 'technical_retryable',
                'failure_stage': 'bind',
                'retryable': True,
                'requires_human_action': False,
                'human_action_type': None,
                'operator_reason': 'Temporary bind execution error. System will retry automatically.',
            }
        return {
            'failure_category': 'bind_failed',
            'failure_stage': 'bind',
            'retryable': False,
            'requires_human_action': False,
            'human_action_type': None,
            'operator_reason': reason_text or 'Bind failed.',
        }

    def _format_operator_bind_failure_reason(self, *, failure_meta: Dict[str, Any], raw_reason: Optional[str], retried: bool = False) -> str:
        category = str((failure_meta or {}).get('failure_category') or '').strip()
        base_reason = str((failure_meta or {}).get('operator_reason') or '').strip() or str(raw_reason or '').strip() or 'Bind failed.'
        if category == 'technical_retryable' and retried:
            return f'Bind failed after {self.bind_retry_max_attempts} retries. Check guild executor/network manually.'
        return base_reason

    def _build_bind_retry_task_payload(self, *, source_payload: Dict[str, Any], retry_count: int) -> Dict[str, Any]:
        payload = dict(source_payload or {})
        payload['retry_count'] = retry_count
        return payload

    def _schedule_bind_retry_task(
        self,
        conn: sqlite3.Connection,
        *,
        lead_id: str,
        source_task_payload: Dict[str, Any],
        source_created_by: Optional[str],
        retry_count: int,
    ) -> Dict[str, Any]:
        submission_id = str(source_task_payload.get('submission_id') or '').strip()
        payload = self._build_bind_retry_task_payload(source_payload=source_task_payload, retry_count=retry_count)
        task_id = create_id('task')
        dedupe_parts = ['bind_retry', lead_id, submission_id or 'no_submission', str(retry_count)]
        conn.execute(
            """
            INSERT INTO automation_tasks (
                task_id, lead_id, task_type, priority, payload, dedupe_key, created_by, created_at, status,
                result_code, result_reason, retry_count, raw_result
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                lead_id,
                'bind_check',
                'P0',
                json.dumps(payload, ensure_ascii=False),
                ':'.join(dedupe_parts),
                source_created_by or 'system:auto_retry_bind',
                utc_now(),
                'pending',
                'bind_retry_pending',
                f'bind retry scheduled {retry_count}/{self.bind_retry_max_attempts}',
                retry_count,
                json.dumps({'retry_count': retry_count}, ensure_ascii=False),
            ),
        )
        return {'task_id': task_id, 'retry_count': retry_count}

    def _sync_crm_after_bind_success(
        self,
        conn: sqlite3.Connection,
        *,
        lead_id: str,
        account_id: Optional[str],
        task_id: str,
        bind_result_reason: Optional[str],
        bind_raw_result: Optional[Dict[str, Any]],
        submission_id: Optional[str] = None,
        reply_context: Optional[Dict[str, Any]] = None,
        retry_attempt: int = 0,
        suppress_failure_notification: bool = False,
    ) -> Dict[str, Any]:
        crm_sync_failed = None
        crm_retry_pending = False
        crm_retryable = False
        crm_payload = None
        crm_response = None
        verified_row = None
        lead_row = conn.execute("SELECT * FROM leads WHERE lead_id = ?", (lead_id,)).fetchone()
        if self.crm_adapter is not None and lead_row:
            lead_dict = dict(lead_row)
            submission_row = None
            if submission_id:
                submission_row = conn.execute(
                    "SELECT submission_id, source_channel, submitted_by FROM account_submissions WHERE submission_id = ?",
                    (submission_id,),
                ).fetchone()
            submission_dict = dict(submission_row) if submission_row else {}
            crm_creator_name = str(submission_dict.get('submitted_by') or '').strip()
            if crm_creator_name.startswith('lark:ops:'):
                crm_creator_name = crm_creator_name[len('lark:ops:'):].strip()
            elif crm_creator_name.startswith('ops:'):
                crm_creator_name = crm_creator_name[len('ops:'):].strip()
            id_only_cms_bind = is_external_app_id_only_phone(lead_dict.get('mobile'))
            lead_mobile = str(lead_dict.get('mobile') or '')
            crm_mobile = None if id_only_cms_bind else lead_mobile
            phone_raw = lead_mobile if id_only_cms_bind else (f"+{lead_dict.get('area_code')} {lead_dict.get('mobile')}" if lead_dict.get('area_code') and lead_dict.get('mobile') else str(lead_dict.get('mobile') or ''))
            phone_e164 = '' if id_only_cms_bind else (f"+{lead_dict.get('area_code')}{lead_dict.get('mobile')}" if lead_dict.get('area_code') and lead_dict.get('mobile') else '')
            resolved_app = self._resolve_crm_app_mapping(lead_dict.get('app_name'))
            resolved_dept = self._resolve_crm_dept_mapping(
                (bind_raw_result or {}).get('deptName') or lead_dict.get('dept_name'),
                (bind_raw_result or {}).get('deptId'),
            )
            crm_payload = {
                'mobile': crm_mobile,
                'mobilePlaceholder': lead_mobile if id_only_cms_bind else '',
                'phoneRaw': phone_raw,
                'phoneE164': phone_e164,
                'ywId': str(account_id or ''),
                'name': '',
                'remark': bind_result_reason or '',
                'dept': '',
                'wa': '',
                'areaCode': '' if id_only_cms_bind else str(lead_dict.get('area_code') or ''),
                'inviterId': lead_dict.get('inviter_id'),
                'appName': resolved_app['appName'],
                'appId': resolved_app['appId'],
                'pendaftaranGroup': lead_dict.get('pendaftaran_group') or '',
                'paymentStatus': '',
                'pzStatus': 0,
                'userQuality': '',
                'fileUrl': '',
                'deptName': resolved_dept['deptName'],
                'deptId': resolved_dept['deptId'],
                'submissionId': str(submission_dict.get('submission_id') or submission_id or ''),
                'sourceChannel': str(submission_dict.get('source_channel') or ''),
                'creatorName': crm_creator_name,
                'bindStatus': 'bind_success',
                'officialGroupStatus': 'pending',
            }
            mapping_failure = self._precheck_crm_mapping_failure(
                resolved_app=resolved_app,
                resolved_dept=resolved_dept,
            )
            if mapping_failure:
                self._record_sync_log(
                    conn,
                    lead_id=lead_id,
                    task_id=task_id,
                    sync_type='customer_upsert',
                    target_system='crm',
                    status='failed',
                    request_snapshot=crm_payload,
                    response_snapshot={
                        'action': 'mapping_precheck',
                        'mapping_failure': mapping_failure,
                        'resolved_app': resolved_app,
                        'resolved_dept': resolved_dept,
                        'submission_id': submission_id,
                        'retry_attempt': retry_attempt,
                    },
                )
                crm_sync_failed = mapping_failure
            else:
                verified_row = None
                if retry_attempt > 0:
                    verified_row = self._find_existing_customer_with_fallback(
                        yw_id=account_id,
                        mobile=crm_mobile,
                        app_name=resolved_app['appName'],
                        dept_name=resolved_dept['deptName'],
                        registration_group=lead_dict.get('pendaftaran_group') or '',
                    )
                if verified_row:
                    self._record_sync_log(
                        conn,
                        lead_id=lead_id,
                        task_id=task_id,
                        sync_type='customer_upsert',
                        target_system='crm',
                        status='success',
                        request_snapshot=crm_payload,
                        response_snapshot={
                            'action': 'verify_before_retry',
                            'crm_response': {'code': 0, 'msg': 'verified_existing_before_retry'},
                            'verified_after_write': True,
                            'submission_id': submission_id,
                            'retry_attempt': retry_attempt,
                            'reply_context': reply_context or {},
                        },
                    )
                    self._record_verified_crm_state(conn, lead_id=lead_id, crm_payload=crm_payload)
                    mobile, yw_id = self._resolve_lead_notification_context(conn, lead_id)
                    self._queue_operator_notification(
                        conn,
                        lead_id=lead_id,
                        notification_type='crm_record_success',
                        mobile=mobile,
                        yw_id=yw_id,
                        write_result='success',
                    )
                else:
                    crm_write_started_at = utc_now()
                    crm_write_monotonic = time.perf_counter()
                    crm_response = self.crm_adapter.create_customer(crm_payload)
                    crm_write_finished_at = utc_now()
                    crm_write_elapsed_seconds = round(max(0.0, time.perf_counter() - crm_write_monotonic), 3)
                    crm_action = 'create'
                    verified_after_write = None
                    crm_verify_started_at = None
                    crm_verify_finished_at = None
                    crm_verify_elapsed_seconds = 0.0
                    crm_write_confirmed = self._crm_response_confirms_customer_write(crm_response, allow_duplicate_sid=True)
                    if crm_response.get('code') == 0 or crm_write_confirmed:
                        crm_verify_started_at = utc_now()
                        crm_verify_monotonic = time.perf_counter()
                        verified_after_write = self._find_existing_customer_with_fallback(
                            yw_id=account_id,
                            mobile=crm_mobile,
                            app_name=resolved_app['appName'],
                            dept_name=resolved_dept['deptName'],
                            registration_group=lead_dict.get('pendaftaran_group') or '',
                        )
                        if not verified_after_write and crm_write_confirmed:
                            response_data = crm_response.get('data') if isinstance(crm_response.get('data'), dict) else {}
                            verified_after_write = {
                                'id': response_data.get('customerId') or response_data.get('id'),
                                'ywId': response_data.get('ywId') or str(account_id or ''),
                                'mobile': response_data.get('mobile') or crm_mobile,
                                'appName': resolved_app['appName'],
                                'deptName': resolved_dept['deptName'],
                                'pendaftaranGroup': lead_dict.get('pendaftaran_group') or '',
                                'verified_by': 'crm_write_response',
                            }
                        crm_verify_finished_at = utc_now()
                        crm_verify_elapsed_seconds = round(max(0.0, time.perf_counter() - crm_verify_monotonic), 3)
                    verified_row = verified_after_write
                    self._record_sync_log(
                        conn,
                        lead_id=lead_id,
                        task_id=task_id,
                        sync_type='customer_upsert',
                        target_system='crm',
                        status='success' if crm_write_confirmed and verified_after_write else 'failed',
                        request_snapshot=crm_payload,
                        response_snapshot={
                            'action': crm_action,
                            'crm_response': crm_response,
                            'verified_after_write': bool(verified_after_write),
                            'submission_id': submission_id,
                            'retry_attempt': retry_attempt,
                            'reply_context': reply_context or {},
                            'crm_write_started_at': crm_write_started_at,
                            'crm_write_finished_at': crm_write_finished_at,
                            'crm_verify_started_at': crm_verify_started_at,
                            'crm_verify_finished_at': crm_verify_finished_at,
                            'crm_write_elapsed_seconds': crm_write_elapsed_seconds,
                            'crm_verify_elapsed_seconds': crm_verify_elapsed_seconds,
                            'crm_total_elapsed_seconds': round(crm_write_elapsed_seconds + crm_verify_elapsed_seconds, 3),
                        },
                    )
                    if crm_write_confirmed and verified_after_write:
                        self._record_verified_crm_state(conn, lead_id=lead_id, crm_payload=crm_payload)
                        mobile, yw_id = self._resolve_lead_notification_context(conn, lead_id)
                        self._queue_operator_notification(
                            conn,
                            lead_id=lead_id,
                            notification_type='crm_record_success',
                            mobile=mobile,
                            yw_id=yw_id,
                            write_result='success',
                        )
                    elif crm_response.get('code') != 0:
                        crm_sync_failed = self._normalize_crm_failure_reason(
                            crm_response,
                            fallback_found=False,
                        )
                        if id_only_cms_bind:
                            crm_sync_failed = 'CRM rejected ID-only placeholder; wait for phone backfill.'
                            crm_retryable = self._is_retryable_crm_failure(crm_response)
                        else:
                            crm_retryable = self._is_retryable_crm_failure(crm_response)
                        crm_retry_pending = crm_retryable and retry_attempt < self.crm_retry_max_attempts
                    elif not verified_after_write:
                        crm_sync_failed = 'CRM write could not be verified.'
                    else:
                        self._record_verified_crm_state(conn, lead_id=lead_id, crm_payload=crm_payload)
                        mobile, yw_id = self._resolve_lead_notification_context(conn, lead_id)
                        self._queue_operator_notification(
                            conn,
                            lead_id=lead_id,
                            notification_type='crm_record_success',
                            mobile=mobile,
                            yw_id=yw_id,
                            write_result='success',
                        )
        if crm_sync_failed and not (crm_retry_pending or suppress_failure_notification):
            lead_mobile_row = conn.execute("SELECT mobile FROM leads WHERE lead_id = ?", (lead_id,)).fetchone()
            self._queue_operator_notification(
                conn,
                lead_id=lead_id,
                notification_type="crm_record_failed",
                mobile=(lead_mobile_row['mobile'] if lead_mobile_row else ''),
                yw_id=account_id,
                write_result="failed",
                reason=self._format_operator_crm_failure_reason(retried=retry_attempt > 0),
            )
        return {
            'crm_sync_failed': crm_sync_failed,
            'crm_verified': crm_sync_failed is None,
            'current_submission_crm_verified': crm_sync_failed is None,
            'crm_retry_pending': crm_retry_pending,
            'crm_retryable': crm_retryable,
            'crm_verified_row': verified_row,
            'crm_payload': crm_payload,
            'crm_response': crm_response,
        }

    def _crm_response_confirms_customer_write(self, crm_response: Optional[Dict[str, Any]], *, allow_duplicate_sid: bool = False) -> bool:
        if not isinstance(crm_response, dict):
            return False
        data = crm_response.get('data') if isinstance(crm_response.get('data'), dict) else {}
        if crm_response.get('code') != 0:
            duplicate_code = str(data.get('code') or '').strip().upper()
            mismatch_fields = data.get('mismatchFields') if isinstance(data.get('mismatchFields'), list) else []
            if (
                allow_duplicate_sid
                and duplicate_code == 'DUPLICATE_SID'
                and (data.get('customerId') or data.get('id'))
                and data.get('ywId')
                and not mismatch_fields
            ):
                return True
            return False
        if data.get('customerId') or data.get('id'):
            return True
        if data.get('ywId') and any(data.get(flag) is True for flag in ('success', 'created', 'updated', 'idempotent')):
            return True
        return False

    def _is_retryable_crm_failure(self, crm_response: Optional[Dict[str, Any]]) -> bool:
        if not isinstance(crm_response, dict):
            return False
        if self._crm_response_looks_like_duplicate(crm_response):
            return False
        code = crm_response.get('code')
        msg = str(crm_response.get('msg') or '').strip().lower()
        if isinstance(code, int) and code >= 500:
            return True
        retry_keywords = (
            '服务器内部异常',
            'internal',
            'timeout',
            'timed out',
            'gateway',
            'temporarily',
            'unavailable',
            'non-json response',
            'connection',
            'reset',
            'broken pipe',
        )
        lowered = msg.lower()
        return any(keyword.lower() in lowered for keyword in retry_keywords)

    def _build_crm_retry_task_payload(
        self,
        *,
        submission_id: str,
        lead_id: str,
        account_id: str,
        bind_result_reason: str,
        bind_raw_result: Optional[Dict[str, Any]],
        source_payload: Optional[Dict[str, Any]],
        retry_count: int,
        next_retry_at: str,
    ) -> Dict[str, Any]:
        source_payload = source_payload or {}
        return {
            'submission_id': submission_id,
            'lead_id': lead_id,
            'account_id': account_id,
            'bind_result_reason': bind_result_reason,
            'bind_raw_result': bind_raw_result or {},
            'source_message_id': str(source_payload.get('source_message_id') or ''),
            'source_chat_id': str(source_payload.get('source_chat_id') or ''),
            'source_bot_app_id': str(source_payload.get('source_bot_app_id') or ''),
            'retry_count': retry_count,
            'next_retry_at': next_retry_at,
        }

    def _schedule_crm_retry_task(
        self,
        conn: sqlite3.Connection,
        *,
        submission_id: str,
        lead_id: str,
        account_id: str,
        bind_result_reason: str,
        bind_raw_result: Optional[Dict[str, Any]],
        source_payload: Optional[Dict[str, Any]],
        retry_count: int,
    ) -> Optional[Dict[str, Any]]:
        if retry_count > self.crm_retry_max_attempts:
            return None
        delay_index = min(max(retry_count - 1, 0), max(len(self.crm_retry_delays_seconds) - 1, 0))
        delay_seconds = int(self.crm_retry_delays_seconds[delay_index]) if self.crm_retry_delays_seconds else 0
        next_retry_dt = datetime.now(timezone.utc) + timedelta(seconds=max(0, delay_seconds))
        next_retry_at = next_retry_dt.isoformat()
        payload = self._build_crm_retry_task_payload(
            submission_id=submission_id,
            lead_id=lead_id,
            account_id=account_id,
            bind_result_reason=bind_result_reason,
            bind_raw_result=bind_raw_result,
            source_payload=source_payload,
            retry_count=retry_count,
            next_retry_at=next_retry_at,
        )
        existing = conn.execute(
            "SELECT task_id FROM automation_tasks WHERE dedupe_key = ? LIMIT 1",
            (f'crm_retry:{submission_id}',),
        ).fetchone()
        now = utc_now()
        if existing:
            task_id = str(existing['task_id'])
            conn.execute(
                """
                UPDATE automation_tasks
                SET payload = ?, priority = ?, created_by = ?, status = 'pending', retry_count = ?,
                    result_code = ?, result_reason = ?, started_at = NULL, finished_at = NULL, raw_result = ?, created_at = ?
                WHERE task_id = ?
                """,
                (
                    json.dumps(payload, ensure_ascii=False),
                    'P0',
                    'system:auto_retry_crm',
                    retry_count,
                    'crm_retry_pending',
                    f'crm retry scheduled attempt {retry_count}/{self.crm_retry_max_attempts}',
                    json.dumps({'retry_count': retry_count, 'next_retry_at': next_retry_at}, ensure_ascii=False),
                    now,
                    task_id,
                ),
            )
        else:
            task_id = create_id('task')
            conn.execute(
                """
                INSERT INTO automation_tasks (
                    task_id, lead_id, task_type, priority, payload, dedupe_key, created_by, created_at, status,
                    result_code, result_reason, retry_count, raw_result
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    lead_id,
                    'crm_sync_retry',
                    'P0',
                    json.dumps(payload, ensure_ascii=False),
                    f'crm_retry:{submission_id}',
                    'system:auto_retry_crm',
                    now,
                    'pending',
                    'crm_retry_pending',
                    f'crm retry scheduled attempt {retry_count}/{self.crm_retry_max_attempts}',
                    retry_count,
                    json.dumps({'retry_count': retry_count, 'next_retry_at': next_retry_at}, ensure_ascii=False),
                ),
            )
        return {'task_id': task_id, 'retry_count': retry_count, 'next_retry_at': next_retry_at}

    def run_crm_failure_compensation_patrol(self, *, limit: int = 50) -> Dict[str, Any]:
        """Find bind-satisfied/CRM-failed ops cards that have no retry task and enqueue bounded CRM recovery."""
        if self.crm_adapter is None:
            return {'queued_count': 0, 'skipped_count': 0, 'queued': [], 'skipped': [{'reason': 'crm_adapter_not_configured'}]}
        queued: List[Dict[str, Any]] = []
        skipped: List[Dict[str, Any]] = []
        max_limit = max(1, min(int(limit or 50), 200))
        with self.db.connect() as conn:
            rows = [dict(r) for r in conn.execute(
                """
                SELECT item_id, guild_name, parsed_phone, parsed_account_id, result_snapshot, result_code, result_reason, created_at
                FROM ops_intake_items
                WHERE system_status = 'partial_success_crm_failed'
                  AND COALESCE(feedback_status, '') NOT IN ('feedback_done', 'cleared')
                ORDER BY COALESCE(processed_at, created_at) ASC
                LIMIT ?
                """,
                (max_limit,),
            ).fetchall()]
            for row in rows:
                try:
                    snapshot = json.loads(row.get('result_snapshot') or '{}')
                except Exception:
                    snapshot = {}
                lead_id = str(snapshot.get('lead_id') or '').strip()
                submission_id = str(snapshot.get('submission_id') or '').strip()
                account_id = str(snapshot.get('account_id') or row.get('parsed_account_id') or '').strip()
                if not lead_id or not account_id:
                    skipped.append({'item_id': row.get('item_id'), 'reason': 'missing_lead_or_account_id'})
                    continue
                if is_external_app_id_only_phone(row.get('parsed_phone')):
                    skipped.append({'item_id': row.get('item_id'), 'reason': 'id_only_waiting_for_phone_backfill'})
                    continue
                if not submission_id:
                    submission_id = f"ops-item-{row.get('item_id')}"
                existing = conn.execute(
                    """
                    SELECT task_id, status, result_code, retry_count FROM automation_tasks
                    WHERE task_type = 'crm_sync_retry'
                      AND dedupe_key = ?
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (f'crm_retry:{submission_id}',),
                ).fetchone()
                if existing:
                    skipped.append({
                        'item_id': row.get('item_id'),
                        'reason': 'retry_already_exists',
                        'task_id': existing['task_id'],
                        'status': existing['status'],
                        'result_code': existing['result_code'],
                        'retry_count': existing['retry_count'],
                    })
                    continue
                scheduled = self._schedule_crm_retry_task(
                    conn,
                    submission_id=submission_id,
                    lead_id=lead_id,
                    account_id=account_id,
                    bind_result_reason=str(row.get('result_reason') or 'Bind satisfied, CRM sync failed'),
                    bind_raw_result=snapshot.get('bind_raw_result') if isinstance(snapshot.get('bind_raw_result'), dict) else {},
                    source_payload={
                        'source_message_id': str(snapshot.get('source_message_id') or ''),
                        'source_chat_id': str(snapshot.get('source_chat_id') or ''),
                        'source_bot_app_id': str(snapshot.get('source_bot_app_id') or ''),
                    },
                    retry_count=1,
                )
                if scheduled:
                    queued.append({'item_id': row.get('item_id'), 'lead_id': lead_id, 'retry_task_id': scheduled['task_id'], 'next_retry_at': scheduled['next_retry_at']})
            conn.commit()
        return {'queued_count': len(queued), 'skipped_count': len(skipped), 'queued': queued, 'skipped': skipped}

    def _update_ops_intake_items_after_crm_retry(
        self,
        conn: sqlite3.Connection,
        *,
        lead_id: str,
        submission_id: str,
        account_id: str,
        result: Dict[str, Any],
    ) -> None:
        system_status = 'fully_success' if result.get('crm_verified') else 'partial_success_crm_failed'
        feedback_status = 'pending_feedback'
        result_code = str(result.get('result_code') or ('crm_retry_succeeded' if result.get('crm_verified') else 'crm_retry_failed'))
        result_reason = str(result.get('result_reason') or ('CRM retry succeeded and verified' if result.get('crm_verified') else 'CRM retry failed'))
        snapshot_update = {
            'lead_id': lead_id,
            'submission_id': submission_id,
            'account_id': account_id,
            'crm_retry_result': result,
            'updated_by': 'system:crm_compensation',
            'updated_at': utc_now(),
        }
        target_rows = conn.execute(
            """
            SELECT item_id, parsed_phone, parsed_account_id, parsed_group, parsed_code, reply_text, result_snapshot
            FROM ops_intake_items
            WHERE system_status = 'partial_success_crm_failed'
              AND COALESCE(feedback_status, '') NOT IN ('feedback_done', 'cleared')
              AND (
                    result_snapshot LIKE ?
                 OR result_snapshot LIKE ?
                 OR (parsed_account_id = ? AND guild_name IN (SELECT COALESCE(dept_name, '') FROM leads WHERE lead_id = ?))
              )
            """,
            (
                f'%"submission_id": "{submission_id}"%' if submission_id else '__no_submission_match__',
                f'%"lead_id": "{lead_id}"%',
                account_id,
                lead_id,
            ),
        ).fetchall()
        for row in target_rows:
            row = dict(row)
            try:
                existing_snapshot = json.loads(row.get('result_snapshot') or '{}')
            except Exception:
                existing_snapshot = {}
            if not isinstance(existing_snapshot, dict):
                existing_snapshot = {}
            merged_snapshot = {**existing_snapshot, **snapshot_update}
            reply_text = str(row.get('reply_text') or '')
            if result.get('crm_verified'):
                reply_envelope = {
                    **existing_snapshot,
                    **result,
                    'accepted': True,
                    'lead_status': 'bind_success',
                    'crm_verified': True,
                    'current_submission_crm_verified': True,
                    'reply_phone': row.get('parsed_phone') or existing_snapshot.get('reply_phone'),
                    'reply_id': row.get('parsed_account_id') or existing_snapshot.get('reply_id') or account_id,
                    'reply_group': row.get('parsed_group') or existing_snapshot.get('reply_group'),
                    'reply_code': row.get('parsed_code') or existing_snapshot.get('reply_code') or '-',
                }
                reply_text = self._format_lark_reply_text(reply_envelope)
                merged_snapshot['reply_text'] = reply_text
            conn.execute(
                """
                UPDATE ops_intake_items
                SET system_status = ?, feedback_status = ?, result_code = ?, result_reason = ?, processed_at = ?, reply_text = ?, result_snapshot = ?
                WHERE item_id = ?
                """,
                (
                    system_status,
                    feedback_status,
                    result_code,
                    result_reason,
                    utc_now(),
                    reply_text,
                    json.dumps(merged_snapshot, ensure_ascii=False),
                    row.get('item_id'),
                ),
            )

    def _process_crm_retry_task(self, task: Dict[str, Any]) -> Dict[str, Any]:
        payload = dict(task.get('payload_dict') or {})
        task_id = str(task.get('task_id') or '')
        submission_id = str(payload.get('submission_id') or '').strip()
        lead_id = str(payload.get('lead_id') or task.get('lead_id') or '').strip()
        account_id = str(payload.get('account_id') or '').strip()
        bind_result_reason = str(payload.get('bind_result_reason') or '').strip()
        bind_raw_result = payload.get('bind_raw_result') if isinstance(payload.get('bind_raw_result'), dict) else {}
        reply_context = {
            'source_message_id': str(payload.get('source_message_id') or ''),
            'source_chat_id': str(payload.get('source_chat_id') or ''),
            'source_bot_app_id': str(payload.get('source_bot_app_id') or ''),
        }
        retry_count = int(payload.get('retry_count') or task.get('retry_count') or 0)
        now = utc_now()
        with self.db.connect() as conn:
            crm_sync = self._sync_crm_after_bind_success(
                conn,
                lead_id=lead_id,
                account_id=account_id,
                task_id=task_id,
                bind_result_reason=bind_result_reason,
                bind_raw_result=bind_raw_result,
                submission_id=submission_id,
                reply_context=reply_context,
                retry_attempt=retry_count,
                suppress_failure_notification=True,
            )
            final_verified_row = crm_sync.get('crm_verified_row') if isinstance(crm_sync.get('crm_verified_row'), dict) else None
            if crm_sync.get('crm_sync_failed') and not crm_sync.get('crm_retry_pending') and not final_verified_row:
                crm_payload = crm_sync.get('crm_payload') if isinstance(crm_sync.get('crm_payload'), dict) else {}
                try:
                    final_verified_row = self._find_existing_customer_with_fallback(
                        yw_id=account_id,
                        mobile=crm_payload.get('mobile'),
                        app_name=crm_payload.get('appName'),
                        dept_name=crm_payload.get('deptName'),
                        registration_group=crm_payload.get('pendaftaranGroup') or '',
                    )
                except Exception:
                    final_verified_row = None
                if final_verified_row:
                    self._record_sync_log(
                        conn,
                        lead_id=lead_id,
                        task_id=task_id,
                        sync_type='customer_upsert',
                        target_system='crm',
                        status='success',
                        request_snapshot=crm_payload,
                        response_snapshot={
                            'action': 'verify_after_final_retry',
                            'crm_response': {'code': 0, 'msg': 'verified_existing_after_final_retry'},
                            'verified_after_write': True,
                            'submission_id': submission_id,
                            'retry_attempt': retry_count,
                            'reply_context': reply_context or {},
                        },
                    )
                    self._record_verified_crm_state(conn, lead_id=lead_id, crm_payload=crm_payload)
                    crm_sync = {
                        **crm_sync,
                        'crm_sync_failed': None,
                        'crm_verified': True,
                        'current_submission_crm_verified': True,
                        'crm_verified_row': final_verified_row,
                    }
            if crm_sync.get('crm_sync_failed') is None:
                conn.execute(
                    "UPDATE automation_tasks SET status = 'success', result_code = ?, result_reason = ?, finished_at = ?, raw_result = ? WHERE task_id = ?",
                    (
                        'crm_retry_succeeded',
                        'crm retry succeeded and verified',
                        now,
                        json.dumps({'crm_verified': True}, ensure_ascii=False),
                        task_id,
                    ),
                )
                created_group_join = self._queue_group_join_after_verified_crm(
                    conn,
                    lead_id=lead_id,
                    submission_id=submission_id or None,
                    account_id=account_id,
                    created_at=now,
                )
                conn.commit()
                result = {
                    'task_id': task_id,
                    'lead_status': 'bind_success',
                    'next_action': 'queue_group_join',
                    'crm_verified': True,
                    'current_submission_crm_verified': True,
                    **created_group_join,
                    'accepted': True,
                    'reason': None,
                    'result_code': 'crm_retry_succeeded',
                    'result_reason': None,
                }
                self._update_ops_intake_items_after_crm_retry(
                    conn,
                    lead_id=lead_id,
                    submission_id=submission_id,
                    account_id=account_id,
                    result=result,
                )
                conn.commit()
            elif crm_sync.get('crm_retry_pending'):
                scheduled = self._schedule_crm_retry_task(
                    conn,
                    submission_id=submission_id,
                    lead_id=lead_id,
                    account_id=account_id,
                    bind_result_reason=bind_result_reason,
                    bind_raw_result=bind_raw_result,
                    source_payload=reply_context,
                    retry_count=retry_count + 1,
                )
                if scheduled:
                    conn.commit()
                    return {
                        'task_id': task_id,
                        'lead_status': 'bind_success',
                        'next_action': 'queue_crm_sync_retry',
                        'reason': 'crm_sync_retry_pending',
                        'result_reason': crm_sync.get('crm_sync_failed'),
                        'crm_verified': False,
                        'current_submission_crm_verified': False,
                        'accepted': False,
                        'retry_task_id': scheduled['task_id'],
                        'retry_count': scheduled['retry_count'],
                        'next_retry_at': scheduled['next_retry_at'],
                    }
                mobile, yw_id = self._resolve_lead_notification_context(conn, lead_id)
                response_code = crm_sync.get('crm_response_code')
                detail = crm_sync.get('crm_sync_failed') or 'CRM write was rejected.'
                final_reason = f'crm retry exhausted after {retry_count} attempts: {detail}' + (f' (code={response_code})' if response_code not in (None, '') else '')
                self._queue_operator_notification(
                    conn,
                    lead_id=lead_id,
                    notification_type='crm_record_failed',
                    mobile=mobile,
                    yw_id=yw_id,
                    write_result='failed',
                    reason=self._format_operator_crm_failure_reason(retried=True),
                )
                conn.execute(
                    "UPDATE automation_tasks SET status = 'failed', result_code = ?, result_reason = ?, finished_at = ?, raw_result = ? WHERE task_id = ?",
                    ('crm_retry_exhausted', final_reason, now, json.dumps({'crm_retry_exhausted': True}, ensure_ascii=False), task_id),
                )
                conn.commit()
                result = {
                    'task_id': task_id,
                    'lead_status': 'bind_success',
                    'next_action': 'retry_crm_sync',
                    'reason': 'crm_sync_failed',
                    'result_code': 'crm_retry_exhausted',
                    'result_reason': final_reason,
                    'crm_verified': False,
                    'current_submission_crm_verified': False,
                    'accepted': False,
                }
            else:
                mobile, yw_id = self._resolve_lead_notification_context(conn, lead_id)
                response_code = crm_sync.get('crm_response_code')
                detail = crm_sync.get('crm_sync_failed') or 'CRM write was rejected.'
                final_reason = f'crm retry exhausted after {retry_count} attempts: {detail}' + (f' (code={response_code})' if response_code not in (None, '') else '')
                self._queue_operator_notification(
                    conn,
                    lead_id=lead_id,
                    notification_type='crm_record_failed',
                    mobile=mobile,
                    yw_id=yw_id,
                    write_result='failed',
                    reason=self._format_operator_crm_failure_reason(retried=True),
                )
                conn.execute(
                    "UPDATE automation_tasks SET status = 'failed', result_code = ?, result_reason = ?, finished_at = ?, raw_result = ? WHERE task_id = ?",
                    ('crm_retry_failed', final_reason, now, json.dumps({'crm_retry_failed': True}, ensure_ascii=False), task_id),
                )
                conn.commit()
                result = {
                    'task_id': task_id,
                    'lead_status': 'bind_success',
                    'next_action': 'retry_crm_sync',
                    'reason': 'crm_sync_failed',
                    'result_code': 'crm_retry_failed',
                    'result_reason': final_reason,
                    'crm_verified': False,
                    'current_submission_crm_verified': False,
                    'accepted': False,
                }

        message_id = reply_context.get('source_message_id') or ''
        chat_id = reply_context.get('source_chat_id') or ''
        if message_id or chat_id:
            with self.db.connect() as conn:
                lead_row = conn.execute("SELECT mobile, area_code, pendaftaran_group, inviter_id FROM leads WHERE lead_id = ?", (lead_id,)).fetchone()
            reply_envelope = {
                'accepted': bool(result.get('accepted')),
                'reason': result.get('reason'),
                'result_code': result.get('result_code'),
                'result_reason': result.get('result_reason'),
                'lead_status': result.get('lead_status'),
                'next_action': result.get('next_action'),
                'reply_phone': str((lead_row['mobile'] if lead_row else '') or '-'),
                'reply_area_code': int((lead_row['area_code'] if lead_row and lead_row['area_code'] is not None else 0) or 0),
                'reply_id': account_id or '-',
                'reply_group': str((lead_row['pendaftaran_group'] if lead_row else '') or '-'),
                'reply_code': str((lead_row['inviter_id'] if lead_row else '') or '-'),
                'crm_verified': result.get('crm_verified'),
                'current_submission_crm_verified': result.get('current_submission_crm_verified'),
            }
            if self._should_emit_lark_reply(reply_envelope):
                reply_adapter = self._resolve_lark_reply_adapter(app_id=reply_context.get('source_bot_app_id') or None)
                reply_text = self._format_lark_reply_text(reply_envelope)
                result['reply_text'] = reply_text
                self._reply_lark_message(message_id=message_id, chat_id=chat_id, text=reply_text, adapter=reply_adapter)
        return result

    def _queue_group_join_after_verified_crm(
        self,
        conn: sqlite3.Connection,
        *,
        lead_id: str,
        submission_id: Optional[str],
        account_id: Optional[str],
        created_at: str,
    ) -> Dict[str, Any]:
        dedupe_key = f"group_join:{lead_id}:{submission_id}"
        existing_dedupe_task = conn.execute(
            "SELECT task_id FROM automation_tasks WHERE dedupe_key = ? ORDER BY created_at DESC LIMIT 1",
            (dedupe_key,),
        ).fetchone()
        if existing_dedupe_task:
            return {
                'group_join_task_type': 'group_join',
                'group_join_task_id': existing_dedupe_task['task_id'],
            }
        existing_group_join = conn.execute(
            "SELECT task_id FROM automation_tasks WHERE lead_id = ? AND task_type = 'group_join' AND status = 'pending' ORDER BY created_at DESC LIMIT 1",
            (lead_id,),
        ).fetchone()
        if existing_group_join:
            group_join_task_id = existing_group_join['task_id']
        else:
            group_join_task_id = create_id("task")
            lead_row = conn.execute("SELECT * FROM leads WHERE lead_id = ?", (lead_id,)).fetchone()
            lead = dict(lead_row) if lead_row else {}
            resolved_target_group = self._resolve_official_group_target_group(lead=lead)
            group_payload = {
                "submission_id": submission_id,
                "lead_id": lead_id,
                "account_id": account_id,
                "target_group": resolved_target_group,
            }
            conn.execute(
                """
                INSERT INTO automation_tasks (
                    task_id, lead_id, task_type, priority, payload, dedupe_key, created_by, created_at, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    group_join_task_id,
                    lead_id,
                    "group_join",
                    "P0",
                    json.dumps(group_payload, ensure_ascii=False),
                    dedupe_key,
                    "system",
                    created_at,
                    "pending",
                ),
            )
        return {
            'group_join_task_type': 'group_join',
            'group_join_task_id': group_join_task_id,
        }

    def _resolve_official_group_target_group(self, *, lead: Dict[str, Any]) -> Optional[str]:
        if not isinstance(lead, dict):
            return None
        direct_candidate = str(lead.get('crm_verified_official_group') or '').strip()
        known_target_groups = {
            str(value or '').strip()
            for value in dict(self.official_group_target_map or {}).values()
            if str(value or '').strip()
        }
        if direct_candidate and direct_candidate in known_target_groups:
            return direct_candidate
        registration_group = str(lead.get('pendaftaran_group') or '').strip()
        dept_name = str(lead.get('crm_verified_dept_name') or lead.get('dept_name') or '').strip()
        app_name = str(lead.get('crm_verified_app_name') or lead.get('app_name') or '').strip()
        registration_prefix = registration_group.split('-', 1)[0].strip() if registration_group else ''
        lookup_keys = [
            f'registration_group:{registration_group.lower()}' if registration_group else '',
            f'registration_group_prefix:{registration_prefix.lower()}' if registration_prefix else '',
            f'dept_name:{dept_name.lower()}' if dept_name else '',
            f'app_name:{app_name.lower()}' if app_name else '',
            registration_group.lower() if registration_group else '',
            registration_prefix.lower() if registration_prefix else '',
            dept_name.lower() if dept_name else '',
            app_name.lower() if app_name else '',
        ]
        for key in lookup_keys:
            if not key:
                continue
            candidate = str(self.official_group_target_map.get(key) or '').strip()
            if candidate:
                return candidate
        if direct_candidate:
            return direct_candidate
        return None

    @staticmethod
    def _official_group_phone_match_keys(*, phone: Any, area_code: Any = 0, country: Any = '') -> set[str]:
        keys: set[str] = set()
        raw = str(phone or '').strip()
        digits_only = ''.join(ch for ch in raw if ch.isdigit())
        if digits_only:
            keys.add(digits_only)
        keys.update(localized_phone_match_keys(phone=raw, area_code=area_code, country=country))
        try:
            normalized_mobile, normalized_area_code, _ = normalize_phone_identity(
                mobile=raw,
                area_code=int(area_code or 0),
                country=str(country or '').strip(),
            )
        except Exception:
            normalized_mobile, normalized_area_code = digits_only, int(area_code or 0)
        normalized_mobile = str(normalized_mobile or '').strip()
        if normalized_mobile:
            keys.add(normalized_mobile)
            if normalized_area_code:
                keys.add(f'{int(normalized_area_code)}{normalized_mobile}')
        expanded_keys = set(keys)
        for key in list(keys):
            digits = ''.join(ch for ch in str(key or '') if ch.isdigit())
            if not digits:
                continue
            expanded_keys.update(Service._brazil_phone_ninth_digit_variants(digits))
        return {item for item in expanded_keys if item}

    @staticmethod
    def _brazil_phone_ninth_digit_variants(digits: str) -> set[str]:
        normalized = ''.join(ch for ch in str(digits or '') if ch.isdigit())
        has_country_code = normalized.startswith('55') and len(normalized) in {12, 13}
        has_area_code_only = len(normalized) in {10, 11}
        if not has_country_code and not has_area_code_only:
            return set()
        if has_country_code:
            area = normalized[2:4]
            local = normalized[4:]
        else:
            area = normalized[:2]
            local = normalized[2:]
        if not area or not local:
            return set()
        variants = {normalized, f'{area}{local}', local}
        if not has_country_code:
            variants.add(f'55{area}{local}')
        if len(local) == 9 and local.startswith('9'):
            without_ninth = local[1:]
            variants.update({
                f'55{area}{without_ninth}',
                f'{area}{without_ninth}',
                without_ninth,
            })
        elif len(local) == 8:
            with_ninth = f'9{local}'
            variants.update({
                f'55{area}{with_ninth}',
                f'{area}{with_ninth}',
                with_ninth,
            })
        return variants

    @staticmethod
    def _official_group_requester_id_phone_candidate(requester_id: Any) -> str:
        raw = str(requester_id or '').strip()
        lowered = raw.lower()
        if lowered.endswith('@lid') or lowered.endswith('@hosted.lid'):
            return ''
        return raw

    def _match_official_group_requesters_to_leads(
        self,
        *,
        lead_rows: List[sqlite3.Row],
        requesters: List[Dict[str, Any]],
        release_count: int,
    ) -> Tuple[List[sqlite3.Row], List[Dict[str, Any]]]:
        if not requesters:
            return [], []
        candidate_entries: List[Dict[str, Any]] = []
        for lead_row in lead_rows:
            lead = dict(lead_row)
            phone_keys = self._official_group_phone_match_keys(
                phone=lead.get('mobile'),
                area_code=lead.get('area_code'),
                country=lead.get('country'),
            )
            candidate_entries.append({
                'lead_row': lead_row,
                'lead_id': str(lead.get('lead_id') or '').strip(),
                'phone_keys': phone_keys,
            })
        selected_rows: List[Dict[str, Any]] = []
        unmatched_requesters: List[Dict[str, Any]] = []
        used_lead_ids: set[str] = set()
        for requester in list(requesters or [])[:max(0, release_count)]:
            requester_id = str((requester or {}).get('requesterId') or '').strip()
            requester_phone_keys = set()
            requester_phone_keys.update(self._official_group_phone_match_keys(phone=(requester or {}).get('phoneNormalized')))
            requester_phone_keys.update(self._official_group_phone_match_keys(phone=(requester or {}).get('phoneRaw')))
            requester_phone_keys.update(self._official_group_phone_match_keys(phone=(requester or {}).get('debugLidPhoneRaw')))
            requester_phone_keys.update(self._official_group_phone_match_keys(phone=(requester or {}).get('debugContactNumberRaw')))
            requester_phone_keys.update(self._official_group_phone_match_keys(phone=self._official_group_requester_id_phone_candidate(requester_id)))
            matches = [
                entry for entry in candidate_entries
                if entry['lead_id'] and entry['lead_id'] not in used_lead_ids and requester_phone_keys.intersection(entry['phone_keys'])
            ]
            if len(matches) == 1:
                matched_lead = dict(matches[0]['lead_row'])
                requester_phone_candidates = self._official_group_requester_phone_candidates(requester)
                matched_lead['matched_requester_phone_hint'] = requester_phone_candidates[0] if requester_phone_candidates else None
                matched_lead['matched_requester_name_hint'] = str((requester or {}).get('displayName') or '').strip() or None
                matched_lead['matched_requester_id'] = requester_id or None
                selected_rows.append(matched_lead)
                used_lead_ids.add(matches[0]['lead_id'])
                continue
            unmatched_requesters.append({
                'requester_id': requester_id or None,
                'display_name': str((requester or {}).get('displayName') or '').strip() or None,
                'phone_raw': str((requester or {}).get('phoneRaw') or '').strip() or None,
                'phone_normalized': str((requester or {}).get('phoneNormalized') or '').strip() or None,
                'debugLidPhoneRaw': str((requester or {}).get('debugLidPhoneRaw') or '').strip() or None,
                'debugContactNumberRaw': str((requester or {}).get('debugContactNumberRaw') or '').strip() or None,
                'requested_at_iso': str((requester or {}).get('requestedAtIso') or '').strip() or None,
                'requested_at_unix': (requester or {}).get('requestedAtUnix'),
                'match_count': len(matches),
            })
        return selected_rows, unmatched_requesters

    def _official_group_customer_projection_candidate_rows(self) -> List[Dict[str, Any]]:
        with self.db.connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT
                        COALESCE(l.lead_id, cp.lead_id) AS lead_id,
                        COALESCE(l.mobile, cp.mobile) AS mobile,
                        COALESCE(l.area_code, cp.area_code) AS area_code,
                        COALESCE(l.country, '') AS country,
                        COALESCE(l.yw_id, cp.yw_id) AS yw_id,
                        COALESCE(l.app_name, '') AS app_name,
                        COALESCE(l.dept_name, '') AS dept_name,
                        COALESCE(l.pendaftaran_group, cp.pendaftaran_group) AS pendaftaran_group,
                        COALESCE(l.current_status, 'crm_phone_matched') AS current_status,
                        cp.customer_id AS matched_customer_id,
                        cp.updated_at AS crm_projection_updated_at
                    FROM customer_projection cp
                    LEFT JOIN leads l ON l.lead_id = cp.lead_id
                    WHERE COALESCE(cp.mobile, '') <> ''
                    ORDER BY cp.updated_at DESC
                    """
                ).fetchall()
            ]

    def _match_official_group_requesters_to_phone_records(
        self,
        *,
        requesters: List[Dict[str, Any]],
        release_count: int,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        return self._match_official_group_requesters_to_leads(
            lead_rows=self._official_group_customer_projection_candidate_rows(),
            requesters=requesters,
            release_count=release_count,
        )

    def _official_group_requester_phone_candidates(self, requester: Dict[str, Any]) -> List[str]:
        candidates: List[str] = []
        seen: set[str] = set()

        def add_candidate(value: Any) -> None:
            raw = str(value or '').strip()
            if not raw:
                return
            digits = ''.join(ch for ch in raw if ch.isdigit())
            if digits and digits not in seen:
                seen.add(digits)
                candidates.append(digits)
            for key in sorted(localized_phone_match_keys(phone=raw)):
                normalized_key = key.lstrip('+').replace(' ', '')
                if normalized_key and normalized_key not in seen:
                    seen.add(normalized_key)
                    candidates.append(normalized_key)
            if raw.startswith('+'):
                try:
                    normalized_mobile, normalized_area_code, _ = normalize_phone_identity(mobile=raw, area_code=0, country='')
                except Exception:
                    normalized_mobile, normalized_area_code = digits, 0
                normalized_mobile = str(normalized_mobile or '').strip()
                if normalized_mobile and normalized_mobile not in seen:
                    seen.add(normalized_mobile)
                    candidates.append(normalized_mobile)
                if normalized_mobile and normalized_area_code:
                    prefixed = f'{int(normalized_area_code)}{normalized_mobile}'
                    if prefixed not in seen:
                        seen.add(prefixed)
                        candidates.append(prefixed)
            elif digits:
                for prefix in sorted(PHONE_PREFIX_COUNTRY_MAP.keys(), key=len, reverse=True):
                    if digits.startswith(prefix) and len(digits) > len(prefix) + 5:
                        local = digits[len(prefix):]
                        if local and local not in seen:
                            seen.add(local)
                            candidates.append(local)
                        break

        add_candidate((requester or {}).get('debugLidPhoneRaw'))
        add_candidate((requester or {}).get('debugContactNumberRaw'))
        add_candidate((requester or {}).get('phoneNormalized'))
        add_candidate((requester or {}).get('phone_normalized'))
        add_candidate((requester or {}).get('phoneRaw'))
        add_candidate((requester or {}).get('phone_raw'))
        add_candidate(self._official_group_requester_id_phone_candidate((requester or {}).get('requesterId')))
        add_candidate(self._official_group_requester_id_phone_candidate((requester or {}).get('requester_id')))
        return candidates

    def _find_crm_customer_for_official_group_requester(self, requester: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        if self.crm_adapter is None:
            return None, None
        for mobile_candidate in self._official_group_requester_phone_candidates(requester):
            try:
                row = self.crm_adapter.find_customer(mobile=mobile_candidate)
            except Exception:
                row = None
            if row:
                return dict(row), mobile_candidate
        return None, None

    def _find_customer_projection_for_official_group_phone(self, phone: Any) -> Optional[Dict[str, Any]]:
        phone_keys = self._official_group_phone_match_keys(phone=phone)
        if not phone_keys:
            return None
        rows = self._official_group_customer_projection_candidate_rows()
        for row in rows:
            row_keys = set()
            row_keys.update(self._official_group_phone_match_keys(phone=row.get('mobile'), area_code=row.get('area_code'), country=row.get('country')))
            if phone_keys.intersection(row_keys):
                return dict(row)
        return None

    def _official_group_phone_approval_check(
        self,
        *,
        target_group: str,
        target_phone_hint: Any,
        checked_at: str,
        checked_by: Optional[str] = None,
        checked_by_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        phone = str(target_phone_hint or '').strip()
        result: Dict[str, Any] = {
            'lead_id': None,
            'target_group': target_group,
            'checked_at': checked_at,
            'checked_by': checked_by,
            'checked_by_name': checked_by_name,
            'approval_requester_phone': phone or None,
            'crm_customer_found': False,
            'crm_identity_match': False,
            'crm_snapshot': None,
            'eligible': False,
            'reason_code': 'unknown',
            'reason_detail': None,
            'next_action': 'manual_review_official_group_approval',
            'source': 'official_group_phone_match',
        }
        if not phone:
            result.update({
                'reason_code': 'approval_requester_phone_missing',
                'reason_detail': 'WhatsApp approval requester phone is required for official-group approval.',
            })
            return result
        projection = self._find_customer_projection_for_official_group_phone(phone)
        if projection:
            result.update({
                'crm_customer_found': True,
                'crm_identity_match': True,
                'matched_customer_id': projection.get('matched_customer_id'),
                'lead_id': projection.get('lead_id') or None,
                'closing_record_yw_id': projection.get('yw_id') or None,
                'crm_snapshot': {
                    'id': projection.get('matched_customer_id'),
                    'mobile': projection.get('mobile'),
                    'ywId': projection.get('yw_id'),
                    'pendaftaranGroup': projection.get('pendaftaran_group'),
                    'source': 'customer_projection',
                },
                'eligible': True,
                'reason_code': 'eligible',
                'reason_detail': 'WhatsApp requester phone matched local CRM projection; official-group approval is allowed.',
                'next_action': 'approve_official_group',
            })
            return result
        if self.crm_adapter is not None:
            crm_row = None
            matched_mobile = ''
            for mobile_candidate in self._official_group_requester_phone_candidates({'phoneNormalized': phone, 'phoneRaw': phone}):
                try:
                    crm_row = self.crm_adapter.find_customer(mobile=mobile_candidate)
                except Exception:
                    crm_row = None
                if crm_row:
                    matched_mobile = mobile_candidate
                    break
            if crm_row:
                result.update({
                    'crm_customer_found': True,
                    'crm_identity_match': True,
                    'matched_customer_id': str(crm_row.get('id') or '').strip() or None,
                    'closing_record_yw_id': str(crm_row.get('ywId') or '').strip() or None,
                    'crm_snapshot': {
                        'id': crm_row.get('id'),
                        'mobile': crm_row.get('mobile') or matched_mobile,
                        'ywId': crm_row.get('ywId'),
                        'appName': crm_row.get('appName'),
                        'deptName': crm_row.get('deptName'),
                        'pendaftaranGroup': crm_row.get('pendaftaranGroup'),
                        'wa': crm_row.get('wa'),
                        'joinGroup': crm_row.get('joinGroup'),
                        'source': 'live_crm_phone',
                    },
                    'eligible': True,
                    'reason_code': 'eligible',
                    'reason_detail': 'WhatsApp requester phone matched live CRM; official-group approval is allowed.',
                    'next_action': 'approve_official_group',
                })
                return result
        result.update({
            'reason_code': 'crm_phone_not_found',
            'reason_detail': 'WhatsApp requester phone did not match local CRM projection or live CRM.',
        })
        return result

    def bind_check_result(self, task_id: str, payload: BindCheckResultRequest) -> Dict[str, Any]:
        now = utc_now()
        with self.db.connect() as conn:
            task = conn.execute("SELECT lead_id, payload, retry_count, created_by FROM automation_tasks WHERE task_id = ?", (task_id,)).fetchone()
            if not task:
                raise HTTPException(status_code=404, detail="task not found")
            task_payload = json.loads(task["payload"] or "{}")
            submission_id = task_payload.get("submission_id")
            account_id = task_payload.get("account_id")
            current_retry_count = int(task['retry_count'] or 0)
            attempt_number = current_retry_count + 1
            lead_row = conn.execute("SELECT * FROM leads WHERE lead_id = ?", (task['lead_id'],)).fetchone()
            effective_raw_result = dict(payload.raw_result or {})
            effective_status = payload.status
            effective_result_code = payload.result_code
            effective_result_reason = payload.result_reason
            bind_human_action = self._classify_bind_human_action(
                result_code=effective_result_code,
                result_reason=effective_result_reason,
                raw_result=effective_raw_result,
            )
            bind_failure_meta = self._classify_bind_failure(
                result_code=effective_result_code,
                result_reason=effective_result_reason,
                raw_result=effective_raw_result,
            )
            effective_raw_result.update({k: v for k, v in bind_human_action.items() if v is not None})
            effective_raw_result.update({k: v for k, v in bind_failure_meta.items() if v is not None})
            effective_raw_result['attempt_number'] = attempt_number
            effective_raw_result['retry_count'] = current_retry_count
            if str(effective_raw_result.get('precheck') or '').strip() == 'already_in_target_guild':
                effective_status = 'failed'
                effective_result_code = 'already_in_target_guild'
                effective_result_reason = 'Previously registered in this agency'
                bind_human_action = self._classify_bind_human_action(
                    result_code=effective_result_code,
                    result_reason=effective_result_reason,
                    raw_result=effective_raw_result,
                )
                bind_failure_meta = self._classify_bind_failure(
                    result_code=effective_result_code,
                    result_reason=effective_result_reason,
                    raw_result=effective_raw_result,
                )
                effective_raw_result.update({k: v for k, v in bind_human_action.items() if v is not None})
                effective_raw_result.update({k: v for k, v in bind_failure_meta.items() if v is not None})
            if payload.status == "success" and effective_status == "success":
                mismatch = self._detect_bind_backend_guild_mismatch(
                    task_payload=task_payload,
                    lead_row=lead_row,
                    raw_result=effective_raw_result,
                )
                if mismatch:
                    effective_status = 'failed'
                    effective_result_code = 'bind_backend_guild_mismatch'
                    effective_result_reason = mismatch['result_reason']
                    effective_raw_result.update(mismatch)
                    bind_failure_meta = self._classify_bind_failure(
                        result_code=effective_result_code,
                        result_reason=effective_result_reason,
                        raw_result=effective_raw_result,
                    )
                    effective_raw_result.update({k: v for k, v in bind_failure_meta.items() if v is not None})
            conn.execute(
                """
                INSERT OR REPLACE INTO bind_check_jobs (
                    job_id, lead_id, submission_id, account_id, guild_code, check_source, status,
                    result_code, result_reason, raw_result, retry_count, scheduled_at, finished_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    task["lead_id"],
                    submission_id,
                    account_id,
                    (effective_raw_result or {}).get("guild_code"),
                    "manual_backend",
                    effective_status,
                    effective_result_code,
                    effective_result_reason,
                    json.dumps(effective_raw_result, ensure_ascii=False),
                    current_retry_count,
                    payload.finished_at,
                    payload.finished_at,
                    now,
                ),
            )
            conn.execute(
                """
                UPDATE automation_tasks
                SET status = ?, result_code = ?, result_reason = ?, finished_at = ?, raw_result = ?, lease_until = '', heartbeat_at = ''
                WHERE task_id = ?
                """,
                (
                    effective_status,
                    effective_result_code,
                    effective_result_reason,
                    payload.finished_at,
                    json.dumps(effective_raw_result, ensure_ascii=False),
                    task_id,
                ),
            )
            if effective_status == "success":
                conn.execute("UPDATE leads SET current_status = ?, updated_at = ? WHERE lead_id = ?", ("bind_success", now, task["lead_id"]))
                self._record_status_history(
                    conn,
                    lead_id=task["lead_id"],
                    from_status="bind_check_pending",
                    to_status="bind_success",
                    trigger_type="bind_check_success",
                    trigger_source="bind_check_result",
                    trigger_task_id=task_id,
                )
                reply_context = {
                    'source_message_id': str(task_payload.get('source_message_id') or ''),
                    'source_chat_id': str(task_payload.get('source_chat_id') or ''),
                    'source_bot_app_id': str(task_payload.get('source_bot_app_id') or ''),
                }
                crm_sync = self._sync_crm_after_bind_success(
                    conn,
                    lead_id=task['lead_id'],
                    account_id=account_id,
                    task_id=task_id,
                    bind_result_reason=effective_result_reason,
                    bind_raw_result=effective_raw_result,
                    submission_id=submission_id,
                    reply_context=reply_context,
                )
                crm_sync_failed = crm_sync['crm_sync_failed']
                if crm_sync_failed:
                    if crm_sync.get('crm_retry_pending'):
                        scheduled = self._schedule_crm_retry_task(
                            conn,
                            submission_id=str(submission_id or ''),
                            lead_id=task['lead_id'],
                            account_id=str(account_id or ''),
                            bind_result_reason=effective_result_reason or '',
                            bind_raw_result=effective_raw_result,
                            source_payload=reply_context,
                            retry_count=1,
                        )
                        if scheduled:
                            return {
                                "task_id": task_id,
                                "lead_status": "bind_success",
                                "next_action": "queue_crm_sync_retry",
                                "reason": "crm_sync_retry_pending",
                                "result_reason": crm_sync_failed,
                                "group_join_task_type": None,
                                "crm_verified": False,
                                "current_submission_crm_verified": False,
                                "requires_human_action": False,
                                "human_action_type": None,
                                "retry_task_id": scheduled['task_id'],
                                "retry_count": scheduled['retry_count'],
                                "next_retry_at": scheduled['next_retry_at'],
                            }
                    return {
                        "task_id": task_id,
                        "lead_status": "bind_success",
                        "next_action": "retry_crm_sync",
                        "reason": "crm_sync_failed",
                        "result_reason": crm_sync_failed,
                        "bind_precheck": effective_raw_result.get('precheck'),
                        "group_join_task_type": None,
                        "crm_verified": False,
                        "current_submission_crm_verified": False,
                        "requires_human_action": False,
                        "human_action_type": None,
                    }
                group_join_meta = self._queue_group_join_after_verified_crm(
                    conn,
                    lead_id=task['lead_id'],
                    submission_id=submission_id,
                    account_id=account_id,
                    created_at=payload.finished_at,
                )
                return {
                    "task_id": task_id,
                    "lead_status": "bind_success",
                    "next_action": "queue_group_join",
                    **group_join_meta,
                    "bind_precheck": effective_raw_result.get('precheck'),
                    "crm_verified": True,
                    "current_submission_crm_verified": True,
                    "requires_human_action": False,
                    "human_action_type": None,
                }
            should_retry_bind = bool(bind_failure_meta.get('retryable')) and current_retry_count < self.bind_retry_max_attempts
            if should_retry_bind:
                retry_meta = self._schedule_bind_retry_task(
                    conn,
                    lead_id=task['lead_id'],
                    source_task_payload=task_payload,
                    source_created_by=str(task['created_by'] or '').strip() or None,
                    retry_count=current_retry_count + 1,
                )
                conn.execute("UPDATE leads SET current_status = ?, updated_at = ? WHERE lead_id = ?", ("bind_check_pending", now, task["lead_id"]))
                self._record_status_history(
                    conn,
                    lead_id=task["lead_id"],
                    from_status="bind_check_pending",
                    to_status="bind_check_pending",
                    trigger_type="bind_check_retry_scheduled",
                    trigger_source="bind_check_result",
                    trigger_task_id=retry_meta['task_id'],
                    remark=f"retry {retry_meta['retry_count']}/{self.bind_retry_max_attempts}",
                )
                return {
                    "task_id": task_id,
                    "lead_status": "bind_check_pending",
                    "next_action": "queue_bind_retry",
                    "reason": "bind_retry_pending",
                    "result_code": effective_result_code,
                    "result_reason": effective_result_reason,
                    "group_join_task_type": None,
                    "requires_human_action": False,
                    "human_action_type": None,
                    "bind_failure_category": bind_failure_meta.get('failure_category'),
                    "retry_task_id": retry_meta['task_id'],
                    "retry_count": retry_meta['retry_count'],
                }
            conn.execute("UPDATE leads SET current_status = ?, updated_at = ? WHERE lead_id = ?", ("bind_failed", now, task["lead_id"]))
            lead_row = conn.execute("SELECT mobile FROM leads WHERE lead_id = ?", (task['lead_id'],)).fetchone()
            self._queue_operator_notification(
                conn,
                lead_id=task['lead_id'],
                notification_type="bind_check_failed",
                mobile=(lead_row['mobile'] if lead_row else ''),
                yw_id=account_id,
                write_result="failed",
                reason=self._format_operator_bind_failure_reason(
                    failure_meta=bind_failure_meta,
                    raw_reason=effective_result_reason,
                    retried=current_retry_count >= self.bind_retry_max_attempts,
                ),
            )
            self._record_status_history(
                conn,
                lead_id=task["lead_id"],
                from_status="bind_check_pending",
                to_status="bind_failed",
                trigger_type="bind_check_failed",
                trigger_source="bind_check_result",
                trigger_task_id=task_id,
            )
            return {
                "task_id": task_id,
                "lead_status": "bind_failed",
                "next_action": "queue_reengagement",
                "reason": "bind_backend_guild_mismatch" if effective_result_code == "bind_backend_guild_mismatch" else ("already_in_target_guild" if effective_result_code == "already_in_target_guild" else "bind_check_failed"),
                "result_code": effective_result_code,
                "result_reason": effective_result_reason,
                "bind_precheck": effective_raw_result.get('precheck'),
                "group_join_task_type": None,
                "requires_human_action": bool(bind_human_action.get('requires_human_action')),
                "human_action_type": bind_human_action.get('human_action_type'),
                "bind_failure_category": bind_failure_meta.get('failure_category'),
            }

    def group_join_result(self, task_id: str, payload: GroupJoinResultRequest) -> Dict[str, Any]:
        now = utc_now()
        with self.db.connect() as conn:
            task = conn.execute("SELECT lead_id, payload FROM automation_tasks WHERE task_id = ?", (task_id,)).fetchone()
            if not task:
                raise HTTPException(status_code=404, detail="task not found")
            task_payload = json.loads(task["payload"] or "{}")
            submission_id = task_payload.get("submission_id")
            account_id = task_payload.get("account_id")
            conn.execute(
                """
                INSERT OR REPLACE INTO group_join_jobs (
                    job_id, lead_id, submission_id, account_id, target_group, join_type, status,
                    result_code, result_reason, raw_result, retry_count, scheduled_at, finished_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    task["lead_id"],
                    submission_id,
                    account_id,
                    (payload.raw_result or {}).get("target_group"),
                    "official_group",
                    payload.status,
                    payload.result_code,
                    payload.result_reason,
                    json.dumps(payload.raw_result, ensure_ascii=False),
                    0,
                    payload.finished_at,
                    payload.finished_at,
                    now,
                ),
            )
            conn.execute(
                """
                UPDATE automation_tasks
                SET status = ?, result_code = ?, result_reason = ?, finished_at = ?, raw_result = ?, lease_until = '', heartbeat_at = ''
                WHERE task_id = ?
                """,
                (
                    payload.status,
                    payload.result_code,
                    payload.result_reason,
                    payload.finished_at,
                    json.dumps(payload.raw_result, ensure_ascii=False),
                    task_id,
                ),
            )
            if payload.status == "success":
                conn.execute("UPDATE leads SET current_status = ?, updated_at = ? WHERE lead_id = ?", ("group_join_success", now, task["lead_id"]))
                crm_sync_status = 'skipped'
                crm_result_reason = None
                if self.crm_adapter is not None:
                    lead_row = conn.execute("SELECT * FROM leads WHERE lead_id = ?", (task['lead_id'],)).fetchone()
                    if lead_row:
                        lead_dict = dict(lead_row)
                        existing = self.crm_adapter.find_customer(yw_id=account_id, mobile=lead_dict.get('mobile'))
                        if existing:
                            crm_payload = dict(existing)
                            raw_result = payload.raw_result or {}
                            official_group_display_name = self._resolve_official_group_display_name(
                                target_group=str(raw_result.get('target_group') or '').strip(),
                                raw_result=raw_result,
                            )
                            if official_group_display_name:
                                crm_payload['wa'] = official_group_display_name
                            crm_payload['pendaftaranGroup'] = lead_dict.get('pendaftaran_group') or existing.get('pendaftaranGroup') or ''
                            crm_response = self.crm_adapter.update_customer(crm_payload)
                            verified_row = None
                            official_group_for_verify = official_group_display_name or str(raw_result.get('target_group') or '').strip()
                            if crm_response.get('code') == 0:
                                verified_row = self._find_existing_customer_with_fallback(
                                    yw_id=account_id,
                                    mobile=lead_dict.get('mobile'),
                                    app_name=crm_payload.get('appName'),
                                    dept_name=crm_payload.get('deptName'),
                                    registration_group=crm_payload.get('pendaftaranGroup'),
                                    official_group=official_group_for_verify,
                                )
                            crm_sync_status = 'success' if crm_response.get('code') == 0 and verified_row else 'failed'
                            if crm_response.get('code') != 0:
                                crm_result_reason = self._normalize_crm_failure_reason(crm_response, fallback_found=False)
                            elif not verified_row:
                                crm_result_reason = 'CRM write could not be verified.'
                            else:
                                self._record_verified_crm_state(
                                    conn,
                                    lead_id=task['lead_id'],
                                    crm_payload=crm_payload,
                                    official_group=crm_payload.get('wa'),
                                )
                            self._record_sync_log(
                                conn,
                                lead_id=task['lead_id'],
                                task_id=task_id,
                                sync_type='official_group_update',
                                target_system='crm',
                                status=crm_sync_status,
                                request_snapshot=crm_payload,
                                response_snapshot={
                                    'action': 'update',
                                    'crm_response': crm_response,
                                    'verified_after_write': bool(verified_row),
                                },
                            )
                self._record_status_history(
                    conn,
                    lead_id=task["lead_id"],
                    from_status="group_join_pending",
                    to_status="group_join_success",
                    trigger_type="group_join_success",
                    trigger_source="group_join_result",
                    trigger_task_id=task_id,
                )
                return {
                    "task_id": task_id,
                    "lead_status": "group_join_success",
                    "next_action": "close_or_education",
                    "crm_sync_status": crm_sync_status,
                    "crm_result_reason": crm_result_reason,
                    "crm_verified": crm_sync_status == 'success',
                    "current_submission_crm_verified": crm_sync_status == 'success',
                }
            conn.execute("UPDATE leads SET current_status = ?, updated_at = ? WHERE lead_id = ?", ("group_join_failed", now, task["lead_id"]))
            self._record_status_history(
                conn,
                lead_id=task["lead_id"],
                from_status="group_join_pending",
                to_status="group_join_failed",
                trigger_type="group_join_failed",
                trigger_source="group_join_result",
                trigger_task_id=task_id,
            )
            return {
                "task_id": task_id,
                "lead_status": "group_join_failed",
                "next_action": "queue_reengagement",
            }

    def ops_manual_review_queue(self) -> Dict[str, Any]:
        with self.db.connect() as conn:
            rows = [dict(r) for r in conn.execute(
                """
                SELECT l.lead_id, l.mobile, l.area_code, l.yw_id, l.app_name, l.dept_name, l.pendaftaran_group,
                       l.current_status, l.updated_at, l.parser_confidence, l.parser_status,
                       l.review_reason_codes, l.routing_decision, l.recommended_next_action,
                       l.parser_raw_ocr_text,
                       (SELECT t.task_id FROM automation_tasks t
                         WHERE t.lead_id = l.lead_id AND t.task_type = 'manual_review'
                         ORDER BY t.created_at DESC LIMIT 1) AS task_id
                FROM leads l
                WHERE l.current_status = 'manual_review_pending'
                ORDER BY l.updated_at DESC
                """
            ).fetchall()]
            for row in rows:
                row['review_reason_codes'] = json.loads(row.get('review_reason_codes') or '[]')
                recognition_codes = {}
                latest_submission = conn.execute(
                    "SELECT recognition_raw FROM account_submissions WHERE lead_id = ? ORDER BY created_at DESC LIMIT 1",
                    (row['lead_id'],),
                ).fetchone()
                if latest_submission and latest_submission['recognition_raw']:
                    recognition_raw = json.loads(latest_submission['recognition_raw'] or '{}')
                    recognition_codes = recognition_raw.get('normalized') or recognition_raw
                elif row.get('parser_raw_ocr_text'):
                    recognition_codes = normalize_native_ocr_fields(row['parser_raw_ocr_text'])
                row['person_code'] = recognition_codes.get('person_code')
                row['guild_invite_code'] = recognition_codes.get('guild_invite_code')
            return {'rows': rows}

    def resolve_manual_review(self, lead_id: str, payload: ManualReviewResolveRequest) -> Dict[str, Any]:
        if payload.decision not in {'approve_bind', 'reject_submission', 'request_recognition_retry'}:
            raise HTTPException(status_code=400, detail='unsupported decision')
        with self.db.connect() as conn:
            lead = conn.execute("SELECT * FROM leads WHERE lead_id = ?", (lead_id,)).fetchone()
            if not lead:
                raise HTTPException(status_code=404, detail='lead not found')
            lead_dict = dict(lead)
            if lead_dict.get('current_status') != 'manual_review_pending':
                raise HTTPException(status_code=400, detail='lead is not pending manual review')
            latest_review_task = conn.execute(
                "SELECT task_id, payload FROM automation_tasks WHERE lead_id = ? AND task_type = 'manual_review' ORDER BY created_at DESC LIMIT 1",
                (lead_id,),
            ).fetchone()
            review_id = create_id('review')
            correction_count = 0
            snapshot_before = {
                'yw_id': lead_dict.get('yw_id'),
                'app_name': lead_dict.get('app_name'),
                'dept_name': lead_dict.get('dept_name'),
                'registration_group': lead_dict.get('pendaftaran_group'),
                'parser_status': lead_dict.get('parser_status'),
                'routing_decision': lead_dict.get('routing_decision'),
            }
            updates = {
                'yw_id': payload.account_id or lead_dict.get('yw_id'),
                'app_name': payload.app_name or lead_dict.get('app_name'),
                'dept_name': payload.dept_name or lead_dict.get('dept_name'),
                'pendaftaran_group': payload.registration_group or lead_dict.get('pendaftaran_group'),
            }
            for field_name, old_value, new_value in [
                ('account_id', lead_dict.get('yw_id'), updates['yw_id']),
                ('app_name', lead_dict.get('app_name'), updates['app_name']),
                ('dept_name', lead_dict.get('dept_name'), updates['dept_name']),
                ('registration_group', lead_dict.get('pendaftaran_group'), updates['pendaftaran_group']),
            ]:
                if (old_value or '') != (new_value or '') and new_value is not None:
                    correction_count += 1
                    conn.execute(
                        """
                        INSERT INTO lead_corrections (
                            correction_id, lead_id, field_name, old_value, new_value, corrected_by, review_id, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (create_id('corr'), lead_id, field_name, old_value, new_value, payload.reviewed_by, review_id, utc_now()),
                    )
            if correction_count == 0 and lead_dict.get('parser_status') == 'conflict' and payload.account_id:
                correction_count += 1
                conn.execute(
                    """
                    INSERT INTO lead_corrections (
                        correction_id, lead_id, field_name, old_value, new_value, corrected_by, review_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        create_id('corr'),
                        lead_id,
                        'account_id',
                        'conflict_resolved',
                        payload.account_id,
                        payload.reviewed_by,
                        review_id,
                        utc_now(),
                    ),
                )
            review_task_payload = json.loads((latest_review_task['payload'] if latest_review_task else '{}') or '{}')
            created_task_id = None
            next_action = 'manual_followup'
            review_status = 'rejected'
            if payload.decision == 'approve_bind':
                account_id = updates['yw_id']
                if not str(account_id or '').isdigit():
                    raise HTTPException(status_code=400, detail='account_id is required for approve_bind')
                conn.execute(
                    """
                    UPDATE leads
                    SET yw_id = ?, app_name = ?, dept_name = ?, pendaftaran_group = ?, parser_status = ?,
                        routing_decision = ?, recommended_next_action = ?, review_status = ?, review_notes = ?,
                        reviewed_by = ?, reviewed_at = ?, correction_count = correction_count + ?, updated_at = ?
                    WHERE lead_id = ?
                    """,
                    (
                        updates['yw_id'], updates['app_name'], updates['dept_name'], updates['pendaftaran_group'], 'reviewed_ready',
                        'queue_bind_check', 'queue_bind_check', 'approved', payload.review_note,
                        payload.reviewed_by, payload.submitted_at, correction_count, utc_now(), lead_id,
                    ),
                )
                created = self.submit_account(
                    AccountSubmissionRequest(
                        lead_id=lead_id,
                        submission_type='account_id',
                        account_id=str(account_id),
                        account_id_type='platform_uid',
                        source_channel='manual_review',
                        submitted_by=payload.reviewed_by,
                        submitted_at=payload.submitted_at,
                        remark=payload.review_note,
                    )
                )
                created_task_id = created['task_id']
                next_action = created['next_action']
                review_status = 'approved'
            elif payload.decision == 'request_recognition_retry':
                conn.execute(
                    """
                    UPDATE leads
                    SET parser_status = ?, routing_decision = ?, recommended_next_action = ?, review_status = ?,
                        review_notes = ?, reviewed_by = ?, reviewed_at = ?, updated_at = ?
                    WHERE lead_id = ?
                    """,
                    (
                        'needs_recognition',
                        'queue_account_recognition',
                        'queue_account_recognition',
                        'retry_requested',
                        payload.review_note,
                        payload.reviewed_by,
                        payload.submitted_at,
                        utc_now(),
                        lead_id,
                    ),
                )
                created = self.submit_account(
                    AccountSubmissionRequest(
                        lead_id=lead_id,
                        submission_type='screenshot',
                        file_url=review_task_payload.get('file_url'),
                        file_type=review_task_payload.get('file_type'),
                        source_channel='manual_review_retry',
                        submitted_by=payload.reviewed_by,
                        submitted_at=payload.submitted_at,
                        remark=payload.review_note,
                    )
                )
                created_task_id = created['task_id']
                next_action = created['next_action']
                review_status = 'retry_requested'
            else:
                conn.execute(
                    """
                    UPDATE leads
                    SET parser_status = ?, routing_decision = ?, recommended_next_action = ?, review_status = ?,
                        review_notes = ?, reviewed_by = ?, reviewed_at = ?, updated_at = ?
                    WHERE lead_id = ?
                    """,
                    ('rejected', 'manual_followup', 'manual_followup', 'rejected', payload.review_note, payload.reviewed_by, payload.submitted_at, utc_now(), lead_id),
                )
                conn.execute("UPDATE leads SET current_status = ?, updated_at = ? WHERE lead_id = ?", ('re_engage_pending', utc_now(), lead_id))
                self._record_status_history(
                    conn,
                    lead_id=lead_id,
                    from_status='manual_review_pending',
                    to_status='re_engage_pending',
                    trigger_type='manual_review_rejected',
                    trigger_source='ops_manual_review',
                    trigger_task_id=latest_review_task['task_id'] if latest_review_task else None,
                    operator_name=payload.reviewed_by,
                    remark=payload.review_note,
                )
            if latest_review_task:
                conn.execute(
                    "UPDATE automation_tasks SET status = ?, result_code = ?, result_reason = ?, finished_at = ?, raw_result = ? WHERE task_id = ?",
                    ('success', payload.decision, payload.review_note, payload.submitted_at, json.dumps({'decision': payload.decision}, ensure_ascii=False), latest_review_task['task_id']),
                )
            snapshot_after = {
                'yw_id': updates['yw_id'],
                'app_name': updates['app_name'],
                'dept_name': updates['dept_name'],
                'registration_group': updates['pendaftaran_group'],
                'decision': payload.decision,
                'next_action': next_action,
            }
            conn.execute(
                """
                INSERT INTO manual_review_history (
                    review_id, lead_id, decision, reviewed_by, review_note, snapshot_before, snapshot_after,
                    created_task_id, submitted_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    review_id,
                    lead_id,
                    payload.decision,
                    payload.reviewed_by,
                    payload.review_note,
                    json.dumps(snapshot_before, ensure_ascii=False),
                    json.dumps(snapshot_after, ensure_ascii=False),
                    created_task_id,
                    payload.submitted_at,
                    utc_now(),
                ),
            )
            return {
                'accepted': True,
                'lead_id': lead_id,
                'decision': payload.decision,
                'task_id': created_task_id,
                'next_action': next_action,
                'correction_count': correction_count,
                'review_status': review_status,
            }

    def parser_quality_summary(self) -> Dict[str, Any]:
        with self.db.connect() as conn:
            manual_review_count = conn.execute("SELECT COUNT(*) FROM leads WHERE review_status IN ('pending','approved','rejected')").fetchone()[0]
            approved_review_count = conn.execute("SELECT COUNT(*) FROM leads WHERE review_status = 'approved'").fetchone()[0]
            parser_conflict_count = conn.execute("SELECT COUNT(*) FROM leads WHERE parser_status = 'conflict' OR review_reason_codes LIKE '%account_id_conflict%'").fetchone()[0]
            low_confidence_count = conn.execute("SELECT COUNT(*) FROM leads WHERE parser_status = 'low_confidence'").fetchone()[0]
            correction_count = conn.execute("SELECT COUNT(*) FROM lead_corrections").fetchone()[0]
            return {
                'manual_review_count': manual_review_count,
                'approved_review_count': approved_review_count,
                'parser_conflict_count': parser_conflict_count,
                'low_confidence_count': low_confidence_count,
                'correction_count': correction_count,
            }

    def lead_timeline(self, lead_id: str) -> Dict[str, Any]:
        with self.db.connect() as conn:
            lead = conn.execute("SELECT * FROM leads WHERE lead_id = ?", (lead_id,)).fetchone()
            if not lead:
                raise HTTPException(status_code=404, detail="lead not found")
            lead_dict = dict(lead)
            lead_dict['parser_missing_fields'] = json.loads(lead_dict.get('parser_missing_fields') or '[]')
            lead_dict['parser_conflicts'] = json.loads(lead_dict.get('parser_conflicts') or '[]')
            lead_dict['review_reason_codes'] = json.loads(lead_dict.get('review_reason_codes') or '[]')
            lead_dict['crm_verified_payload'] = json.loads(lead_dict.get('crm_verified_payload') or 'null')
            events = [dict(row) for row in conn.execute("SELECT * FROM lead_events WHERE lead_id = ? ORDER BY created_at ASC", (lead_id,)).fetchall()]
            tasks = [dict(row) for row in conn.execute("SELECT * FROM automation_tasks WHERE lead_id = ? ORDER BY created_at ASC", (lead_id,)).fetchall()]
            sync_logs = [dict(row) for row in conn.execute("SELECT * FROM sync_logs WHERE lead_id = ? ORDER BY created_at ASC", (lead_id,)).fetchall()]
            submissions = [dict(row) for row in conn.execute("SELECT * FROM account_submissions WHERE lead_id = ? ORDER BY created_at ASC", (lead_id,)).fetchall()]
            status_history = [dict(row) for row in conn.execute("SELECT * FROM lead_status_history WHERE lead_id = ? ORDER BY created_at ASC", (lead_id,)).fetchall()]
            review_history = [dict(row) for row in conn.execute("SELECT * FROM manual_review_history WHERE lead_id = ? ORDER BY created_at ASC", (lead_id,)).fetchall()]
            correction_history = [dict(row) for row in conn.execute("SELECT * FROM lead_corrections WHERE lead_id = ? ORDER BY created_at ASC", (lead_id,)).fetchall()]
            for task in tasks:
                task['payload'] = json.loads(task.get('payload') or '{}')
                task['raw_result'] = json.loads(task.get('raw_result') or '{}')
            for submission in submissions:
                submission['recognition_raw'] = json.loads(submission.get('recognition_raw') or '{}')
            return {
                "lead": lead_dict,
                "events": events,
                "tasks": tasks,
                "sync_logs": sync_logs,
                "account_submissions": submissions,
                "status_history": status_history,
                "review_history": review_history,
                "correction_history": correction_history,
            }

    def funnel_report(self) -> Dict[str, Any]:
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    source_platform,
                    COALESCE(source_campaign, '') AS source_campaign,
                    country,
                    COUNT(*) AS lead_count,
                    SUM(CASE WHEN current_status NOT IN ('new') THEN 1 ELSE 0 END) AS engaged_count,
                    SUM(CASE WHEN current_status IN ('account_submitted','bind_check_pending','bind_success','bind_failed','group_join_pending','group_join_success','group_join_failed','re_engage_pending','closed','synced') THEN 1 ELSE 0 END) AS account_submitted_count,
                    SUM(CASE WHEN current_status IN ('bind_success','group_join_pending','group_join_success','group_join_failed','closed','synced') THEN 1 ELSE 0 END) AS bind_success_count,
                    SUM(CASE WHEN current_status IN ('group_join_success','closed','synced') THEN 1 ELSE 0 END) AS group_join_success_count
                FROM leads
                GROUP BY source_platform, COALESCE(source_campaign, ''), country
                ORDER BY source_platform, source_campaign, country
                """
            ).fetchall()
            return {"rows": [dict(r) for r in rows]}

    def attach_voucher_for_lead(self, lead_id: str, image_path: str, remark_suffix: Optional[str] = None) -> Dict[str, Any]:
        if self.crm_adapter is None:
            raise HTTPException(status_code=400, detail='crm adapter not configured')
        with self.db.connect() as conn:
            lead = conn.execute("SELECT lead_id, mobile, area_code, yw_id FROM leads WHERE lead_id = ?", (lead_id,)).fetchone()
            if not lead:
                raise HTTPException(status_code=404, detail='lead not found')
            lead_dict = dict(lead)
            existing = self.crm_adapter.find_customer(yw_id=lead_dict.get('yw_id'), mobile=lead_dict.get('mobile'))
            if not existing:
                raise HTTPException(status_code=404, detail='crm customer not found for lead')
            image_url = self.crm_adapter.upload_voucher(customer_id=str(existing['id']), image_path=image_path)
            self.crm_adapter.attach_voucher(existing, image_url, remark_suffix=remark_suffix)
            return {'lead_id': lead_id, 'crm_customer_id': existing['id'], 'image_url': image_url, 'attached': True}

    def _resolve_registration_group_display_name(
        self,
        *,
        registration_group: str,
        raw_result: Optional[Dict[str, Any]] = None,
        expected_group_state: Optional[Dict[str, Any]] = None,
        current_group_state: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        def usable_group_name(value: Any) -> str:
            text = str(value or '').strip()
            if not text:
                return ''
            if _looks_like_whatsapp_invite_link(text) or _looks_like_whatsapp_group_jid(text):
                return ''
            return text

        for source in (raw_result, current_group_state, expected_group_state):
            if not isinstance(source, dict):
                continue
            for key in ('group_name', 'registration_group_name'):
                value = usable_group_name(source.get(key))
                if value:
                    return value
        binding_match = self._find_whatsapp_approval_account_binding(
            responsible_type='registration_group',
            target_group=str(registration_group or '').strip(),
        )
        binding = binding_match.get('binding') if isinstance(binding_match, dict) else {}
        if isinstance(binding, dict):
            value = usable_group_name(binding.get('group_name') or binding.get('target_group_label'))
            if value:
                return value
        value = usable_group_name(registration_group)
        return value or None

    def _resolve_official_group_display_name(
        self,
        *,
        target_group: str,
        raw_result: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        if isinstance(raw_result, dict):
            for key in ('group_name', 'official_group_name'):
                value = str(raw_result.get(key) or '').strip()
                if value:
                    return value
            nested_raw_result = raw_result.get('raw_result')
            if isinstance(nested_raw_result, dict):
                for key in ('group_name', 'official_group_name'):
                    value = str(nested_raw_result.get(key) or '').strip()
                    if value:
                        return value
        binding_match = self._find_whatsapp_approval_account_binding(
            responsible_type='official_group',
            target_group=target_group,
        )
        binding = binding_match.get('binding') if isinstance(binding_match, dict) else {}
        if isinstance(binding, dict):
            value = str(binding.get('group_name') or '').strip()
            if value:
                return value
        return None

    def _official_group_target_aliases(self, *, target_group: str) -> set[str]:
        aliases: set[str] = set()
        normalized_target = str(target_group or '').strip()
        if normalized_target:
            aliases.add(normalized_target.lower())
        binding_match = self._find_whatsapp_approval_account_binding(
            responsible_type='official_group',
            target_group=normalized_target,
        )
        binding = binding_match.get('binding') if isinstance(binding_match, dict) else {}
        if isinstance(binding, dict):
            runtime_group_id = self._whatsapp_binding_runtime_group_id(binding)
            if runtime_group_id:
                aliases.add(runtime_group_id.lower())
            for key in ('group_name', 'registration_group', 'group_id', 'link'):
                value = str(binding.get(key) or '').strip().lower()
                if value:
                    aliases.add(value)
        aliases.discard('')
        return aliases

    def _official_group_value_matches_target(self, *, value: Any, target_group: str) -> bool:
        normalized_value = str(value or '').strip().lower()
        if not normalized_value:
            return False
        if normalized_value in self._official_group_target_aliases(target_group=target_group):
            return True
        value_binding_match = self._find_whatsapp_approval_account_binding(
            responsible_type='official_group',
            target_group=normalized_value,
        )
        value_binding = value_binding_match.get('binding') if isinstance(value_binding_match, dict) else {}
        if isinstance(value_binding, dict):
            runtime_group_id = self._whatsapp_binding_runtime_group_id(value_binding)
            value_aliases = set()
            if runtime_group_id:
                value_aliases.add(runtime_group_id.lower())
            value_aliases.update({
                str(value_binding.get('group_name') or '').strip().lower(),
                str(value_binding.get('registration_group') or '').strip().lower(),
                str(value_binding.get('group_id') or '').strip().lower(),
                str(value_binding.get('link') or '').strip().lower(),
            })
            value_aliases.discard('')
            if str(target_group or '').strip().lower() in value_aliases:
                return True
        return False

    @staticmethod
    def _lead_has_already_in_target_guild_evidence(lead: Dict[str, Any]) -> bool:
        if not isinstance(lead, dict):
            return False
        current_status = str(lead.get('current_status') or '').strip()
        if current_status != 'bind_failed':
            return False
        evidence_values = [
            lead.get('result_code'),
            lead.get('result_reason'),
            lead.get('latest_result_code'),
            lead.get('latest_result_reason'),
            lead.get('bind_precheck'),
            lead.get('bind_failure_category'),
        ]
        raw_payload = lead.get('raw_result')
        if isinstance(raw_payload, str):
            try:
                raw_payload = json.loads(raw_payload or '{}')
            except Exception:
                raw_payload = {}
        if isinstance(raw_payload, dict):
            evidence_values.extend([
                raw_payload.get('result_code'),
                raw_payload.get('result_reason'),
                raw_payload.get('reason'),
                raw_payload.get('precheck'),
                raw_payload.get('category'),
            ])
        evidence_text = ' '.join(str(item or '').strip().lower() for item in evidence_values if str(item or '').strip())
        return bool(
            'already_in_target_guild' in evidence_text
            or 'previously registered in this agency' in evidence_text
        )

    def _lead_eligible_for_official_group_runtime_matching(
        self,
        *,
        lead: Dict[str, Any],
        target_group: str,
        official_statuses: tuple[str, ...],
    ) -> bool:
        if not isinstance(lead, dict):
            return False
        current_status = str(lead.get('current_status') or '').strip()
        if current_status == 'archived_test_residue':
            return False
        if current_status not in official_statuses:
            if self._lead_has_already_in_target_guild_evidence(lead):
                return True
            if current_status != 'console_cleared_test_data':
                return False
            if not (
                str(lead.get('matched_customer_id') or '').strip()
                or str(lead.get('crm_verified_official_group') or '').strip()
                or str(lead.get('crm_verified_registration_group') or '').strip()
            ):
                return False
        normalized_target_group = str(target_group or '').strip()
        if not normalized_target_group:
            return True
        candidate_values = [
            self._resolve_official_group_target_group(lead=lead),
            lead.get('crm_verified_official_group'),
        ]
        if any(
            self._official_group_value_matches_target(value=value, target_group=normalized_target_group)
            for value in candidate_values
            if str(value or '').strip()
        ):
            return True
        return bool(
            str(lead.get('yw_id') or '').strip()
            or str(lead.get('matched_customer_id') or '').strip()
            or str(lead.get('crm_verified_payload') or '').strip()
            or str(lead.get('crm_verified_at') or '').strip()
            or str(lead.get('crm_verified_official_group') or '').strip()
            or str(lead.get('crm_verified_registration_group') or '').strip()
        )

    def create_registration_group_approval_batch(self, payload: RegistrationGroupApprovalBatchRequest) -> Dict[str, Any]:
        if self.crm_adapter is None:
            raise HTTPException(status_code=400, detail='crm adapter not configured')
        resolved_group_no = str(payload.registration_group_name or '').strip() or str(payload.registration_group or '').strip()
        request_snapshot = {
            'registration_group': payload.registration_group,
            'registration_group_name': payload.registration_group_name,
            'approved_count': payload.approved_count,
            'approved_by': payload.approved_by,
            'approved_by_name': payload.approved_by_name,
            'source_platform': payload.source_platform,
            'source_campaign': payload.source_campaign,
            'source_adset': payload.source_adset,
            'source_ad': payload.source_ad,
            'approved_at': payload.approved_at,
            'area': payload.area,
            'remark': payload.remark,
            'approval_run_id': payload.approval_run_id,
        }
        normalized_run_id = str(payload.approval_run_id or '').strip()
        crm_payload = {
            'area': payload.area,
            'groupNo': resolved_group_no,
            'groupPeopleNum': str(payload.approved_count),
        }
        request_snapshot_with_payload = {
            **request_snapshot,
            'crm_payload': crm_payload,
        }
        with self._registration_group_approval_batch_lock:
            if normalized_run_id:
                claimed = self._claim_registration_group_approval_batch_run(normalized_run_id, request_snapshot_with_payload)
                if not claimed.get('claimed'):
                    existing = dict(claimed.get('row') or {})
                    if str(existing.get('status') or '').strip() == 'processing':
                        existing = self._wait_for_registration_group_approval_batch_run(normalized_run_id) or existing
                    if existing:
                        return self._build_registration_group_approval_batch_existing_response(
                            existing,
                            request_snapshot=request_snapshot,
                            fallback_crm_payload=crm_payload,
                        )
            started = time.perf_counter()
            try:
                crm_response = self.crm_adapter.create_registration_group_batch(crm_payload)
            except Exception as exc:
                crm_response = {
                    'code': -1,
                    'msg': str(exc),
                    'error_type': type(exc).__name__,
                }
            elapsed_seconds = round(time.perf_counter() - started, 3)
            sync_status = 'success' if crm_response.get('code') == 0 else 'failed'
            sync_log_id = create_id('sync')
            now = utc_now()
            with self.db.connect() as conn:
                conn.execute('BEGIN IMMEDIATE')
                conn.execute(
                    "INSERT INTO sync_logs (sync_log_id, lead_id, task_id, sync_type, target_system, status, request_snapshot, response_snapshot, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        sync_log_id,
                        None,
                        None,
                        'registration_group_approval_batch',
                        'crm',
                        sync_status,
                        json.dumps(request_snapshot_with_payload, ensure_ascii=False),
                        json.dumps(crm_response, ensure_ascii=False),
                        now,
                    ),
                )
                if normalized_run_id:
                    conn.execute(
                        "UPDATE registration_group_approval_batch_runs SET sync_log_id = ?, status = ?, request_snapshot = ?, response_snapshot = ?, updated_at = ? WHERE approval_run_id = ?",
                        (
                            sync_log_id,
                            sync_status,
                            json.dumps(request_snapshot_with_payload, ensure_ascii=False),
                            json.dumps(crm_response, ensure_ascii=False),
                            now,
                            normalized_run_id,
                        ),
                    )
                conn.commit()
        return {
            'accepted': True,
            'crm_sync_status': sync_status,
            'crm_payload': crm_payload,
            'crm_response': crm_response,
            'approval_run_id': payload.approval_run_id,
            'request_snapshot': request_snapshot,
            'elapsed_seconds': elapsed_seconds,
        }


__all__ = ['IntakeServiceMixin']
