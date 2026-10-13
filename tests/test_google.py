from datetime import datetime

from conftest import FIXTURE_DATE, fixture_text
from fast_flights.parser import parse

from daytrip_flights.models import SearchTask
from daytrip_flights.sources.google import GoogleSource


def html_by_route(query) -> str:
    """Fake fetcher: serve the saved page matching the query's legs."""
    legs = [(f.from_airport, f.to_airport) for f in query._flights]
    name = {
        (("ICN", "FUK"),): "oneway_ICN_FUK",
        (("FUK", "ICN"),): "oneway_FUK_ICN",
        (("GMP", "FUK"),): "oneway_GMP_FUK",
        (("ICN", "FUK"), ("FUK", "ICN")): "rt_outbound_ICN_FUK",
    }[tuple(legs)]
    return fixture_text("google", f"{name}.html")


def task(kind: str, origin: str = "ICN") -> SearchTask:
    return SearchTask(
        source="google", route="seoul-fukuoka", origin=origin, date=FIXTURE_DATE, kind=kind
    )


def make_source(returns_fixture: str = "rt_return_ICN_FUK", outbounds: int = 2) -> GoogleSource:
    return GoogleSource(
        fetch_html=html_by_route,
        get_returns=lambda q: parse(fixture_text("google", f"{returns_fixture}.html")),
        bundle_outbounds=outbounds,
    )


def test_outbound_returns_all_direct_flights_normalized(route, pacer):
    offers = make_source().search(task("out"), route, pacer).offers
    assert len(offers) == 14
    first = min(offers, key=lambda o: o.price)
    assert first.ticket_type == "oneway" and first.back_leg is None
    assert first.out_leg.flight_no == "7C1401"
    assert first.out_leg.origin == "ICN" and first.out_leg.destination == "FUK"
    assert first.out_leg.departure == datetime(2026, 11, 10, 6, 30)
    assert first.price == first.price_krw == 130500 and first.currency == "KRW"
    assert first.booking_url.startswith("https://www.google.com/travel/flights")
    assert pacer.requests == 1


def test_back_task_queries_reverse_direction(route, pacer):
    offers = make_source().search(task("back"), route, pacer).offers
    assert len(offers) == 16
    assert all(o.back_leg and o.back_leg.origin == "FUK" and o.out_leg is None for o in offers)


def test_route_without_service_is_empty(route, pacer):
    assert make_source().search(task("out", origin="GMP"), route, pacer).offers == []


def test_bundle_keeps_only_in_window_itineraries(route, pacer):
    offers = make_source(outbounds=1).search(task("bundle"), route, pacer).offers
    # cheapest in-window outbound is BX445 07:25; in-window returns are TW208 20:20, LJ462 20:05
    assert [(o.out_leg.flight_no, o.back_leg.flight_no, o.price) for o in offers] == [
        ("BX445", "TW208", 413113),
        ("BX445", "LJ462", 438445),
    ]
    assert all(o.ticket_type == "roundtrip" for o in offers)
    assert pacer.requests == 2  # outbound page + one return page


def test_bundle_rejects_one_way_fallback_prices(route, pacer):
    """If the library silently falls back to one-way prices, totals drop below the round-trip
    'from' price; those must not be reported as bundle prices."""
    offers = (
        make_source(returns_fixture="oneway_FUK_ICN", outbounds=1)
        .search(task("bundle"), route, pacer)
        .offers
    )
    assert offers == []


def test_google_declares_bundle_support():
    assert GoogleSource.supports_bundle is True
