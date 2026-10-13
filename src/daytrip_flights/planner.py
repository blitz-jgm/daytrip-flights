"""Decide which (source, route, origin, date, kind) searches a run performs.

Tiering keeps the request volume polite: a full scan of every date at most once per
`full_scan_every_hours`, and in between only the next few days plus the currently cheapest
dates. Round-trip bundles are priced only for the top dates found by the one-way phase.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from daytrip_flights.combine import best_for_date
from daytrip_flights.config import Route, Settings
from daytrip_flights.models import SearchTask
from daytrip_flights.store import Store


def choose_mode(settings: Settings, store: Store, now: datetime) -> str:
    last = store.last_full_scan()
    if last is None or now - last >= timedelta(hours=settings.tiers.full_scan_every_hours):
        return "full"
    return "refresh"


def route_dates(
    settings: Settings,
    store: Store,
    route: Route,
    mode: str,
    today: date,
    days_ahead: int | None = None,
) -> list[date]:
    horizon = days_ahead or route.days_ahead
    every = [today + timedelta(days=i) for i in range(horizon)]
    if mode == "full":
        return every
    near = set(every[: settings.tiers.refresh_next_days])
    known = [b for b in store.latest_best(route.name, since=today) if b["date"] in set(every)]
    known.sort(key=lambda b: b["total_krw"])
    top = {b["date"] for b in known[: settings.tiers.refresh_top_dates]}
    return sorted(near | top)


def _served(settings: Settings, store: Store, source: str, a: str, b: str, now: datetime) -> bool:
    return not store.no_service(source, a, b, now, settings.tiers.no_service_cache_days)


def plan_oneway(
    settings: Settings,
    store: Store,
    mode: str,
    today: date,
    now: datetime,
    sources: list[str],
    days_ahead: int | None = None,
) -> list[SearchTask]:
    tasks = []
    for source in sources:
        for route in settings.routes:
            for day in route_dates(settings, store, route, mode, today, days_ahead):
                for origin in route.origins:
                    for kind, (a, b) in (
                        ("out", (origin, route.destination)),
                        ("back", (route.destination, origin)),
                    ):
                        if _served(settings, store, source, a, b, now):
                            tasks.append(
                                SearchTask(
                                    source=source,
                                    route=route.name,
                                    origin=origin,
                                    date=day,
                                    kind=kind,
                                )
                            )
    return tasks


def plan_bundles(
    settings: Settings, store: Store, run_id: int, now: datetime, sources: list[str]
) -> list[SearchTask]:
    """Bundle searches for the cheapest dates found so far in this run."""
    tasks = []
    for route in settings.routes:
        offers = [o for _, o in store.offers_for_run(run_id, route.name)]
        bests = [
            b
            for d in store.covered_dates(run_id, route.name)
            if (b := best_for_date(route, d, offers)) is not None
        ]
        bests.sort(key=lambda b: b.total_krw)
        for b in bests[: settings.tiers.bundle_top_dates]:
            # only origins that actually have an in-window outbound flight that day
            flying = {o.out_leg.origin for o in offers if o.date == b.date and o.out_leg}
            for source in sources:
                for origin in [o for o in route.origins if o in flying]:
                    if _served(settings, store, source, origin, route.destination, now) and _served(
                        settings, store, source, route.destination, origin, now
                    ):
                        tasks.append(
                            SearchTask(
                                source=source,
                                route=route.name,
                                origin=origin,
                                date=b.date,
                                kind="bundle",
                            )
                        )
    return tasks
