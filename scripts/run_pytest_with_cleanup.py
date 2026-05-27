#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parents[1]


def _parse_args(argv: Sequence[str]) -> tuple[argparse.Namespace, list[str]]:
    parser = argparse.ArgumentParser(
        description='Run pytest in an isolated process group and always run guarded MCN browser-temp cleanup afterwards.',
    )
    parser.add_argument(
        '--timeout-seconds',
        type=float,
        default=0.0,
        help='Optional hard timeout for the pytest subprocess. 0 means no timeout.',
    )
    return parser.parse_known_args(list(argv))


def _cleanup_webjs_temp() -> None:
    cleanup_script = ROOT / 'scripts' / 'webjs_temp_cleanup.py'
    if not cleanup_script.exists():
        return
    subprocess.run(
        [
            sys.executable,
            str(cleanup_script),
            '--apply',
            '--min-age-hours',
            '0',
            '--json-indent',
            '0',
        ],
        cwd=str(ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def _terminate_process_group(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.time() + 5.0
    while time.time() < deadline:
        if process.poll() is not None:
            return
        time.sleep(0.2)
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return


def run(argv: Sequence[str]) -> int:
    args, pytest_args = _parse_args(argv)
    if not pytest_args:
        pytest_args = ['tests']

    process = subprocess.Popen(
        [sys.executable, '-m', 'pytest', *pytest_args],
        cwd=str(ROOT),
        start_new_session=True,
    )
    exit_code = 1
    try:
        try:
            exit_code = process.wait(timeout=args.timeout_seconds if args.timeout_seconds > 0 else None)
        except subprocess.TimeoutExpired:
            _terminate_process_group(process)
            exit_code = 124
    except KeyboardInterrupt:
        _terminate_process_group(process)
        exit_code = 130
    finally:
        _terminate_process_group(process)
        _cleanup_webjs_temp()
    return int(exit_code or 0)


if __name__ == '__main__':
    raise SystemExit(run(sys.argv[1:]))
