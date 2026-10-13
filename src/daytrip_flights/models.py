"""Normalized data shared by every source."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel

Kind = Literal["out", "back", "bundle"]
TicketType = Literal["oneway", "roundtrip"]


class Leg(BaseModel):
    """One direct flight. Times are airport-local (KST and JST are both UTC+9)."""

    flight_no: str  # e.g. "7C1401"; empty if the source does not expose it
    airline: str
    origin: str
    destination: str
    departure: datetime
    arrival: datetime


class FlightOffer(BaseModel):
    source: str
    route: str
    date: date
    ticket_type: TicketType
    out_leg: Leg | None = None  # set for oneway-out and roundtrip
    back_leg: Leg | None = None  # set for oneway-back and roundtrip
    price: int  # in `currency`
    currency: str
    price_krw: int | None = None  # None when we could not convert -> excluded from ranking
    fx_rate: float | None = None  # KRW per 1 unit of `currency`
    booking_url: str | None = None
    collected_at: datetime

    @property
    def legs(self) -> list[Leg]:
        return [leg for leg in (self.out_leg, self.back_leg) if leg is not None]


class SearchTask(BaseModel):
    """One unit of resumable work.

    kind="out":    origin -> route.destination on `date`
    kind="back":   route.destination -> origin on `date`
    kind="bundle": round trip origin <-> route.destination, both legs on `date`
    """

    source: str
    route: str
    origin: str
    date: date
    kind: Kind

    @property
    def key(self) -> tuple[str, str, str, str, str]:
        return (self.source, self.route, self.origin, self.date.isoformat(), self.kind)


class SearchResult(BaseModel):
    """`offers` are every direct flight the source returned for the task (before time-window
    filtering for one-way tasks), so the caller can also tell "no service" from "no fit"."""

    offers: list[FlightOffer]
