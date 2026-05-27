from pathlib import Path


def test_pytest_sessionfinish_schedules_post_test_webjs_cleanup():
    conftest = Path(__file__).with_name('conftest.py').read_text(encoding='utf-8')

    assert 'def pytest_sessionfinish(session, exitstatus):' in conftest
    assert 'MCN_SKIP_POST_PYTEST_CLEANUP' in conftest
    assert 'webjs_temp_cleanup.py' in conftest
    assert "'--apply'" in conftest
    assert "'--min-age-hours'" in conftest
    assert "'0'" in conftest


def test_pytest_cleanup_wrapper_kills_test_process_group_and_runs_webjs_cleanup():
    wrapper = Path(__file__).resolve().parents[1] / 'scripts' / 'run_pytest_with_cleanup.py'
    source = wrapper.read_text(encoding='utf-8')

    assert 'start_new_session=True' in source
    assert 'os.killpg(process.pid, signal.SIGTERM)' in source
    assert 'os.killpg(process.pid, signal.SIGKILL)' in source
    assert 'webjs_temp_cleanup.py' in source
    assert "'--apply'" in source
    assert "'--min-age-hours'" in source
    assert "'0'" in source
