import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def pytest_sessionfinish(session, exitstatus):
    """Schedule a safe local cleanup after pytest exits.

    Some WhatsApp/browser tests can leave orphaned temp Chrome roots after pytest
    has already finished. Run the existing guarded cleanup out-of-process so it
    can see those roots after the pytest process is gone. The cleanup script
    protects local service ports and LocalAuth-backed sessions.
    """
    if os.environ.get('MCN_SKIP_POST_PYTEST_CLEANUP') == '1':
        return
    if sys.platform != 'darwin':
        return
    cleanup_script = ROOT / 'scripts' / 'webjs_temp_cleanup.py'
    if not cleanup_script.exists():
        return
    cleanup_code = (
        'import subprocess, sys, time; '
        'time.sleep(2); '
        'subprocess.run(['
        'sys.executable, '
        f'{str(cleanup_script)!r}, '
        "'--apply', "
        "'--min-age-hours', "
        "'0', "
        "'--json-indent', "
        "'0'"
        '], cwd=' + repr(str(ROOT)) + ', stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)'
    )
    try:
        subprocess.Popen(
            [sys.executable, '-c', cleanup_code],
            cwd=str(ROOT),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        # Cleanup is best-effort and must never change the pytest result.
        return
