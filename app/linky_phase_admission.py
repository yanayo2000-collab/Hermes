from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path


LINKY_SOURCE_PHASE_MAX_LOAD1 = 12.0
LINKY_COMPUTE_PHASE_MAX_LOAD1 = 2.5


def linky_source_phase_soft_reasons(
    *,
    task_id: str,
    resource_claims: Iterable[str],
    soft_reasons: Iterable[str],
) -> list[str]:
    """Ignore only CPU pressure while Linky is still in its network phase."""
    reasons = [str(value) for value in soft_reasons]
    claims = {str(value) for value in resource_claims}
    if task_id == 'linky-daily-incremental' and claims == {'network_fetch'}:
        return [value for value in reasons if value != 'sustained_normalized_load']
    return reasons


def linky_compute_admission_command(root: Path) -> list[str]:
    return [
        str(root / '.venv' / 'bin' / 'python'),
        str(root / 'scripts' / 'check_batch_admission.py'),
        '--max-used-percent', '80',
        '--min-free-gb', '15',
        '--min-mem-available-gb', '3',
        '--max-load1', str(LINKY_COMPUTE_PHASE_MAX_LOAD1),
        '--max-iowait-percent', '20',
        '--iowait-sample-seconds', '5',
        '--backend-health-url', 'http://127.0.0.1:8011/health',
        '--backend-health-checks', '3',
        '--backend-health-timeout-seconds', '2',
        '--max-backend-health-latency-seconds', '0.5',
        '--recent-window-minutes', '5',
        '--max-db-locked-count', '0',
        '--db-lock-event-dedupe-seconds', '10',
        '--db-lock-cooldown-seconds', '30',
        '--db-lock-path', '/run/lock/mcn-sqlite-etl.lock',
        '--db-lock-path', '/run/lock/mcn-sqlite-writer.lock',
        '--api-slow-threshold-ms', '1000',
        '--observe-api-slow',
        '--max-nginx-504-count', '0',
    ]
