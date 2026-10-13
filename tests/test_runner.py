import threading
import time
from datetime import UTC, date, datetime, timedelta

import pytest
from factories import bundle, oneway

from daytrip_flights.config import Settings, Tiers
from daytrip_flights.models import SearchResult
from daytrip_flights.runner import Runner
from daytrip_flights.store import Store

NOW = datetime(2026, 11, 9, 18, 0, tzinfo=UTC)  # 2026-11-10 03:00 KST
TODAY = date(2026, 11, 10)


class FakeSource:
    """ICN has flights every day; GMP has none. Prices drop by date so ranking is testable."""

    supports_bundle = True

    def __init__(self, name="google", fail_on=None, blocked_after=None):
        self.name = name
        self.calls = []
        self.fail_on = fail_on or set()
        self.blocked_after = blocked_after

    def search(self, task, route, pacer):
        def go():
            self.calls.append(task.key)
            if self.blocked_after is not None and len(self.calls) > self.blocked_after:
                raise RuntimeError("Google Flights returned HTTP 429.")
            if (task.kind, task.date) in self.fail_on:
                raise ValueError("parse error")
            if task.origin == "GMP":
                return SearchResult(offers=[])
            n = (task.date - TODAY).days
            if task.kind == "out":
                return SearchResult(
                    offers=[
                        oneway(
                            "out", "7C1", "ICN", "FUK", "06:30", 100000 - n, self.name, task.date
                        ),
                        oneway("out", "TW1", "ICN", "FUK", "14:00", 1, self.name, task.date),
                    ]
                )
            if task.kind == "back":
                return SearchResult(
                    offers=[
                        oneway("back", "7C2", "FUK", "ICN", "19:00", 100000, self.name, task.date)
                    ]
                )
            return SearchResult(
                offers=[bundle("BX1", "07:00", "TW2", "20:00", 150000, self.name, task.date)]
            )

        return pacer.call(go)


def settings(days=10, **tiers):
    return Settings.model_validate(
        {
            "routes": [
                {
                    "name": "seoul-fukuoka",
                    "origins": ["ICN", "GMP"],
                    "destination": "FUK",
                    "out_window": "06:00-09:00",
                    "back_window": "18:00-21:00",
                    "days_ahead": days,
                }
            ],
            "sources": ["google"],
            "budget": {"delay_min_s": 0, "delay_max_s": 0, "retries": 0},
            "tiers": Tiers(
                **{
                    "bundle_top_dates": 2,
                    "bundle_outbounds_per_date": 1,
                    "no_service_after_empty": 3,
                    **tiers,
                }
            ).model_dump(),
        }
    )


def runner(tmp_path, src, cfg=None, now=NOW):
    return Runner(
        cfg or settings(),
        Store(tmp_path / "f.db"),
        {src.name: src},
        now=lambda: now,
        sleep=lambda s: None,
    )


def test_full_run_stores_daily_best_with_bundles_for_top_dates(tmp_path):
    src = FakeSource()
    r = runner(tmp_path, src)
    run_id = r.run()
    store = r.store
    best = store.latest_best("seoul-fukuoka")
    assert [b["date"] for b in best] == [TODAY + timedelta(days=i) for i in range(10)]
    # combo = (100000 - n) + 100000 on every date; bundle 150000 only on the 2 cheapest dates
    by_date = {b["date"]: b for b in best}
    assert {d for d, b in by_date.items() if b["kind"] == "bundle"} == {
        TODAY + timedelta(days=9),
        TODAY + timedelta(days=8),
    }
    assert by_date[TODAY]["total_krw"] == 200000
    run = store.recent_runs(1)[0]
    assert run["status"] == "done" and run["mode"] == "full"
    assert run["sources"]["google"]["status"] == "ok"
    # GMP: 3 empty outbound + 3 empty return answers, then the rest is skipped as no service
    gmp_calls = [c for c in src.calls if c[2] == "GMP"]
    assert len(gmp_calls) == 6
    assert store.no_service("google", "GMP", "FUK", NOW, cache_days=7)
    assert run_id == run["id"]


def test_second_run_within_a_day_is_a_refresh_of_near_and_top_dates(tmp_path):
    cfg = settings(days=40, refresh_next_days=3, refresh_top_dates=2)
    runner(tmp_path, FakeSource(), cfg).run()
    src = FakeSource()
    r = runner(tmp_path, src, cfg, now=NOW + timedelta(hours=3))
    r.run()
    dates = sorted({date.fromisoformat(c[3]) for c in src.calls if c[4] != "bundle"})
    expected = [TODAY + timedelta(days=i) for i in (0, 1, 2, 38, 39)]
    assert dates == expected
    assert all(c[2] == "ICN" for c in src.calls)  # GMP no-service cache is honored
    assert r.store.recent_runs(1)[0]["mode"] == "refresh"


def test_failure_in_one_task_does_not_stop_the_run(tmp_path):
    src = FakeSource(fail_on={("back", TODAY)})
    r = runner(tmp_path, src)
    r.run()
    run = r.store.recent_runs(1)[0]
    assert run["status"] == "partial"
    assert run["sources"]["google"]["failed"] == 2  # ICN and GMP return searches
    assert TODAY not in {b["date"] for b in r.store.latest_best("seoul-fukuoka")}


def test_one_blocked_source_does_not_stop_the_other(tmp_path):
    bad = FakeSource("google", blocked_after=2)
    good = FakeSource("naver")
    cfg = settings()
    cfg.sources = ["google", "naver"]
    r = Runner(
        cfg,
        Store(tmp_path / "f.db"),
        {"google": bad, "naver": good},
        now=lambda: NOW,
        sleep=lambda s: None,
    )
    r.run()
    run = r.store.recent_runs(1)[0]
    assert run["sources"]["google"]["status"] == "blocked"
    assert run["sources"]["naver"]["status"] == "ok"
    assert len(bad.calls) == 3  # stopped right after the 429
    assert len(r.store.latest_best("seoul-fukuoka")) == 10


def test_interrupted_run_resumes_only_remaining_tasks(tmp_path):
    class Crash(BaseException):
        pass

    src = FakeSource()
    calls = {"n": 0}
    real = src.search

    def crashing(task, route, pacer):
        calls["n"] += 1
        if calls["n"] == 5:
            raise Crash
        return real(task, route, pacer)

    src.search = crashing
    with pytest.raises(Crash):
        runner(tmp_path, src).run()
    first_calls = list(src.calls)

    src2 = FakeSource()
    r = runner(tmp_path, src2, now=NOW + timedelta(minutes=30))
    run_id = r.run()
    assert run_id == 1  # same run continued
    assert not set(first_calls) & set(src2.calls)  # finished tasks were not repeated
    assert r.store.recent_runs(1)[0]["status"] == "done"
    assert len(r.store.latest_best("seoul-fukuoka")) == 10


def two_source_runner(tmp_path, google, naver, now=NOW):
    cfg = settings()
    cfg.sources = ["google", "naver"]
    return Runner(
        cfg,
        Store(tmp_path / "f.db"),
        {"google": google, "naver": naver},
        now=lambda: now,
        sleep=lambda s: None,
    )


class TracingSource(FakeSource):
    """Records every call in a log shared by all sources and how many calls overlap."""

    def __init__(self, name, log, barrier=None, pause=0.002):
        super().__init__(name)
        self.log = log
        self.barrier = barrier
        self.pause = pause
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()

    def search(self, task, route, pacer):
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if self.barrier is not None and not self.calls:
                # both sources must be inside their first search at the same time
                self.barrier.wait(timeout=5)
            time.sleep(self.pause)
            result = super().search(task, route, pacer)
            self.log.append((self.name, task.kind))
            return result
        finally:
            with self.lock:
                self.active -= 1


def test_sources_search_in_parallel(tmp_path):
    log = []
    barrier = threading.Barrier(2)
    google = TracingSource("google", log, barrier)
    naver = TracingSource("naver", log, barrier)
    r = two_source_runner(tmp_path, google, naver)
    r.run()  # a sequential runner breaks the barrier and the first search fails
    run = r.store.recent_runs(1)[0]
    assert run["status"] == "done"
    for src in (google, naver):
        assert run["sources"][src.name]["status"] == "ok"
        assert run["sources"][src.name].get("failed", 0) == 0
        assert len(src.calls) == len(set(src.calls))  # no task searched twice
    assert len(r.store.latest_best("seoul-fukuoka")) == 10


def test_each_source_searches_one_task_at_a_time(tmp_path):
    log = []
    google = TracingSource("google", log)
    naver = TracingSource("naver", log)
    two_source_runner(tmp_path, google, naver).run()
    assert google.max_active == 1
    assert naver.max_active == 1


def test_bundles_start_after_both_sources_finish_one_way_searches(tmp_path):
    log = []
    google = TracingSource("google", log)
    naver = TracingSource("naver", log, pause=0.01)  # naver finishes its one-ways last
    two_source_runner(tmp_path, google, naver).run()
    kinds = [kind for _, kind in log]
    first_bundle = kinds.index("bundle")
    assert "bundle" not in kinds[:first_bundle]
    assert all(k == "bundle" for k in kinds[first_bundle:])
    assert {name for name, kind in log if kind == "bundle"} == {"google", "naver"}


def test_blocked_source_stops_while_the_other_keeps_searching_in_parallel(tmp_path):
    log = []
    bad = TracingSource("google", log)
    bad.blocked_after = 2
    good = TracingSource("naver", log)
    r = two_source_runner(tmp_path, bad, good)
    r.run()
    run = r.store.recent_runs(1)[0]
    assert run["sources"]["google"]["status"] == "blocked"
    assert run["sources"]["naver"]["status"] == "ok"
    assert len(bad.calls) == 3
    assert len(r.store.latest_best("seoul-fukuoka")) == 10


def test_crash_in_one_source_is_resumed_without_repeating_finished_tasks(tmp_path):
    class Crash(BaseException):
        pass

    google, naver = FakeSource("google"), FakeSource("naver")
    real = google.search

    def crashing(task, route, pacer):
        if len(google.calls) == 4:
            raise Crash
        return real(task, route, pacer)

    google.search = crashing
    with pytest.raises(Crash):
        two_source_runner(tmp_path, google, naver).run()
    first = set(google.calls) | set(naver.calls)

    google2, naver2 = FakeSource("google"), FakeSource("naver")
    r = two_source_runner(tmp_path, google2, naver2, now=NOW + timedelta(minutes=30))
    assert r.run() == 1
    assert not first & (set(google2.calls) | set(naver2.calls))
    assert google2.calls  # google still had work left
    assert r.store.recent_runs(1)[0]["status"] == "done"
    assert len(r.store.latest_best("seoul-fukuoka")) == 10


def test_bundles_are_only_searched_for_origins_with_flights(tmp_path):
    # GMP never reaches the no-service threshold here, but has no flights: no GMP bundle
    src = FakeSource()
    cfg = settings(days=3, no_service_after_empty=99)
    runner(tmp_path, src, cfg).run()
    bundles = [c for c in src.calls if c[4] == "bundle"]
    assert bundles and all(c[2] == "ICN" for c in bundles)


def test_horizon_limited_run_does_not_count_as_full_scan(tmp_path):
    cfg = settings(days=10)
    smoke = Runner(
        cfg,
        Store(tmp_path / "f.db"),
        {"google": FakeSource()},
        days_ahead=2,
        now=lambda: NOW,
        sleep=lambda s: None,
    )
    smoke.run()
    assert smoke.store.recent_runs(1)[0]["mode"] == "limited"
    r = runner(tmp_path, FakeSource(), cfg, now=NOW + timedelta(hours=1))
    r.run()
    assert r.store.recent_runs(1)[0]["mode"] == "full"
