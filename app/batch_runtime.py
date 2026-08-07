from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


PRODUCTION_PROJECT_ROOT = Path('/opt/mcn-ai-automation')


def current_cgroup_text(path: Path = Path('/proc/self/cgroup')) -> str:
    try:
        return path.read_text(encoding='utf-8')
    except OSError as exc:
        raise RuntimeError('batch_runtime_cgroup_unreadable') from exc


def cgroup_contains_slice(cgroup_text: str, slice_name: str) -> bool:
    expected = f'/{str(slice_name).strip().strip("/")}/'
    return bool(expected != '//' and expected in str(cgroup_text or ''))


def assert_managed_batch_runtime(
    job_name: str,
    *,
    project_root: Optional[Path] = None,
    required_slice: str = '',
    cgroup_text: Optional[str] = None,
) -> None:
    resolved_root = (project_root or Path(__file__).resolve().parents[1]).resolve()
    if resolved_root != PRODUCTION_PROJECT_ROOT:
        return
    if os.getenv('MCN_BATCH_LAUNCHER_ACTIVE') != '1':
        raise RuntimeError(f'{job_name}_requires_mcn_batch_launcher')
    if required_slice:
        observed = current_cgroup_text() if cgroup_text is None else cgroup_text
        if not cgroup_contains_slice(observed, required_slice):
            raise RuntimeError(f'{job_name}_requires_{required_slice.replace(".", "_")}')
