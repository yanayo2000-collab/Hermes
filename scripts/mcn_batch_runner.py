#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.batch_runtime import (  # noqa: E402
    PRODUCTION_PROJECT_ROOT,
    cgroup_contains_slice,
    current_cgroup_text,
)
from app.linky_phase_admission import LINKY_SOURCE_PHASE_MAX_LOAD1  # noqa: E402


@dataclass(frozen=True)
class BatchJob:
    name: str
    slice_name: str
    timeout_seconds: int
    command: tuple[str, ...]
    strict_admission: bool = False
    preflight_command: tuple[str, ...] = ()
    start_mutex_required: bool = False
    block_on_api_slow: bool = False
    disk_cleanup_fallback: bool = False
    max_load1_override: Optional[float] = None


PYTHON = str(ROOT / '.venv' / 'bin' / 'python')
JOBS = {
    'ad-dashboard': BatchJob(
        name='ad-dashboard',
        slice_name='mcn-batch.slice',
        timeout_seconds=30 * 60,
        command=(str(ROOT / 'scripts' / 'run_ad_dashboard_daily_backfill.sh'),),
    ),
    'linky': BatchJob(
        name='linky',
        slice_name='mcn-batch-linky.slice',
        timeout_seconds=90 * 60,
        strict_admission=True,
        start_mutex_required=False,
        block_on_api_slow=False,
        disk_cleanup_fallback=True,
        max_load1_override=LINKY_SOURCE_PHASE_MAX_LOAD1,
        command=(
            PYTHON,
            str(ROOT / 'scripts' / 'materialize_streamer_external_feed.py'),
            '--app', 'linky', '--days', '1', '--fail-on-lock-busy',
        ),
    ),
    'sugo': BatchJob(
        name='sugo',
        slice_name='mcn-batch.slice',
        timeout_seconds=20 * 60,
        command=(
            PYTHON,
            str(ROOT / 'scripts' / 'materialize_streamer_external_feed.py'),
            '--app', 'sugo', '--days', '1', '--fail-on-lock-busy',
        ),
    ),
    'timo': BatchJob(
        name='timo',
        slice_name='mcn-batch.slice',
        timeout_seconds=30 * 60,
        command=(
            PYTHON,
            str(ROOT / 'scripts' / 'materialize_timo_external_feed.py'),
            '--four-hour-cadence', '--fail-on-lock-busy',
        ),
    ),
    'timo-retry': BatchJob(
        name='timo-retry',
        slice_name='mcn-batch.slice',
        timeout_seconds=30 * 60,
        command=(
            PYTHON,
            str(ROOT / 'scripts' / 'timo_incremental_retry_worker.py'),
            '--max-dates', '1', '--fail-on-lock-busy',
        ),
        preflight_command=(
            PYTHON,
            str(ROOT / 'scripts' / 'timo_incremental_retry_worker.py'),
            '--max-dates', '1', '--check-due-only',
        ),
    ),
    'timo-realtime': BatchJob(
        name='timo-realtime',
        slice_name='mcn-batch.slice',
        timeout_seconds=20 * 60,
        command=(
            PYTHON,
            str(ROOT / 'scripts' / 'timo_realtime_sync_worker.py'),
            '--fail-on-lock-busy',
        ),
    ),
    'timo-bi-mart': BatchJob(
        name='timo-bi-mart',
        slice_name='mcn-batch.slice',
        timeout_seconds=20 * 60,
        command=(
            PYTHON,
            str(ROOT / 'scripts' / 'materialize_timo_bi_mart.py'),
            '--fail-on-lock-busy',
        ),
    ),
    'timo-history-bootstrap': BatchJob(
        name='timo-history-bootstrap',
        slice_name='mcn-batch.slice',
        timeout_seconds=60 * 60,
        command=(
            PYTHON,
            str(ROOT / 'scripts' / 'bootstrap_timo_incremental_history.py'),
            '--max-scopes', '500', '--fail-on-lock-busy',
        ),
    ),
    'streamer-analytics-publish': BatchJob(
        name='streamer-analytics-publish',
        slice_name='mcn-batch-linky.slice',
        timeout_seconds=120 * 60,
        strict_admission=True,
        # This job builds an isolated candidate and only takes the physical
        # publish lock for the final swap. Ordinary API latency and one short
        # iowait spike are observations, not reasons to cancel the build.
        block_on_api_slow=False,
        command=(
            PYTHON,
            str(ROOT / 'scripts' / 'publish_streamer_analytics_candidate.py'),
        ),
    ),
}


def _restart_receipt_attributed() -> bool:
    completed = subprocess.run(
        [
            PYTHON,
            str(ROOT / 'scripts' / 'mcn_release_governance.py'),
            'audit-restart',
            '--unit',
            'mcn-backend.service',
        ],
        cwd=str(ROOT),
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    return completed.returncode == 0


@contextlib.contextmanager
def _singleflight(job: BatchJob):
    lock_path = Path('/tmp/mcn-ai-automation-batch-singleflight') / f'{job.name}.lock'
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open('a+', encoding='utf-8') as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _admission_command(job: BatchJob) -> list[str]:
    command = [
        PYTHON,
        str(ROOT / 'scripts' / 'check_batch_admission.py'),
        # Keep the strict free-space floor at 15 GiB, but align the percentage
        # ceiling with the automatic disk-cleanup trigger.  A 75% ceiling while
        # cleanup starts at 80% creates a permanent no-owner gap.
        '--max-used-percent', '80' if job.strict_admission else '85',
        '--min-free-gb', '15' if job.strict_admission else '10',
        '--min-mem-available-gb', '3' if job.strict_admission else '2',
        '--max-load1', str(
            job.max_load1_override
            if job.max_load1_override is not None
            else (2.5 if job.strict_admission else 4)
        ),
        '--max-iowait-percent', (
            '35' if job.name == 'streamer-analytics-publish'
            else ('20' if job.strict_admission else '30')
        ),
        '--iowait-sample-seconds', '5' if job.strict_admission else '2',
        '--backend-health-url', 'http://127.0.0.1:8011/health',
        '--backend-health-checks', '3' if job.strict_admission else '1',
        '--backend-health-timeout-seconds', '2',
        '--max-backend-health-latency-seconds', '0.5' if job.strict_admission else '1',
        '--recent-window-minutes', (
            '1' if job.name in {'ad-dashboard', 'streamer-analytics-publish'} else '5'
        ),
        '--max-db-locked-count', '0',
        '--db-lock-event-dedupe-seconds', '10',
        '--db-lock-cooldown-seconds', '30',
        '--db-lock-path', '/run/lock/mcn-sqlite-etl.lock',
        '--db-lock-path', '/run/lock/mcn-sqlite-writer.lock',
        '--api-slow-threshold-ms', '1000' if job.strict_admission else '3000',
        '--observe-api-slow',
        '--max-nginx-504-count', '0',
    ]
    if job.block_on_api_slow:
        command.extend([
            '--max-api-slow-count',
            '2' if job.strict_admission else '5',
        ])
    if job.start_mutex_required:
        command.extend([
            '--mutex-lock-path',
            '/tmp/mcn-ai-automation-sqlite-job-locks/sqlite-etl.lock',
        ])
    return command


def _preflight_requires_execution(job: BatchJob) -> Optional[bool]:
    if not job.preflight_command:
        return True
    env = dict(os.environ)
    env['MCN_BATCH_LAUNCHER_ACTIVE'] = '1'
    env.setdefault('MCN_DISABLE_GLOBAL_APP_BOOTSTRAP', '1')
    try:
        completed = subprocess.run(
            list(job.preflight_command),
            cwd=str(ROOT),
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        payload = json.loads((completed.stdout or '').strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        return None
    if completed.returncode != 0 or payload.get('ok') is not True:
        return None
    status = str(payload.get('status') or '')
    if status == 'idle':
        return False
    if status == 'due':
        return True
    return None


def _admission_failure_exit_code(completed: subprocess.CompletedProcess[str]) -> int:
    try:
        payload = json.loads(
            str(completed.stdout or "").strip().splitlines()[-1]
        )
    except (IndexError, json.JSONDecodeError):
        print(
            json.dumps(
                {
                    "event": "batch_admission_contract_error",
                    "returncode": int(completed.returncode or 1),
                    "stdout_tail": str(completed.stdout or "")[-500:],
                    "stderr_tail": str(completed.stderr or "")[-500:],
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            flush=True,
        )
        return 1
    if payload.get("ok") is not False or not isinstance(
        payload.get("reasons"), list
    ):
        print(
            json.dumps(
                {
                    "event": "batch_admission_contract_error",
                    "returncode": int(completed.returncode or 1),
                    "payload": payload,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            flush=True,
        )
        return 1
    # Every valid admission refusal is a temporary, fail-closed resource
    # decision.  Preserve it as exit 75 so the durable task ledger can apply
    # bounded backoff instead of marooning the task in manual review.
    print(
        json.dumps(
            {
                "event": "batch_admission_deferred",
                "admission": payload.get("admission"),
                "reasons": payload.get("reasons"),
                "checks": payload.get("checks"),
                "used_percent": payload.get("used_percent"),
                "available_bytes": payload.get("available_bytes"),
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        flush=True,
    )
    return 75


def _admission_reasons(completed: subprocess.CompletedProcess[str]) -> set[str]:
    try:
        payload = json.loads(str(completed.stdout or '').strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        return set()
    reasons = payload.get('reasons') if isinstance(payload, dict) else []
    return {str(reason) for reason in reasons} if isinstance(reasons, list) else set()


def _run_disk_cleanup(job: BatchJob) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        [
            PYTHON,
            str(ROOT / 'scripts' / 'mcn_disk_guard_cleanup.py'),
            '--apply',
            '--free-threshold-gb',
            '15' if job.strict_admission else '10',
        ],
        cwd=str(ROOT),
        check=False,
        capture_output=True,
        text=True,
        timeout=300,
    )
    print(
        json.dumps(
            {
                'event': 'batch_disk_cleanup_fallback',
                'job': job.name,
                'returncode': int(completed.returncode or 0),
                'stdout_tail': str(completed.stdout or '')[-2000:],
                'stderr_tail': str(completed.stderr or '')[-500:],
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        flush=True,
    )
    return completed


def _run_inside(job: BatchJob) -> int:
    if ROOT.resolve() == PRODUCTION_PROJECT_ROOT:
        cgroup = current_cgroup_text()
        if not cgroup_contains_slice(cgroup, job.slice_name):
            raise RuntimeError(f'batch_runner_wrong_slice:{job.slice_name}')
    if ROOT.resolve() == PRODUCTION_PROJECT_ROOT and not _restart_receipt_attributed():
        return 1
    with _singleflight(job) as singleflight_acquired:
        if not singleflight_acquired:
            print(
                json.dumps(
                    {
                        "event": "batch_runner_deferred",
                        "job": job.name,
                        "reason": "singleflight_busy",
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
            return 75
        preflight = _preflight_requires_execution(job)
        if preflight is False:
            return 0
        if preflight is not True:
            print(
                json.dumps(
                    {
                        "event": "batch_runner_deferred",
                        "job": job.name,
                        "reason": "preflight_not_ready",
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
            return 75
        admission = subprocess.run(
            _admission_command(job),
            cwd=str(ROOT),
            check=False,
            capture_output=True,
            text=True,
        )
        if (
            admission.returncode != 0
            and job.disk_cleanup_fallback
            and 'disk_guard' in _admission_reasons(admission)
        ):
            _run_disk_cleanup(job)
            admission = subprocess.run(
                _admission_command(job),
                cwd=str(ROOT),
                check=False,
                capture_output=True,
                text=True,
            )
        if admission.returncode != 0:
            return _admission_failure_exit_code(admission)
        env = dict(os.environ)
        env['MCN_BATCH_LAUNCHER_ACTIVE'] = '1'
        env.setdefault('MCN_DISABLE_GLOBAL_APP_BOOTSTRAP', '1')
        completed = subprocess.run(
            list(job.command),
            cwd=str(ROOT),
            env=env,
            check=False,
            timeout=job.timeout_seconds,
        )
        return int(completed.returncode or 0)


def _transient_command(job: BatchJob) -> list[str]:
    unit = f'mcn-batch-{job.name}-{int(time.time())}-{os.getpid()}'
    return [
        'systemd-run', '--wait', '--collect', '--pipe', '--quiet',
        f'--unit={unit}',
        f'--slice={job.slice_name}',
        f'--property=WorkingDirectory={ROOT}',
        '--property=Environment=MCN_BATCH_RUNNER_INSIDE=1',
        '--property=Environment=MCN_DISABLE_GLOBAL_APP_BOOTSTRAP=1',
        '--property=Nice=15',
        '--property=IOSchedulingClass=idle',
        '--property=IOSchedulingPriority=7',
        '--property=KillMode=control-group',
        '--property=SuccessExitStatus=75',
        f'--property=TimeoutStartSec={job.timeout_seconds}',
        '--property=TimeoutStopSec=45s',
        PYTHON,
        str(Path(__file__).resolve()),
        job.name,
    ]


def run(job: BatchJob) -> int:
    if ROOT.resolve() != PRODUCTION_PROJECT_ROOT:
        return _run_inside(job)
    cgroup = current_cgroup_text()
    if os.getenv('MCN_BATCH_RUNNER_INSIDE') == '1' or cgroup_contains_slice(cgroup, job.slice_name):
        return _run_inside(job)
    completed = subprocess.run(_transient_command(job), cwd=str(ROOT), check=False)
    return int(completed.returncode or 0)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description='Run a managed MCN SQLite batch job.')
    parser.add_argument('job', choices=tuple(JOBS))
    args = parser.parse_args(argv)
    return run(JOBS[args.job])


if __name__ == '__main__':
    raise SystemExit(main())
