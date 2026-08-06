from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_CONTROL_DB = Path(
    os.getenv("MCN_CONTROL_PLANE_DB") or "/data/mcn-data/control/mcn_control_plane.db"
)
BLOCKED_STATES = {"blocked", "review"}


def _connect(path: Path = DEFAULT_CONTROL_DB, *, write: bool = False) -> sqlite3.Connection:
    if write:
        conn = sqlite3.connect(path, timeout=15)
    else:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
        conn.execute("PRAGMA query_only=ON")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def list_unified_tasks(
    *, db_path: Path = DEFAULT_CONTROL_DB, normalized_status: str = "",
    source_system: str = "", query: str = "", blocked_only: bool = False,
    view: str = "",
    limit: int = 200, offset: int = 0,
) -> dict[str, Any]:
    clauses: list[str] = []
    values: list[Any] = []
    normalized_view = str(view or "").strip().lower()
    if normalized_view == "execution_queue":
        clauses.append("normalized_status='queued'")
        clauses.append("NOT (source_table='automation_tasks' AND task_type='group_join')")
        clauses.append("NOT (source_system='systemd' AND status='scheduled')")
        clauses.append("NOT (source_system='control_plane' AND task_type='acceptance_observation')")
    elif normalized_view == "waiting_user":
        clauses.append("normalized_status='queued'")
        clauses.append("source_table='automation_tasks' AND task_type='group_join'")
    elif normalized_view == "completed":
        clauses.append("normalized_status='success'")
    elif blocked_only:
        clauses.append("normalized_status IN ('blocked','review')")
    elif normalized_status:
        clauses.append("normalized_status=?")
        values.append(normalized_status)
    if source_system:
        clauses.append("source_system=?")
        values.append(source_system)
    if query:
        clauses.append("(native_task_id LIKE ? OR title LIKE ? OR task_type LIKE ? OR error_code LIKE ?)")
        pattern = f"%{query[:100]}%"
        values.extend([pattern, pattern, pattern, pattern])
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    page_limit = max(1, min(int(limit), 500))
    page_offset = max(0, int(offset))
    with _connect(db_path) as conn:
        total = int(conn.execute("SELECT COUNT(*) FROM tasks" + where, values).fetchone()[0])
        rows = conn.execute(
            "SELECT task_key,source_system,source_table,native_task_id,task_type,title,status,"
            "normalized_status,blocked_reason,error_code,owner,execution_authority,managed_mode,"
            "created_at_utc,updated_at_utc,observed_at_utc FROM tasks" + where +
            " ORDER BY CASE normalized_status WHEN 'failed' THEN 1 WHEN 'review' THEN 2 "
            "WHEN 'blocked' THEN 3 WHEN 'running' THEN 4 ELSE 5 END, updated_at_utc DESC "
            "LIMIT ? OFFSET ?",
            (*values, page_limit, page_offset),
        ).fetchall()
        counts = {row[0]: int(row[1]) for row in conn.execute(
            "SELECT normalized_status,COUNT(*) FROM tasks GROUP BY normalized_status"
        )}
        actionable = conn.execute(
            "SELECT COUNT(*),"
            "SUM(CASE WHEN source_system='control_plane' AND managed_mode='managed' THEN 1 ELSE 0 END) "
            "FROM tasks WHERE normalized_status IN ('blocked','review')"
        ).fetchone()
        view_summary = conn.execute(
            "SELECT "
            "SUM(CASE WHEN normalized_status='queued' "
            "AND NOT (source_table='automation_tasks' AND task_type='group_join') "
            "AND NOT (source_system='systemd' AND status='scheduled') "
            "AND NOT (source_system='control_plane' AND task_type='acceptance_observation') THEN 1 ELSE 0 END),"
            "SUM(CASE WHEN normalized_status='queued' AND source_table='automation_tasks' "
            "AND task_type='group_join' THEN 1 ELSE 0 END),"
            "SUM(CASE WHEN source_system='systemd' AND status='scheduled' THEN 1 ELSE 0 END),"
            "SUM(CASE WHEN normalized_status='success' THEN 1 ELSE 0 END) FROM tasks"
        ).fetchone()
        sources = [row[0] for row in conn.execute(
            "SELECT DISTINCT source_system FROM tasks ORDER BY source_system"
        )]
        coverage = [dict(row) for row in conn.execute(
            "SELECT source_system,source_table,expected_rows,projected_rows,status,error,observed_at_utc "
            "FROM task_source_inventory ORDER BY source_system,source_table"
        )]
    return {
        "ok": True, "tasks": [dict(row) for row in rows], "total": total,
        "limit": page_limit, "offset": page_offset, "counts": counts,
        "actionable_summary": {
            "total": int(actionable[0] or 0),
            "in_page": int(actionable[1] or 0),
            "in_source": int(actionable[0] or 0) - int(actionable[1] or 0),
        },
        "view_summary": {
            "execution_queue": int(view_summary[0] or 0),
            "waiting_user": int(view_summary[1] or 0),
            "scheduled": int(view_summary[2] or 0),
            "completed": int(view_summary[3] or 0),
        },
        "sources": sources, "coverage": coverage,
        "coverage_complete": all(row["status"] == "complete" for row in coverage),
    }


def get_unified_task(task_key: str, *, db_path: Path = DEFAULT_CONTROL_DB) -> dict[str, Any]:
    with _connect(db_path) as conn:
        row = conn.execute("SELECT * FROM tasks WHERE task_key=?", (task_key,)).fetchone()
        if not row:
            raise KeyError("task_not_found")
        events = [dict(item) for item in conn.execute(
            "SELECT previous_status,current_status,observed_at_utc,detail_json "
            "FROM task_projection_events WHERE task_key=? ORDER BY event_id DESC LIMIT 50",
            (task_key,),
        )]
        native_events: list[dict[str, Any]] = []
        if row["source_system"] == "control_plane":
            native_events = [dict(item) for item in conn.execute(
                "SELECT event_type,from_state,to_state,detail_json,created_at_utc "
                "FROM work_events WHERE work_id=? ORDER BY event_id DESC LIMIT 100",
                (row["native_task_id"],),
            )]
    return {"ok": True, "task": dict(row), "projection_events": events, "native_events": native_events}


def manage_unified_task(
    task_key: str, *, action: str, expected_version: int,
    reason: str, actor: str, db_path: Path = DEFAULT_CONTROL_DB,
) -> dict[str, Any]:
    if action not in {"requeue", "cancel", "fail"}:
        raise ValueError("task_action_invalid")
    now = datetime.now(timezone.utc).isoformat()
    with _connect(db_path, write=True) as conn:
        conn.execute("BEGIN IMMEDIATE")
        projected = conn.execute("SELECT * FROM tasks WHERE task_key=?", (task_key,)).fetchone()
        if not projected:
            conn.rollback()
            raise KeyError("task_not_found")
        if projected["source_system"] != "control_plane" or projected["managed_mode"] != "managed":
            conn.rollback()
            raise PermissionError("native_task_read_only")
        work = conn.execute(
            "SELECT work_id,state,version FROM work_items WHERE work_id=?",
            (projected["native_task_id"],),
        ).fetchone()
        if not work:
            conn.rollback()
            raise KeyError("work_item_not_found")
        if int(work["version"]) != int(expected_version):
            conn.rollback()
            raise RuntimeError("task_version_conflict")
        current = str(work["state"])
        if action == "requeue":
            if current not in {"blocked_soft", "blocked_hard", "manual_review", "escalated"}:
                conn.rollback()
                raise RuntimeError(f"task_action_not_allowed:{current}:requeue")
            target = "queued"
            conn.execute(
                "UPDATE work_stages SET state='queued',not_before_utc='',lease_owner='',lease_expires_at_utc='',"
                "updated_at_utc=?,version=version+1 WHERE work_id=? AND state IN ('blocked_soft','blocked_hard','manual_review')",
                (now, work["work_id"]),
            )
        elif action == "cancel":
            if current in {"accepted", "superseded", "cancelled", "failed"}:
                conn.rollback()
                raise RuntimeError(f"task_action_not_allowed:{current}:cancel")
            target = "cancelled"
            conn.execute(
                "UPDATE work_stages SET state='cancelled',lease_owner='',lease_expires_at_utc='',finished_at_utc=?,"
                "updated_at_utc=?,version=version+1 WHERE work_id=? AND state NOT IN ('succeeded','superseded','cancelled','failed')",
                (now, now, work["work_id"]),
            )
            conn.execute("DELETE FROM resource_leases WHERE stage_id IN (SELECT stage_id FROM work_stages WHERE work_id=?)", (work["work_id"],))
        else:
            if current in {"accepted", "superseded", "cancelled", "failed"}:
                conn.rollback()
                raise RuntimeError(f"task_action_not_allowed:{current}:fail")
            target = "failed"
            conn.execute(
                "UPDATE work_stages SET state='failed',lease_owner='',lease_expires_at_utc='',finished_at_utc=?,"
                "updated_at_utc=?,version=version+1 WHERE work_id=? AND state NOT IN ('succeeded','superseded','cancelled','failed')",
                (now, now, work["work_id"]),
            )
            conn.execute("DELETE FROM resource_leases WHERE stage_id IN (SELECT stage_id FROM work_stages WHERE work_id=?)", (work["work_id"],))
        detail = json.dumps({"reason": reason[:500], "actor": actor[:120], "source": "ops_task_control"}, ensure_ascii=False, sort_keys=True)
        changed = conn.execute(
            "UPDATE work_items SET state=?,block_reason=?,not_before_utc='',updated_at_utc=?,version=version+1 WHERE work_id=? AND version=?",
            (target, "" if action == "requeue" else reason[:500], now, work["work_id"], expected_version),
        )
        if changed.rowcount != 1:
            conn.rollback()
            raise RuntimeError("task_version_conflict")
        conn.execute(
            "INSERT INTO work_events(work_id,event_type,from_state,to_state,detail_json,created_at_utc) VALUES(?,?,?,?,?,?)",
            (work["work_id"], f"ops_{action}", current, target, detail, now),
        )
        conn.execute(
            "UPDATE tasks SET status=?,normalized_status=?,blocked_reason=?,updated_at_utc=?,observed_at_utc=? WHERE task_key=?",
            (target, "queued" if target == "queued" else target, "" if action == "requeue" else reason[:500], now, now, task_key),
        )
        conn.commit()
        updated = conn.execute("SELECT work_id,state,version FROM work_items WHERE work_id=?", (work["work_id"],)).fetchone()
    return {"ok": True, "work": dict(updated), "action": action}


TASK_CONTROL_PAGE_HTML = r'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>异常任务</title><style>
.task-workbench,.task-list-panel{background:var(--ops-panel,#fff);border:1px solid var(--ops-border,#e5ebf3);border-radius:var(--ops-r-xl,22px);box-shadow:var(--ops-shadow-card,0 8px 22px rgba(38,55,91,.045))}.task-workbench{overflow:hidden}.task-overview{display:grid;grid-template-columns:minmax(210px,1.25fr) repeat(3,minmax(128px,.62fr));align-items:stretch}.task-overview-primary,.task-overview-item{padding:18px 20px;min-width:0}.task-overview-item{border-left:1px solid var(--ops-border,#e5ebf3)}.task-eyebrow{display:block;margin-bottom:4px;color:var(--ops-muted,#718095);font-size:12px;font-weight:700}.task-overview-primary strong{display:block;font-size:30px;line-height:1.05;letter-spacing:-.03em;font-variant-numeric:tabular-nums}.task-overview-primary p{margin:7px 0 0;color:var(--ops-muted,#718095);font-size:12px}.task-overview-item strong{display:block;font-size:20px;line-height:1.15;font-variant-numeric:tabular-nums}.task-overview-item.is-danger strong{color:var(--ops-red,#dc2626)!important}.task-overview-item.is-warning strong{color:#b45309!important}.task-overview-item.is-healthy strong{color:#15803d!important}.task-views{display:flex;align-items:center;gap:4px;padding:10px 16px;border-top:1px solid var(--ops-border,#e5ebf3);border-bottom:1px solid var(--ops-border,#e5ebf3);background:var(--ops-surface,#f8fbff);overflow-x:auto}.task-view{min-height:32px!important;padding:6px 11px!important;border:1px solid transparent!important;border-radius:10px!important;background:transparent!important;color:var(--ops-text-2,#334155)!important;box-shadow:none!important;white-space:nowrap}.task-view:hover{transform:none!important;background:#fff!important;border-color:var(--ops-border,#e5ebf3)!important}.task-view.is-active{background:var(--ops-text,#172033)!important;border-color:var(--ops-text,#172033)!important;color:#fff!important}.task-filters{display:grid;grid-template-columns:minmax(220px,1fr) minmax(180px,.58fr) auto;gap:10px;align-items:center;padding:14px 16px}.task-search,.task-source{margin:0!important;min-height:38px!important}.task-refresh{min-height:38px!important;margin:0!important}.task-list-panel{padding:0;overflow:hidden}.task-list-head{display:flex;align-items:center;justify-content:space-between;gap:14px;padding:16px 18px;border-bottom:1px solid var(--ops-border,#e5ebf3)}.task-list-title{display:flex;align-items:baseline;gap:9px;min-width:0}.task-list-title h2{margin:0!important;font-size:17px!important}.task-result-count{color:var(--ops-muted,#718095);font-size:12px;font-variant-numeric:tabular-nums}.task-list-health{display:flex;align-items:center;gap:12px;flex-wrap:wrap;justify-content:flex-end}.task-refresh-meta{color:var(--ops-muted,#718095);font-size:11px;white-space:nowrap}.task-coverage{display:inline-flex;align-items:center;gap:7px;color:#166534;font-size:12px;font-weight:700;white-space:nowrap}.task-coverage::before{content:'';width:7px;height:7px;border-radius:999px;background:#22c55e}.task-coverage.is-bad{color:#b91c1c}.task-coverage.is-bad::before{background:#ef4444}.task-table-wrap{overflow:auto;max-height:calc(100vh - 390px);min-height:280px;background:#fff;overscroll-behavior:contain}.task-table{width:100%;min-width:940px!important;margin:0!important;border:0!important;border-radius:0!important;box-shadow:none!important}.task-head-cell{position:sticky;top:0;z-index:3;background:#f5f8ff!important}.task-head-cell:first-child,.task-cell:first-child{position:sticky;left:0;z-index:2}.task-head-cell:first-child{z-index:4;background:#f5f8ff!important}.task-cell:first-child{background:#fff!important;box-shadow:1px 0 0 var(--ops-border,#e5ebf3)}.task-row:hover .task-cell:first-child{background:#f8fbff!important}.task-name{display:block;max-width:440px;color:var(--ops-text,#172033);font-size:13px;font-weight:720;line-height:1.35;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.task-id{display:block;max-width:440px;margin-top:4px;color:var(--ops-muted,#718095);font:500 11px/1.35 var(--ops-mono,monospace);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.task-meta{display:block;margin-top:4px;color:var(--ops-muted,#718095);font-size:11px;line-height:1.35}.task-reason{display:block;max-width:300px;margin-top:7px;color:#7c2d12;font-size:12px;line-height:1.4;overflow-wrap:anywhere}.task-status{display:inline-flex;align-items:center;min-height:24px;padding:3px 8px;border:1px solid #d7e5ff;border-radius:999px;background:var(--ops-blue-soft,#edf4ff);color:#1d4ed8;font-size:12px;font-weight:720;white-space:nowrap}.task-status.failed{border-color:#fecaca;background:#fef2f2;color:#b91c1c}.task-status.blocked,.task-status.review{border-color:#fed7aa;background:#fff7ed;color:#9a3412}.task-status.running{border-color:#bfdbfe;background:#eff6ff;color:#1d4ed8}.task-status.success{border-color:#bbf7d0;background:#f0fdf4;color:#166534}.task-status.cancelled,.task-status.superseded{border-color:#e2e8f0;background:#f8fafc;color:#64748b}.task-source-name{display:block;font-weight:680;color:var(--ops-text-2,#334155)}.task-actions{display:flex;align-items:center;gap:6px}.task-actions button,.task-actions a{min-height:30px!important;padding:5px 9px!important;border-radius:9px!important;font-size:12px!important;box-shadow:none!important}.task-link{display:inline-flex;align-items:center;text-decoration:none;background:var(--ops-blue,#2f6bff);color:#fff!important}.task-empty{padding:54px 20px!important;text-align:center;color:var(--ops-muted,#718095)!important}.task-error{padding:12px 16px;border-top:1px solid #fecaca;background:#fff7f7;color:#b91c1c;font-size:12px}.task-action-dialog{position:fixed;inset:0;z-index:1000;display:none!important;align-items:center;justify-content:center;padding:20px;background:rgba(15,23,42,.42);backdrop-filter:blur(5px)}.task-action-dialog.is-open{display:flex!important}.task-action-card{width:min(560px,100%);max-height:min(760px,calc(100vh - 40px));overflow:auto;padding:20px;background:#fff;border:1px solid var(--ops-border,#e5ebf3);border-radius:20px;box-shadow:0 24px 64px rgba(15,23,42,.24)}.task-action-card h3{margin-bottom:5px!important}.task-action-card p{margin-bottom:14px;color:var(--ops-muted,#718095);font-size:13px}.task-action-card textarea{margin:0 0 14px!important}.task-detail-grid{display:grid;grid-template-columns:110px 1fr;gap:9px 14px;margin:16px 0 18px;padding:14px;border-radius:14px;background:#f8fafc;font-size:12px}.task-detail-grid dt{color:var(--ops-muted,#718095)}.task-detail-grid dd{margin:0;color:var(--ops-text,#172033);overflow-wrap:anywhere}.task-detail-options{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 16px}.task-detail-options button{box-shadow:none!important}.task-pagination{display:flex;align-items:center;justify-content:center;padding:12px;border-top:1px solid var(--ops-border,#e5ebf3)}.task-pagination button{min-height:34px!important;box-shadow:none!important}.task-dialog-actions{display:flex;justify-content:flex-end;gap:8px}.task-dialog-actions button{box-shadow:none!important}.task-action-cancel{background:#fff!important;border-color:var(--ops-border-strong,#d7e0ec)!important;color:var(--ops-text-2,#334155)!important}.task-action-confirm.is-danger{background:var(--ops-red,#dc2626)!important;border-color:var(--ops-red,#dc2626)!important}.task-loading{opacity:.58;pointer-events:none}
.task-coverage{margin-left:auto}.task-refresh-meta{margin-left:-4px}
@media(max-width:1120px){.task-overview{grid-template-columns:repeat(3,minmax(0,1fr))}.task-overview-primary{grid-column:1/-1;border-bottom:1px solid var(--ops-border,#e5ebf3)}.task-overview-item:first-of-type{border-left:0}.task-table-wrap{max-height:none}}
@media(max-width:720px){.task-overview{grid-template-columns:1fr 1fr}.task-overview-primary{grid-column:1/-1}.task-overview-item{padding:14px 16px}.task-overview-item:last-child{grid-column:1/-1;border-left:0;border-top:1px solid var(--ops-border,#e5ebf3)}.task-filters{grid-template-columns:1fr}.task-list-head{align-items:flex-start;flex-direction:column}.task-table{min-width:760px!important}}
</style></head><body><div class="page-shell"><div class="hero"><div><h1>任务治理</h1><p>集中处理阻断和待复核任务</p></div></div><section class="task-workbench" aria-label="任务概览与筛选"><div class="task-overview"><div class="task-overview-primary"><span class="task-eyebrow">当前需要处理</span><strong id="actionableCount">—</strong><p>仅统计阻断与待复核，不包含历史失败记录</p></div><div class="task-overview-item is-danger"><span class="task-eyebrow">阻断</span><strong id="blockedCount">—</strong></div><div class="task-overview-item is-warning"><span class="task-eyebrow">待复核</span><strong id="reviewCount">—</strong></div><div class="task-overview-item is-healthy"><span class="task-eyebrow">数据源</span><strong id="coverageCount">—</strong></div></div><div class="task-views" role="tablist" aria-label="任务视图"><button class="task-view is-active" type="button" data-view="actionable">待处理</button><button class="task-view" type="button" data-view="running">运行中</button><button class="task-view" type="button" data-view="queued">排队中</button><button class="task-view" type="button" data-view="failed">失败记录</button><button class="task-view" type="button" data-view="all">全部任务</button></div><div class="task-filters"><input class="task-search" id="query" type="search" autocomplete="off" spellcheck="false" placeholder="搜索任务 ID、类型或错误码…"><select class="task-source" id="source" aria-label="任务来源"><option value="">全部来源</option></select><button class="task-refresh" id="refreshButton" type="button">刷新</button></div></section><section class="task-list-panel"><div class="task-list-head"><div class="task-list-title"><h2 id="listTitle">待处理任务</h2><span id="resultCount" class="task-result-count"></span></div><span id="coverage" class="task-coverage">数据源检查中</span></div><div class="task-table-wrap" tabindex="0"><table class="task-table"><thead><tr><th class="task-head-cell">任务</th><th class="task-head-cell">状态与原因</th><th class="task-head-cell">来源</th><th class="task-head-cell">更新时间</th><th class="task-head-cell">操作</th></tr></thead><tbody id="rows"></tbody></table></div><div id="pageError" class="task-error" hidden></div></section><div id="actionDialog" class="task-action-dialog" role="dialog" aria-modal="true" aria-labelledby="actionTitle"><div class="task-action-card"><h3 id="actionTitle">处理任务</h3><p id="actionHint"></p><textarea id="actionReason" rows="3" placeholder="填写处理原因…"></textarea><div class="task-dialog-actions"><button class="task-action-cancel" id="actionCancel" type="button">返回</button><button class="task-action-confirm" id="actionConfirm" type="button">确认</button></div></div></div></div><script>
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function api(url,options={}){const r=await fetch(url,{credentials:'same-origin',...options,headers:{'Content-Type':'application/json',...(options.headers||{})}});const d=await r.json();if(!r.ok)throw new Error(d.detail||'request_failed');return d}
const statusLabels={blocked:'阻断',review:'待复核',failed:'失败',queued:'排队中',running:'运行中',success:'成功',cancelled:'已取消',superseded:'已替代',unknown:'待识别'};
const sourceLabels={control_plane:'统一调度',automation_db:'业务任务',deploy_queue:'发布队列',systemd:'系统任务',legacy_p0:'历史任务'};
const viewTitles={actionable:'待处理任务',execution_queue:'执行队列',waiting_user:'等待用户操作',running:'运行中的任务',failed:'失败记录',completed:'已完成任务'};
const sourceRoutes={mcn_operation_tasks:'/ops/production-ops',registration_group_approval_batch_runs:'/ops/production-ops',wa_runtime_actions:'/ops/production-ops',bind_check_jobs:'/ops/intake-submit',group_join_jobs:'/ops/intake-submit',creative_generation_tasks:'/ops/ad-data-dashboard',creative_pro_work_queue:'/ops/ad-data-dashboard',streamer_external_sync_jobs:'/ops/streamer-analytics'};
let currentView='actionable',pendingAction=null,searchTimer=0,loadedTasks=[],currentTotal=0,autoRefreshTimer=0;
function initWorkbench(){document.title='异常任务';document.querySelector('.hero h1').textContent='异常任务';document.querySelector('.hero p').textContent='区分需要处理、正在执行和等待外部动作的任务';document.querySelector('.task-overview').innerHTML='<div class="task-overview-primary"><span class="task-eyebrow">需要关注</span><strong id="actionableCount">—</strong><p>阻断与待复核任务，不包含历史失败</p></div><div class="task-overview-item is-danger"><span class="task-eyebrow">执行队列</span><strong id="executionQueueCount">—</strong></div><div class="task-overview-item is-warning"><span class="task-eyebrow">等待用户</span><strong id="waitingUserCount">—</strong></div><div class="task-overview-item is-healthy"><span class="task-eyebrow">已完成</span><strong id="completedCount">—</strong></div>';document.querySelector('.task-views').innerHTML='<button class="task-view is-active" type="button" data-view="actionable">待处理</button><button class="task-view" type="button" data-view="execution_queue">执行队列</button><button class="task-view" type="button" data-view="waiting_user">等待用户</button><button class="task-view" type="button" data-view="running">运行中</button><button class="task-view" type="button" data-view="failed">失败记录</button><button class="task-view" type="button" data-view="completed">已完成</button>';const coverage=document.getElementById('coverage');coverage.insertAdjacentHTML('afterend','<span id="lastRefresh" class="task-refresh-meta">准备自动刷新</span>');document.querySelector('.task-list-panel').insertAdjacentHTML('beforeend','<div class="task-pagination"><button id="loadMoreButton" class="secondary" type="button" hidden>加载更多</button></div>');document.getElementById('actionHint').insertAdjacentHTML('afterend','<dl id="detailBody" class="task-detail-grid" hidden></dl><div id="detailOptions" class="task-detail-options"></div>');document.getElementById('actionConfirm').hidden=true;document.getElementById('actionReason').hidden=true}
function labelStatus(v){return statusLabels[v]||v||'待识别'}
function labelSource(v){return sourceLabels[v]||v||'其他来源'}
function formatTime(v){if(!v)return'—';const d=new Date(v);return Number.isNaN(d.getTime())?v:new Intl.DateTimeFormat('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).format(d)}
function isWaitingUser(t){return t.source_table==='automation_tasks'&&t.task_type==='group_join'&&t.normalized_status==='queued'}
function routeFor(t){if(isWaitingUser(t))return'/ops/production-ops';return sourceRoutes[t.source_table]||''}
function displayStatus(t){return isWaitingUser(t)?'等待用户':labelStatus(t.normalized_status)}
function actionButtons(t){const managed=t.source_system==='control_plane'&&t.managed_mode==='managed'&&['blocked','review'].includes(t.normalized_status),route=routeFor(t);if(managed)return `<div class="task-actions"><button type="button" data-detail="${esc(t.task_key)}">处理</button></div>`;if(route)return `<div class="task-actions"><a class="task-link" href="${esc(route)}">${isWaitingUser(t)?'查看审批':'去处理'}</a></div>`;return `<div class="task-actions"><button class="secondary" type="button" data-detail="${esc(t.task_key)}">查看详情</button></div>`}
function renderRows(tasks){document.getElementById('rows').innerHTML=tasks.map(t=>{const title=t.title||t.native_task_id||'未命名任务';const reason=t.blocked_reason||t.error_code||(isWaitingUser(t)?'等待用户发起官方群入群申请，不占用执行资源':'');const meta=[t.task_type,t.owner].filter(Boolean).join(' · ');return `<tr class="task-row"><td class="task-cell"><span class="task-name" title="${esc(title)}">${esc(title)}</span><span class="task-id" title="${esc(t.native_task_id)}">${esc(t.native_task_id)}</span></td><td class="task-cell"><span class="task-status ${esc(t.normalized_status)}">${esc(displayStatus(t))}</span>${reason?`<span class="task-reason">${esc(reason)}</span>`:''}</td><td class="task-cell"><span class="task-source-name">${esc(labelSource(t.source_system))}</span><span class="task-meta">${esc(t.source_table)}${meta?' · '+esc(meta):''}</span></td><td class="task-cell">${esc(formatTime(t.updated_at_utc||t.observed_at_utc))}</td><td class="task-cell">${actionButtons(t)}</td></tr>`}).join('')||'<tr><td class="task-empty" colspan="5">当前视图没有任务</td></tr>'}
async function loadTasks(append=false,background=false){const panel=document.querySelector('.task-list-panel'),error=document.getElementById('pageError');if(!background)panel.classList.add('task-loading');error.hidden=true;const p=new URLSearchParams({limit:'50',offset:append?String(loadedTasks.length):'0'}),src=document.getElementById('source').value,q=document.getElementById('query').value.trim();if(currentView==='actionable')p.set('blocked_only','true');else if(['execution_queue','waiting_user','completed'].includes(currentView))p.set('view',currentView);else p.set('status',currentView);if(src)p.set('source',src);if(q)p.set('q',q);try{const d=await api('/api/ops/task-control/tasks?'+p);const source=document.getElementById('source');if(source.options.length===1)d.sources.forEach(x=>source.add(new Option(labelSource(x),x)));const summary=d.actionable_summary||{},views=d.view_summary||{},complete=d.coverage.filter(x=>x.status==='complete').length;document.getElementById('actionableCount').textContent=Number(summary.total||0).toLocaleString('zh-CN');document.getElementById('executionQueueCount').textContent=Number(views.execution_queue||0).toLocaleString('zh-CN');document.getElementById('waitingUserCount').textContent=Number(views.waiting_user||0).toLocaleString('zh-CN');document.getElementById('completedCount').textContent=Number(views.completed||0).toLocaleString('zh-CN');const coverage=document.getElementById('coverage');coverage.classList.toggle('is-bad',!d.coverage_complete);coverage.textContent=d.coverage_complete?`${complete} 个数据源正常`:`${d.coverage.length-complete} 个数据源异常`;loadedTasks=append?loadedTasks.concat(d.tasks):d.tasks;currentTotal=Number(d.total||0);document.getElementById('listTitle').textContent=viewTitles[currentView];document.getElementById('resultCount').textContent=`显示 ${loadedTasks.length} / ${currentTotal}`;document.getElementById('lastRefresh').textContent=`页面更新 ${new Intl.DateTimeFormat('zh-CN',{hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}).format(new Date())} · 每 60 秒自动刷新`;renderRows(loadedTasks);const more=document.getElementById('loadMoreButton');more.hidden=loadedTasks.length>=currentTotal;more.textContent=`加载更多（剩余 ${(currentTotal-loadedTasks.length).toLocaleString('zh-CN')}）`}catch(e){error.textContent='加载失败：'+e.message;error.hidden=false;if(!append){loadedTasks=[];renderRows([])}}finally{panel.classList.remove('task-loading')}}
function selectView(view){currentView=view;loadedTasks=[];document.querySelectorAll('.task-view').forEach(b=>b.classList.toggle('is-active',b.dataset.view===view));loadTasks()}
async function openDetail(key){try{const detail=await api('/api/ops/task-control/tasks/'+encodeURIComponent(key)),t=detail.task,managed=t.source_system==='control_plane'&&t.managed_mode==='managed'&&['blocked','review'].includes(t.normalized_status);document.getElementById('actionTitle').textContent=t.title||t.native_task_id||'任务详情';document.getElementById('actionHint').textContent=t.blocked_reason||t.error_code||(isWaitingUser(t)?'等待用户发起官方群入群申请，不占用执行资源':'当前没有补充说明');document.getElementById('detailBody').innerHTML=[['状态',displayStatus(t)],['任务 ID',t.native_task_id],['类型',t.task_type||'—'],['来源',labelSource(t.source_system)+' / '+t.source_table],['负责人',t.owner||'—'],['更新时间',formatTime(t.updated_at_utc||t.observed_at_utc)]].map(x=>`<dt>${esc(x[0])}</dt><dd>${esc(x[1])}</dd>`).join('');document.getElementById('detailBody').hidden=false;document.getElementById('actionReason').hidden=true;document.getElementById('actionConfirm').hidden=true;document.getElementById('detailOptions').innerHTML=managed?`<button type="button" data-action="requeue" data-key="${esc(key)}">重新排队</button><button class="danger" type="button" data-action="cancel" data-key="${esc(key)}">取消任务</button>`:'';document.getElementById('actionDialog').classList.add('is-open')}catch(e){document.getElementById('pageError').textContent='读取任务失败：'+e.message;document.getElementById('pageError').hidden=false}}
async function openAction(key,action){try{const detail=await api('/api/ops/task-control/tasks/'+encodeURIComponent(key));let version=0;try{version=Number(JSON.parse(detail.task.detail_json||'{}').version||0)}catch(_e){}pendingAction={key,action,version};document.getElementById('actionTitle').textContent=action==='requeue'?'重新排队':'取消任务';document.getElementById('actionHint').textContent=action==='requeue'?'确认问题已经解除，并填写本次处理说明。':'取消后任务不会继续执行，请填写原因。';document.getElementById('detailBody').hidden=true;document.getElementById('detailOptions').innerHTML='';document.getElementById('actionReason').hidden=false;document.getElementById('actionReason').value='';document.getElementById('actionConfirm').hidden=false;document.getElementById('actionConfirm').textContent=action==='requeue'?'确认重新排队':'确认取消';document.getElementById('actionConfirm').classList.toggle('is-danger',action==='cancel');document.getElementById('actionDialog').classList.add('is-open');setTimeout(()=>document.getElementById('actionReason').focus(),0)}catch(e){document.getElementById('pageError').textContent='读取任务失败：'+e.message;document.getElementById('pageError').hidden=false}}
function closeAction(){pendingAction=null;document.getElementById('actionDialog').classList.remove('is-open');document.getElementById('detailOptions').innerHTML=''}
async function confirmAction(){if(!pendingAction)return;const button=document.getElementById('actionConfirm'),reason=document.getElementById('actionReason').value.trim();if(!reason){document.getElementById('actionReason').focus();return}button.disabled=true;try{await api('/api/ops/task-control/tasks/'+encodeURIComponent(pendingAction.key)+'/actions',{method:'POST',body:JSON.stringify({action:pendingAction.action,expected_version:pendingAction.version,reason})});closeAction();await loadTasks()}catch(e){document.getElementById('actionHint').textContent='处理失败：'+e.message}finally{button.disabled=false}}
initWorkbench();document.querySelectorAll('.task-view').forEach(b=>b.addEventListener('click',()=>selectView(b.dataset.view)));document.getElementById('source').addEventListener('change',()=>loadTasks());document.getElementById('refreshButton').addEventListener('click',()=>loadTasks());document.getElementById('loadMoreButton').addEventListener('click',()=>loadTasks(true));document.getElementById('query').addEventListener('input',()=>{clearTimeout(searchTimer);searchTimer=setTimeout(()=>loadTasks(),280)});document.getElementById('rows').addEventListener('click',e=>{const b=e.target.closest('button[data-detail]');if(b)openDetail(b.dataset.detail)});document.getElementById('detailOptions').addEventListener('click',e=>{const b=e.target.closest('button[data-action]');if(b)openAction(b.dataset.key,b.dataset.action)});document.getElementById('actionCancel').addEventListener('click',closeAction);document.getElementById('actionConfirm').addEventListener('click',confirmAction);document.getElementById('actionDialog').addEventListener('click',e=>{if(e.target.id==='actionDialog')closeAction()});document.addEventListener('keydown',e=>{if(e.key==='Escape')closeAction()});loadTasks();autoRefreshTimer=setInterval(()=>{if(document.visibilityState==='visible'&&!document.getElementById('actionDialog').classList.contains('is-open')&&loadedTasks.length<=50)loadTasks(false,true)},60000);
</script></body></html>'''
