from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from inspect import signature
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from app.sqlite_observability import connect_observed_sqlite
from app.streamer_analytics import build_streamer_analytics_metadata, build_streamer_analytics_payload
from app.streamer_analytics_page import STREAMER_ANALYTICS_PAGE_HTML
from app.streamer_roi import (
    StreamerAnalyticsSnapshotUnavailable,
    build_streamer_weekly_roi_payload,
    list_streamer_roi_policies,
    require_streamer_analytics_snapshot_ready,
    save_streamer_roi_policy,
    save_streamer_weekly_roi_inputs,
)
from app.timo_guild_identity import decorate_timo_guild_display_names, timo_guild_storage_name


class StreamerRoiGuildCostRequest(BaseModel):
    country: str
    guild_name: str
    ad_cost_usd: Optional[float] = None
    admin_cost_usd: Optional[float] = None
    customer_service_cost_usd: Optional[float] = None
    media_buyer_cost_usd: Optional[float] = None
    activity_cost_usd: Optional[float] = None
    correction_reason: Optional[str] = None


class StreamerRoiWeeklyInputRequest(BaseModel):
    app: str
    week_start: str
    status: str
    rows: List[StreamerRoiGuildCostRequest]


class StreamerRoiPolicyTierRequest(BaseModel):
    tier_level: int
    threshold_income_units: float
    cumulative_reward_units: float
    incremental_reward_units: float


class StreamerRoiPolicySaveRequest(BaseModel):
    app: str
    country: str
    guild_name: str
    effective_from: str
    calculation_mode: str = 'flat'
    income_units_per_usd: float
    cps_rate: float = 0
    newcomer_cpa_usd: float = 0
    non_certified_cpa_usd: float = 0
    certified_cpa_usd: float = 0
    bonus_7d_usd: float = 0
    bonus_10d_usd: float = 0
    guild_eligible_host_min_units: float = 0
    streamer_tiers: List[StreamerRoiPolicyTierRequest] = Field(default_factory=list)
    guild_tiers: List[StreamerRoiPolicyTierRequest] = Field(default_factory=list)
    policy_note: str = ''
    source_label: str = '运营后台配置'
    change_reason: str


def create_streamer_analytics_router(
    *,
    db: Any,
    require_ops_user: Callable[..., Dict[str, Any]],
    with_ops_shell_style: Callable[..., str],
    super_admin_role: str,
) -> APIRouter:
    router = APIRouter()
    supports_refresh_flag = 'refresh_session_activity' in signature(require_ops_user).parameters
    supports_sqlite_uri = 'uri' in signature(connect_observed_sqlite).parameters

    def storage_guild_name(app_name: object, guild_name: object) -> str:
        normalized_app = str(app_name or '').strip().lower()
        return timo_guild_storage_name(guild_name) if normalized_app == 'timo' else str(guild_name or '').strip()

    def public_payload(app_name: object, payload: Dict[str, Any]) -> Dict[str, Any]:
        return decorate_timo_guild_display_names(payload) if str(app_name or '').strip().lower() == 'timo' else payload

    def authorize(request: Request, *, refresh_session_activity: bool = True) -> Dict[str, Any]:
        kwargs: Dict[str, Any] = {'role': super_admin_role}
        if supports_refresh_flag:
            kwargs['refresh_session_activity'] = refresh_session_activity
        return require_ops_user(request, **kwargs)

    @contextmanager
    def readonly_connection(database_path: str, *, source: str):
        normalized = str(database_path or '').strip()
        if normalized == ':memory:':
            yield db.connect()
            return
        readonly_uri = f'file:{quote(str(Path(normalized).resolve()))}?mode=ro'
        if supports_sqlite_uri:
            conn = connect_observed_sqlite(
                readonly_uri,
                source=source,
                timeout=5.0,
                uri=True,
            )
        else:
            # Older local fixtures predate URI support in the observed connector.
            conn = sqlite3.connect(readonly_uri, timeout=5.0, uri=True)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA busy_timeout=5000')
            conn.execute('PRAGMA query_only=ON')
            yield conn
        finally:
            conn.close()

    @router.get('/ops/streamer-analytics', response_class=HTMLResponse)
    def ops_streamer_analytics_page(request: Request) -> HTMLResponse:
        user = authorize(request)
        return HTMLResponse(
            with_ops_shell_style(
                STREAMER_ANALYTICS_PAGE_HTML,
                str(user.get('role') or '').strip(),
                page='streamer-analytics',
            ),
            headers={
                'Cache-Control': 'no-store, no-cache, must-revalidate, max-age=0',
                'Pragma': 'no-cache',
                'Expires': '0',
            },
        )

    @router.get('/api/ops/streamer-analytics/summary')
    def ops_streamer_analytics_summary(
        request: Request,
        app: str = 'timo',
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        country: str = '',
        guild_name: str = '',
        limit: int = 20,
    ) -> Dict[str, Any]:
        authorize(request, refresh_session_activity=False)
        try:
            analytics_db_path = str(os.getenv('STREAMER_ANALYTICS_DB_PATH') or '').strip()
            if analytics_db_path and Path(analytics_db_path).is_file():
                try:
                    with readonly_connection(
                        analytics_db_path,
                        source='app.streamer_analytics_routes:summary-read',
                    ) as analytics_conn:
                        return public_payload(app, build_streamer_analytics_payload(
                            analytics_conn,
                            app_name=app,
                            date_from=date_from,
                            date_to=date_to,
                            country=country,
                            guild_name=storage_guild_name(app, guild_name),
                            limit=limit,
                        ))
                except sqlite3.Error:
                    pass
            with readonly_connection(
                db.db_path,
                source='app.streamer_analytics_routes:summary-fallback-read',
            ) as conn:
                return public_payload(app, build_streamer_analytics_payload(
                    conn,
                    app_name=app,
                    date_from=date_from,
                    date_to=date_to,
                    country=country,
                    guild_name=storage_guild_name(app, guild_name),
                    limit=limit,
                ))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get('/api/ops/streamer-analytics/metadata')
    def ops_streamer_analytics_metadata(request: Request) -> Dict[str, Any]:
        authorize(request, refresh_session_activity=False)
        analytics_db_path = str(os.getenv('STREAMER_ANALYTICS_DB_PATH') or '').strip()
        if analytics_db_path and Path(analytics_db_path).is_file():
            try:
                with readonly_connection(
                    analytics_db_path,
                    source='app.streamer_analytics_routes:metadata-read',
                ) as analytics_conn:
                    return build_streamer_analytics_metadata(analytics_conn)
            except sqlite3.Error:
                pass
        with readonly_connection(
            db.db_path,
            source='app.streamer_analytics_routes:metadata-fallback-read',
        ) as conn:
            return build_streamer_analytics_metadata(conn)

    @router.get('/api/ops/streamer-analytics/weekly-roi')
    def ops_streamer_analytics_weekly_roi(
        request: Request,
        app: str = 'timo',
        week_start: Optional[str] = None,
        country: str = '',
        guild_name: str = '',
    ) -> Dict[str, Any]:
        authorize(request, refresh_session_activity=False)
        try:
            with readonly_connection(
                db.db_path,
                source='app.streamer_analytics_routes:roi-control-read',
            ) as conn:
                analytics_db_path = str(os.getenv('STREAMER_ANALYTICS_DB_PATH') or '').strip()
                if analytics_db_path and Path(analytics_db_path).is_file():
                    try:
                        with readonly_connection(
                            analytics_db_path,
                            source='app.streamer_analytics_routes:roi-analytics-read',
                        ) as analytics_conn:
                            return public_payload(app, build_streamer_weekly_roi_payload(
                                conn,
                                app_name=app,
                                week_start=week_start,
                                country=country,
                                guild_name=storage_guild_name(app, guild_name),
                                analytics_conn=analytics_conn,
                                ensure_schema=False,
                                require_ready_snapshot=True,
                            ))
                    except (sqlite3.Error, StreamerAnalyticsSnapshotUnavailable) as exc:
                        raise HTTPException(
                            status_code=503,
                            detail='streamer_analytics_snapshot_unavailable',
                        ) from exc
                if analytics_db_path:
                    raise HTTPException(
                        status_code=503,
                        detail='streamer_analytics_snapshot_unavailable',
                    )
                return public_payload(app, build_streamer_weekly_roi_payload(
                    conn,
                    app_name=app,
                    week_start=week_start,
                    country=country,
                    guild_name=storage_guild_name(app, guild_name),
                    analytics_conn=conn,
                    ensure_schema=False,
                ))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post('/api/ops/streamer-analytics/weekly-roi')
    def ops_streamer_analytics_save_weekly_roi(
        request: Request,
        payload: StreamerRoiWeeklyInputRequest,
    ) -> Dict[str, Any]:
        user = authorize(request)
        try:
            analytics_db_path = str(os.getenv('STREAMER_ANALYTICS_DB_PATH') or '').strip()
            if analytics_db_path:
                if not Path(analytics_db_path).is_file():
                    raise HTTPException(
                        status_code=503,
                        detail='streamer_analytics_snapshot_unavailable',
                    )
                try:
                    with readonly_connection(
                        analytics_db_path,
                        source='app.streamer_analytics_routes:roi-analytics-write-read',
                    ) as analytics_conn:
                        require_streamer_analytics_snapshot_ready(
                            analytics_conn,
                            app_name=payload.app,
                        )
                        with db.connect() as conn:
                            rows = [item.model_dump() if hasattr(item, 'model_dump') else item.dict() for item in payload.rows]
                            for row in rows:
                                row['guild_name'] = storage_guild_name(payload.app, row.get('guild_name'))
                            return public_payload(payload.app, save_streamer_weekly_roi_inputs(
                                conn,
                                app_name=payload.app,
                                week_start=payload.week_start,
                                status=payload.status,
                                rows=rows,
                                actor=str(user.get('username') or user.get('display_name') or 'super_admin'),
                                analytics_conn=analytics_conn,
                                require_ready_snapshot=True,
                            ))
                except (sqlite3.Error, StreamerAnalyticsSnapshotUnavailable) as exc:
                    raise HTTPException(
                        status_code=503,
                        detail='streamer_analytics_snapshot_unavailable',
                    ) from exc
            with db.connect() as conn:
                rows = [item.model_dump() if hasattr(item, 'model_dump') else item.dict() for item in payload.rows]
                for row in rows:
                    row['guild_name'] = storage_guild_name(payload.app, row.get('guild_name'))
                return public_payload(payload.app, save_streamer_weekly_roi_inputs(
                    conn,
                    app_name=payload.app,
                    week_start=payload.week_start,
                    status=payload.status,
                    rows=rows,
                    actor=str(user.get('username') or user.get('display_name') or 'super_admin'),
                    analytics_conn=conn,
                ))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get('/api/ops/streamer-analytics/roi-policies')
    def ops_streamer_analytics_roi_policies(
        request: Request,
        app: str = 'timo',
        country: str = '',
        guild_name: str = '',
    ) -> Dict[str, Any]:
        authorize(request, refresh_session_activity=False)
        with readonly_connection(
            db.db_path,
            source='app.streamer_analytics_routes:roi-policy-read',
        ) as conn:
            return public_payload(app, list_streamer_roi_policies(
                conn,
                app_name=app,
                country=country,
                guild_name=storage_guild_name(app, guild_name),
                ensure_schema=False,
            ))

    @router.post('/api/ops/streamer-analytics/roi-policies')
    def ops_streamer_analytics_save_roi_policy(
        request: Request,
        payload: StreamerRoiPolicySaveRequest,
    ) -> Dict[str, Any]:
        user = authorize(request)
        try:
            with db.connect() as conn:
                policy_payload = payload.model_dump() if hasattr(payload, 'model_dump') else payload.dict()
                policy_payload['guild_name'] = storage_guild_name(payload.app, policy_payload.get('guild_name'))
                return public_payload(payload.app, save_streamer_roi_policy(
                    conn,
                    payload=policy_payload,
                    actor=str(user.get('username') or user.get('display_name') or 'super_admin'),
                ))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return router
