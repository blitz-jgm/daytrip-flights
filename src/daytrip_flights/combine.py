"""Pick a date's cheapest valid day trip: round-trip bundle vs. two one-way tickets."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal

from daytrip_flights.config import Route
from daytrip_flights.models import FlightOffer


@dataclass(frozen=True)
class DailyBest:
    route: str
    date: date
    kind: Literal["combo", "bundle"]
    total_krw: int
    out: FlightOffer | None = None  # combo: outbound one-way ticket
    back: FlightOffer | None = None  # combo: return one-way ticket
    bundle: FlightOffer | None = None


def _out_ok(route: Route, o: FlightOffer) -> bool:
    leg = o.out_leg
    return (
        leg is not None
        and leg.origin in route.origins
        and leg.destination == route.destination
        and route.out_window.contains(leg.departure.time())
    )


def _back_ok(route: Route, o: FlightOffer) -> bool:
    leg = o.back_leg
    return (
        leg is not None
        and leg.origin == route.destination
        and leg.destination in route.origins
        and route.back_window.contains(leg.departure.time())
    )


def in_window(route: Route, o: FlightOffer) -> bool:
    """Whether every leg of the offer fits the route's time windows."""
    if o.ticket_type == "roundtrip":
        return _out_ok(route, o) and _back_ok(route, o)
    return _out_ok(route, o) if o.out_leg else _back_ok(route, o)


def best_for_date(route: Route, day: date, offers: list[FlightOffer]) -> DailyBest | None:
    """Cheapest of (a) the cheapest bundle and (b) cheapest out + cheapest back one-way.

    Offers without a KRW price are ignored (fail closed). Different airlines, sources and
    Seoul airports may be mixed in a one-way combo. Ties go to the bundle (one booking).
    """
    priced = [
        o for o in offers if o.date == day and o.price_krw is not None and in_window(route, o)
    ]
    outs = [o for o in priced if o.ticket_type == "oneway" and o.out_leg]
    backs = [o for o in priced if o.ticket_type == "oneway" and o.back_leg]
    bundles = [o for o in priced if o.ticket_type == "roundtrip"]

    candidates: list[DailyBest] = []
    if bundles:
        b = min(bundles, key=lambda o: o.price_krw)
        candidates.append(DailyBest(route.name, day, "bundle", b.price_krw, bundle=b))
    if outs and backs:
        o = min(outs, key=lambda x: x.price_krw)
        r = min(backs, key=lambda x: x.price_krw)
        candidates.append(DailyBest(route.name, day, "combo", o.price_krw + r.price_krw, o, r))
    if not candidates:
        return None
    return min(candidates, key=lambda c: (c.total_krw, c.kind != "bundle"))
