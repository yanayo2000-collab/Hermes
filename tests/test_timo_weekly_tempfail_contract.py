from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_weekly_cache_treats_tempfail_as_deferred_and_self_removes_reobserve_timer():
    dropin = (
        ROOT
        / 'scripts/systemd/mcn-timo-anchor-export-cache-weekly.service.d/30-tempfail-reobserve.conf'
    ).read_text(encoding='utf-8')
    assert 'SuccessExitStatus=75' in dropin
    assert 'disable --now mcn-timo-anchor-export-cache-weekly-reobserve.timer' in dropin


def test_reobserve_timer_is_one_shot_and_targets_only_weekly_cache():
    timer = (
        ROOT / 'scripts/systemd/mcn-timo-anchor-export-cache-weekly-reobserve.timer'
    ).read_text(encoding='utf-8')
    assert 'OnActiveSec=2min' in timer
    assert 'OnUnitInactiveSec' not in timer
    assert 'OnCalendar' not in timer
    assert 'Unit=mcn-timo-anchor-export-cache-weekly.service' in timer
