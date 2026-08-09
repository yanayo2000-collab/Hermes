from __future__ import annotations

import json
import signal
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_get_protected_pid_set_includes_descendants():
    from scripts.webjs_temp_cleanup import get_protected_pid_set

    ps_rows = [
        {'pid': 500, 'ppid': 1, 'command': 'python production_ops_daemon.py'},
        {'pid': 501, 'ppid': 500, 'command': 'node child-worker.js'},
        {'pid': 502, 'ppid': 501, 'command': 'Google Chrome --user-data-dir=/tmp/demo'},
        {'pid': 900, 'ppid': 1, 'command': 'unrelated'},
    ]

    protected = get_protected_pid_set(
        protected_ports=[],
        protected_cmd_substrings=['production_ops_daemon.py'],
        ps_rows=ps_rows,
        fixture={'protected_pids': []},
    )

    assert protected == {500, 501, 502}



def test_get_protected_pid_set_matches_server_js_path_variants():
    from scripts.webjs_temp_cleanup import get_protected_pid_set

    ps_rows = [
        {'pid': 600, 'ppid': 1, 'command': 'node ./src/server.js'},
        {'pid': 601, 'ppid': 600, 'command': 'Google Chrome --user-data-dir=/tmp/demo-1'},
        {'pid': 700, 'ppid': 1, 'command': 'node /Users/demo/app/src/server.js'},
        {'pid': 701, 'ppid': 700, 'command': 'Google Chrome --user-data-dir=/tmp/demo-2'},
    ]

    protected = get_protected_pid_set(
        protected_ports=[],
        protected_cmd_substrings=['src/server.js'],
        ps_rows=ps_rows,
        fixture={'protected_pids': []},
    )

    assert protected == {600, 601, 700, 701}



def test_collect_cleanup_targets_only_selects_stale_temp_orphans(tmp_path):
    from scripts.webjs_temp_cleanup import collect_cleanup_targets

    temp_root = tmp_path / 'T'
    temp_root.mkdir()
    stale_temp = temp_root / 'webjs-fresh-state-stale'
    stale_temp.mkdir()
    stale_temp.joinpath('data.bin').write_bytes(b'x' * 32)

    fresh_temp = temp_root / 'webjs-fresh-state-fresh'
    fresh_temp.mkdir()

    active_auth = tmp_path / 'webjs-approval-worker' / '.wwebjs_auth_accounts' / 'wa-admin-1' / 'session-wa-approval-wa-admin-1'
    active_auth.mkdir(parents=True)

    ps_rows = [
        {
            'pid': 111,
            'ppid': 1,
            'command': f'/Applications/Google Chrome --user-data-dir={stale_temp} --headless=new',
        },
        {
            'pid': 222,
            'ppid': 1,
            'command': f'/Applications/Google Chrome --user-data-dir={fresh_temp} --headless=new',
        },
        {
            'pid': 333,
            'ppid': 999,
            'command': f'/Applications/Google Chrome --user-data-dir={stale_temp} --headless=new',
        },
        {
            'pid': 444,
            'ppid': 1,
            'command': f'/Applications/Google Chrome --user-data-dir={active_auth} --headless=new',
        },
    ]

    targets = collect_cleanup_targets(
        temp_root=temp_root,
        ps_rows=ps_rows,
        protected_pids={444},
        min_age_hours=1.0,
        now=10_000.0,
        stat_map={
            str(stale_temp): {'mtime': 1_000.0, 'size_kb': 128},
            str(fresh_temp): {'mtime': 9_900.0, 'size_kb': 64},
            str(active_auth): {'mtime': 1_000.0, 'size_kb': 512},
        },
    )

    assert [item['pid'] for item in targets] == [111]
    assert targets[0]['user_data_dir'] == str(stale_temp)
    assert targets[0]['age_hours'] > 1.0



def test_execute_cleanup_kills_all_root_pids_for_same_dir_then_removes_unreferenced_dirs(tmp_path):
    from scripts.webjs_temp_cleanup import execute_cleanup

    temp_root = tmp_path / 'T'
    temp_root.mkdir()
    stale_temp = temp_root / 'webjs-fresh-state-stale'
    stale_temp.mkdir()
    stale_temp.joinpath('payload.bin').write_bytes(b'x' * 16)
    zero_shell = temp_root / 'webjs-approval-approve-profile-shell'
    zero_shell.mkdir()

    killed = []
    live_pids = {111, 112}

    def fake_kill(pid: int, sig: int) -> None:
        killed.append((pid, sig))
        if sig == signal.SIGTERM:
            live_pids.discard(pid)

    result = execute_cleanup(
        targets=[
            {
                'pid': 111,
                'user_data_dir': str(stale_temp),
            },
            {
                'pid': 112,
                'user_data_dir': str(stale_temp),
            },
        ],
        temp_root=temp_root,
        min_age_hours=1.0,
        now=10_000.0,
        stat_map={
            str(stale_temp): {'mtime': 1_000.0, 'size_kb': 16},
            str(zero_shell): {'mtime': 1_000.0, 'size_kb': 0},
        },
        kill_fn=fake_kill,
        is_pid_alive_fn=lambda pid: pid in live_pids,
        sleep_fn=lambda _seconds: None,
        referenced_dirs_provider=lambda: set(),
        ps_rows_provider=lambda: [
            {
                'pid': 111,
                'ppid': 1,
                'command': f'Google Chrome --user-data-dir={stale_temp}',
            },
            {
                'pid': 112,
                'ppid': 1,
                'command': f'Google Chrome --user-data-dir={stale_temp}',
            },
        ],
    )

    assert killed == [(111, signal.SIGTERM), (112, signal.SIGTERM)]
    assert result['term_sent'] == [111, 112]
    assert result['kill_sent'] == []
    assert str(stale_temp) in result['removed_dirs']
    assert str(zero_shell) in result['removed_dirs']
    assert not stale_temp.exists()
    assert not zero_shell.exists()



def test_execute_cleanup_does_not_kill_pid_when_revalidation_fails(tmp_path):
    from scripts.webjs_temp_cleanup import execute_cleanup

    temp_root = tmp_path / 'T'
    temp_root.mkdir()
    stale_temp = temp_root / 'webjs-fresh-state-stale'
    stale_temp.mkdir()
    stale_temp.joinpath('payload.bin').write_bytes(b'x' * 16)

    killed = []

    result = execute_cleanup(
        targets=[
            {
                'pid': 111,
                'user_data_dir': str(stale_temp),
            },
        ],
        temp_root=temp_root,
        min_age_hours=1.0,
        now=10_000.0,
        stat_map={
            str(stale_temp): {'mtime': 1_000.0, 'size_kb': 16},
        },
        kill_fn=lambda pid, sig: killed.append((pid, sig)),
        is_pid_alive_fn=lambda _pid: True,
        sleep_fn=lambda _seconds: None,
        referenced_dirs_provider=lambda: {str(stale_temp)},
        ps_rows_provider=lambda: [
            {
                'pid': 111,
                'ppid': 2,
                'command': 'Google Chrome --user-data-dir=/tmp/changed-profile',
            }
        ],
    )

    assert killed == []
    assert result['term_sent'] == []
    assert result['kill_sent'] == []
    assert {'pid': 111, 'reason': 'target_revalidation_failed'} in result['skipped_pids']
    assert result['failed_remove'] == [{'dir': str(stale_temp), 'reason': 'still_referenced_by_live_process'}]
    assert stale_temp.exists()



def test_execute_cleanup_does_not_remove_recent_zero_size_shells(tmp_path):
    from scripts.webjs_temp_cleanup import execute_cleanup

    temp_root = tmp_path / 'T'
    temp_root.mkdir()
    recent_zero_shell = temp_root / 'webjs-approval-approve-profile-recent'
    recent_zero_shell.mkdir()

    result = execute_cleanup(
        targets=[],
        temp_root=temp_root,
        min_age_hours=1.0,
        now=10_000.0,
        stat_map={
            str(recent_zero_shell): {'mtime': 9_900.0, 'size_kb': 0},
        },
        kill_fn=lambda *_args: None,
        is_pid_alive_fn=lambda _pid: False,
        sleep_fn=lambda _seconds: None,
        referenced_dirs_provider=lambda: set(),
        ps_rows_provider=lambda: [],
    )

    assert result['removed_dirs'] == []
    assert recent_zero_shell.exists()



def test_cli_dry_run_outputs_targets_json(tmp_path):
    temp_root = tmp_path / 'T'
    temp_root.mkdir()
    stale_temp = temp_root / 'webjs-fresh-state-stale'
    stale_temp.mkdir()
    stale_temp.joinpath('payload.bin').write_bytes(b'x' * 16)

    fixture = {
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
            'scripts/webjs_temp_cleanup.py',
            '--fixture',
            str(fixture_path),
            '--min-age-hours',
            '1',
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )

    payload = json.loads(result.stdout)
    assert payload['apply'] is False
    assert payload['summary']['target_count'] == 1
    assert payload['targets'][0]['pid'] == 111
