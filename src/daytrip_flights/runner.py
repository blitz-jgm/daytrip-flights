"""One search run: plan -> one-way phase -> bundle phase -> daily-best snapshot.

Every task outcome is written to SQLite as soon as it finishes, so an interrupted run can be
resumed and will only perform the searches that are still open.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from daytrip_flights import planner
from daytrip_flights.combine import best_for_date, in_window
from daytrip_flights.config import Settings
from daytrip_flights.models import SearchTask
from daytrip_flights.pacer import BudgetExceeded, Pacer, SourceBlocked
from daytrip_flights.sources.base import Source
from daytrip_flights.store import Store

log = logging.getLogger(__name__)


class Runner:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        sources: dict[str, Source],
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], None] = time.sleep,
        days_ahead: int | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.sources = sources
        self.now = now
        self.days_ahead = days_ahead
        b = settings.budget
        self.pacers = {
            name: Pacer(
                name,
                max_requests=b.max_requests_per_run,
                delay=(b.delay_min_s, b.delay_max_s),
                retries=b.retries,
                backoff_base=b.backoff_base_s,
                sleep=sleep,
            )
            for name in sources
        }
        self.routes = {r.name: r for r in settings.routes}
        self.stopped: dict[str, str] = {}  # source -> "blocked" | "budget"
        self.errors: dict[str, str] = {}
        # no-service detection: (source, from, to) -> empty answers / any flight seen this run
        self.empty: dict[tuple[str, str, str], int] = defaultdict(int)
        self.seen: set[tuple[str, str, str]] = set()
        self.dead: set[tuple[str, str, str]] = set()

    # ---- public -----------------------------------------------------------------------------

    def run(self, resume: bool = True) -> int:
        now = self.now()
        today = now.astimezone(ZoneInfo(self.settings.timezone)).date()
        active = [s for s in self.settings.sources if s in self.sources]
        run_id = (
            self.store.resumable_run(now, self.settings.resume_max_age_hours) if resume else None
        )
        if run_id is not None:
            log.info("resuming run %d (%s)", run_id, self.store.run_mode(run_id))
        else:
            self.store.abandon_stale_runs(now)
            mode = planner.choose_mode(self.settings, self.store, now)
            run_id = self.store.start_run(mode, now)
            tasks = planner.plan_oneway(
                self.settings, self.store, mode, today, now, active, self.days_ahead
            )
            self.store.add_tasks(run_id, tasks)
            log.info("run %d: %s scan, %d one-way searches planned", run_id, mode, len(tasks))

        for task in self.store.open_tasks(run_id, ("out", "back")):
            self._execute(run_id, task)

        if not self.store.has_tasks(run_id, "bundle"):
            bundle_sources = [s for s in active if self.sources[s].supports_bundle]
            tasks = planner.plan_bundles(
                self.settings, self.store, run_id, self.now(), bundle_sources
            )
            self.store.add_tasks(run_id, tasks)
            log.info("run %d: %d bundle searches planned", run_id, len(tasks))
        for task in self.store.open_tasks(run_id, ("bundle",)):
            self._execute(run_id, task)

        self._snapshot(run_id)
        self._finish(run_id)
        return run_id

    # ---- internals --------------------------------------------------------------------------

    def _direction(self, task: SearchTask) -> tuple[str, str, str]:
        dest = self.routes[task.route].destination
        if task.kind == "back":
            return (task.source, dest, task.origin)
        return (task.source, task.origin, dest)

    def _execute(self, run_id: int, task: SearchTask) -> None:
        source = self.sources.get(task.source)
        route = self.routes.get(task.route)
        if source is None or route is None:
            self.store.finish_task(run_id, task, "skipped", error="source/route not configured")
            return
        if task.source in self.stopped:
            self.store.finish_task(run_id, task, "skipped", error=self.stopped[task.source])
            return
        key = self._direction(task)
        if key in self.dead:
            self.store.finish_task(run_id, task, "no_service")
            return

        pacer = self.pacers[task.source]
        before = pacer.requests
        try:
            result = source.search(task, route, pacer)
        except SourceBlocked as exc:
            self.stopped[task.source] = "blocked"
            self.errors[task.source] = str(exc)
            log.error("%s blocked; skipping it for the rest of this run: %s", task.source, exc)
            self.store.finish_task(
                run_id, task, "failed", error=str(exc), requests=pacer.requests - before
            )
            return
        except BudgetExceeded as exc:
            self.stopped[task.source] = "budget"
            self.errors[task.source] = str(exc)
            log.warning("%s", exc)
            self.store.finish_task(run_id, task, "skipped", error=str(exc))
            return
        except Exception as exc:
            log.warning(
                "%s %s %s %s failed: %s", task.source, task.kind, task.origin, task.date, exc
            )
            self.store.finish_task(
                run_id,
                task,
                "failed",
                error=f"{type(exc).__name__}: {exc}",
                requests=pacer.requests - before,
            )
            return

        kept = [o for o in result.offers if in_window(route, o)]
        self.store.add_offers(run_id, kept)
        self.store.finish_task(
            run_id, task, "done", requests=pacer.requests - before, offers=len(kept)
        )
        if task.kind != "bundle":
            self._track_service(key, bool(result.offers))

    def _track_service(self, key: tuple[str, str, str], any_flights: bool) -> None:
        source, a, b = key
        if any_flights:
            if key not in self.seen:
                self.seen.add(key)
                self.store.set_service(source, a, b, True, self.now())
            return
        self.empty[key] += 1
        if key not in self.seen and self.empty[key] >= self.settings.tiers.no_service_after_empty:
            log.info(
                "%s: no direct %s->%s flights in %d searches; skipping this direction",
                source,
                a,
                b,
                self.empty[key],
            )
            self.dead.add(key)
            self.store.set_service(source, a, b, False, self.now())

    def _snapshot(self, run_id: int) -> None:
        for route in self.settings.routes:
            loaded = self.store.offers_for_run(run_id, route.name)
            ids = {id(o): i for i, o in loaded}
            offers = [o for _, o in loaded]
            for day in self.store.covered_dates(run_id, route.name):
                best = best_for_date(route, day, offers)
                if best is None:
                    continue
                self.store.add_daily_best(
                    run_id,
                    route.name,
                    day,
                    best.kind,
                    best.total_krw,
                    ids.get(id(best.out)),
                    ids.get(id(best.back)),
                    ids.get(id(best.bundle)),
                )

    def _finish(self, run_id: int) -> None:
        counts = self.store.task_counts(run_id)
        sources = {}
        for name, c in counts.items():
            done = c.get("done", 0) + c.get("no_service", 0)
            total = sum(v for k, v in c.items() if k != "requests")
            if name in self.stopped:
                status = self.stopped[name]
            elif done == total:
                status = "ok"
            elif c.get("done", 0):
                status = "partial"
            else:
                status = "failed"
            sources[name] = {**c, "status": status, "error": self.errors.get(name)}
            log.info(
                "run %d: %s %s, %d requests, tasks %s",
                run_id,
                name,
                status,
                c["requests"],
                {k: v for k, v in c.items() if k != "requests"},
            )
        statuses = {s["status"] for s in sources.values()}
        if statuses <= {"ok"}:
            run_status = "done"
        elif statuses <= {"failed", "blocked", "budget"} and not any(
            s.get("done") for s in sources.values()
        ):
            run_status = "failed"
        else:
            run_status = "partial"
        self.store.finish_run(run_id, self.now(), run_status, sources)
        total = sum(s["requests"] for s in sources.values())
        log.info("run %d finished: %s, %d requests in total", run_id, run_status, total)
