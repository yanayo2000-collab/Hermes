#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence


ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / '.venv/bin/python'
DEFAULT_QUEUE_ROOT = Path('/var/lib/mcn-ai-automation/deploy-queue')
QUEUE_STATES = ('queued', 'running', 'succeeded', 'failed', 'manual-review')
DEPLOY_LOCK = Path('/var/lock/mcn-deploy.lock')
ETL_LOCK = Path('/tmp/mcn-ai-automation-sqlite-job-locks/sqlite-etl.lock')
WRITER_LOCK = Path('/tmp/mcn-ai-automation-sqlite-job-locks/sqlite-writer.lock')
RELEASE_ID_PATTERN = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$')
RELEASE_FAMILY_SUFFIX_PATTERN = re.compile(
    r'(?:-v\d+)?-\d{8}T\d{6}Z(?:-v\d+)?$', re.IGNORECASE,
)
ALLOWED_RUNNER_HEADERS = (b'#!/usr/bin/env bash\n', b'#!/bin/bash\n')
RELEASE_FREEZES_FILE = 'release-freezes.json'
ADMISSION_PASS_TTL = timedelta(minutes=10)
RUNNER_DEFER_BACKOFF = timedelta(minutes=5)
GLOBAL_HARD_ADMISSION_REASONS = {'restart_receipt', 'failed_units'}
CRITICAL_RESTART_FAILED_UNITS = {'mcn-backend.service', 'mcn-db-writer.service'}
SQLITE_RESOURCES = {'sqlite_etl', 'automation_db_writer', 'analytics_active_writer'}
SOFT_ADMISSION_RETRY_ATTEMPTS = 4
SOFT_ADMISSION_RETRY_SECONDS = 10.0
DISK_CLEANUP_RETRY_COOLDOWN = timedelta(minutes=30)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _utc_datetime(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or ''))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _venv_distribution_snapshot() -> dict[str, Any]:
    """Return a stable, secret-free fingerprint of the active Python environment."""
    from importlib import metadata

    distributions = sorted({
        (
            str(distribution.metadata.get('Name') or '').strip().lower(),
            str(distribution.version or '').strip(),
        )
        for distribution in metadata.distributions()
        if str(distribution.metadata.get('Name') or '').strip()
    })
    encoded = json.dumps(distributions, ensure_ascii=True, separators=(',', ':')).encode('utf-8')
    return {
        'sha256': hashlib.sha256(encoded).hexdigest(),
        'distribution_count': len(distributions),
    }


def _fsync_path(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f'.{path.name}.{os.getpid()}.{time.time_ns()}.tmp')
    with temporary.open('x', encoding='utf-8') as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write('\n')
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    _fsync_path(path.parent)


def _load_job(path: Path) -> dict[str, Any]:
    payload = json.loads((path / 'job.json').read_text(encoding='utf-8'))
    if not isinstance(payload, dict):
        raise RuntimeError(f'deploy_queue_job_invalid:{path.name}')
    return payload


def _prepare_queue_root(queue_root: Path) -> None:
    queue_root.mkdir(parents=True, exist_ok=True)
    os.chmod(queue_root, 0o700)
    for state in QUEUE_STATES:
        directory = queue_root / state
        directory.mkdir(exist_ok=True)
        os.chmod(directory, 0o700)


@contextmanager
def _queue_lock(path: Path) -> Iterator[bool]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+', encoding='utf-8') as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _regular_locked_down(path: Path) -> bool:
    details = path.lstat()
    return (
        stat.S_ISREG(details.st_mode)
        and details.st_uid == os.geteuid()
        and not details.st_mode & 0o022
    )


def _release_family(release_id: str) -> str:
    without_timestamp = RELEASE_FAMILY_SUFFIX_PATTERN.sub('', str(release_id or '').strip())
    return re.sub(r'-v\d+$', '', without_timestamp, flags=re.IGNORECASE)


def _load_release_freezes(queue_root: Path) -> list[dict[str, str]]:
    path = queue_root / RELEASE_FREEZES_FILE
    if not path.is_file():
        return []
    payload = json.loads(path.read_text(encoding='utf-8'))
    rows = payload.get('freezes') if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise RuntimeError('deploy_queue_release_freezes_invalid')
    return [row for row in rows if isinstance(row, dict) and str(row.get('prefix') or '').strip()]


def _matching_release_freeze(queue_root: Path, release_id: str) -> dict[str, str] | None:
    value = str(release_id or '')
    return next(
        (row for row in _load_release_freezes(queue_root) if value.startswith(str(row['prefix']))),
        None,
    )


def freeze_release_prefix(*, prefix: str, reason: str, queue_root: Path) -> dict[str, Any]:
    normalized_prefix = str(prefix or '').strip()
    normalized_reason = str(reason or '').strip()
    if not normalized_prefix or not normalized_reason:
        raise RuntimeError('deploy_queue_release_freeze_requires_prefix_and_reason')
    _prepare_queue_root(queue_root)
    rows = [
        row for row in _load_release_freezes(queue_root)
        if str(row.get('prefix') or '') != normalized_prefix
    ]
    row = {'prefix': normalized_prefix, 'reason': normalized_reason, 'created_at_utc': _utc_now()}
    rows.append(row)
    _write_json_atomic(queue_root / RELEASE_FREEZES_FILE, {'schema_version': 1, 'freezes': rows})
    return {'ok': True, 'frozen': True, **row}


def unfreeze_release_prefix(*, prefix: str, queue_root: Path) -> dict[str, Any]:
    normalized_prefix = str(prefix or '').strip()
    _prepare_queue_root(queue_root)
    rows = [
        row for row in _load_release_freezes(queue_root)
        if str(row.get('prefix') or '') != normalized_prefix
    ]
    _write_json_atomic(queue_root / RELEASE_FREEZES_FILE, {'schema_version': 1, 'freezes': rows})
    return {'ok': True, 'frozen': False, 'prefix': normalized_prefix}


def _validate_runner_contract(runner: Path) -> None:
    source = runner.read_text(encoding='utf-8', errors='replace')
    if re.search(r'\bsystemctl\s+--failed\b', source):
        raise RuntimeError('deploy_queue_runner_global_failed_units_forbidden')
    if re.search(r'\bln\s+(?:-[^\n]*\s+)*[^\n]*\.venv(?:/|\s|["\'])', source):
        raise RuntimeError('deploy_queue_runner_must_not_symlink_production_venv')
    if 'pytest' in source and not re.search(r'(^|\n)\s*(?:echo\s+)?["\']?(?:phase|step)=', source):
        raise RuntimeError('deploy_queue_runner_missing_phase_markers')
    artifact_tests = 'pytest' in source and re.search(r'artifact_dir[^\n]*test_|artifacts/[^\n]*test_', source)
    if artifact_tests and not re.search(r'PYTHONPATH\s*=.*(?:\$\{?root\}?|/opt/mcn-ai-automation)', source):
        raise RuntimeError('deploy_queue_artifact_pytest_requires_project_pythonpath')
    if 'systemctl stop' in source and '.timer' in source and 'restore_timer_states' not in source:
        raise RuntimeError('deploy_queue_timer_stop_requires_exit_restore')


def _latest_failed_family_job(queue_root: Path, release_id: str) -> tuple[Path, dict[str, Any]] | None:
    family = _release_family(release_id)
    candidates: list[tuple[Path, dict[str, Any]]] = []
    for path in (queue_root / 'failed').iterdir():
        job = _load_job(path)
        if _release_family(str(job.get('release_id') or '')) == family:
            candidates.append((path, job))
    return max(candidates, key=lambda item: item[0].name) if candidates else None


def _jobs_matching(
    queue_root: Path,
    states: Sequence[str],
    field: str,
    value: str,
) -> list[tuple[Path, dict[str, Any]]]:
    normalized = str(value or '').strip()
    if not normalized:
        return []
    matches: list[tuple[Path, dict[str, Any]]] = []
    for state in states:
        for path in (queue_root / state).iterdir():
            job = _load_job(path)
            if str(job.get(field) or '').strip() == normalized:
                matches.append((path, job))
    return matches


def _persist_runner_output(job_dir: Path, stdout: object, stderr: object) -> dict[str, Any]:
    def text_value(value: object) -> str:
        if isinstance(value, bytes):
            return value.decode('utf-8', 'replace')
        return str(value or '')

    combined = text_value(stdout) + '\n' + text_value(stderr)
    attempt = 1
    log_path = job_dir / 'runner-output.log'
    while log_path.exists():
        attempt += 1
        log_path = job_dir / f'runner-output-{attempt}.log'
    with log_path.open('x', encoding='utf-8') as handle:
        handle.write(combined)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(log_path, 0o600)
    phases = re.findall(r'(?m)^(?:phase|step)=([^\r\n]+)', combined)
    return {
        'output_sha256': hashlib.sha256(combined.encode('utf-8', 'replace')).hexdigest(),
        'output_log': log_path.name,
        'output_size_bytes': log_path.stat().st_size,
        'failure_phase': phases[-1].strip() if phases else 'runner_process',
    }


def enqueue(
    *,
    release_id: str,
    description: str,
    runner: Path,
    artifacts: Sequence[Path],
    queue_root: Path,
    required_passes: int,
    failed_queue_id: str = '',
    failure_diagnosis: str = '',
    work_item_id: str = '',
    priority_class: int = 2,
    deadline_at_utc: str = '',
    restart_policy: str = '',
    dependency_units: Sequence[str] = (),
    blocking_units: Sequence[str] = (),
    blocking_queues: Sequence[str] = (),
    required_resources: Sequence[str] = (),
    batch_id: str = '',
    candidate_id: str = '',
    max_production_attempts: int = 2,
    allow_venv_mutation: bool = False,
    _queue_locked: bool = False,
) -> dict[str, Any]:
    if not RELEASE_ID_PATTERN.fullmatch(release_id):
        raise RuntimeError('deploy_queue_release_id_invalid')
    if not runner.is_file():
        raise RuntimeError('deploy_queue_runner_missing')
    if restart_policy not in {'backend', 'none'}:
        raise RuntimeError('deploy_queue_restart_policy_invalid')
    if int(priority_class) not in {1, 2, 3, 4}:
        raise RuntimeError('deploy_queue_priority_class_invalid')
    if deadline_at_utc and _utc_datetime(deadline_at_utc) is None:
        raise RuntimeError('deploy_queue_deadline_invalid')
    with runner.open('rb') as handle:
        header = handle.readline(256)
    if header not in ALLOWED_RUNNER_HEADERS:
        raise RuntimeError('deploy_queue_runner_must_be_bash')
    _validate_runner_contract(runner)
    _prepare_queue_root(queue_root)
    if not _queue_locked:
        with _queue_lock(queue_root / '.dispatcher.lock') as acquired:
            if not acquired:
                raise RuntimeError('deploy_queue_dispatcher_busy')
            return enqueue(
                release_id=release_id,
                description=description,
                runner=runner,
                artifacts=artifacts,
                queue_root=queue_root,
                required_passes=required_passes,
                failed_queue_id=failed_queue_id,
                failure_diagnosis=failure_diagnosis,
                work_item_id=work_item_id,
                priority_class=priority_class,
                deadline_at_utc=deadline_at_utc,
                restart_policy=restart_policy,
                dependency_units=dependency_units,
                blocking_units=blocking_units,
                blocking_queues=blocking_queues,
                required_resources=required_resources,
                batch_id=batch_id,
                candidate_id=candidate_id,
                max_production_attempts=max_production_attempts,
                allow_venv_mutation=allow_venv_mutation,
                _queue_locked=True,
            )
    normalized_work_item = str(work_item_id or '').strip()
    if _jobs_matching(queue_root, ('queued', 'running'), 'work_item_id', normalized_work_item):
        raise RuntimeError(f'deploy_queue_work_item_already_active:{normalized_work_item}')
    normalized_candidate = str(candidate_id or '').strip()
    attempt_limit = int(max_production_attempts)
    if attempt_limit < 1 or attempt_limit > 10:
        raise RuntimeError('deploy_queue_max_production_attempts_invalid')
    prior_attempts = _jobs_matching(
        queue_root, ('failed', 'manual-review'), 'candidate_id', normalized_candidate,
    )
    if normalized_candidate and len(prior_attempts) >= attempt_limit:
        raise RuntimeError(
            f'deploy_queue_candidate_attempt_budget_exhausted:{normalized_candidate}:{attempt_limit}'
        )
    frozen = _matching_release_freeze(queue_root, release_id)
    if frozen:
        raise RuntimeError(f'deploy_queue_release_frozen:{frozen["prefix"]}:{frozen["reason"]}')
    prior_failure = _latest_failed_family_job(queue_root, release_id)
    failure_ack = None
    if prior_failure is not None:
        prior_path, prior_job = prior_failure
        diagnosis = str(failure_diagnosis or '').strip()
        if str(failed_queue_id or '') != str(prior_job.get('queue_id') or '') or len(diagnosis) < 20:
            raise RuntimeError(f'deploy_queue_prior_failure_requires_diagnosis:{prior_path.name}')
        failure_ack = {
            'failed_queue_id': str(failed_queue_id),
            'diagnosis': diagnosis,
            'acknowledged_at_utc': _utc_now(),
        }
    queue_id = f'{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{release_id}-{uuid.uuid4().hex[:8]}'
    temporary = queue_root / f'.enqueue-{queue_id}'
    target = queue_root / 'queued' / queue_id
    temporary.mkdir(mode=0o700)
    try:
        staged_runner = temporary / 'runner.sh'
        shutil.copyfile(runner, staged_runner)
        os.chmod(staged_runner, 0o500)
        _fsync_path(staged_runner)
        artifact_rows = []
        artifact_dir = temporary / 'artifacts'
        artifact_dir.mkdir(mode=0o700)
        seen_names: set[str] = set()
        for source in artifacts:
            if not source.is_file() or source.name in seen_names:
                raise RuntimeError(f'deploy_queue_artifact_invalid:{source}')
            seen_names.add(source.name)
            staged = artifact_dir / source.name
            shutil.copyfile(source, staged)
            os.chmod(staged, 0o400)
            _fsync_path(staged)
            artifact_rows.append({
                'name': source.name,
                'sha256': _sha256(staged),
                'size_bytes': staged.stat().st_size,
            })
        job = {
            'schema_version': 3,
            'queue_id': queue_id,
            'release_id': release_id,
            'description': str(description or '').strip(),
            'state': 'queued',
            'created_at_utc': _utc_now(),
            'updated_at_utc': _utc_now(),
            'required_consecutive_passes': max(1, min(int(required_passes), 10)),
            'consecutive_admission_passes': 0,
            'last_admission_pass_at_utc': '',
            'deferred_until_utc': '',
            'soft_block_count': 0,
            'work_item_id': normalized_work_item,
            'candidate_id': normalized_candidate,
            'production_attempt_number': len(prior_attempts) + 1,
            'max_production_attempts': attempt_limit,
            'priority_class': int(priority_class),
            'deadline_at_utc': str(deadline_at_utc or '').strip(),
            'restart_policy': restart_policy,
            'dependency_units': sorted({str(value).strip() for value in dependency_units if str(value).strip()}),
            'blocking_units': sorted({str(value).strip() for value in blocking_units if str(value).strip()}),
            'blocking_queues': sorted({str(value).strip() for value in blocking_queues if str(value).strip()}),
            'required_resources': sorted({str(value).strip() for value in required_resources if str(value).strip()}),
            'batch_id': str(batch_id or '').strip(),
            'runner': {'name': 'runner.sh', 'sha256': _sha256(staged_runner)},
            'artifacts': artifact_rows,
            'last_admission': None,
            'result': None,
            'failure_ack': failure_ack,
            'allow_venv_mutation': bool(allow_venv_mutation),
        }
        _write_json_atomic(temporary / 'job.json', job)
        os.replace(temporary, target)
        _fsync_path(target.parent)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return {'ok': True, 'queued': True, 'queue_id': queue_id, 'path': str(target)}


def supersede(
    *,
    queue_id: str,
    replacement_release_id: str,
    reason: str,
    queue_root: Path,
) -> dict[str, Any]:
    if not RELEASE_ID_PATTERN.fullmatch(replacement_release_id):
        raise RuntimeError('deploy_queue_replacement_release_id_invalid')
    normalized_reason = str(reason or '').strip()
    if not normalized_reason:
        raise RuntimeError('deploy_queue_supersede_reason_required')
    _prepare_queue_root(queue_root)
    with _queue_lock(queue_root / '.dispatcher.lock') as acquired:
        if not acquired:
            return {'ok': True, 'deferred': True, 'reason': 'queue_dispatcher_busy'}
        job_dir = queue_root / 'queued' / queue_id
        if not job_dir.is_dir():
            raise RuntimeError('deploy_queue_queued_job_not_found')
        job = _load_job(job_dir)
        job['result'] = {
            'status': 'manual_review',
            'reason': f'superseded_before_execution:{normalized_reason}',
            'replacement_release_id': replacement_release_id,
        }
        target = _move_job(job_dir, queue_root, 'manual-review', job)
    return {
        'ok': True,
        'queue_id': queue_id,
        'state': 'manual-review',
        'replacement_release_id': replacement_release_id,
        'path': str(target),
    }


def _run_json(command: list[str], *, timeout: float = 30.0) -> tuple[int, dict[str, Any]]:
    try:
        completed = subprocess.run(
            command, cwd=str(ROOT), check=False, capture_output=True,
            text=True, timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 70, {'ok': False, 'error': f'{type(exc).__name__}:{str(exc)[:160]}'}
    try:
        payload = json.loads((completed.stdout or '').strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        payload = {'ok': False, 'error': 'command_output_not_json'}
    return completed.returncode, payload


def _lock_available(path: Path) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+', encoding='utf-8') as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return True


def _active_queue_counts(path: Path) -> dict[str, int]:
    now = _utc_now()
    with sqlite3.connect(f'file:{path}?mode=ro', uri=True, timeout=10.0) as conn:
        row = conn.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM creative_generation_tasks
               WHERE status IN ('claimed','generating')
                 AND (lease_expires_at IS NULL OR lease_expires_at > ?)),
              (SELECT COUNT(*) FROM im_llm_diagnosis_tasks
               WHERE status='claimed'
                 AND (lease_expires_at IS NULL OR lease_expires_at > ?)),
              (SELECT COUNT(*) FROM automation_tasks
               WHERE status IN ('processing','running','claimed')
                 AND (lease_until IS NULL OR lease_until > ?)),
              (SELECT COUNT(*) FROM mcn_operation_tasks
               WHERE status='running'
                 AND (lease_until IS NULL OR lease_until > ?))
            """,
            (now, now, now, now),
        ).fetchone()
    return dict(zip(('creative', 'im', 'automation', 'operations'), map(int, row)))


def collect_admission(job: dict[str, Any] | None = None) -> dict[str, Any]:
    job = job or {'schema_version': 3}
    checks: dict[str, Any] = {}
    reasons: list[str] = []

    def add(name: str, ok: bool, detail: Any) -> None:
        checks[name] = {'ok': bool(ok), 'detail': detail}
        if not ok:
            reasons.append(name)

    receipt_code, receipt = _run_json([
        str(PYTHON), str(ROOT / 'scripts/mcn_release_governance.py'),
        'audit-restart', '--unit', 'mcn-backend.service',
    ])
    add('restart_receipt', receipt_code == 0 and receipt.get('ok') is True, {
        'classification': receipt.get('classification'),
        'invocation_id': receipt.get('current_invocation_id'),
        'receipt': receipt.get('matching_receipt'),
    })
    restart_policy = str(job.get('restart_policy') or '')
    if restart_policy not in {'backend', 'none'}:
        add('restart_policy', False, restart_policy or 'missing')
    required_resources = set(job.get('required_resources') or ())
    requires_sqlite = bool(required_resources.intersection(SQLITE_RESOURCES))
    resource_command = [
        str(PYTHON), str(ROOT / 'scripts/check_batch_admission.py'),
        '--max-used-percent', '85', '--min-free-gb', '10',
        '--backend-health-url', 'http://127.0.0.1:8011/health',
        '--backend-health-checks', '3', '--backend-health-timeout-seconds', '2',
        '--max-backend-health-latency-seconds', '0.5',
        '--observe-api-slow', '--api-slow-threshold-ms', '3000',
    ]
    if restart_policy == 'backend':
        resource_command.extend([
            '--min-mem-available-gb', '2',
            '--max-load1', str(max(4.0, (os.cpu_count() or 1) * 1.5)),
            '--max-iowait-percent', '20', '--iowait-sample-seconds', '2',
            '--max-nginx-504-count', '0',
        ])
    if restart_policy == 'backend' or requires_sqlite:
        resource_command.extend([
            '--recent-window-minutes', '5', '--max-db-locked-count', '0',
            '--db-lock-event-dedupe-seconds', '10', '--db-lock-cooldown-seconds', '30',
            '--db-lock-path', str(ETL_LOCK), '--db-lock-path', str(WRITER_LOCK),
        ])
    resource_code, resources = _run_json(resource_command)
    add('resources', resource_code == 0 and resources.get('ok') is True, {
        'admission': resources.get('admission'),
        'reasons': resources.get('reasons'),
    })
    blocking_units = tuple(job.get('blocking_units') or ())
    active_batches = [
        unit for unit in blocking_units
        if subprocess.run(
            ['systemctl', 'is-active', '--quiet', unit], check=False,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ).returncode == 0
    ]
    add('batch_units', not active_batches, active_batches)
    failed = subprocess.run(
        ['systemctl', '--failed', '--no-legend', '--plain'], check=False,
        capture_output=True, text=True, timeout=15,
    ).stdout.splitlines()
    failed_names = [line.strip().split()[0] for line in failed if line.strip()]
    failed_names = [name for name in failed_names if name != 'mcn-deploy-queue.service']
    global_failed = sorted(CRITICAL_RESTART_FAILED_UNITS.intersection(failed_names))
    unrelated_failed = sorted(set(failed_names).difference(global_failed))
    # Only failed units that can invalidate the backend restart path freeze the
    # dispatcher globally. Every other failed unit is retained as diagnostics;
    # declared dependencies still block only their own candidate below.
    add(
        'failed_units',
        restart_policy == 'none' or not global_failed,
        global_failed[:20],
    )
    checks['unrelated_failed_units'] = {
        'ok': True,
        'blocking': False,
        'detail': unrelated_failed[:20],
    }
    dependencies = set(job.get('dependency_units') or ())
    relevant_failed = sorted(dependencies.intersection(failed_names))
    add('dependency_failed_units', not relevant_failed, relevant_failed[:20])
    add('deploy_lock', _lock_available(DEPLOY_LOCK), str(DEPLOY_LOCK))
    if requires_sqlite:
        add('sqlite_etl_lock', _lock_available(ETL_LOCK), str(ETL_LOCK))
    blocking_queues = set(job.get('blocking_queues') or ())
    try:
        queue_counts = _active_queue_counts(ROOT / 'data/automation.db') if blocking_queues else {}
        selected_counts = {
            name: count for name, count in queue_counts.items() if name in blocking_queues
        }
        if blocking_queues:
            add('production_queues', not any(selected_counts.values()), selected_counts)
    except (OSError, sqlite3.Error) as exc:
        if blocking_queues:
            add('production_queues', False, f'{type(exc).__name__}:{str(exc)[:160]}')
    return {
        'ok': not reasons,
        'checked_at_utc': _utc_now(),
        'reasons': reasons,
        'checks': checks,
    }


def _collect_admission_window(job: dict[str, Any] | None = None) -> dict[str, Any]:
    admission = collect_admission(job)
    for _ in range(SOFT_ADMISSION_RETRY_ATTEMPTS - 1):
        if admission['ok'] or GLOBAL_HARD_ADMISSION_REASONS.intersection(admission['reasons']):
            break
        time.sleep(SOFT_ADMISSION_RETRY_SECONDS)
        admission = collect_admission(job)
    return admission


def _disk_only_admission_failure(admission: dict[str, Any]) -> bool:
    if admission.get('ok') is True or set(admission.get('reasons') or []) != {'resources'}:
        return False
    resources = ((admission.get('checks') or {}).get('resources') or {}).get('detail') or {}
    return set(resources.get('reasons') or []) == {'disk_guard'}


def _disk_cleanup_due(job: dict[str, Any]) -> bool:
    previous = _utc_datetime(job.get('last_disk_cleanup_attempt_at_utc'))
    return previous is None or datetime.now(timezone.utc) - previous >= DISK_CLEANUP_RETRY_COOLDOWN


def _run_safe_disk_cleanup() -> dict[str, Any]:
    code, payload = _run_json([
        str(PYTHON), str(ROOT / 'scripts/mcn_disk_guard_cleanup.py'),
        '--apply', '--threshold', '75', '--free-threshold-gb', '15',
    ], timeout=300)
    return {'ok': code == 0, 'returncode': code, 'detail': payload}


def _validate_staged_job(job_dir: Path, job: dict[str, Any]) -> Path:
    if int(job.get('schema_version') or 0) < 2:
        raise RuntimeError('deploy_queue_scoped_contract_required')
    runner = job_dir / str((job.get('runner') or {}).get('name') or '')
    if not _regular_locked_down(runner) or _sha256(runner) != (job.get('runner') or {}).get('sha256'):
        raise RuntimeError('deploy_queue_runner_integrity_failed')
    for artifact in job.get('artifacts') or []:
        path = job_dir / 'artifacts' / str(artifact.get('name') or '')
        if not _regular_locked_down(path) or _sha256(path) != artifact.get('sha256'):
            raise RuntimeError(f'deploy_queue_artifact_integrity_failed:{path.name}')
    return runner


def _move_job(job_dir: Path, queue_root: Path, state: str, job: dict[str, Any]) -> Path:
    job['state'] = state
    job['updated_at_utc'] = _utc_now()
    _write_json_atomic(job_dir / 'job.json', job)
    target = queue_root / state / job_dir.name
    os.replace(job_dir, target)
    _fsync_path(job_dir.parent)
    _fsync_path(target.parent)
    return target


def _eligible_jobs(queue_root: Path) -> tuple[list[Path], list[Path]]:
    now = datetime.now(timezone.utc)
    eligible: list[Path] = []
    deferred: list[Path] = []
    rows: list[tuple[tuple[Any, ...], Path, bool]] = []
    for path in (queue_root / 'queued').iterdir():
        job = _load_job(path)
        until = _utc_datetime(job.get('deferred_until_utc'))
        deadline = _utc_datetime(job.get('deadline_at_utc'))
        created = _utc_datetime(job.get('created_at_utc')) or datetime.max.replace(tzinfo=timezone.utc)
        key = (
            int(job.get('priority_class') or 2),
            deadline is None,
            deadline or datetime.max.replace(tzinfo=timezone.utc),
            created,
            path.name,
        )
        rows.append((key, path, until is not None and until > now))
    for _, path, is_deferred in sorted(rows, key=lambda item: item[0]):
        (deferred if is_deferred else eligible).append(path)
    return eligible, deferred


def _defer_blocked_job(job_dir: Path, job: dict[str, Any], admission: dict[str, Any]) -> None:
    count = min(int(job.get('soft_block_count') or 0) + 1, 20)
    delay_seconds = min(60 * (2 ** min(count - 1, 6)), 3600)
    job['soft_block_count'] = count
    job['deferred_until_utc'] = (datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)).isoformat()
    created = _utc_datetime(job.get('created_at_utc'))
    if created and datetime.now(timezone.utc) - created >= timedelta(minutes=120):
        job['escalated_at_utc'] = str(job.get('escalated_at_utc') or _utc_now())
        job['escalation_reason'] = 'deployment_wait_exceeded_120_minutes'
    job['last_admission'] = admission
    job['updated_at_utc'] = _utc_now()
    _write_json_atomic(job_dir / 'job.json', job)


def _audit_job_release(job: dict[str, Any], *, before_invocation: str) -> tuple[bool, dict[str, Any]]:
    restart_policy = str(job.get('restart_policy') or 'backend')
    if restart_policy == 'backend':
        audit_command = [
            str(PYTHON), str(ROOT / 'scripts/mcn_release_governance.py'),
            'audit-restart', '--unit', 'mcn-backend.service',
        ]
    else:
        audit_command = [
            str(PYTHON), str(ROOT / 'scripts/mcn_release_governance.py'),
            'audit-deployment', '--release-id', str(job.get('release_id') or ''),
            '--unit', 'mcn-backend.service', '--expected-invocation-id', before_invocation,
        ]
    audit_code, audit = _run_json(audit_command)
    after_invocation = str(audit.get('current_invocation_id') or '')
    receipt_path = Path(str(audit.get('matching_receipt') or ''))
    try:
        receipt_release_id = str(json.loads(receipt_path.read_text(encoding='utf-8')).get('release_id') or '')
    except (OSError, json.JSONDecodeError):
        receipt_release_id = ''
    invocation_ok = (
        after_invocation != before_invocation if restart_policy == 'backend'
        else after_invocation == before_invocation
    )
    valid = (
        audit_code == 0 and audit.get('ok') is True and bool(after_invocation)
        and invocation_ok and receipt_release_id == str(job.get('release_id') or '')
    )
    return valid, {
        'audit': audit,
        'invocation_id': after_invocation,
        'receipt_release_id': receipt_release_id,
        'restart_policy': restart_policy,
    }


def _execute_ready_job(
    *, job_dir: Path, job: dict[str, Any], runner: Path,
    queue_root: Path, before_invocation: str,
) -> dict[str, Any]:
    running = _move_job(job_dir, queue_root, 'running', job)
    venv_before = _venv_distribution_snapshot()
    environment = dict(os.environ)
    environment.update({
        'MCN_DEPLOY_QUEUE_ACTIVE': '1',
        'MCN_DEPLOY_QUEUE_ID': str(job.get('queue_id') or ''),
        'MCN_DEPLOY_QUEUE_JOB_DIR': str(running),
        'MCN_DEPLOY_RESTART_POLICY': str(job.get('restart_policy') or 'backend'),
        'MCN_DEPLOY_DEPENDENCY_UNITS': ':'.join(
            str(value).strip() for value in (job.get('dependency_units') or [])
            if str(value).strip()
        ),
        'MCN_DEPLOY_EXPECTED_INVOCATION_ID': before_invocation,
    })
    required_resources = set(job.get('required_resources') or ())
    extra_locks: set[Path] = set()
    if str(job.get('restart_policy') or 'backend') == 'backend':
        extra_locks.update({ETL_LOCK, WRITER_LOCK})
    if required_resources.intersection(
        {'sqlite_etl', 'automation_db_writer', 'analytics_active_writer'}
    ):
        extra_locks.add(ETL_LOCK)
    if required_resources.intersection(
        {'automation_db_writer', 'analytics_active_writer'}
    ):
        extra_locks.add(WRITER_LOCK)
    environment['MCN_DEPLOY_EXTRA_LOCK_PATHS'] = ':'.join(
        str(path) for path in sorted(extra_locks, key=str)
    )
    environment['MCN_DEPLOY_RESOURCE_LOCK_WAIT_SECONDS'] = '90'
    try:
        completed = subprocess.run(
            [str(ROOT / 'scripts/mcn_deploy_lock.sh'), '/bin/bash', str(running / runner.name)],
            cwd=str(ROOT), env=environment, check=False, capture_output=True,
            text=True, timeout=45 * 60,
        )
    except subprocess.TimeoutExpired as exc:
        output = _persist_runner_output(running, exc.stdout, exc.stderr)
        job['result'] = {
            'status': 'manual_review', 'reason': 'runner_timeout', **output,
            'venv_before': venv_before, 'venv_after': _venv_distribution_snapshot(),
        }
        target = _move_job(running, queue_root, 'manual-review', job)
        return {'ok': False, 'queue_id': job.get('queue_id'), 'state': 'manual-review', 'path': str(target)}
    output = _persist_runner_output(running, completed.stdout, completed.stderr)
    venv_after = _venv_distribution_snapshot()
    if (
        venv_after.get('sha256') != venv_before.get('sha256')
        and not bool(job.get('allow_venv_mutation'))
    ):
        job['result'] = {
            'status': 'manual_review',
            'reason': 'production_venv_mutated_without_explicit_allowance',
            'returncode': completed.returncode,
            **output,
            'venv_before': venv_before,
            'venv_after': venv_after,
        }
        target = _move_job(running, queue_root, 'manual-review', job)
        return {
            'ok': False, 'queue_id': job.get('queue_id'),
            'state': 'manual-review', 'path': str(target),
        }
    if completed.returncode == 75:
        job['consecutive_admission_passes'] = 0
        job['last_admission_pass_at_utc'] = ''
        count = min(int(job.get('soft_block_count') or 0) + 1, 20)
        delay = min(int(RUNNER_DEFER_BACKOFF.total_seconds()) * (2 ** min(count - 1, 4)), 3600)
        job['soft_block_count'] = count
        job['deferred_until_utc'] = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
        job['result'] = {'status': 'deferred', 'returncode': 75, **output}
        target = _move_job(running, queue_root, 'queued', job)
        return {
            'ok': True, 'deferred': True, 'reason': 'runner_deferred_with_backoff',
            'deferred_until_utc': job['deferred_until_utc'], 'path': str(target),
        }
    if completed.returncode in {76, 77}:
        job['result'] = {
            'status': 'manual_review', 'returncode': completed.returncode,
            'reason': 'runner_reported_uncertain_production_boundary', **output,
        }
        target = _move_job(running, queue_root, 'manual-review', job)
        return {'ok': False, 'queue_id': job.get('queue_id'), 'state': 'manual-review', 'path': str(target)}
    if completed.returncode != 0:
        release_applied, release_evidence = _audit_job_release(
            job, before_invocation=before_invocation,
        )
        if release_applied:
            job['result'] = {
                'status': 'manual_review', 'returncode': completed.returncode,
                'reason': 'runner_failed_after_attributed_release', **output,
                'governance_audit': release_evidence['audit'],
                'invocation_id': release_evidence['invocation_id'],
                'receipt': release_evidence['audit'].get('matching_receipt'),
                'receipt_status': release_evidence['audit'].get('matching_receipt_status'),
                'restart_policy': release_evidence['restart_policy'],
            }
            target = _move_job(running, queue_root, 'manual-review', job)
            return {
                'ok': False, 'queue_id': job.get('queue_id'),
                'state': 'manual-review', 'path': str(target),
            }
        job['result'] = {'status': 'failed', 'returncode': completed.returncode, **output}
        target = _move_job(running, queue_root, 'failed', job)
        return {'ok': False, 'queue_id': job.get('queue_id'), 'state': 'failed', 'path': str(target)}

    release_applied, release_evidence = _audit_job_release(
        job, before_invocation=before_invocation,
    )
    audit = release_evidence['audit']
    after_invocation = release_evidence['invocation_id']
    restart_policy = release_evidence['restart_policy']
    if not release_applied:
        job['result'] = {
            'status': 'manual_review', 'returncode': completed.returncode,
            **output, 'governance_audit': audit, 'restart_policy': restart_policy,
        }
        target = _move_job(running, queue_root, 'manual-review', job)
        return {'ok': False, 'queue_id': job.get('queue_id'), 'state': 'manual-review', 'path': str(target)}
    job['result'] = {
        'status': 'succeeded', 'returncode': 0, **output,
        'invocation_id': after_invocation, 'receipt': audit.get('matching_receipt'),
        'receipt_status': audit.get('matching_receipt_status'), 'restart_policy': restart_policy,
        'venv_before': venv_before, 'venv_after': venv_after,
    }
    target = _move_job(running, queue_root, 'succeeded', job)
    return {
        'ok': True, 'queue_id': job.get('queue_id'), 'state': 'succeeded',
        'receipt': audit.get('matching_receipt'), 'invocation_id': after_invocation,
        'path': str(target),
    }


def run_once(*, queue_root: Path) -> dict[str, Any]:
    _prepare_queue_root(queue_root)
    with _queue_lock(queue_root / '.dispatcher.lock') as acquired:
        if not acquired:
            return {'ok': True, 'deferred': True, 'reason': 'queue_dispatcher_busy'}
        for stale in sorted((queue_root / 'running').iterdir()):
            job = _load_job(stale)
            job['result'] = {
                'status': 'manual_review',
                'reason': 'dispatcher_interrupted_after_runner_start',
            }
            _move_job(stale, queue_root, 'manual-review', job)
        jobs, deferred_jobs = _eligible_jobs(queue_root)
        if not jobs:
            if deferred_jobs:
                return {
                    'ok': True, 'deferred': True, 'reason': 'all_queued_jobs_in_backoff',
                    'queue_ids': [_load_job(path).get('queue_id') for path in deferred_jobs],
                }
            return {'ok': True, 'idle': True}
        considered: list[dict[str, Any]] = []
        for job_dir in jobs:
            job = _load_job(job_dir)
            try:
                runner = _validate_staged_job(job_dir, job)
            except Exception as exc:
                job['result'] = {'status': 'failed', 'reason': str(exc)}
                target = _move_job(job_dir, queue_root, 'failed', job)
                considered.append({'queue_id': job.get('queue_id'), 'state': 'failed', 'path': str(target)})
                continue
            admission = _collect_admission_window(job)
            if _disk_only_admission_failure(admission) and _disk_cleanup_due(job):
                job['last_disk_cleanup_attempt_at_utc'] = _utc_now()
                job['last_disk_cleanup_result'] = _run_safe_disk_cleanup()
                admission = _collect_admission_window(job)
            job['last_admission'] = admission
            if GLOBAL_HARD_ADMISSION_REASONS.intersection(admission['reasons']):
                job['consecutive_admission_passes'] = 0
                job['last_admission_pass_at_utc'] = ''
                job['updated_at_utc'] = _utc_now()
                _write_json_atomic(job_dir / 'job.json', job)
                return {
                    'ok': True, 'deferred': True, 'reason': 'global_production_freeze',
                    'queue_id': job.get('queue_id'), 'reasons': admission['reasons'],
                }
            if not admission['ok']:
                if 'dependency_failed_units' in admission['reasons']:
                    job['consecutive_admission_passes'] = 0
                    job['last_admission_pass_at_utc'] = ''
                _defer_blocked_job(job_dir, job, admission)
                considered.append({
                    'queue_id': job.get('queue_id'), 'state': 'deferred',
                    'reasons': admission['reasons'], 'until': job.get('deferred_until_utc'),
                })
                continue
            now = datetime.now(timezone.utc)
            previous_pass = _utc_datetime(job.get('last_admission_pass_at_utc'))
            if previous_pass is None or now - previous_pass > ADMISSION_PASS_TTL:
                job['consecutive_admission_passes'] = 0
            passes = int(job.get('consecutive_admission_passes') or 0) + 1
            job['consecutive_admission_passes'] = passes
            job['last_admission_pass_at_utc'] = now.isoformat()
            job['soft_block_count'] = 0
            job['deferred_until_utc'] = ''
            required = int(job.get('required_consecutive_passes') or 2)
            if passes < required:
                job['updated_at_utc'] = _utc_now()
                _write_json_atomic(job_dir / 'job.json', job)
                considered.append({
                    'queue_id': job.get('queue_id'), 'state': 'stabilizing',
                    'passes': passes, 'required': required,
                })
                continue
            before_invocation = str(admission['checks']['restart_receipt']['detail'].get('invocation_id') or '')
            return _execute_ready_job(
                job_dir=job_dir, job=job, runner=runner, queue_root=queue_root,
                before_invocation=before_invocation,
            )
        return {'ok': True, 'deferred': True, 'reason': 'no_job_ready_this_pass', 'considered': considered}


def list_jobs(queue_root: Path) -> dict[str, Any]:
    _prepare_queue_root(queue_root)
    rows = []
    for state in QUEUE_STATES:
        for path in sorted((queue_root / state).iterdir()):
            try:
                job = _load_job(path)
            except Exception:
                continue
            rows.append({
                'queue_id': job.get('queue_id'), 'release_id': job.get('release_id'),
                'work_item_id': job.get('work_item_id'), 'batch_id': job.get('batch_id'),
                'priority_class': job.get('priority_class'), 'deadline_at_utc': job.get('deadline_at_utc'),
                'restart_policy': job.get('restart_policy', ''),
                'state': state, 'description': job.get('description'),
                'created_at_utc': job.get('created_at_utc'),
                'updated_at_utc': job.get('updated_at_utc'),
                'deferred_until_utc': job.get('deferred_until_utc'),
                'escalated_at_utc': job.get('escalated_at_utc'),
                'last_admission_reasons': ((job.get('last_admission') or {}).get('reasons') or []),
                'result': job.get('result'),
            })
    return {'ok': True, 'jobs': rows}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Durable, fail-closed MCN governed deployment queue.')
    parser.add_argument('--queue-root', type=Path, default=DEFAULT_QUEUE_ROOT)
    subparsers = parser.add_subparsers(dest='command', required=True)
    enqueue_parser = subparsers.add_parser('enqueue')
    enqueue_parser.add_argument('--release-id', required=True)
    enqueue_parser.add_argument('--description', required=True)
    enqueue_parser.add_argument('--runner', type=Path, required=True)
    enqueue_parser.add_argument('--artifact', type=Path, action='append', default=[])
    enqueue_parser.add_argument('--required-consecutive-passes', type=int, default=2)
    enqueue_parser.add_argument('--failed-queue-id', default='')
    enqueue_parser.add_argument('--failure-diagnosis', default='')
    enqueue_parser.add_argument('--work-item-id', default='')
    enqueue_parser.add_argument('--priority-class', type=int, default=2)
    enqueue_parser.add_argument('--deadline-at-utc', default='')
    enqueue_parser.add_argument('--restart-policy', choices=('backend', 'none'), required=True)
    enqueue_parser.add_argument('--dependency-unit', action='append', default=[])
    enqueue_parser.add_argument('--blocking-unit', action='append', default=[])
    enqueue_parser.add_argument('--blocking-queue', action='append', default=[])
    enqueue_parser.add_argument('--required-resource', action='append', default=[])
    enqueue_parser.add_argument('--batch-id', default='')
    enqueue_parser.add_argument('--candidate-id', default='')
    enqueue_parser.add_argument('--max-production-attempts', type=int, default=2)
    enqueue_parser.add_argument(
        '--allow-venv-mutation', action='store_true',
        help='Explicitly allow an intentional production dependency change.',
    )
    supersede_parser = subparsers.add_parser('supersede')
    supersede_parser.add_argument('--queue-id', required=True)
    supersede_parser.add_argument('--replacement-release-id', required=True)
    supersede_parser.add_argument('--reason', required=True)
    freeze_parser = subparsers.add_parser('freeze')
    freeze_parser.add_argument('--prefix', required=True)
    freeze_parser.add_argument('--reason', required=True)
    unfreeze_parser = subparsers.add_parser('unfreeze')
    unfreeze_parser.add_argument('--prefix', required=True)
    admission_parser = subparsers.add_parser('admission')
    admission_parser.add_argument(
        '--scoped',
        action='store_true',
        help='Deprecated compatibility flag; admission is always explicitly scoped.',
    )
    admission_parser.add_argument('--dependency-unit', action='append', default=[])
    admission_parser.add_argument('--blocking-unit', action='append', default=[])
    admission_parser.add_argument('--blocking-queue', action='append', default=[])
    admission_parser.add_argument('--required-resource', action='append', default=[])
    admission_parser.add_argument('--restart-policy', choices=('backend', 'none'), required=True)
    subparsers.add_parser('run-once')
    subparsers.add_parser('list')
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == 'enqueue':
            result = enqueue(
                release_id=args.release_id, description=args.description,
                runner=args.runner.resolve(), artifacts=[path.resolve() for path in args.artifact],
                queue_root=args.queue_root.resolve(), required_passes=args.required_consecutive_passes,
                failed_queue_id=args.failed_queue_id, failure_diagnosis=args.failure_diagnosis,
                work_item_id=args.work_item_id, priority_class=args.priority_class,
                deadline_at_utc=args.deadline_at_utc, restart_policy=args.restart_policy,
                dependency_units=args.dependency_unit, blocking_units=args.blocking_unit,
                blocking_queues=args.blocking_queue, required_resources=args.required_resource,
                batch_id=args.batch_id, candidate_id=args.candidate_id,
                max_production_attempts=args.max_production_attempts,
                allow_venv_mutation=args.allow_venv_mutation,
            )
        elif args.command == 'supersede':
            result = supersede(
                queue_id=args.queue_id,
                replacement_release_id=args.replacement_release_id,
                reason=args.reason,
                queue_root=args.queue_root.resolve(),
            )
        elif args.command == 'freeze':
            result = freeze_release_prefix(
                prefix=args.prefix, reason=args.reason, queue_root=args.queue_root.resolve(),
            )
        elif args.command == 'unfreeze':
            result = unfreeze_release_prefix(prefix=args.prefix, queue_root=args.queue_root.resolve())
        elif args.command == 'admission':
            job = {
                'schema_version': 3,
                'dependency_units': args.dependency_unit,
                'blocking_units': args.blocking_unit,
                'blocking_queues': args.blocking_queue,
                'required_resources': args.required_resource,
                'restart_policy': args.restart_policy,
            }
            result = collect_admission(job)
        elif args.command == 'run-once':
            result = run_once(queue_root=args.queue_root.resolve())
        else:
            result = list_jobs(args.queue_root.resolve())
    except Exception as exc:
        result = {'ok': False, 'error': f'{type(exc).__name__}:{str(exc)[:300]}'}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get('ok') else 1


if __name__ == '__main__':
    raise SystemExit(main())
