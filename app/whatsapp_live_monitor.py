from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


REQUEST_JOIN_PATTERN = re.compile(r'(?P<name>[^\n]+)\s*\n\s*请求加入。点击以审核。')
PHONE_PATTERN = re.compile(r'\+\d[\d\s-]{6,}')


def load_monitor_state(state_path: Path) -> Dict[str, Any]:
    if state_path.exists():
        try:
            return json.loads(state_path.read_text())
        except Exception:
            return {}
    return {}


def save_monitor_state(state: Dict[str, Any], state_path: Path) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def update_first_seen_at(
    state: Dict[str, Any],
    *,
    group_name: str,
    pending_count: int,
    now: datetime,
    state_path: Path,
) -> Optional[datetime]:
    bucket = state.setdefault(group_name, {})
    if pending_count <= 0:
        bucket['first_seen_at'] = None
        save_monitor_state(state, state_path)
        return None
    first_seen_raw = bucket.get('first_seen_at')
    if first_seen_raw:
        try:
            return datetime.fromisoformat(first_seen_raw)
        except Exception:
            pass
    bucket['first_seen_at'] = now.isoformat()
    save_monitor_state(state, state_path)
    return now


def parse_pending_requests(body_text: str) -> Dict[str, Any]:
    text = str(body_text or '')
    has_pending_section = '待处理请求' in text
    relevant_text = text.split('待处理请求', 1)[1] if has_pending_section else ''
    requesters: List[str] = []
    for match in REQUEST_JOIN_PATTERN.finditer(relevant_text):
        name = str(match.group('name') or '').strip()
        if name and name not in requesters:
            requesters.append(name)
    phones = []
    for phone in PHONE_PATTERN.findall(relevant_text):
        normalized = ' '.join(str(phone).split())
        if normalized not in phones:
            phones.append(normalized)
    pending_count = 0
    pending_match = re.search(r'待处理请求\s*(\d+)', text)
    if pending_match:
        pending_count = int(pending_match.group(1))
    elif has_pending_section and requesters:
        pending_count = len(requesters)
    return {
        'pending_count': pending_count,
        'requesters': requesters,
        'phone_numbers': phones,
        'has_pending_section': has_pending_section,
        'has_review_actions': False,
    }


def compute_registration_release_state(
    *,
    pending_count: int,
    first_seen_at: Optional[datetime],
    now: datetime,
    batch_size: int = 30,
    timeout_minutes: int = 20,
) -> Dict[str, Any]:
    pending_count = max(int(pending_count or 0), 0)
    first_seen = first_seen_at or now
    if first_seen.tzinfo is None:
        first_seen = first_seen.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    deadline = first_seen + timedelta(minutes=timeout_minutes)
    remaining_seconds = max(0, int((deadline - now).total_seconds()))
    elapsed_minutes = max(0, int((now - first_seen).total_seconds() // 60))
    if pending_count >= batch_size:
        return {
            'ready': True,
            'reason_code': 'batch_size_reached',
            'remaining_seconds': 0,
            'elapsed_minutes': elapsed_minutes,
            'release_at': first_seen.isoformat(),
            'poll_interval_seconds': 10,
        }
    if pending_count > 0 and now >= deadline:
        return {
            'ready': True,
            'reason_code': 'timeout_flush',
            'remaining_seconds': 0,
            'elapsed_minutes': elapsed_minutes,
            'release_at': deadline.isoformat(),
            'poll_interval_seconds': 10,
        }
    if remaining_seconds <= 120:
        poll_interval_seconds = 10
    elif remaining_seconds <= 1200:
        poll_interval_seconds = 30
    else:
        poll_interval_seconds = 120
    return {
        'ready': False,
        'reason_code': 'waiting_for_batch',
        'remaining_seconds': remaining_seconds,
        'elapsed_minutes': elapsed_minutes,
        'release_at': deadline.isoformat(),
        'poll_interval_seconds': poll_interval_seconds,
    }


def clone_chrome_profile(src_root: Path, profile_dir_name: str, dst_root: Path) -> Path:
    if dst_root.exists():
        shutil.rmtree(dst_root)
    dst_root.mkdir(parents=True)
    for name in ['Local State', profile_dir_name]:
        src = src_root / name
        dst = dst_root / name
        if src.is_dir():
            shutil.copytree(src, dst, symlinks=True)
        else:
            shutil.copy2(src, dst)
    return dst_root


def summarize_monitor_result(group_name: str, body_text: str, first_seen_at: Optional[datetime], now: datetime) -> Dict[str, Any]:
    pending = parse_pending_requests(body_text)
    release = compute_registration_release_state(
        pending_count=pending['pending_count'],
        first_seen_at=first_seen_at,
        now=now,
    )
    return {
        'group_name': group_name,
        'pending': pending,
        'release': release,
        'checked_at': now.isoformat(),
    }
