#!/usr/bin/env python3
from __future__ import annotations

import argparse
import difflib
import fcntl
import fnmatch
import getpass
import hashlib
import json
import os
import re
import shlex
import socket
import sqlite3
import stat
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote
from urllib.request import Request, urlopen


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_RECEIPT_DIR = Path('/var/lib/mcn-ai-automation/release-receipts')
SCHEMA_VERSION = 1
RELEASE_ID_PATTERN = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$')
SHA256_PATTERN = re.compile(r'^[0-9a-fA-F]{64}$')
PROTECTED_RELEASE_PATH_PATTERNS = (
    'app/main.py',
    'app/main_*.py',
    'app/production_ops.py',
    'scripts/mcn_*.py',
)
CHANGE_SCOPE_HARD_MAX_HUNKS = 20
CHANGE_SCOPE_HARD_MAX_CHANGED_LINES = 400
CHANGE_SCOPE_HARD_MAX_DELETED_LINES = 200
CHANGE_SCOPE_HARD_MAX_SHRINK_PERCENT = 3.0
STREAMER_ANALYTICS_GUARDED_FILES = {
    'app/streamer_analytics.py',
    'scripts/check_streamer_analytics_runtime_contract.py',
    'scripts/materialize_streamer_external_feed.py',
    'scripts/publish_streamer_analytics_candidate.py',
    'scripts/systemd/mcn-linky-external-feed.service',
    'scripts/systemd/mcn-sugo-external-feed.service',
    'scripts/systemd/mcn-streamer-analytics-publish.service',
}


class ReleaseGovernanceError(RuntimeError):
    pass


def _validate_streamer_analytics_contract_if_needed(
    root: Path,
    artifact_paths: list[str],
) -> dict[str, Any]:
    guarded = sorted(STREAMER_ANALYTICS_GUARDED_FILES.intersection(artifact_paths))
    if not guarded:
        return {'required': False, 'guarded_files': []}
    checker = root / 'scripts' / 'check_streamer_analytics_runtime_contract.py'
    if not checker.is_file():
        raise ReleaseGovernanceError(
            'streamer analytics release requires scripts/check_streamer_analytics_runtime_contract.py'
        )
    python = root / '.venv' / 'bin' / 'python'
    command = [str(python if python.is_file() else Path(sys.executable)), str(checker), '--root', str(root)]
    try:
        completed = subprocess.run(
            command,
            cwd=str(root),
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReleaseGovernanceError(f'streamer analytics contract guard unavailable: {exc}') from exc
    output = (completed.stdout or '').strip().splitlines()
    if completed.returncode != 0:
        detail = output[-1] if output else (completed.stderr or '').strip()[-1000:]
        raise ReleaseGovernanceError(f'streamer analytics contract guard failed: {detail}')
    try:
        result = json.loads(output[-1])
    except (IndexError, json.JSONDecodeError) as exc:
        raise ReleaseGovernanceError('streamer analytics contract guard output unreadable') from exc
    if result.get('ok') is not True:
        raise ReleaseGovernanceError(f'streamer analytics contract guard rejected: {result}')
    return {'required': True, 'guarded_files': guarded, 'result': result}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_copy(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False))


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _with_integrity(payload: dict[str, Any]) -> dict[str, Any]:
    result = _json_copy(payload)
    result['integrity'] = {
        'algorithm': 'sha256',
        'payload_sha256': _canonical_sha256(result),
    }
    return result


def _verify_integrity(payload: dict[str, Any], *, label: str) -> None:
    integrity = payload.get('integrity')
    if not isinstance(integrity, dict):
        raise ReleaseGovernanceError(f'{label} has no integrity block')
    expected = str(integrity.get('payload_sha256') or '').strip()
    unsigned = dict(payload)
    unsigned.pop('integrity', None)
    actual = _canonical_sha256(unsigned)
    if expected != actual:
        raise ReleaseGovernanceError(
            f'{label} integrity mismatch: expected={expected or "missing"} actual={actual}'
        )


def _write_json_atomic(path: Path, payload: dict[str, Any], *, overwrite: bool = False) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not overwrite:
        raise ReleaseGovernanceError(f'refusing to overwrite existing release record: {path}')
    temporary = path.with_name(f'.{path.name}.{os.getpid()}.{time.time_ns()}.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o640)
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        raise ReleaseGovernanceError(f'could not durably write release record {path}: {exc}') from exc
    finally:
        if temporary.exists():
            temporary.unlink()


def _prepare_receipt_directory(path: Path) -> Path:
    """Fail before a restart when its durable receipt cannot be written."""
    try:
        resolved = path.resolve()
        resolved.mkdir(parents=True, exist_ok=True)
        if not resolved.is_dir():
            raise OSError('receipt destination is not a directory')
        probe = resolved / f'.receipt-write-probe.{os.getpid()}.{time.time_ns()}'
        descriptor = os.open(probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(descriptor, b'mcn-release-receipt-write-probe\n')
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        probe.unlink()
        directory_fd = os.open(resolved, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        raise ReleaseGovernanceError(
            f'restart receipt directory is not writable: {path}: {exc}'
        ) from exc
    return resolved


def _regular_file_snapshot(path: Path, *, recorded_path: str) -> dict[str, Any]:
    try:
        resolved = path.resolve(strict=True)
        file_stat = resolved.stat()
    except FileNotFoundError as exc:
        raise ReleaseGovernanceError(f'release artifact does not exist: {path}') from exc
    if not stat.S_ISREG(file_stat.st_mode):
        raise ReleaseGovernanceError(f'release artifact is not a regular file: {resolved}')
    return {
        'path': recorded_path,
        'sha256': _sha256_file(resolved),
        'size_bytes': file_stat.st_size,
        'mode': stat.S_IMODE(file_stat.st_mode),
        'mtime_ns': file_stat.st_mtime_ns,
    }


def _repo_file_snapshot(root: Path, raw_path: str) -> dict[str, Any]:
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ReleaseGovernanceError('release file paths must be non-empty strings')
    root = root.resolve(strict=True)
    candidate = (root / raw_path).resolve(strict=True)
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise ReleaseGovernanceError(f'release file escapes repository root: {raw_path}') from exc
    return _regular_file_snapshot(candidate, recorded_path=relative.as_posix())


def _backup_artifact_snapshot(root: Path, raw_path: str) -> dict[str, Any]:
    declared_path = Path(raw_path)
    candidate = declared_path if declared_path.is_absolute() else root / declared_path
    try:
        file_stat = candidate.lstat()
    except FileNotFoundError as exc:
        raise ReleaseGovernanceError(f'backup artifact does not exist: {raw_path}') from exc
    if not stat.S_ISREG(file_stat.st_mode):
        raise ReleaseGovernanceError(f'backup artifact is not a regular file: {raw_path}')
    resolved = candidate.resolve(strict=True)
    if not declared_path.is_absolute():
        try:
            resolved.relative_to(root.resolve(strict=True))
        except ValueError as exc:
            raise ReleaseGovernanceError(
                f'relative backup artifact escapes repository root: {raw_path}'
            ) from exc
    snapshot = _regular_file_snapshot(resolved, recorded_path=raw_path)
    snapshot['resolved_path'] = str(resolved)
    return snapshot


def _resolve_repo_file(root: Path, raw_path: str) -> Path:
    snapshot = _repo_file_snapshot(root, raw_path)
    return (root.resolve(strict=True) / snapshot['path']).resolve(strict=True)


def _resolve_backup_artifact(root: Path, raw_path: str) -> Path:
    return Path(_backup_artifact_snapshot(root, raw_path)['resolved_path'])


def _snapshot_backup(backup: dict[str, Any], *, root: Path) -> dict[str, Any]:
    result = _json_copy(backup)
    for artifact in result['artifacts']:
        snapshot = _backup_artifact_snapshot(root, artifact['path'])
        declared_sha256 = artifact['sha256'].lower()
        if snapshot['sha256'] != declared_sha256:
            raise ReleaseGovernanceError(
                f'backup artifact SHA mismatch: {artifact["path"]}'
            )
        artifact['sha256'] = declared_sha256
        artifact['snapshot'] = snapshot
    return result


def _parse_systemctl_properties(output: str) -> dict[str, str]:
    properties: dict[str, str] = {}
    for line in output.splitlines():
        if '=' not in line:
            continue
        key, value = line.split('=', 1)
        properties[key] = value
    return properties


def snapshot_unit(unit: str, *, systemctl_binary: str = 'systemctl') -> dict[str, Any]:
    property_names = (
        'Id,LoadState,ActiveState,SubState,UnitFileState,FragmentPath,DropInPaths,'
        'InvocationID,ActiveEnterTimestamp,ActiveEnterTimestampMonotonic,MainPID,'
        'ExecMainStartTimestamp,NeedDaemonReload'
    )
    try:
        completed = subprocess.run(
            [
                systemctl_binary,
                'show',
                unit,
                '--no-pager',
                f'--property={property_names}',
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReleaseGovernanceError(f'could not inspect systemd unit {unit}: {exc}') from exc
    if completed.returncode != 0:
        error = completed.stderr.strip() or completed.stdout.strip() or 'systemctl show failed'
        raise ReleaseGovernanceError(f'could not inspect systemd unit {unit}: {error}')

    properties = _parse_systemctl_properties(completed.stdout)
    if properties.get('LoadState') != 'loaded':
        raise ReleaseGovernanceError(
            f'systemd unit is not loaded: {unit} ({properties.get("LoadState") or "unknown"})'
        )
    fragment_path = str(properties.get('FragmentPath') or '').strip()
    if not fragment_path:
        raise ReleaseGovernanceError(f'systemd unit has no FragmentPath: {unit}')

    fragment = _regular_file_snapshot(Path(fragment_path), recorded_path=fragment_path)
    drop_ins = [
        _regular_file_snapshot(Path(path), recorded_path=path)
        for path in shlex.split(str(properties.get('DropInPaths') or ''))
    ]
    config_payload = {
        'fragment': {
            key: fragment[key]
            for key in ('path', 'sha256', 'size_bytes', 'mode')
        },
        'drop_ins': [
            {
                key: drop_in[key]
                for key in ('path', 'sha256', 'size_bytes', 'mode')
            }
            for drop_in in drop_ins
        ],
    }
    return {
        'name': unit,
        'state': {
            key: properties.get(key, '')
            for key in (
                'Id',
                'LoadState',
                'ActiveState',
                'SubState',
                'UnitFileState',
                'InvocationID',
                'ActiveEnterTimestamp',
                'ActiveEnterTimestampMonotonic',
                'MainPID',
                'ExecMainStartTimestamp',
                'NeedDaemonReload',
            )
        },
        'fragment': fragment,
        'drop_ins': drop_ins,
        'config_fingerprint': _canonical_sha256(config_payload),
    }


def _stat_identity(path: Path) -> dict[str, Any]:
    file_stat = path.stat()
    return {
        'device': file_stat.st_dev,
        'inode': file_stat.st_ino,
        'size_bytes': file_stat.st_size,
        'mtime_ns': file_stat.st_mtime_ns,
    }


def snapshot_database(specification: dict[str, Any]) -> dict[str, Any]:
    name = str(specification.get('name') or '').strip()
    raw_path = str(specification.get('path') or '').strip()
    health_check = str(specification.get('health_check') or 'probe').strip()
    if not name or not raw_path:
        raise ReleaseGovernanceError('database entries require name and path')
    if health_check not in {'probe', 'quick_check'}:
        raise ReleaseGovernanceError(
            f'unsupported database health_check for {name}: {health_check}'
        )

    try:
        path = Path(raw_path).resolve(strict=True)
    except FileNotFoundError as exc:
        raise ReleaseGovernanceError(f'database does not exist: {raw_path}') from exc
    if not path.is_file():
        raise ReleaseGovernanceError(f'database is not a regular file: {path}')

    before = _stat_identity(path)
    uri = f'file:{quote(str(path))}?mode=ro'
    try:
        with sqlite3.connect(uri, uri=True, timeout=15.0) as connection:
            connection.execute('PRAGMA query_only=ON')
            probe_value = int(connection.execute('SELECT 1').fetchone()[0])
            user_version = int(connection.execute('PRAGMA user_version').fetchone()[0])
            schema_version = int(connection.execute('PRAGMA schema_version').fetchone()[0])
            journal_mode = str(connection.execute('PRAGMA journal_mode').fetchone()[0]).lower()
            page_count = int(connection.execute('PRAGMA page_count').fetchone()[0])
            page_size = int(connection.execute('PRAGMA page_size').fetchone()[0])
            quick_check = 'not_run'
            if health_check == 'quick_check':
                rows = connection.execute('PRAGMA quick_check').fetchall()
                quick_check = 'ok' if rows == [('ok',)] else '; '.join(str(row[0]) for row in rows[:10])
                if quick_check != 'ok':
                    raise ReleaseGovernanceError(
                        f'database quick_check failed for {name}: {quick_check}'
                    )
    except (sqlite3.Error, OSError) as exc:
        raise ReleaseGovernanceError(f'database health probe failed for {name}: {exc}') from exc

    after = _stat_identity(path)
    sidecars: dict[str, dict[str, Any]] = {}
    for suffix in ('-wal', '-shm', '-journal'):
        sidecar = Path(str(path) + suffix)
        if sidecar.exists():
            sidecars[suffix.removeprefix('-')] = _stat_identity(sidecar)
    generation_payload = {
        'database': after,
        'sidecars': sidecars,
        'user_version': user_version,
        'schema_version': schema_version,
    }
    return {
        'name': name,
        'path': str(path),
        'health_check': health_check,
        'health': {
            'status': 'ok',
            'readonly_probe': probe_value == 1,
            'quick_check': quick_check,
        },
        'sqlite': {
            'journal_mode': journal_mode,
            'user_version': user_version,
            'schema_version': schema_version,
            'page_count': page_count,
            'page_size': page_size,
        },
        'generation': {
            'kind': 'sqlite_filesystem_snapshot',
            'fingerprint': _canonical_sha256(generation_payload),
            'declared': specification.get('declared_generation'),
            'stable_during_probe': before == after,
            'database': after,
            'sidecars': sidecars,
        },
    }


def _require_non_empty_string(mapping: dict[str, Any], key: str, *, label: str) -> None:
    if not isinstance(mapping.get(key), str) or not str(mapping[key]).strip():
        raise ReleaseGovernanceError(f'{label}.{key} must be a non-empty string')


def _validate_release_id(value: Any) -> None:
    release_id = str(value or '').strip()
    if release_id and not RELEASE_ID_PATTERN.fullmatch(release_id):
        raise ReleaseGovernanceError(
            'release_id must use only letters, numbers, dot, underscore, and dash (max 128)'
        )


def _validate_verification_entries(entries: Any, *, label: str) -> list[dict[str, Any]]:
    if not isinstance(entries, list) or not entries:
        raise ReleaseGovernanceError(f'{label} must contain at least one entry')
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ReleaseGovernanceError(f'{label}[{index}] must be an object')
        _require_non_empty_string(entry, 'name', label=f'{label}[{index}]')
        _require_non_empty_string(entry, 'status', label=f'{label}[{index}]')
        _require_non_empty_string(entry, 'evidence', label=f'{label}[{index}]')
        if entry['status'] not in {'pending', 'passed', 'failed', 'skipped'}:
            raise ReleaseGovernanceError(
                f'{label}[{index}].status is unsupported: {entry["status"]}'
            )
    return entries


def _is_protected_release_path(path: str) -> bool:
    normalized = str(path).strip().lstrip('./')
    return any(fnmatch.fnmatchcase(normalized, pattern) for pattern in PROTECTED_RELEASE_PATH_PATTERNS)


def _change_scope_diff_snapshot(preimage: Path, current: Path) -> dict[str, Any]:
    try:
        before = preimage.read_text(encoding='utf-8').splitlines(keepends=True)
        after = current.read_text(encoding='utf-8').splitlines(keepends=True)
    except (OSError, UnicodeDecodeError) as exc:
        raise ReleaseGovernanceError(f'change scope input is not readable UTF-8 text: {exc}') from exc

    matcher = difflib.SequenceMatcher(a=before, b=after, autojunk=False)
    groups = list(matcher.get_grouped_opcodes(n=8))
    added_lines = 0
    deleted_lines = 0
    regions: list[dict[str, Any]] = []
    for group in groups:
        changed = [opcode for opcode in group if opcode[0] != 'equal']
        if not changed:
            continue
        old_start = min(item[1] for item in changed)
        old_end = max(item[2] for item in changed)
        new_start = min(item[3] for item in changed)
        new_end = max(item[4] for item in changed)
        deleted_lines += sum(item[2] - item[1] for item in changed if item[0] in {'delete', 'replace'})
        added_lines += sum(item[4] - item[3] for item in changed if item[0] in {'insert', 'replace'})
        context = ''.join(
            before[max(0, old_start - 8):min(len(before), old_end + 8)]
            + after[max(0, new_start - 8):min(len(after), new_end + 8)]
        )
        regions.append({
            'old_start': old_start + 1,
            'old_end': old_end,
            'new_start': new_start + 1,
            'new_end': new_end,
            'context': context,
        })

    normalized_diff = ''.join(difflib.unified_diff(
        before,
        after,
        fromfile='preimage',
        tofile='candidate',
        n=3,
        lineterm='\n',
    )).encode('utf-8')
    shrink_percent = 0.0
    if before and len(after) < len(before):
        shrink_percent = round((len(before) - len(after)) * 100.0 / len(before), 6)
    return {
        'preimage_sha256': _sha256_file(preimage),
        'candidate_sha256': _sha256_file(current),
        'diff_sha256': hashlib.sha256(normalized_diff).hexdigest(),
        'hunks': len(regions),
        'added_lines': added_lines,
        'deleted_lines': deleted_lines,
        'changed_lines': added_lines + deleted_lines,
        'shrink_percent': shrink_percent,
        'regions': regions,
    }


def _validate_change_scope_structure(plan: dict[str, Any]) -> None:
    protected = sorted({path for path in plan.get('files', []) if _is_protected_release_path(path)})
    release_id = str(plan.get('release_id') or '').strip()
    scope = plan.get('change_scope')
    if not protected:
        if scope not in (None, {}):
            if not isinstance(scope, dict):
                raise ReleaseGovernanceError('change_scope must be an object')
        return
    if release_id.endswith('-rollback') and scope in (None, {}):
        return
    if not isinstance(scope, dict) or scope.get('mode') != 'minimal_patch':
        raise ReleaseGovernanceError(
            'protected release files require change_scope.mode=minimal_patch'
        )
    entries = scope.get('files')
    if not isinstance(entries, list):
        raise ReleaseGovernanceError('change_scope.files must be a list')
    by_path: dict[str, dict[str, Any]] = {}
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ReleaseGovernanceError(f'change_scope.files[{index}] must be an object')
        _require_non_empty_string(entry, 'path', label=f'change_scope.files[{index}]')
        path = str(entry['path'])
        if path in by_path:
            raise ReleaseGovernanceError(f'duplicate change_scope entry: {path}')
        by_path[path] = entry
        for key in ('preimage_path', 'preimage_sha256', 'expected_diff_sha256'):
            _require_non_empty_string(entry, key, label=f'change_scope.files[{index}]')
        for key in ('preimage_sha256', 'expected_diff_sha256'):
            if not SHA256_PATTERN.fullmatch(str(entry[key])):
                raise ReleaseGovernanceError(f'change_scope.files[{index}].{key} must be sha256')
        markers = entry.get('allowed_regions')
        if not isinstance(markers, list) or not markers or any(
            not isinstance(marker, str) or len(marker.strip()) < 8 for marker in markers
        ):
            raise ReleaseGovernanceError(
                f'change_scope.files[{index}].allowed_regions must contain markers of at least 8 characters'
            )
        for key in (
            'expected_hunks', 'expected_changed_lines', 'expected_deleted_lines',
            'max_hunks', 'max_changed_lines',
        ):
            value = entry.get(key)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ReleaseGovernanceError(f'change_scope.files[{index}].{key} must be >= 0')
    if sorted(by_path) != protected:
        raise ReleaseGovernanceError(
            f'change_scope files must exactly match protected release files: {protected}'
        )


def _evaluate_change_scope(plan: dict[str, Any], *, root: Path) -> dict[str, Any]:
    _validate_change_scope_structure(plan)
    protected = sorted({path for path in plan.get('files', []) if _is_protected_release_path(path)})
    release_id = str(plan.get('release_id') or '').strip()
    if not protected or (release_id.endswith('-rollback') and not plan.get('change_scope')):
        return {'required': bool(protected), 'rollback_exemption': bool(protected), 'files': []}

    backup_paths = {
        str(item.get('path')): item
        for item in plan.get('backup', {}).get('artifacts', [])
        if isinstance(item, dict)
    }
    entries = {str(item['path']): item for item in plan['change_scope']['files']}
    evaluated = []
    for path in protected:
        entry = entries[path]
        preimage_path = str(entry['preimage_path'])
        backup = backup_paths.get(preimage_path)
        if backup is None:
            raise ReleaseGovernanceError(
                f'change scope preimage must be a declared backup artifact: {preimage_path}'
            )
        preimage = _resolve_backup_artifact(root, preimage_path)
        current = _resolve_repo_file(root, path)
        snapshot = _change_scope_diff_snapshot(preimage, current)
        if snapshot['preimage_sha256'] != str(entry['preimage_sha256']).lower():
            raise ReleaseGovernanceError(f'change scope preimage SHA mismatch: {path}')
        if snapshot['preimage_sha256'] != str(backup['sha256']).lower():
            raise ReleaseGovernanceError(f'change scope preimage does not match backup SHA: {path}')
        if snapshot['hunks'] == 0:
            raise ReleaseGovernanceError(f'change scope refuses no-op protected release: {path}')
        hard_limit_failures = []
        if snapshot['hunks'] > CHANGE_SCOPE_HARD_MAX_HUNKS:
            hard_limit_failures.append(f'hunks={snapshot["hunks"]}>{CHANGE_SCOPE_HARD_MAX_HUNKS}')
        if snapshot['changed_lines'] > CHANGE_SCOPE_HARD_MAX_CHANGED_LINES:
            hard_limit_failures.append(
                f'changed_lines={snapshot["changed_lines"]}>{CHANGE_SCOPE_HARD_MAX_CHANGED_LINES}'
            )
        if snapshot['deleted_lines'] > CHANGE_SCOPE_HARD_MAX_DELETED_LINES:
            hard_limit_failures.append(
                f'deleted_lines={snapshot["deleted_lines"]}>{CHANGE_SCOPE_HARD_MAX_DELETED_LINES}'
            )
        if snapshot['shrink_percent'] > CHANGE_SCOPE_HARD_MAX_SHRINK_PERCENT:
            hard_limit_failures.append(
                f'shrink_percent={snapshot["shrink_percent"]}>{CHANGE_SCOPE_HARD_MAX_SHRINK_PERCENT}'
            )
        if hard_limit_failures:
            raise ReleaseGovernanceError(
                f'change scope hard limit exceeded for {path}: {", ".join(hard_limit_failures)}'
            )
        if snapshot['hunks'] > entry['max_hunks']:
            raise ReleaseGovernanceError(f'change scope declared hunk limit exceeded: {path}')
        if snapshot['changed_lines'] > entry['max_changed_lines']:
            raise ReleaseGovernanceError(f'change scope declared line limit exceeded: {path}')
        expected = {
            'diff_sha256': str(entry['expected_diff_sha256']).lower(),
            'hunks': entry['expected_hunks'],
            'changed_lines': entry['expected_changed_lines'],
            'deleted_lines': entry['expected_deleted_lines'],
        }
        for key, value in expected.items():
            if snapshot[key] != value:
                raise ReleaseGovernanceError(
                    f'change scope expected {key} mismatch for {path}: expected={value} actual={snapshot[key]}'
                )
        markers = [str(marker) for marker in entry['allowed_regions']]
        before_text = preimage.read_text(encoding='utf-8')
        after_text = current.read_text(encoding='utf-8')
        for marker in markers:
            if max(before_text.count(marker), after_text.count(marker)) != 1:
                raise ReleaseGovernanceError(
                    f'change scope marker must identify exactly one region in {path}: {marker}'
                )
        for region in snapshot['regions']:
            if not any(marker in region['context'] for marker in markers):
                raise ReleaseGovernanceError(
                    f'change scope contains a hunk outside allowed regions for {path} '
                    f'at new line {region["new_start"]}'
                )
        evidence = dict(snapshot)
        evidence.pop('regions', None)
        evidence.update({'path': path, 'preimage_path': preimage_path, 'allowed_regions': markers})
        evaluated.append(evidence)
    return {'required': True, 'rollback_exemption': False, 'mode': 'minimal_patch', 'files': evaluated}


def validate_plan(plan: dict[str, Any], *, phase: str = 'create') -> None:
    if not isinstance(plan, dict):
        raise ReleaseGovernanceError('release plan must be a JSON object')
    _validate_release_id(plan.get('release_id'))
    change_source = plan.get('change_source')
    if not isinstance(change_source, dict):
        raise ReleaseGovernanceError('change_source must be an object')
    for key in ('kind', 'reference', 'base_revision'):
        _require_non_empty_string(change_source, key, label='change_source')

    files = plan.get('files')
    if not isinstance(files, list) or not files:
        raise ReleaseGovernanceError('files must contain at least one repository-relative path')
    if any(not isinstance(path, str) or not path.strip() for path in files):
        raise ReleaseGovernanceError('files must contain only non-empty strings')

    units = plan.get('units')
    if not isinstance(units, list) or not units:
        raise ReleaseGovernanceError('units must contain at least one systemd unit')
    if any(not isinstance(unit, str) or not unit.strip() for unit in units):
        raise ReleaseGovernanceError('units must contain only non-empty strings')

    databases = plan.get('databases')
    if not isinstance(databases, list) or not databases:
        raise ReleaseGovernanceError('databases must contain at least one database health specification')
    for index, database in enumerate(databases):
        if not isinstance(database, dict):
            raise ReleaseGovernanceError(f'databases[{index}] must be an object')
        _require_non_empty_string(database, 'name', label=f'databases[{index}]')
        _require_non_empty_string(database, 'path', label=f'databases[{index}]')

    backup = plan.get('backup')
    if not isinstance(backup, dict) or not isinstance(backup.get('required'), bool):
        raise ReleaseGovernanceError('backup.required must be true or false')
    _require_non_empty_string(backup, 'status', label='backup')
    if backup['status'] not in {'not_required', 'pending', 'verified', 'failed'}:
        raise ReleaseGovernanceError(f'unsupported backup.status: {backup["status"]}')
    artifacts = backup.get('artifacts')
    if not isinstance(artifacts, list):
        raise ReleaseGovernanceError('backup.artifacts must be a list')
    if backup['status'] == 'verified' and not artifacts:
        raise ReleaseGovernanceError('verified backup requires at least one artifact')
    for index, artifact in enumerate(artifacts):
        if not isinstance(artifact, dict):
            raise ReleaseGovernanceError(f'backup.artifacts[{index}] must be an object')
        for key in ('path', 'sha256', 'verification'):
            _require_non_empty_string(artifact, key, label=f'backup.artifacts[{index}]')
        if not SHA256_PATTERN.fullmatch(artifact['sha256']):
            raise ReleaseGovernanceError(
                f'backup.artifacts[{index}].sha256 must be a 64-character hexadecimal digest'
            )

    tests = _validate_verification_entries(plan.get('tests'), label='tests')
    _validate_verification_entries(plan.get('smokes'), label='smokes')
    rollback = plan.get('rollback')
    if not isinstance(rollback, dict):
        raise ReleaseGovernanceError('rollback must be an object')
    for key in ('status', 'strategy'):
        _require_non_empty_string(rollback, key, label='rollback')

    _validate_change_scope_structure(plan)

    if phase == 'restart':
        if backup['required'] and backup['status'] != 'verified':
            raise ReleaseGovernanceError('restart refused: required backup is not verified')
        if any(entry['status'] != 'passed' for entry in tests):
            raise ReleaseGovernanceError('restart refused: all declared tests must have status=passed')
        if rollback['status'] != 'ready':
            raise ReleaseGovernanceError('restart refused: rollback.status must be ready')


def create_manifest(
    plan: dict[str, Any],
    *,
    root: Path = ROOT_DIR,
    systemctl_binary: str = 'systemctl',
) -> dict[str, Any]:
    validate_plan(plan, phase='create')
    root = root.resolve(strict=True)
    plan_hash = _canonical_sha256(plan)
    release_id = str(plan.get('release_id') or '').strip()
    if not release_id:
        release_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + plan_hash[:12]
    effective_plan = dict(plan)
    effective_plan['release_id'] = release_id
    change_scope = _evaluate_change_scope(effective_plan, root=root)

    payload = {
        'schema_version': SCHEMA_VERSION,
        'record_type': 'mcn_release_manifest',
        'release_id': release_id,
        'created_at_utc': _utc_now(),
        'environment': {
            'host': socket.gethostname(),
            'user': getpass.getuser(),
            'repository_root': str(root),
        },
        'change_source': _json_copy(plan['change_source']),
        'plan_sha256': plan_hash,
        'artifacts': {
            'files': [_repo_file_snapshot(root, path) for path in plan['files']],
        },
        'systemd': {
            'units': [
                snapshot_unit(unit, systemctl_binary=systemctl_binary)
                for unit in plan['units']
            ],
        },
        'databases': [snapshot_database(specification) for specification in plan['databases']],
        'backup': _snapshot_backup(plan['backup'], root=root),
        'verification': {
            'tests': _json_copy(plan['tests']),
            'smokes': _json_copy(plan['smokes']),
        },
        'rollback': _json_copy(plan['rollback']),
        'change_scope': change_scope,
    }
    return _with_integrity(payload)


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseGovernanceError(f'could not read release manifest {path}: {exc}') from exc
    if not isinstance(payload, dict):
        raise ReleaseGovernanceError('release manifest must be a JSON object')
    _verify_integrity(payload, label='release manifest')
    if payload.get('schema_version') != SCHEMA_VERSION:
        raise ReleaseGovernanceError(
            f'unsupported release manifest schema: {payload.get("schema_version")}'
        )
    if payload.get('record_type') != 'mcn_release_manifest':
        raise ReleaseGovernanceError('unexpected release manifest record_type')
    _validate_release_id(payload.get('release_id'))
    if not payload.get('release_id'):
        raise ReleaseGovernanceError('release manifest has no release_id')
    return payload


def _plan_view_from_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        'release_id': manifest.get('release_id'),
        'change_source': manifest.get('change_source'),
        'files': [item.get('path') for item in manifest.get('artifacts', {}).get('files', [])],
        'units': [item.get('name') for item in manifest.get('systemd', {}).get('units', [])],
        'databases': [
            {
                'name': item.get('name'),
                'path': item.get('path'),
                'health_check': item.get('health_check'),
            }
            for item in manifest.get('databases', [])
        ],
        'backup': manifest.get('backup'),
        'tests': manifest.get('verification', {}).get('tests'),
        'smokes': manifest.get('verification', {}).get('smokes'),
        'rollback': manifest.get('rollback'),
        'change_scope': {
            'mode': manifest.get('change_scope', {}).get('mode'),
            'files': [
                {
                    'path': item.get('path'),
                    'preimage_path': item.get('preimage_path'),
                    'preimage_sha256': item.get('preimage_sha256'),
                    'expected_diff_sha256': item.get('diff_sha256'),
                    'expected_hunks': item.get('hunks'),
                    'expected_changed_lines': item.get('changed_lines'),
                    'expected_deleted_lines': item.get('deleted_lines'),
                    'max_hunks': item.get('hunks'),
                    'max_changed_lines': item.get('changed_lines'),
                    'allowed_regions': item.get('allowed_regions'),
                }
                for item in manifest.get('change_scope', {}).get('files', [])
            ],
        } if manifest.get('change_scope', {}).get('files') else None,
    }


def validate_manifest(
    manifest: dict[str, Any],
    *,
    phase: str = 'restart',
    systemctl_binary: str = 'systemctl',
) -> dict[str, Any]:
    _verify_integrity(manifest, label='release manifest')
    validate_plan(_plan_view_from_manifest(manifest), phase=phase)
    root = Path(str(manifest.get('environment', {}).get('repository_root') or '')).resolve(strict=True)
    change_scope = _evaluate_change_scope(_plan_view_from_manifest(manifest), root=root)
    if change_scope != manifest.get('change_scope'):
        raise ReleaseGovernanceError('change scope evidence drift')

    file_checks = []
    for expected in manifest['artifacts']['files']:
        current = _repo_file_snapshot(root, expected['path'])
        matches = (
            current['sha256'] == expected['sha256']
            and current['mode'] == expected['mode']
        )
        file_checks.append({'path': expected['path'], 'matches': matches})
        if not matches:
            raise ReleaseGovernanceError(f'artifact hash/mode drift: {expected["path"]}')

    analytics_contract = _validate_streamer_analytics_contract_if_needed(
        root,
        [str(item['path']) for item in manifest['artifacts']['files']],
    )

    backup_checks = []
    for expected in manifest['backup']['artifacts']:
        recorded = expected.get('snapshot')
        if not isinstance(recorded, dict):
            raise ReleaseGovernanceError(
                f'backup artifact snapshot is missing: {expected["path"]}'
            )
        current = _backup_artifact_snapshot(root, expected['path'])
        matches = (
            current['resolved_path'] == recorded.get('resolved_path')
            and current['sha256'] == expected['sha256']
            and current['sha256'] == recorded.get('sha256')
            and current['mode'] == recorded.get('mode')
        )
        backup_checks.append({'path': expected['path'], 'matches': matches})
        if not matches:
            raise ReleaseGovernanceError(f'backup artifact drift: {expected["path"]}')

    unit_checks = []
    for expected in manifest['systemd']['units']:
        current = snapshot_unit(expected['name'], systemctl_binary=systemctl_binary)
        if current['state']['NeedDaemonReload'] == 'yes':
            raise ReleaseGovernanceError(
                f'systemd daemon-reload required before restart: {expected["name"]}'
            )
        matches = current['config_fingerprint'] == expected['config_fingerprint']
        unit_checks.append({'name': expected['name'], 'config_matches': matches})
        if not matches:
            raise ReleaseGovernanceError(f'systemd unit/drop-in drift: {expected["name"]}')

    database_checks = []
    for expected in manifest['databases']:
        current = snapshot_database({
            'name': expected['name'],
            'path': expected['path'],
            'health_check': 'probe',
        })
        database_checks.append({
            'name': expected['name'],
            'status': current['health']['status'],
            'generation_fingerprint': current['generation']['fingerprint'],
        })

    return {
        'ok': True,
        'phase': phase,
        'release_id': manifest['release_id'],
        'files': file_checks,
        'change_scope': change_scope,
        'streamer_analytics_contract': analytics_contract,
        'backup_artifacts': backup_checks,
        'units': unit_checks,
        'databases': database_checks,
    }


def _deploy_lock_is_held() -> bool:
    if os.environ.get('MCN_DEPLOY_LOCK_ACTIVE') != '1':
        return False
    if not sys.platform.startswith('linux'):
        return True
    descriptor = str(os.environ.get('MCN_DEPLOY_LOCK_FD') or '')
    lock_path = str(os.environ.get('MCN_DEPLOY_LOCK_PATH') or '')
    if not descriptor.isdigit() or not lock_path:
        return False
    try:
        file_descriptor = int(descriptor)
        actual = Path(f'/proc/self/fd/{descriptor}').resolve(strict=True)
        expected = Path(lock_path).resolve(strict=True)
        descriptor_stat = os.fstat(file_descriptor)
        path_stat = expected.stat()
        if (descriptor_stat.st_dev, descriptor_stat.st_ino) != (path_stat.st_dev, path_stat.st_ino):
            return False
        fcntl.flock(file_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    except (OSError, RuntimeError):
        return False
    return actual == expected


def _deploy_lock_pass_fds() -> tuple[int, ...]:
    if not sys.platform.startswith('linux'):
        return ()
    descriptor = str(os.environ.get('MCN_DEPLOY_LOCK_FD') or '')
    if not descriptor.isdigit():
        return ()
    file_descriptor = int(descriptor)
    try:
        os.fstat(file_descriptor)
    except OSError:
        return ()
    return (file_descriptor,)


def _receipt_unit_view(snapshot: dict[str, Any] | None) -> dict[str, Any] | None:
    if snapshot is None:
        return None
    return {
        'name': snapshot['name'],
        'state': snapshot['state'],
        'config_fingerprint': snapshot['config_fingerprint'],
    }


def _wait_for_active_unit(
    unit: str,
    *,
    systemctl_binary: str,
    deadline: float,
) -> dict[str, Any]:
    last_snapshot: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        last_snapshot = snapshot_unit(unit, systemctl_binary=systemctl_binary)
        if last_snapshot['state']['ActiveState'] == 'active':
            return last_snapshot
        time.sleep(1)
    state = (last_snapshot or {}).get('state', {}).get('ActiveState', 'unknown')
    raise ReleaseGovernanceError(f'{unit} did not become active before timeout (state={state})')


def _wait_for_http(url: str, *, deadline: float) -> dict[str, Any]:
    last_error = 'not attempted'
    while time.monotonic() < deadline:
        try:
            request = Request(url, headers={'User-Agent': 'mcn-release-governance/1'})
            with urlopen(request, timeout=3) as response:
                status = int(getattr(response, 'status', 200))
                response.read(1024)
            if 200 <= status < 400:
                return {'kind': 'http', 'target': url, 'status': 'passed', 'http_status': status}
            last_error = f'HTTP {status}'
        except Exception as exc:  # Network stacks expose several exception types.
            last_error = str(exc)
        time.sleep(1)
    raise ReleaseGovernanceError(f'HTTP smoke failed for {url}: {last_error}')


def controlled_restart(
    manifest_path: Path,
    *,
    unit: str,
    command: list[str],
    receipt_dir: Path = DEFAULT_RECEIPT_DIR,
    health_urls: list[str] | None = None,
    timeout_seconds: float = 60.0,
    systemctl_binary: str = 'systemctl',
) -> dict[str, Any]:
    if not _deploy_lock_is_held():
        raise ReleaseGovernanceError(
            'controlled restart requires scripts/mcn_deploy_lock.sh; direct invocation refused'
        )
    if not command:
        raise ReleaseGovernanceError('controlled restart requires a restart command after --')

    manifest_path = manifest_path.resolve(strict=True)
    manifest = load_manifest(manifest_path)
    validation = validate_manifest(
        manifest,
        phase='restart',
        systemctl_binary=systemctl_binary,
    )
    declared_units = {item['name'] for item in manifest['systemd']['units']}
    if unit not in declared_units:
        raise ReleaseGovernanceError(f'restart unit is not declared in manifest: {unit}')

    # A restart without its receipt is indistinguishable from an unattributed
    # direct systemctl operation. Prove the destination is durable before any
    # traffic-affecting command is executed.
    receipt_dir = _prepare_receipt_directory(receipt_dir)

    started_at = _utc_now()
    before = snapshot_unit(unit, systemctl_binary=systemctl_binary)
    after: dict[str, Any] | None = None
    smokes: list[dict[str, Any]] = []
    command_result: dict[str, Any] = {'returncode': None, 'timed_out': False}
    error: str | None = None
    command_fingerprint = _canonical_sha256(command)
    receipt_id = f'{manifest["release_id"]}-{time.time_ns()}'
    receipt_path = receipt_dir / f'{receipt_id}.json'
    intent_payload = {
        'schema_version': SCHEMA_VERSION,
        'record_type': 'mcn_controlled_restart_receipt',
        'receipt_id': receipt_id,
        'receipt_path': str(receipt_path),
        'release_id': manifest['release_id'],
        'manifest': {
            'path': str(manifest_path),
            'payload_sha256': manifest['integrity']['payload_sha256'],
        },
        'unit': unit,
        'started_at_utc': started_at,
        'finished_at_utc': None,
        'status': 'started',
        'error': None,
        'validation': validation,
        'before': _receipt_unit_view(before),
        'after': None,
        'command': {
            'executable': command[0],
            'argument_count': max(len(command) - 1, 0),
            'argv_sha256': command_fingerprint,
            'result': command_result,
        },
        'smokes': [],
    }
    _write_json_atomic(receipt_path, _with_integrity(intent_payload))
    deadline = time.monotonic() + max(timeout_seconds, 1.0)
    try:
        remaining = max(deadline - time.monotonic(), 1.0)
        completed = subprocess.run(
            command,
            cwd=manifest['environment']['repository_root'],
            check=False,
            pass_fds=_deploy_lock_pass_fds(),
            timeout=remaining,
        )
        command_result['returncode'] = completed.returncode
        if completed.returncode != 0:
            raise ReleaseGovernanceError(
                f'restart command exited with status {completed.returncode}'
            )
        after = _wait_for_active_unit(
            unit,
            systemctl_binary=systemctl_binary,
            deadline=deadline,
        )
        before_invocation = before['state']['InvocationID']
        after_invocation = after['state']['InvocationID']
        if not after_invocation or after_invocation == before_invocation:
            raise ReleaseGovernanceError(
                f'{unit} InvocationID did not change; restart attribution cannot be proven'
            )
        smokes.append({
            'kind': 'systemd',
            'target': unit,
            'status': 'passed',
            'active_state': after['state']['ActiveState'],
            'sub_state': after['state']['SubState'],
        })
        for url in health_urls or []:
            try:
                smokes.append(_wait_for_http(url, deadline=deadline))
            except ReleaseGovernanceError as exc:
                smokes.append({
                    'kind': 'http',
                    'target': url,
                    'status': 'failed',
                    'error': str(exc),
                })
                raise
    except subprocess.TimeoutExpired:
        command_result['timed_out'] = True
        error = f'restart command timed out after {timeout_seconds:.1f}s'
    except OSError as exc:
        error = f'could not execute restart command: {exc}'
    except ReleaseGovernanceError as exc:
        error = str(exc)
    finally:
        if after is None:
            try:
                after = snapshot_unit(unit, systemctl_binary=systemctl_binary)
            except ReleaseGovernanceError:
                after = None

    status_value = 'passed' if error is None else 'failed'
    receipt_payload = {
        'schema_version': SCHEMA_VERSION,
        'record_type': 'mcn_controlled_restart_receipt',
        'receipt_id': receipt_id,
        'receipt_path': str(receipt_path),
        'release_id': manifest['release_id'],
        'manifest': {
            'path': str(manifest_path),
            'payload_sha256': manifest['integrity']['payload_sha256'],
        },
        'unit': unit,
        'started_at_utc': started_at,
        'finished_at_utc': _utc_now(),
        'status': status_value,
        'error': error,
        'validation': validation,
        'before': _receipt_unit_view(before),
        'after': _receipt_unit_view(after),
        'command': {
            'executable': command[0],
            'argument_count': max(len(command) - 1, 0),
            'argv_sha256': command_fingerprint,
            'result': command_result,
        },
        'smokes': smokes,
    }
    receipt = _with_integrity(receipt_payload)
    _write_json_atomic(receipt_path, receipt, overwrite=True)
    return receipt


def audit_restart_attribution(
    unit: str,
    *,
    receipt_dir: Path = DEFAULT_RECEIPT_DIR,
    systemctl_binary: str = 'systemctl',
) -> dict[str, Any]:
    current = snapshot_unit(unit, systemctl_binary=systemctl_binary)
    current_invocation = current['state']['InvocationID']
    matching_receipt: Path | None = None
    matching_status: str | None = None
    incomplete_intent: tuple[Path, dict[str, Any]] | None = None
    invalid_receipts: list[str] = []
    if receipt_dir.exists():
        receipt_paths = sorted(
            receipt_dir.glob('*.json'),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )
        for path in receipt_paths:
            try:
                receipt = _load_json_object(path, label='restart receipt')
                _verify_integrity(receipt, label=f'restart receipt {path.name}')
            except ReleaseGovernanceError:
                invalid_receipts.append(path.name)
                continue
            if receipt.get('record_type') != 'mcn_controlled_restart_receipt':
                continue
            if receipt.get('unit') != unit:
                continue
            if receipt.get('status') == 'started' and incomplete_intent is None:
                incomplete_intent = (path, receipt)
            receipt_invocation = (
                (receipt.get('after') or {}).get('state') or {}
            ).get('InvocationID')
            if receipt_invocation and receipt_invocation == current_invocation:
                matching_receipt = path
                matching_status = str(receipt.get('status') or '')
                break

    if matching_receipt is None and incomplete_intent is not None:
        matching_receipt, intent = incomplete_intent
        matching_status = 'started'
        before_invocation = (
            (intent.get('before') or {}).get('state') or {}
        ).get('InvocationID')
        classification = (
            'controlled_restart_incomplete'
            if current_invocation and current_invocation != before_invocation
            else 'controlled_restart_intent_pending'
        )
    else:
        classification = ''

    attributed = matching_receipt is not None and matching_status == 'passed'
    if attributed:
        classification = 'attributed_controlled_restart'
    elif matching_receipt is not None:
        if matching_status == 'failed':
            classification = 'controlled_restart_failed'
    else:
        classification = 'unattributed_restart'
    return {
        'ok': attributed,
        'unit': unit,
        'classification': classification,
        'current_invocation_id': current_invocation,
        'current_active_enter_timestamp': current['state']['ActiveEnterTimestamp'],
        'matching_receipt': str(matching_receipt) if matching_receipt else None,
        'matching_receipt_status': matching_status,
        'invalid_receipts': invalid_receipts,
    }


def record_deployment_receipt(
    manifest_path: Path,
    *,
    unit: str,
    expected_invocation_id: str,
    receipt_dir: Path = DEFAULT_RECEIPT_DIR,
    systemctl_binary: str = 'systemctl',
) -> dict[str, Any]:
    """Record an immutable governed deployment that intentionally did not restart.

    This receipt type can never satisfy ``audit-restart``.  It proves only that
    the manifest stayed valid and the backend InvocationID stayed unchanged.
    """
    if not _deploy_lock_is_held():
        raise ReleaseGovernanceError('deployment receipt requires the governed deploy lock')
    manifest = load_manifest(manifest_path)
    validation = validate_manifest(manifest, phase='restart', systemctl_binary=systemctl_binary)
    current = snapshot_unit(unit, systemctl_binary=systemctl_binary)
    current_invocation = str(current['state'].get('InvocationID') or '')
    expected = str(expected_invocation_id or '').strip()
    if not expected or current_invocation != expected:
        raise ReleaseGovernanceError(
            f'no-restart deployment invocation drift: expected={expected or "missing"} current={current_invocation or "missing"}'
        )
    destination = _prepare_receipt_directory(receipt_dir)
    receipt_id = f'{manifest["release_id"]}-deploy-{time.time_ns()}'
    receipt_path = destination / f'{receipt_id}.json'
    payload = _with_integrity({
        'schema_version': SCHEMA_VERSION,
        'record_type': 'mcn_governed_deployment_receipt',
        'receipt_kind': 'deployment_no_restart',
        'receipt_id': receipt_id,
        'receipt_path': str(receipt_path),
        'release_id': manifest['release_id'],
        'manifest_path': str(manifest_path.resolve()),
        'manifest_payload_sha256': manifest['integrity']['payload_sha256'],
        'unit': unit,
        'status': 'passed',
        'created_at_utc': _utc_now(),
        'invocation_id': current_invocation,
        'validation': validation,
    })
    _write_json_atomic(receipt_path, payload)
    return payload


def audit_deployment_receipt(
    release_id: str,
    *,
    unit: str,
    expected_invocation_id: str,
    receipt_dir: Path = DEFAULT_RECEIPT_DIR,
    systemctl_binary: str = 'systemctl',
) -> dict[str, Any]:
    current = snapshot_unit(unit, systemctl_binary=systemctl_binary)
    current_invocation = str(current['state'].get('InvocationID') or '')
    expected = str(expected_invocation_id or '').strip()
    matching: Path | None = None
    invalid: list[str] = []
    if receipt_dir.exists():
        receipt_paths = sorted(
            receipt_dir.glob('*.json'),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )
        for path in receipt_paths:
            try:
                receipt = _load_json_object(path, label='deployment receipt')
                _verify_integrity(receipt, label=f'deployment receipt {path.name}')
            except ReleaseGovernanceError:
                invalid.append(path.name)
                continue
            if (
                receipt.get('record_type') == 'mcn_governed_deployment_receipt'
                and receipt.get('receipt_kind') == 'deployment_no_restart'
                and receipt.get('status') == 'passed'
                and receipt.get('release_id') == release_id
                and receipt.get('unit') == unit
                and receipt.get('invocation_id') == expected
            ):
                matching = path
                break
    ok = bool(matching and expected and current_invocation == expected)
    return {
        'ok': ok,
        'classification': 'attributed_governed_deployment_no_restart' if ok else 'deployment_receipt_missing_or_invocation_drift',
        'release_id': release_id,
        'unit': unit,
        'expected_invocation_id': expected,
        'current_invocation_id': current_invocation,
        'matching_receipt': str(matching) if matching else None,
        'matching_receipt_status': 'passed' if matching else None,
        'invalid_receipts': invalid,
    }


def _load_json_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseGovernanceError(f'could not read {label} {path}: {exc}') from exc
    if not isinstance(value, dict):
        raise ReleaseGovernanceError(f'{label} must be a JSON object')
    return value


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='Generate and enforce auditable MCN production release records.'
    )
    subparsers = parser.add_subparsers(dest='command_name', required=True)

    create_parser = subparsers.add_parser('create', help='create an immutable release manifest')
    create_parser.add_argument('--plan', type=Path, required=True)
    create_parser.add_argument('--root', type=Path, default=ROOT_DIR)
    create_parser.add_argument('--output', type=Path, required=True)
    create_parser.add_argument('--systemctl', default='systemctl')

    validate_parser = subparsers.add_parser('validate', help='validate a release manifest')
    validate_parser.add_argument('--manifest', type=Path, required=True)
    validate_parser.add_argument('--phase', choices=('structure', 'restart'), default='restart')
    validate_parser.add_argument('--systemctl', default='systemctl')

    restart_parser = subparsers.add_parser(
        'controlled-restart',
        help='validate, restart, smoke, and write an attribution receipt',
    )
    restart_parser.add_argument('--manifest', type=Path, required=True)
    restart_parser.add_argument('--unit', required=True)
    restart_parser.add_argument('--receipt-dir', type=Path, default=DEFAULT_RECEIPT_DIR)
    restart_parser.add_argument('--health-url', action='append', default=[])
    restart_parser.add_argument('--timeout-seconds', type=float, default=60.0)
    restart_parser.add_argument('--systemctl', default='systemctl')
    restart_parser.add_argument('restart_command', nargs=argparse.REMAINDER)

    audit_parser = subparsers.add_parser(
        'audit-restart',
        help='check whether the current systemd invocation has a controlled receipt',
    )
    audit_parser.add_argument('--unit', required=True)
    audit_parser.add_argument('--receipt-dir', type=Path, default=DEFAULT_RECEIPT_DIR)
    audit_parser.add_argument('--systemctl', default='systemctl')
    deploy_receipt_parser = subparsers.add_parser(
        'record-deployment', help='record a governed deployment that intentionally did not restart',
    )
    deploy_receipt_parser.add_argument('--manifest', type=Path, required=True)
    deploy_receipt_parser.add_argument('--unit', required=True)
    deploy_receipt_parser.add_argument('--expected-invocation-id', required=True)
    deploy_receipt_parser.add_argument('--receipt-dir', type=Path, default=DEFAULT_RECEIPT_DIR)
    deploy_receipt_parser.add_argument('--systemctl', default='systemctl')
    audit_deploy_parser = subparsers.add_parser(
        'audit-deployment', help='audit a no-restart governed deployment receipt',
    )
    audit_deploy_parser.add_argument('--release-id', required=True)
    audit_deploy_parser.add_argument('--unit', required=True)
    audit_deploy_parser.add_argument('--expected-invocation-id', required=True)
    audit_deploy_parser.add_argument('--receipt-dir', type=Path, default=DEFAULT_RECEIPT_DIR)
    audit_deploy_parser.add_argument('--systemctl', default='systemctl')
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command_name == 'create':
            plan = _load_json_object(args.plan, label='release plan')
            manifest = create_manifest(
                plan,
                root=args.root,
                systemctl_binary=args.systemctl,
            )
            _write_json_atomic(args.output, manifest)
            result: dict[str, Any] = {
                'ok': True,
                'release_id': manifest['release_id'],
                'manifest_path': str(args.output.resolve()),
                'payload_sha256': manifest['integrity']['payload_sha256'],
            }
            exit_code = 0
        elif args.command_name == 'validate':
            manifest = load_manifest(args.manifest)
            if args.phase == 'structure':
                result = {
                    'ok': True,
                    'phase': 'structure',
                    'release_id': manifest['release_id'],
                }
            else:
                result = validate_manifest(
                    manifest,
                    phase=args.phase,
                    systemctl_binary=args.systemctl,
                )
            exit_code = 0
        elif args.command_name == 'controlled-restart':
            command = list(args.restart_command)
            if command and command[0] == '--':
                command = command[1:]
            receipt = controlled_restart(
                args.manifest,
                unit=args.unit,
                command=command,
                receipt_dir=args.receipt_dir,
                health_urls=args.health_url,
                timeout_seconds=args.timeout_seconds,
                systemctl_binary=args.systemctl,
            )
            result = receipt
            exit_code = 0 if receipt['status'] == 'passed' else 1
        elif args.command_name == 'audit-restart':
            result = audit_restart_attribution(
                args.unit,
                receipt_dir=args.receipt_dir,
                systemctl_binary=args.systemctl,
            )
            exit_code = 0 if result['ok'] else 3
        elif args.command_name == 'record-deployment':
            result = record_deployment_receipt(
                args.manifest, unit=args.unit, expected_invocation_id=args.expected_invocation_id,
                receipt_dir=args.receipt_dir, systemctl_binary=args.systemctl,
            )
            exit_code = 0
        else:
            result = audit_deployment_receipt(
                args.release_id, unit=args.unit, expected_invocation_id=args.expected_invocation_id,
                receipt_dir=args.receipt_dir, systemctl_binary=args.systemctl,
            )
            exit_code = 0 if result['ok'] else 3
    except ReleaseGovernanceError as exc:
        result = {'ok': False, 'error': str(exc)}
        exit_code = 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return exit_code


if __name__ == '__main__':
    raise SystemExit(main())
