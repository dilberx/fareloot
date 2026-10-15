from datetime import UTC, datetime, timedelta

from fareloot.health import assess

NOW = datetime(2026, 12, 1, 12, tzinfo=UTC)
FRESH = NOW - timedelta(hours=6)


def test_healthy():
    assert assess(NOW, NOW - timedelta(days=10), FRESH, "active").ok


def test_warns_from_day_45_with_days_remaining():
    h = assess(NOW, NOW - timedelta(days=45), FRESH, "active")
    assert len(h.warnings) == 1
    assert "45 days" in h.warnings[0] and "15 day(s)" in h.warnings[0]
    assert "Merge any PR" in h.warnings[0]


def test_quiet_before_day_45():
    assert assess(NOW, NOW - timedelta(days=44), FRESH, "active").ok


def test_past_the_limit_still_warns_without_negative_days():
    h = assess(NOW, NOW - timedelta(days=63), FRESH, "active")
    assert "any time now" in h.warnings[0]
    assert "-3" not in h.warnings[0]


def test_stalled_watcher():
    h = assess(NOW, NOW - timedelta(days=1), NOW - timedelta(hours=40), "active")
    assert len(h.warnings) == 1 and "40 hours" in h.warnings[0]


def test_disabled_workflow():
    h = assess(NOW, NOW - timedelta(days=1), FRESH, "disabled_inactivity")
    assert any("disabled_inactivity" in w for w in h.warnings)


def test_missing_facts_are_warnings_not_silence():
    h = assess(NOW, None, None, "active")
    assert len(h.warnings) == 2


def test_cli_exits_nonzero_and_reports(monkeypatch):
    from typer.testing import CliRunner

    from fareloot.cli import app

    old = (datetime.now(UTC) - timedelta(days=50)).isoformat()
    recent = datetime.now(UTC).isoformat()
    r = CliRunner().invoke(
        app, ["health", "--last-main-commit", old, "--last-watch-success", recent]
    )
    assert r.exit_code == 1
    assert "50 days" in r.output

    r = CliRunner().invoke(
        app, ["health", "--last-main-commit", recent, "--last-watch-success", recent]
    )
    assert r.exit_code == 0 and "healthy" in r.output
