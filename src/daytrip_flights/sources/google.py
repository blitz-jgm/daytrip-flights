"""Google Flights via the `faster-flights` library (scrapes the public search page)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, date, datetime

from fast_flights import FlightQuery, Passengers, create_query, get_return_flights, select_flight
from fast_flights.fetcher import fetch_flights_html
from fast_flights.model import Flights, SimpleDatetime
from fast_flights.parser import parse

from daytrip_flights.config import Route
from daytrip_flights.models import FlightOffer, Leg, SearchResult, SearchTask
from daytrip_flights.pacer import Pacer

log = logging.getLogger(__name__)

# A bundle total below the outbound's round-trip "from" price means the library fell back to
# one-way prices. Allow a little slack for rounding between the two pages.
_FROM_PRICE_TOLERANCE = 0.98


def _dt(d: SimpleDatetime) -> datetime:
    y, m, day = d.date
    hm = list(d.time) + [0, 0]
    return datetime(y, m, day, hm[0], hm[1])


def _leg(f: Flights) -> Leg | None:
    """Normalize a direct flight; None for connections or unpriced rows."""
    if len(f.flights) != 1 or f.price <= 0:
        return None
    s = f.flights[0]
    return Leg(
        flight_no=f"{s.airline_code}{s.flight_number}" if s.flight_number else "",
        airline=f.airlines[0] if f.airlines else s.airline_code,
        origin=s.from_airport.code,
        destination=s.to_airport.code,
        departure=_dt(s.departure),
        arrival=_dt(s.arrival),
    )


class GoogleSource:
    name = "google"

    def __init__(
        self,
        *,
        language: str = "ko",
        currency: str = "KRW",
        bundle_outbounds: int = 2,
        fetch_html: Callable = fetch_flights_html,
        get_returns: Callable = get_return_flights,
    ) -> None:
        if currency != "KRW":
            raise ValueError("only KRW is supported (no FX source is configured)")
        self.language = language
        self.currency = currency
        self.bundle_outbounds = bundle_outbounds
        self._fetch_html = fetch_html
        self._get_returns = get_returns

    def _query(self, legs: list[tuple[str, str]], day: date, trip: str):
        return create_query(
            flights=[
                FlightQuery(date=day.isoformat(), from_airport=a, to_airport=b, max_stops=0)
                for a, b in legs
            ],
            seat="economy",
            trip=trip,
            passengers=Passengers(adults=1),
            language=self.language,
            currency=self.currency,
        )

    def _offer(
        self, task: SearchTask, price: int, url: str, out: Leg | None, back: Leg | None
    ) -> FlightOffer:
        return FlightOffer(
            source=self.name,
            route=task.route,
            date=task.date,
            ticket_type="roundtrip" if out and back else "oneway",
            out_leg=out,
            back_leg=back,
            price=price,
            currency=self.currency,
            price_krw=price,
            fx_rate=1.0,
            booking_url=url,
            collected_at=datetime.now(UTC),
        )

    def search(self, task: SearchTask, route: Route, pacer: Pacer) -> SearchResult:
        if task.kind == "bundle":
            return SearchResult(offers=self._bundle(task, route, pacer))
        legs = [(task.origin, route.destination)]
        if task.kind == "back":
            legs = [(route.destination, task.origin)]
        query = self._query(legs, task.date, "one-way")
        flights = parse(pacer.call(self._fetch_html, query))
        offers = []
        for f in flights:
            leg = _leg(f)
            if leg is None or (leg.origin, leg.destination) != legs[0]:
                continue
            out, back = (leg, None) if task.kind == "out" else (None, leg)
            offers.append(self._offer(task, f.price, query.url(), out, back))
        return SearchResult(offers=offers)

    def _bundle(self, task: SearchTask, route: Route, pacer: Pacer) -> list[FlightOffer]:
        query = self._query(
            [(task.origin, route.destination), (route.destination, task.origin)],
            task.date,
            "round-trip",
        )
        outbounds = []
        for f in parse(pacer.call(self._fetch_html, query)):
            leg = _leg(f)
            if leg is None or leg.origin != task.origin:
                continue
            if route.out_window.contains(leg.departure.time()):
                outbounds.append((f, leg))
        outbounds.sort(key=lambda pair: pair[0].price)

        offers = []
        for f, out_leg in outbounds[: self.bundle_outbounds]:
            return_query = select_flight(query, f)
            returns = pacer.call(self._get_returns, return_query)
            floor = f.price * _FROM_PRICE_TOLERANCE
            for r in returns:
                back_leg = _leg(r)
                if back_leg is None or back_leg.origin != route.destination:
                    continue
                if not route.back_window.contains(back_leg.departure.time()):
                    continue
                if r.price < floor:
                    log.warning(
                        "google bundle %s %s: return total %d below from-price %d; "
                        "treating as unverified one-way fallback",
                        task.date,
                        out_leg.flight_no,
                        r.price,
                        f.price,
                    )
                    continue
                offers.append(self._offer(task, r.price, return_query.url(), out_leg, back_leg))
        offers.sort(key=lambda o: o.price)
        return offers
