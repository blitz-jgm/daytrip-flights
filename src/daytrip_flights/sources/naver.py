"""Naver Flights (flight.naver.com) via the JSON event stream its web page uses.

There is no public API. The page POSTs a search to `searchFlights`, which streams
server-sent events while ~20 booking partners answer; each event carries the full state so
far, and the last one has `status.isCompleted = true`. One search = one request.

Only the general adult fare (`fareType == "A01"`, any payment method) is used. Card- or
pay-specific discounts (`A01/Bxx`) are ignored so the price is one anyone can book.
Round-trip bundles are not searched: Naver caps round-trip results at the cheapest 200 pairs
and its time filter is coarse, so in-window same-day pairs are usually not returned.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

import httpx

from daytrip_flights.config import Route
from daytrip_flights.models import FlightOffer, Leg, SearchResult, SearchTask
from daytrip_flights.pacer import Pacer

log = logging.getLogger(__name__)

SEARCH_URL = "https://flight-api.naver.com/flight/international/searchFlights"
PAGE_URL = (
    "https://flight.naver.com/flights/international/{a}-{b}-{d}?adult=1&isDirect=true&fareType=Y"
)
GENERAL_FARE = "A01"
HEADERS = {
    "user-agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36"
    ),
    # Requests without a browser-like accept-language were answered with HTTP 503.
    "accept-language": "ko-KR,ko;q=0.9,en;q=0.8",
    "accept": "text/event-stream",
    "content-type": "application/json",
    "origin": "https://flight.naver.com",
}


def _body(a: str, b: str, day: str) -> dict[str, Any]:
    return {
        "tripType": "OW",
        "device": "pc",
        "seatClass": "Y",
        "adultCount": 1,
        "childCount": 0,
        "infantCount": 0,
        "isNonstop": True,
        "openReturnDays": 0,
        "initialRequest": True,
        "itineraries": [
            {
                "departureLocationCode": a,
                "arrivalLocationCode": b,
                "departureLocationType": "airport",
                "arrivalLocationType": "airport",
                "departureDate": day,
            }
        ],
        "flightFilter": {
            "filter": {
                "airlines": [],
                "departureAirports": [],
                "arrivalAirports": [],
                "departureTime": [],
                "fareTypes": [],
                "flightDurationSeconds": [],
                "hasCardBenefit": True,
                "isIndividual": False,
                "isLowCarbonEmission": False,
                "isSameAirlines": False,
                "isSameDepArrAirport": False,
                "isTravelClub": False,
                "minFare": {},
                "viaCount": [],
                "selectedItineraries": [],
            },
            "limit": 200,
            "skip": 0,
            "sort": {"adultMinFare": 1},
        },
    }


def parse_stream(lines) -> dict[str, Any]:
    """Return the last event of the stream; it must report completion."""
    last = None
    for line in lines:
        if line.startswith("data: "):
            last = json.loads(line[6:])
    if last is None or not last.get("status", {}).get("isCompleted"):
        raise ValueError("naver: incomplete search stream (not all partners answered)")
    return last


def _dt(point: dict[str, str]) -> datetime:
    # `dateTime` is missing for some airports; `date` + `time` is always present
    return datetime.strptime(point["date"] + point["time"], "%Y%m%d%H%M")


def _flight_no(carrier: dict[str, str]) -> str:
    number = carrier.get("flightNumber", "").lstrip("0")
    return f"{carrier['airlineCode']}{number}" if number else ""


def parse_legs(event: dict[str, Any]) -> list[tuple[Leg, int]]:
    """(direct leg, cheapest general adult fare) for every itinerary with such a fare."""
    names = event.get("status", {}).get("airlinesCodeMap", {})
    itineraries = {i["itineraryId"]: i for i in event.get("itineraries", [])}
    out = []
    for fm in event.get("fareMappings", []):
        itinerary = itineraries.get(fm["itineraryIds"])
        if itinerary is None or len(itinerary["segments"]) != 1:
            continue
        fares = [
            f["adult"]["totalFare"]
            for f in fm["fares"]
            if f.get("fareType") == GENERAL_FARE and f.get("adult", {}).get("totalFare")
        ]
        if not fares:
            continue
        seg = itinerary["segments"][0]
        carrier = seg["marketingCarrier"]
        out.append(
            (
                Leg(
                    flight_no=_flight_no(carrier),
                    airline=names.get(carrier["airlineCode"], carrier["airlineCode"]),
                    origin=seg["departure"]["airportCode"],
                    destination=seg["arrival"]["airportCode"],
                    departure=_dt(seg["departure"]),
                    arrival=_dt(seg["arrival"]),
                ),
                int(min(fares)),
            )
        )
    return out


class NaverSource:
    name = "naver"
    supports_bundle = False

    def __init__(self, client: httpx.Client | None = None, timeout: float = 90.0) -> None:
        self._client = client or httpx.Client(timeout=timeout)

    def _fetch(self, a: str, b: str, day: str) -> dict[str, Any]:
        headers = {**HEADERS, "referer": PAGE_URL.format(a=a, b=b, d=day)}
        with self._client.stream("POST", SEARCH_URL, json=_body(a, b, day), headers=headers) as r:
            r.raise_for_status()
            return parse_stream(r.iter_lines())

    def search(self, task: SearchTask, route: Route, pacer: Pacer) -> SearchResult:
        if task.kind == "bundle":
            return SearchResult(offers=[])
        a, b = task.origin, route.destination
        if task.kind == "back":
            a, b = b, a
        day = f"{task.date:%Y%m%d}"
        event = pacer.call(self._fetch, a, b, day)
        url = PAGE_URL.format(a=a, b=b, d=day)
        now = datetime.now(UTC)
        offers = []
        for leg, price in parse_legs(event):
            if (leg.origin, leg.destination) != (a, b) or leg.departure.date() != task.date:
                continue
            offers.append(
                FlightOffer(
                    source=self.name,
                    route=task.route,
                    date=task.date,
                    ticket_type="oneway",
                    out_leg=leg if task.kind == "out" else None,
                    back_leg=leg if task.kind == "back" else None,
                    price=price,
                    currency="KRW",
                    price_krw=price,
                    fx_rate=1.0,
                    booking_url=url,
                    collected_at=now,
                )
            )
        return SearchResult(offers=offers)
