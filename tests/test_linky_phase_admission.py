from __future__ import annotations

from pathlib import Path

from app.linky_phase_admission import (
    linky_compute_admission_command,
    linky_source_phase_soft_reasons,
)
from scripts import mcn_batch_runner


def test_linky_network_phase_ignores_only_sustained_cpu_pressure() -> None:
    assert linky_source_phase_soft_reasons(
        task_id='linky-daily-incremental',
        resource_claims=['network_fetch'],
        soft_reasons=['sustained_normalized_load', 'backend_latency'],
    ) == ['backend_latency']


def test_linky_compute_claim_does_not_ignore_cpu_pressure() -> None:
    assert linky_source_phase_soft_reasons(
        task_id='linky-daily-incremental',
        resource_claims=['heavy_compute'],
        soft_reasons=['sustained_normalized_load'],
    ) == ['sustained_normalized_load']


def test_other_tasks_do_not_ignore_cpu_pressure() -> None:
    assert linky_source_phase_soft_reasons(
        task_id='sugo-daily-incremental',
        resource_claims=['network_fetch'],
        soft_reasons=['sustained_normalized_load'],
    ) == ['sustained_normalized_load']


def test_linky_source_and_compute_phases_have_separate_load_floors() -> None:
    source_command = mcn_batch_runner._admission_command(
        mcn_batch_runner.JOBS['linky']
    )
    compute_command = linky_compute_admission_command(Path('/srv/mcn'))

    assert source_command[source_command.index('--max-load1') + 1] == '12.0'
    assert compute_command[compute_command.index('--max-load1') + 1] == '2.5'


def test_business_scheduler_bootstraps_repo_before_app_import() -> None:
    script = Path(__file__).resolve().parents[1] / 'scripts' / 'mcn_business_scheduler.py'
    source = script.read_text(encoding='utf-8')
    assert source.index('sys.path.insert(0, str(ROOT))') < source.index(
        'from app.linky_phase_admission import'
    )
