from __future__ import annotations

from typing import Protocol

from daytrip_flights.config import Route
from daytrip_flights.models import SearchResult, SearchTask
from daytrip_flights.pacer import Pacer


class Source(Protocol):
    """A flight data source.

    One-way tasks return every direct flight of the day; the caller applies time windows.
    Bundle tasks return only round-trip offers whose both legs are inside the route windows,
    because picking which outbound to price is part of the search itself.
    A source that cannot price bundles returns an empty result for them.
    All network calls must go through `pacer`.
    """

    name: str

    def search(self, task: SearchTask, route: Route, pacer: Pacer) -> SearchResult: ...
