import re
from datetime import UTC, datetime, timedelta

from factories import D, bundle, oneway

from daytrip_flights.config import Settings
from daytrip_flights.report.render import build_context, write_report
from daytrip_flights.store import Store

T0 = datetime(2026, 11, 9, 18, 0, tzinfo=UTC)


def populated(tmp_path):
    settings = Settings.model_validate(
        {
            "routes": [
                {
                    "name": "seoul-fukuoka",
                    "origins": ["ICN"],
                    "destination": "FUK",
                    "out_window": "06:00-09:00",
                    "back_window": "18:00-21:00",
                }
            ]
        }
    )
    s = Store(tmp_path / "f.db")
    for i, price in enumerate([400000, 380000]):  # two runs: price drops 20,000
        run = s.start_run("full", T0 + timedelta(hours=3 * i))
        o, b = s.add_offers(
            run,
            [
                oneway("out", "7C1401", "ICN", "FUK", "06:30", price // 2),
                oneway("back", "7C1408", "FUK", "ICN", "18:55", price // 2, source="naver"),
            ],
        )
        s.add_daily_best(run, "seoul-fukuoka", D, "combo", price, o, b, None)
        (rt,) = s.add_offers(
            run, [bundle("BX445", "07:25", "TW208", "20:20", 413113, day=D + timedelta(days=1))]
        )
        s.add_daily_best(
            run, "seoul-fukuoka", D + timedelta(days=1), "bundle", 413113, None, None, rt
        )
        s.finish_run(
            run,
            T0 + timedelta(hours=3 * i, minutes=20),
            "partial",
            {
                "google": {"status": "ok", "requests": 120, "done": 60},
                "naver": {"status": "blocked", "requests": 3, "error": "HTTP 429"},
            },
        )
    return settings, s


def test_context_ranks_dates_and_tracks_change(tmp_path):
    settings, store = populated(tmp_path)
    ctx = build_context(settings, store, now=T0 + timedelta(hours=7))
    route = ctx["routes"][0]
    top = route["top"]
    assert [d["date"] for d in top] == ["2026-11-10", "2026-11-11"]
    assert top[0]["total"] == 380000 and top[0]["change"] == -20000
    assert [leg["flight_no"] for leg in top[0]["legs"]] == ["7C1401", "7C1408"]
    assert [leg["source"] for leg in top[0]["legs"]] == ["google", "naver"]
    assert top[1]["kind"] == "bundle" and len(top[1]["links"]) == 1
    assert [p["total"] for p in route["history"]["2026-11-10"]] == [400000, 380000]
    assert ctx["last_run"]["sources"]["naver"]["status"] == "blocked"
    assert ctx["last_run"]["started_kst"] == "2026-11-10 06:00"


def test_report_is_self_contained_html(tmp_path):
    settings, store = populated(tmp_path)
    path = write_report(settings, store, tmp_path / "report.html", now=T0 + timedelta(hours=7))
    html = path.read_text(encoding="utf-8")
    assert html.startswith("<!doctype html>")
    assert "7C1401" in html and "380,000" in html
    assert not re.search(r"<(script|link)[^>]+(src|href)=\"https?://", html)  # no CDN assets


def test_report_with_no_data(tmp_path):
    settings, _ = populated(tmp_path)
    empty = Store(tmp_path / "empty.db")
    html = write_report(settings, empty, tmp_path / "r.html").read_text(encoding="utf-8")
    assert "No results yet" in html
