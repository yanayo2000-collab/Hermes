from __future__ import annotations

import json
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_parse_vm_stat_output_extracts_core_fields():
    from scripts.memory_pressure_relief import parse_vm_stat_output

    parsed = parse_vm_stat_output(
        """
Mach Virtual Memory Statistics: (page size of 4096 bytes)
Pages free:                               2509.
Pages active:                           339741.
Pages inactive:                         185581.
Pages speculative:                       14220.
Pages occupied by compressor:           172089.
Pages stored in compressor:             602715.
""".strip()
    )

    assert parsed['page_size_bytes'] == 4096
    assert parsed['pages_free'] == 2509
    assert parsed['pages_speculative'] == 14220
    assert parsed['pages_occupied_by_compressor'] == 172089
    assert parsed['pages_stored_in_compressor'] == 602715



def test_evaluate_memory_pressure_triggers_when_free_memory_is_low():
    from scripts.memory_pressure_relief import evaluate_memory_pressure

    pressure = evaluate_memory_pressure(
        {
            'page_size_bytes': 4096,
            'pages_free': 2509,
            'pages_speculative': 14220,
            'pages_occupied_by_compressor': 172089,
            'pages_stored_in_compressor': 602715,
        },
        free_mb_threshold=512.0,
        compressor_mb_threshold=512.0,
    )

    assert pressure['triggered'] is True
    assert 'free_mb_below_threshold' in pressure['reasons']
    assert pressure['free_mb'] < 512.0



def test_evaluate_memory_pressure_skips_when_memory_is_healthy():
    from scripts.memory_pressure_relief import evaluate_memory_pressure

    pressure = evaluate_memory_pressure(
        {
            'page_size_bytes': 4096,
            'pages_free': 400000,
            'pages_speculative': 50000,
            'pages_occupied_by_compressor': 40000,
            'pages_stored_in_compressor': 50000,
        },
        free_mb_threshold=512.0,
        compressor_mb_threshold=2048.0,
    )

    assert pressure['triggered'] is False
    assert pressure['reasons'] == []
    assert pressure['free_mb'] > 512.0



def test_run_guard_applies_webjs_cleanup_only_when_pressure_triggered(tmp_path):
    from scripts.memory_pressure_relief import run_guard

    temp_root = tmp_path / 'T'
    temp_root.mkdir()
    stale_temp = temp_root / 'webjs-fresh-state-stale'
    stale_temp.mkdir()
    stale_temp.joinpath('payload.bin').write_bytes(b'x' * 16)

    fixture = {
        'vm_stat_output': """
Mach Virtual Memory Statistics: (page size of 4096 bytes)
Pages free:                               2509.
Pages speculative:                       14220.
Pages occupied by compressor:           172089.
Pages stored in compressor:             602715.
""".strip(),
        'temp_root': str(temp_root),
        'protected_pids': [8011],
        'ps_rows': [
            {
                'pid': 111,
                'ppid': 1,
                'command': f'/Applications/Google Chrome --user-data-dir={stale_temp} --headless=new',
            }
        ],
        'stat_map': {
            str(stale_temp): {
                'mtime': 1_000.0,
                'size_kb': 128,
            }
        },
        'now': 10_000.0,
    }

    payload = run_guard(
        apply=True,
        min_age_hours=1.0,
        free_mb_threshold=512.0,
        compressor_mb_threshold=512.0,
        fixture=fixture,
    )

    assert payload['pressure']['triggered'] is True
    assert payload['cleanup']['summary']['target_count'] == 1
    assert payload['cleanup']['cleanup']['removed_dirs'] == [str(stale_temp)]



def test_run_guard_skips_cleanup_when_pressure_not_triggered(tmp_path):
    from scripts.memory_pressure_relief import run_guard

    temp_root = tmp_path / 'T'
    temp_root.mkdir()

    payload = run_guard(
        apply=True,
        min_age_hours=1.0,
        free_mb_threshold=512.0,
        compressor_mb_threshold=2048.0,
        fixture={
            'vm_stat_output': """
Mach Virtual Memory Statistics: (page size of 4096 bytes)
Pages free:                             400000.
Pages speculative:                       50000.
Pages occupied by compressor:            40000.
Pages stored in compressor:              50000.
""".strip(),
            'temp_root': str(temp_root),
            'protected_pids': [8011],
            'ps_rows': [],
            'stat_map': {},
            'now': 10_000.0,
        },
    )

    assert payload['pressure']['triggered'] is False
    assert payload['cleanup'] is None



def test_cli_apply_outputs_guard_payload(tmp_path):
    temp_root = tmp_path / 'T'
    temp_root.mkdir()
    stale_temp = temp_root / 'webjs-fresh-state-stale'
    stale_temp.mkdir()
    stale_temp.joinpath('payload.bin').write_bytes(b'x' * 16)

    fixture = {
        'vm_stat_output': """
Mach Virtual Memory Statistics: (page size of 4096 bytes)
Pages free:                               2509.
Pages speculative:                       14220.
Pages occupied by compressor:           172089.
Pages stored in compressor:             602715.
""".strip(),
        'temp_root': str(temp_root),
        'protected_pids': [8011],
        'ps_rows': [
            {
                'pid': 111,
                'ppid': 1,
                'command': f'/Applications/Google Chrome --user-data-dir={stale_temp} --headless=new',
            }
        ],
        'stat_map': {
            str(stale_temp): {
                'mtime': 1_000.0,
                'size_kb': 128,
            }
        },
        'now': 10_000.0,
    }
    fixture_path = tmp_path / 'fixture.json'
    fixture_path.write_text(json.dumps(fixture), encoding='utf-8')

    result = subprocess.run(
        [
            'python3',
            'scripts/memory_pressure_relief.py',
            '--apply',
            '--fixture',
            str(fixture_path),
            '--min-age-hours',
            '1',
            '--free-mb-threshold',
            '512',
            '--compressor-mb-threshold',
            '512',
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )

    payload = json.loads(result.stdout)
    assert payload['apply'] is True
    assert payload['pressure']['triggered'] is True
    assert payload['cleanup']['summary']['target_count'] == 1


def test_collect_stale_pytest_groups_only_targets_orphaned_old_groups(tmp_path):
    from scripts.memory_pressure_relief import collect_stale_pytest_groups

    current_pgid = 99999
    orphan_dir = tmp_path / 'pytest-orphan'
    active_dir = tmp_path / 'pytest-active'
    orphan_dir.mkdir()
    active_dir.mkdir()

    groups = collect_stale_pytest_groups(
        ps_rows=[
            {
                'pid': 41001,
                'ppid': 1,
                'pgid': 41001,
                'etime_seconds': 9_000,
                'command': f'/Users/chauncey/work/mcn-ai-automation/.venv/bin/python -m pytest tests/test_api.py --basetemp={orphan_dir}',
            },
            {
                'pid': 41002,
                'ppid': 41001,
                'pgid': 41001,
                'etime_seconds': 8_990,
                'command': '/usr/bin/python3 helper.py',
            },
            {
                'pid': 42001,
                'ppid': 32000,
                'pgid': 42001,
                'etime_seconds': 9_000,
                'command': f'/Users/chauncey/work/mcn-ai-automation/.venv/bin/python -m pytest tests/test_other.py --basetemp={active_dir}',
            },
            {
                'pid': 43001,
                'ppid': 1,
                'pgid': current_pgid,
                'etime_seconds': 9_000,
                'command': '/Users/chauncey/work/mcn-ai-automation/.venv/bin/pytest tests/test_live.py',
            },
        ],
        min_age_hours=1.0,
        current_pgid=current_pgid,
    )

    assert len(groups) == 1
    assert groups[0]['pgid'] == 41001
    assert groups[0]['root_pid'] == 41001
    assert groups[0]['process_count'] == 2
    assert orphan_dir.as_posix() in groups[0]['commands'][0]



def test_run_guard_applies_pytest_cleanup_only_when_pressure_triggered(tmp_path):
    from scripts.memory_pressure_relief import run_guard

    pytest_tmp = tmp_path / 'pytest-stale'
    pytest_tmp.mkdir()

    fixture = {
        'vm_stat_output': """
Mach Virtual Memory Statistics: (page size of 4096 bytes)
Pages free:                               2509.
Pages speculative:                       14220.
Pages occupied by compressor:           172089.
Pages stored in compressor:             602715.
""".strip(),
        'temp_root': str(tmp_path / 'T'),
        'protected_pids': [8011],
        'ps_rows': [],
        'stat_map': {},
        'pytest_ps_rows': [
            {
                'pid': 51001,
                'ppid': 1,
                'pgid': 51001,
                'etime_seconds': 8_000,
                'command': f'/Users/chauncey/work/mcn-ai-automation/.venv/bin/python -m pytest tests/test_api.py --basetemp={pytest_tmp}',
            },
            {
                'pid': 51002,
                'ppid': 51001,
                'pgid': 51001,
                'etime_seconds': 7_990,
                'command': '/usr/bin/python3 helper.py',
            },
        ],
        'current_pgid': 99999,
    }

    payload = run_guard(
        apply=False,
        min_age_hours=1.0,
        free_mb_threshold=512.0,
        compressor_mb_threshold=512.0,
        fixture=fixture,
    )

    assert payload['pressure']['triggered'] is True
    assert payload['pytest_cleanup']['summary']['target_group_count'] == 1
    assert payload['pytest_cleanup']['targets'][0]['pgid'] == 51001



def test_run_guard_skips_pytest_cleanup_when_pressure_not_triggered(tmp_path):
    from scripts.memory_pressure_relief import run_guard

    payload = run_guard(
        apply=False,
        min_age_hours=1.0,
        free_mb_threshold=512.0,
        compressor_mb_threshold=2048.0,
        fixture={
            'vm_stat_output': """
Mach Virtual Memory Statistics: (page size of 4096 bytes)
Pages free:                             400000.
Pages speculative:                       50000.
Pages occupied by compressor:            40000.
Pages stored in compressor:              50000.
""".strip(),
            'temp_root': str(tmp_path / 'T'),
            'protected_pids': [8011],
            'ps_rows': [],
            'pytest_ps_rows': [
                {
                    'pid': 52001,
                    'ppid': 1,
                    'pgid': 52001,
                    'etime_seconds': 8_000,
                    'command': '/Users/chauncey/work/mcn-ai-automation/.venv/bin/pytest tests/test_api.py',
                }
            ],
            'stat_map': {},
            'current_pgid': 99999,
        },
    )

    assert payload['pressure']['triggered'] is False
    assert payload['pytest_cleanup'] is None
