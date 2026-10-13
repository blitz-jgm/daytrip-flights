"""SQLite persistence. Everything is append-only per run so price history is preserved."""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from daytrip_flights.models import FlightOffer, Leg, SearchTask

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,            -- running | done | partial | failed
    mode TEXT NOT NULL,              -- full | refresh | limited
    requests_total INTEGER NOT NULL DEFAULT 0,
    sources_json TEXT                -- per-source status, requests, task counts
);
CREATE TABLE IF NOT EXISTS run_tasks (
    run_id INTEGER NOT NULL REFERENCES runs(id),
    source TEXT NOT NULL,
    route TEXT NOT NULL,
    origin TEXT NOT NULL,
    date TEXT NOT NULL,
    kind TEXT NOT NULL,              -- out | back | bundle
    status TEXT NOT NULL,            -- pending | done | no_service | failed | skipped
    error TEXT,
    requests INTEGER NOT NULL DEFAULT 0,
    offers INTEGER NOT NULL DEFAULT 0,
    seq INTEGER NOT NULL,
    PRIMARY KEY (run_id, source, route, origin, date, kind)
);
CREATE TABLE IF NOT EXISTS offers (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    source TEXT NOT NULL,
    route TEXT NOT NULL,
    date TEXT NOT NULL,
    ticket_type TEXT NOT NULL,       -- oneway | roundtrip
    direction TEXT NOT NULL,         -- out | back | both
    out_json TEXT,
    back_json TEXT,
    flight_nos TEXT NOT NULL,        -- e.g. "7C1401" or "BX445+TW208"
    airlines TEXT NOT NULL,
    price INTEGER NOT NULL,
    currency TEXT NOT NULL,
    price_krw INTEGER,
    fx_rate REAL,
    booking_url TEXT,
    collected_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS offers_route_date ON offers(route, date, collected_at);
CREATE INDEX IF NOT EXISTS offers_run ON offers(run_id, route);
CREATE TABLE IF NOT EXISTS daily_best (
    run_id INTEGER NOT NULL REFERENCES runs(id),
    route TEXT NOT NULL,
    date TEXT NOT NULL,
    kind TEXT NOT NULL,              -- combo | bundle
    total_krw INTEGER NOT NULL,
    out_offer_id INTEGER REFERENCES offers(id),
    back_offer_id INTEGER REFERENCES offers(id),
    bundle_offer_id INTEGER REFERENCES offers(id),
    PRIMARY KEY (run_id, route, date)
);
CREATE TABLE IF NOT EXISTS route_service (
    source TEXT NOT NULL,
    origin TEXT NOT NULL,
    destination TEXT NOT NULL,
    has_direct INTEGER NOT NULL,
    checked_at TEXT NOT NULL,
    PRIMARY KEY (source, origin, destination)
);
"""


class Store:
    """One SQLite connection. It may be used from several threads, but only one at a time:
    callers that share it across threads must serialize access themselves (see Runner)."""

    def __init__(self, path: str | Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        # wait for another process's write (e.g. `daytrip status`) instead of failing
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    # ---- runs -------------------------------------------------------------------------------

    def start_run(self, mode: str, now: datetime) -> int:
        with self.db:
            cur = self.db.execute(
                "INSERT INTO runs(started_at, status, mode) VALUES (?, 'running', ?)",
                (now.isoformat(), mode),
            )
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, now: datetime, status: str, sources: dict[str, Any]) -> None:
        total = sum(int(s.get("requests", 0)) for s in sources.values())
        with self.db:
            self.db.execute(
                "UPDATE runs SET finished_at=?, status=?, requests_total=?, sources_json=? "
                "WHERE id=?",
                (now.isoformat(), status, total, json.dumps(sources), run_id),
            )

    def resumable_run(self, now: datetime, max_age_hours: float) -> int | None:
        """The latest run if it was interrupted (still 'running') and is recent enough."""
        row = self.db.execute(
            "SELECT id, status, started_at FROM runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None or row["status"] != "running":
            return None
        if now - datetime.fromisoformat(row["started_at"]) > timedelta(hours=max_age_hours):
            return None
        return int(row["id"])

    def abandon_stale_runs(self, now: datetime) -> None:
        with self.db:
            self.db.execute(
                "UPDATE runs SET status='failed', finished_at=? WHERE status='running'",
                (now.isoformat(),),
            )

    def run_mode(self, run_id: int) -> str:
        return self.db.execute("SELECT mode FROM runs WHERE id=?", (run_id,)).fetchone()["mode"]

    def last_full_scan(self) -> datetime | None:
        row = self.db.execute(
            "SELECT started_at FROM runs WHERE mode='full' AND status IN ('done', 'partial') "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return datetime.fromisoformat(row["started_at"]) if row else None

    def recent_runs(self, limit: int = 10) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))
        out = []
        for r in rows:
            d = dict(r)
            d["sources"] = json.loads(d.pop("sources_json") or "{}")
            out.append(d)
        return out

    # ---- tasks ------------------------------------------------------------------------------

    def add_tasks(self, run_id: int, tasks: list[SearchTask]) -> None:
        start = self.db.execute(
            "SELECT COUNT(*) FROM run_tasks WHERE run_id=?", (run_id,)
        ).fetchone()[0]
        with self.db:
            self.db.executemany(
                "INSERT OR IGNORE INTO run_tasks(run_id, source, route, origin, date, kind, "
                "status, seq) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)",
                [(run_id, *t.key, start + i) for i, t in enumerate(tasks)],
            )

    def has_tasks(self, run_id: int, kind: str) -> bool:
        return (
            self.db.execute(
                "SELECT 1 FROM run_tasks WHERE run_id=? AND kind=? LIMIT 1", (run_id, kind)
            ).fetchone()
            is not None
        )

    def open_tasks(
        self, run_id: int, kinds: tuple[str, ...] = ("out", "back", "bundle")
    ) -> list[SearchTask]:
        marks = ",".join("?" * len(kinds))
        rows = self.db.execute(
            f"SELECT source, route, origin, date, kind FROM run_tasks WHERE run_id=? "
            f"AND kind IN ({marks}) AND status NOT IN ('done', 'no_service') ORDER BY seq",
            (run_id, *kinds),
        )
        return [
            SearchTask(
                source=r["source"],
                route=r["route"],
                origin=r["origin"],
                date=date.fromisoformat(r["date"]),
                kind=r["kind"],
            )
            for r in rows
        ]

    def finish_task(
        self,
        run_id: int,
        task: SearchTask,
        status: str,
        *,
        error: str | None = None,
        requests: int = 0,
        offers: int = 0,
    ) -> None:
        with self.db:
            self.db.execute(
                "UPDATE run_tasks SET status=?, error=?, requests=requests+?, offers=? "
                "WHERE run_id=? AND source=? AND route=? AND origin=? AND date=? AND kind=?",
                (status, error, requests, offers, run_id, *task.key),
            )

    def task_counts(self, run_id: int) -> dict[str, dict[str, int]]:
        rows = self.db.execute(
            "SELECT source, status, COUNT(*) n, SUM(requests) r FROM run_tasks WHERE run_id=? "
            "GROUP BY source, status",
            (run_id,),
        )
        out: dict[str, dict[str, int]] = {}
        for r in rows:
            s = out.setdefault(r["source"], {"requests": 0})
            s[r["status"]] = r["n"]
            s["requests"] += r["r"] or 0
        return out

    def covered_dates(self, run_id: int, route: str) -> list[date]:
        rows = self.db.execute(
            "SELECT DISTINCT date FROM run_tasks WHERE run_id=? AND route=? AND status='done' "
            "ORDER BY date",
            (run_id, route),
        )
        return [date.fromisoformat(r["date"]) for r in rows]

    # ---- offers -----------------------------------------------------------------------------

    def add_offers(self, run_id: int, offers: list[FlightOffer]) -> list[int]:
        ids = []
        with self.db:
            for o in offers:
                legs = o.legs
                direction = "both" if len(legs) == 2 else ("out" if o.out_leg else "back")
                cur = self.db.execute(
                    "INSERT INTO offers(run_id, source, route, date, ticket_type, direction, "
                    "out_json, back_json, flight_nos, airlines, price, currency, price_krw, "
                    "fx_rate, booking_url, collected_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        run_id,
                        o.source,
                        o.route,
                        o.date.isoformat(),
                        o.ticket_type,
                        direction,
                        o.out_leg.model_dump_json() if o.out_leg else None,
                        o.back_leg.model_dump_json() if o.back_leg else None,
                        "+".join(leg.flight_no for leg in legs),
                        "+".join(leg.airline for leg in legs),
                        o.price,
                        o.currency,
                        o.price_krw,
                        o.fx_rate,
                        o.booking_url,
                        o.collected_at.isoformat(),
                    ),
                )
                ids.append(int(cur.lastrowid))
        return ids

    @staticmethod
    def _offer(row: sqlite3.Row) -> FlightOffer:
        return FlightOffer(
            source=row["source"],
            route=row["route"],
            date=date.fromisoformat(row["date"]),
            ticket_type=row["ticket_type"],
            out_leg=Leg.model_validate_json(row["out_json"]) if row["out_json"] else None,
            back_leg=Leg.model_validate_json(row["back_json"]) if row["back_json"] else None,
            price=row["price"],
            currency=row["currency"],
            price_krw=row["price_krw"],
            fx_rate=row["fx_rate"],
            booking_url=row["booking_url"],
            collected_at=datetime.fromisoformat(row["collected_at"]),
        )

    def offers_for_run(self, run_id: int, route: str) -> list[tuple[int, FlightOffer]]:
        rows = self.db.execute(
            "SELECT * FROM offers WHERE run_id=? AND route=? ORDER BY id", (run_id, route)
        )
        return [(int(r["id"]), self._offer(r)) for r in rows]

    def offer(self, offer_id: int | None) -> FlightOffer | None:
        if offer_id is None:
            return None
        row = self.db.execute("SELECT * FROM offers WHERE id=?", (offer_id,)).fetchone()
        return self._offer(row) if row else None

    # ---- daily best -------------------------------------------------------------------------

    def add_daily_best(
        self,
        run_id: int,
        route: str,
        day: date,
        kind: str,
        total_krw: int,
        out_id: int | None,
        back_id: int | None,
        bundle_id: int | None,
    ) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO daily_best VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, route, day.isoformat(), kind, total_krw, out_id, back_id, bundle_id),
            )

    def latest_best(self, route: str, since: date | None = None) -> list[dict[str, Any]]:
        """Most recent snapshot per date (from the latest run that covered that date)."""
        rows = self.db.execute(
            "SELECT b.*, r.started_at FROM daily_best b JOIN runs r ON r.id = b.run_id "
            "WHERE b.route=? AND b.date >= ? AND b.run_id = ("
            "  SELECT MAX(run_id) FROM daily_best b2 WHERE b2.route=b.route AND b2.date=b.date)"
            " ORDER BY b.date",
            (route, (since or date.min).isoformat()),
        )
        return [{**dict(r), "date": date.fromisoformat(r["date"])} for r in rows]

    def best_history(self, route: str, since: date | None = None) -> list[dict[str, Any]]:
        """Every snapshot, for price-trend charts."""
        rows = self.db.execute(
            "SELECT b.date, b.total_krw, b.kind, r.started_at FROM daily_best b "
            "JOIN runs r ON r.id = b.run_id WHERE b.route=? AND b.date >= ? "
            "ORDER BY b.date, b.run_id",
            (route, (since or date.min).isoformat()),
        )
        return [{**dict(r), "date": date.fromisoformat(r["date"])} for r in rows]

    def cheapest_per_run(self, route: str) -> list[dict[str, Any]]:
        """The cheapest date of every finished run (smoke tests excluded), oldest first."""
        # SQLite takes the bare columns from the row that holds MIN()
        rows = self.db.execute(
            "SELECT r.started_at, b.date, b.kind, MIN(b.total_krw) AS total_krw FROM runs r "
            "JOIN daily_best b ON b.run_id = r.id WHERE b.route=? AND r.mode != 'limited' "
            "AND r.status IN ('done', 'partial') GROUP BY r.id ORDER BY r.started_at",
            (route,),
        )
        return [{**dict(r), "date": date.fromisoformat(r["date"])} for r in rows]

    # ---- route service cache ----------------------------------------------------------------

    def set_service(
        self, source: str, origin: str, destination: str, has_direct: bool, now: datetime
    ) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO route_service VALUES (?, ?, ?, ?, ?)",
                (source, origin, destination, int(has_direct), now.isoformat()),
            )

    def no_service(
        self, source: str, origin: str, destination: str, now: datetime, cache_days: int
    ) -> bool:
        row = self.db.execute(
            "SELECT has_direct, checked_at FROM route_service "
            "WHERE source=? AND origin=? AND destination=?",
            (source, origin, destination),
        ).fetchone()
        if row is None or row["has_direct"]:
            return False
        return now - datetime.fromisoformat(row["checked_at"]) < timedelta(days=cache_days)
