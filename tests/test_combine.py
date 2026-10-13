from factories import D, bundle, oneway

from daytrip_flights.combine import best_for_date


def test_combo_of_cheapest_in_window_legs_across_airlines_and_sources(route):
    offers = [
        oneway("out", "7C1401", "ICN", "FUK", "06:30", 130500),
        oneway("out", "TW205", "ICN", "FUK", "14:45", 90000),  # cheaper but out of window
        oneway("out", "RS433", "ICN", "FUK", "07:20", 139300, source="naver"),
        oneway("back", "7C1408", "FUK", "ICN", "18:55", 233303),
        oneway("back", "TW208", "FUK", "ICN", "20:20", 220000, source="naver"),
        oneway("back", "KE782", "FUK", "ICN", "21:05", 100000),  # 21:05 > 21:00
    ]
    best = best_for_date(route, D, offers)
    assert best.kind == "combo"
    assert best.total_krw == 130500 + 220000
    assert best.out.out_leg.flight_no == "7C1401"
    assert best.back.back_leg.flight_no == "TW208" and best.back.source == "naver"


def test_bundle_wins_when_cheaper(route):
    offers = [
        oneway("out", "7C1401", "ICN", "FUK", "06:30", 200000),
        oneway("back", "7C1408", "FUK", "ICN", "18:55", 233303),
        bundle("BX445", "07:25", "TW208", "20:20", 413113),
    ]
    best = best_for_date(route, D, offers)
    assert best.kind == "bundle" and best.total_krw == 413113 and best.out is None


def test_missing_direction_falls_back_to_bundle_or_none(route):
    only_out = [oneway("out", "7C1401", "ICN", "FUK", "06:30", 130500)]
    assert best_for_date(route, D, only_out) is None
    assert (
        best_for_date(route, D, only_out + [bundle("BX445", "07:25", "TW208", "20:20", 5)]).kind
        == "bundle"
    )


def test_unconverted_currency_is_excluded(route):
    offers = [
        oneway("out", "7C1401", "ICN", "FUK", "06:30", 1, krw=False),
        oneway("back", "7C1408", "FUK", "ICN", "18:55", 233303),
    ]
    assert best_for_date(route, D, offers) is None


def test_mixed_seoul_airports_are_allowed(route):
    offers = [
        oneway("out", "KE2131", "GMP", "FUK", "08:00", 100000),
        oneway("back", "7C1408", "FUK", "ICN", "18:55", 100000),
    ]
    assert best_for_date(route, D, offers).total_krw == 200000
