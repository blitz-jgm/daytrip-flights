from datetime import UTC, datetime, timedelta

from factories import D, bundle, oneway

from daytrip_flights.models import SearchTask
from daytrip_flights.store import Store

NOW = datetime(2026, 10, 13, 1, 0, tzinfo=UTC)


def t(kind, origin="ICN", source="google", day=D):
    return SearchTask(source=source, route="seoul-fukuoka", origin=origin, date=day, kind=kind)


def test_offers_round_trip_through_sqlite(tmp_path):
    s = Store(tmp_path / "f.db")
    run = s.start_run("full", NOW)
    originals = [
        oneway("out", "7C1401", "ICN", "FUK", "06:30", 130500),
        bundle("BX445", "07:25", "TW208", "20:20", 413113),
    ]
    ids = s.add_offers(run, originals)
    loaded = s.offers_for_run(run, "seoul-fukuoka")
    assert [o for _, o in loaded] == originals
    assert [i for i, _ in loaded] == ids


def test_resume_returns_only_unfinished_tasks(tmp_path):
    s = Store(tmp_path / "f.db")
    run = s.start_run("full", NOW)
    tasks = [t("out"), t("back"), t("out", "GMP")]
    s.add_tasks(run, tasks)
    s.finish_task(run, tasks[0], "done", requests=1, offers=3)
    s.finish_task(run, tasks[2], "no_service")
    s.finish_task(run, tasks[1], "failed", error="boom")
    assert s.resumable_run(NOW + timedelta(hours=1), max_age_hours=6) == run
    assert s.open_tasks(run) == [tasks[1]]
    assert s.resumable_run(NOW + timedelta(hours=7), max_age_hours=6) is None
    s.finish_run(run, NOW, "done", {"google": {"requests": 3}})
    assert s.resumable_run(NOW + timedelta(hours=1), max_age_hours=6) is None


def test_no_service_cache_expires(tmp_path):
    s = Store(tmp_path / "f.db")
    s.set_service("google", "GMP", "FUK", False, NOW)
    assert s.no_service("google", "GMP", "FUK", NOW + timedelta(days=6), cache_days=7)
    assert not s.no_service("google", "GMP", "FUK", NOW + timedelta(days=8), cache_days=7)
    assert not s.no_service("google", "ICN", "FUK", NOW, cache_days=7)


def test_last_full_scan_and_latest_best(tmp_path):
    s = Store(tmp_path / "f.db")
    assert s.last_full_scan() is None
    r1 = s.start_run("full", NOW)
    s.add_daily_best(r1, "seoul-fukuoka", D, "combo", 300000, None, None, None)
    s.finish_run(r1, NOW, "done", {})
    r2 = s.start_run("refresh", NOW + timedelta(hours=3))
    s.add_daily_best(r2, "seoul-fukuoka", D, "bundle", 280000, None, None, None)
    s.finish_run(r2, NOW + timedelta(hours=3), "done", {})
    assert s.last_full_scan() == NOW
    latest = s.latest_best("seoul-fukuoka")
    assert [(b["date"], b["total_krw"], b["run_id"]) for b in latest] == [(D, 280000, r2)]
