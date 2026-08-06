from __future__ import annotations

from typing import Any, Dict, Protocol

from fastapi import APIRouter


class ReportReader(Protocol):
    def funnel_report(self) -> Dict[str, Any]: ...

    def daily_summary(self) -> Dict[str, Any]: ...


def create_report_router(report_reader: ReportReader) -> APIRouter:
    router = APIRouter()

    @router.get('/api/reports/funnel')
    def reports_funnel():
        return report_reader.funnel_report()

    @router.get('/api/reports/daily-summary')
    def reports_daily_summary():
        return report_reader.daily_summary()

    return router
