import json
from datetime import datetime

import httpx
import pytest
from conftest import FIXTURE_DATE, fixture_text

from daytrip_flights.models import SearchTask
from daytrip_flights.pacer import SourceBlocked
from daytrip_flights.sources.naver import NaverSource


def task(kind: str, origin: str = "ICN") -> SearchTask:
    return SearchTask(
        source="naver", route="seoul-fukuoka", origin=origin, date=FIXTURE_DATE, kind=kind
    )


def fixture_server(requests: list):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append((request, body))
        leg = body["itineraries"][0]
        name = f"ow_{leg['departureLocationCode']}_{leg['arrivalLocationCode']}"
        return httpx.Response(
            201,
            text=fixture_text("naver", f"{name}.sse"),
            headers={"content-type": "text/event-stream"},
        )

    return handler


def make(requests=None, handler=None) -> NaverSource:
    transport = httpx.MockTransport(
        handler or fixture_server(requests if requests is not None else [])
    )
    return NaverSource(client=httpx.Client(transport=transport))


def test_outbound_parses_final_event_with_general_fares(route, pacer):
    sent = []
    offers = make(sent).search(task("out"), route, pacer).offers
    assert len(offers) == 28
    by_no = {o.out_leg.flight_no: o for o in offers}
    o = by_no["7C1401"]
    assert o.price == o.price_krw == 124690 and o.currency == "KRW"
    assert o.out_leg.departure == datetime(2026, 11, 10, 6, 30)
    assert o.out_leg.origin == "ICN" and o.out_leg.destination == "FUK"
    assert "flight.naver.com/flights/international/ICN-FUK-20261110" in o.booking_url
    # card-only discount (167,500) is ignored; general adult fare is used
    assert by_no["LJ421"].price == 171758
    assert "RS433" in by_no  # leading zeros of "0433" are dropped
    request, body = sent[0]
    assert body["isNonstop"] is True and body["tripType"] == "OW"
    assert body["itineraries"][0]["departureDate"] == "20261110"
    assert request.headers["accept-language"].startswith("ko")
    assert pacer.requests == 1


def test_back_task_queries_reverse_direction(route, pacer):
    offers = make().search(task("back"), route, pacer).offers
    assert len(offers) == 35
    assert {o.back_leg.flight_no: o.price for o in offers}["TW208"] == 217624
    assert all(o.out_leg is None for o in offers)


def test_route_without_service_is_empty(route, pacer):
    assert make().search(task("out", origin="GMP"), route, pacer).offers == []


def test_bundles_are_not_supported(route, pacer):
    assert NaverSource.supports_bundle is False
    assert make().search(task("bundle"), route, pacer).offers == []
    assert pacer.requests == 0


def test_rate_limit_opens_circuit(route, pacer):
    src = make(handler=lambda r: httpx.Response(429, text="slow down"))
    with pytest.raises(SourceBlocked):
        src.search(task("out"), route, pacer)


def test_incomplete_stream_is_an_error(route, pacer):
    """A stream that ends before Naver reports completion may miss cheaper partners."""
    first = fixture_text("naver", "ow_GMP_FUK.sse").split("\n\n")[0]
    partial = first.replace('"isCompleted":true', '"isCompleted":false')
    src = make(handler=lambda r: httpx.Response(201, text=partial + "\n\n"))
    with pytest.raises(ValueError, match="incomplete"):
        src.search(task("out"), route, pacer)
