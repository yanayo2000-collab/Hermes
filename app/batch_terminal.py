from __future__ import annotations

from typing import Any, Mapping, Sequence


RETRYABLE_SOURCE_QUALITY_MARKERS = (
    "source_not_ready",
    "timo_revenue_export_not_ready",
    "timo_revenue_export_detail_mismatch",
    "quality_gate_row_count_drop",
    "circuit open until",
)

DERIVED_INCOMPLETE_MARKERS = (
    "revision_scope_incomplete",
    "source_sync_not_complete",
)


def only_retryable_source_quality_errors(
    errors: Sequence[object],
    analytics: Mapping[str, Any] | None = None,
) -> bool:
    failures = [str(error or "") for error in errors if str(error or "")]
    if not failures or not all(
        any(marker in failure for marker in RETRYABLE_SOURCE_QUALITY_MARKERS)
        for failure in failures
    ):
        return False
    analytics_error = str((analytics or {}).get("error") or "")
    return not analytics_error or any(
        marker in analytics_error
        for marker in RETRYABLE_SOURCE_QUALITY_MARKERS + DERIVED_INCOMPLETE_MARKERS
    )


def source_quality_exit_code(result: Mapping[str, Any]) -> int:
    if result.get("ok") is True:
        return 0
    if only_retryable_source_quality_errors(
        result.get("errors") or (),
        result.get("analytics") if isinstance(result.get("analytics"), Mapping) else None,
    ):
        return 75
    return 1


def source_quality_collection_exit_code(results: Sequence[Mapping[str, Any]]) -> int:
    if all(result.get("ok") is True for result in results):
        return 0
    failed = [result for result in results if result.get("ok") is not True]
    if failed and all(source_quality_exit_code(result) == 75 for result in failed):
        return 75
    return 1
