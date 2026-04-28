from datetime import datetime, timezone
from pathlib import Path

from app.whatsapp_live_monitor import (
    compute_registration_release_state,
    load_monitor_state,
    parse_pending_requests,
    update_first_seen_at,
)


def test_parse_pending_requests_extracts_requesters_and_pending_count():
    body = """
    待处理请求
    1
    ~Eastion
    请求加入。点击以审核。
    新成员需要管理员批准才能加入该群组。
    通过邀请链接
    +86 138 6064 0933
    ~Eastion
    由+86 138 6064 0933添加
    """

    parsed = parse_pending_requests(body)

    assert parsed["pending_count"] == 1
    assert parsed["requesters"] == ["~Eastion"]
    assert parsed["phone_numbers"] == ["+86 138 6064 0933"]
    assert parsed["has_pending_section"] is True


def test_compute_registration_release_state_returns_waiting_until_timeout():
    first_seen_at = datetime(2026, 4, 22, 13, 32, 0, tzinfo=timezone.utc)
    now = datetime(2026, 4, 22, 13, 36, 32, tzinfo=timezone.utc)

    state = compute_registration_release_state(
        pending_count=1,
        first_seen_at=first_seen_at,
        now=now,
        batch_size=30,
        timeout_minutes=30,
    )

    assert state["ready"] is False
    assert state["reason_code"] == "waiting_for_batch"
    assert state["remaining_seconds"] == 1528
    assert state["poll_interval_seconds"] == 30


def test_compute_registration_release_state_returns_ready_when_timeout_reached():
    first_seen_at = datetime(2026, 4, 22, 13, 32, 0, tzinfo=timezone.utc)
    now = datetime(2026, 4, 22, 14, 2, 0, tzinfo=timezone.utc)

    state = compute_registration_release_state(
        pending_count=1,
        first_seen_at=first_seen_at,
        now=now,
        batch_size=30,
        timeout_minutes=30,
    )

    assert state["ready"] is True
    assert state["reason_code"] == "timeout_flush"
    assert state["remaining_seconds"] == 0
    assert state["poll_interval_seconds"] == 10


def test_update_first_seen_at_persists_first_detection_across_runs(tmp_path: Path):
    state_path = tmp_path / 'monitor_state.json'
    state = load_monitor_state(state_path)
    first_now = datetime(2026, 4, 22, 13, 32, 0, tzinfo=timezone.utc)
    second_now = datetime(2026, 4, 22, 13, 36, 32, tzinfo=timezone.utc)

    first_seen = update_first_seen_at(
        state,
        group_name='8️⃣5️⃣',
        pending_count=1,
        now=first_now,
        state_path=state_path,
    )
    second_seen = update_first_seen_at(
        state,
        group_name='8️⃣5️⃣',
        pending_count=1,
        now=second_now,
        state_path=state_path,
    )

    assert first_seen == first_now
    assert second_seen == first_now
    reloaded = load_monitor_state(state_path)
    assert reloaded['8️⃣5️⃣']['first_seen_at'] == first_now.isoformat()


def test_update_first_seen_at_clears_when_pending_drops_to_zero(tmp_path: Path):
    state_path = tmp_path / 'monitor_state.json'
    state = load_monitor_state(state_path)
    first_now = datetime(2026, 4, 22, 13, 32, 0, tzinfo=timezone.utc)

    update_first_seen_at(state, group_name='8️⃣5️⃣', pending_count=1, now=first_now, state_path=state_path)
    cleared = update_first_seen_at(state, group_name='8️⃣5️⃣', pending_count=0, now=first_now, state_path=state_path)

    assert cleared is None
    reloaded = load_monitor_state(state_path)
    assert reloaded['8️⃣5️⃣']['first_seen_at'] is None


def test_parse_pending_requests_returns_zero_when_empty_queue_banner_is_visible():
    body = """
    聊天历史
    ~Eastion
    请求加入。点击以审核。
    待处理请求
    没有要审核的成员
    请求加入该群组且等待批准的用户将在此显示。
    +86 138 6064 0933
    """

    parsed = parse_pending_requests(body)

    assert parsed["pending_count"] == 0
    assert parsed["requesters"] == []
    assert parsed["phone_numbers"] == []
    assert parsed["has_pending_section"] is True


def test_parse_pending_requests_ignores_member_phone_numbers_when_pending_section_only_shows_group_members():
    body = """
    群组信息
    待处理请求
    2
    新成员需要管理员批准才能加入该群组。
    +852 6775 5475
    +62 851-9830-6838
    +62 858-1347-8460
    你
    群组管理员
    """

    parsed = parse_pending_requests(body)

    assert parsed["pending_count"] == 2
    assert parsed["requesters"] == []
    assert parsed["phone_numbers"] == []


def test_parse_pending_requests_prefers_last_pending_section_over_historical_one():
    body = """
    聊天历史
    待处理请求
    通过邀请链接
    +852 6775 5475
    ~G2
    由+852 6775 5475添加
    更多历史
    待处理请求
    没有要审核的成员
    请求加入该群组且等待批准的用户将在此显示。
    """

    parsed = parse_pending_requests(body)

    assert parsed["pending_count"] == 0
    assert parsed["requesters"] == []
    assert parsed["phone_numbers"] == []


def test_parse_pending_requests_ignores_historical_chat_rows_once_group_info_panel_is_open():
    body = """
    聊天历史
    ~Eastion
    请求加入。点击以审核。
    群组信息
    群组 · 6位成员
    添加成员
    使用链接邀请加入群组
    """

    parsed = parse_pending_requests(body)

    assert parsed["pending_count"] == 0
    assert parsed["requesters"] == []
    assert parsed["phone_numbers"] == []
