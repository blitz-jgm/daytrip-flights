"""Static, self-contained HTML report (no server, no CDN)."""

from __future__ import annotations

from collections import defaultdict
from statistics import median
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from jinja2 import Environment, PackageLoader, select_autoescape

from daytrip_flights.config import Settings
from daytrip_flights.models import FlightOffer
from daytrip_flights.store import Store

TOP_N = 10


def _kst(iso: str | None, tz: ZoneInfo) -> str | None:
    return datetime.fromisoformat(iso).astimezone(tz).strftime("%Y-%m-%d %H:%M") if iso else None


def _legs(offer: FlightOffer | None, with_price: bool) -> list[dict[str, Any]]:
    if offer is None:
        return []
    return [
        {
            "flight_no": leg.flight_no or "?",
            "airline": leg.airline,
            "origin": leg.origin,
            "destination": leg.destination,
            "dep": f"{leg.departure:%H:%M}",
            "arr": f"{leg.arrival:%H:%M}",
            "source": offer.source,
            "price": offer.price_krw if with_price else None,
        }
        for leg in offer.legs
    ]


def _entry(store: Store, b: dict[str, Any]) -> dict[str, Any]:
    out, back = store.offer(b["out_offer_id"]), store.offer(b["back_offer_id"])
    rt = store.offer(b["bundle_offer_id"])
    if b["kind"] == "bundle":
        legs = _legs(rt, with_price=False)
        links = [{"label": f"Round trip · {rt.source}", "url": rt.booking_url}] if rt else []
    else:
        legs = _legs(out, True) + _legs(back, True)
        links = [
            {"label": f"{name} · {o.source}", "url": o.booking_url}
            for name, o in (("Outbound", out), ("Return", back))
            if o
        ]
    return {
        "date": b["date"].isoformat(),
        "weekday": f"{b['date']:%a}",
        "total": b["total_krw"],
        "kind": b["kind"],
        "legs": legs,
        "links": [lk for lk in links if lk["url"]],
    }


def _cheapest(store: Store, route: str, tz: ZoneInfo) -> dict[str, Any] | None:
    runs = [
        {
            "t": _kst(r["started_at"], tz),
            "total": r["total_krw"],
            "date": r["date"].isoformat(),
            "weekday": f"{r['date']:%a}",
            "kind": r["kind"],
        }
        for r in store.cheapest_per_run(route)
    ]
    if not runs:
        return None
    totals = [r["total"] for r in runs]
    return {
        "runs": runs,
        "latest": totals[-1],
        "low": min(totals),
        "median": round(median(totals)),
    }


def build_context(settings: Settings, store: Store, now: datetime | None = None) -> dict:
    tz = ZoneInfo(settings.timezone)
    now = now or datetime.now(UTC)
    today = now.astimezone(tz).date()
    routes = []
    for route in settings.routes:
        history: dict[str, list[dict]] = defaultdict(list)
        for h in store.best_history(route.name, since=today):
            history[h["date"].isoformat()].append(
                {"t": _kst(h["started_at"], tz), "total": h["total_krw"], "kind": h["kind"]}
            )
        days = [_entry(store, b) for b in store.latest_best(route.name, since=today)]
        for d in days:
            h = history[d["date"]]
            d["change"] = h[-1]["total"] - h[-2]["total"] if len(h) >= 2 else None
        routes.append(
            {
                "name": route.name,
                "origins": route.origins,
                "destination": route.destination,
                "out_window": str(route.out_window),
                "back_window": str(route.back_window),
                "start": today.isoformat(),
                "days_ahead": route.days_ahead,
                "days": days,
                "top": sorted(days, key=lambda d: (d["total"], d["date"]))[:TOP_N],
                "history": dict(history),
                "cheapest": _cheapest(store, route.name, tz),
            }
        )
    runs = store.recent_runs(1)
    last = None
    if runs:
        r = runs[0]
        last = {
            **r,
            "started_kst": _kst(r["started_at"], tz),
            "finished_kst": _kst(r["finished_at"], tz),
        }
    return {
        "routes": routes,
        "last_run": last,
        "generated_kst": _kst(now.isoformat(), tz),
        "timezone": settings.timezone,
    }


def write_report(
    settings: Settings, store: Store, path: str | Path, now: datetime | None = None
) -> Path:
    env = Environment(
        loader=PackageLoader("daytrip_flights", "report"),
        autoescape=select_autoescape(["html", "j2"]),
    )
    env.filters["krw"] = lambda v: f"{v:,}"
    html = env.get_template("template.html.j2").render(**build_context(settings, store, now))
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(html, encoding="utf-8")
    tmp.replace(path)  # atomic: a browser never sees a half-written report
    return path
