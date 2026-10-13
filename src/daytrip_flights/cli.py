"""Command line entry point: `daytrip search --once`, `daytrip report`, `daytrip status`."""

from __future__ import annotations

import fcntl
import logging
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated

import typer

from daytrip_flights.config import Settings, load_config
from daytrip_flights.runner import Runner
from daytrip_flights.sources.base import Source
from daytrip_flights.store import Store

app = typer.Typer(no_args_is_help=True, add_completion=False)
log = logging.getLogger("daytrip_flights")

ConfigOpt = Annotated[Path, typer.Option(envvar="DAYTRIP_CONFIG", help="Path to config.yaml")]
DataOpt = Annotated[Path, typer.Option(envvar="DAYTRIP_DATA_DIR", help="SQLite + report dir")]


def _setup_logging() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("primp").setLevel(logging.WARNING)  # logs every URL at INFO


def build_sources(settings: Settings) -> dict[str, Source]:
    sources: dict[str, Source] = {}
    for name in settings.sources:
        if name == "google":
            from daytrip_flights.sources.google import GoogleSource

            sources[name] = GoogleSource(bundle_outbounds=settings.tiers.bundle_outbounds_per_date)
        elif name == "naver":
            from daytrip_flights.sources.naver import NaverSource

            sources[name] = NaverSource()
        else:
            raise typer.BadParameter(f"unknown source {name!r}")
    return sources


@contextmanager
def _run_lock(data_dir: Path):
    data_dir.mkdir(parents=True, exist_ok=True)
    with open(data_dir / "run.lock", "w") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log.warning("another run is still in progress; exiting")
            raise typer.Exit(0) from None
        yield


@app.command()
def search(
    once: Annotated[
        bool,
        typer.Option(
            "--once", help="Run one search and exit. Periodic runs are scheduled by supercronic."
        ),
    ] = True,
    config: ConfigOpt = Path("config.yaml"),
    data_dir: DataOpt = Path("data"),
    days: Annotated[int | None, typer.Option(help="Limit the horizon (smoke tests)")] = None,
    source: Annotated[list[str] | None, typer.Option(help="Only these sources")] = None,
    resume: Annotated[bool, typer.Option(help="Continue an interrupted run")] = True,
    report: Annotated[bool, typer.Option(help="Write report.html afterwards")] = True,
) -> None:
    """Search all configured routes once, store the results and refresh the report."""
    _setup_logging()
    settings = load_config(config)
    if source:
        settings.sources = [s for s in settings.sources if s in source] or list(source)
    with _run_lock(data_dir):
        store = Store(data_dir / "flights.db")
        Runner(settings, store, build_sources(settings), days_ahead=days).run(resume=resume)
        if report:
            _write_report(settings, store, data_dir)
        store.close()


def _write_report(settings: Settings, store: Store, data_dir: Path) -> None:
    try:
        from daytrip_flights.report.render import write_report
    except ImportError:  # report module not available yet
        return
    path = write_report(settings, store, data_dir / "report.html")
    log.info("report written to %s", path)


@app.command()
def report(config: ConfigOpt = Path("config.yaml"), data_dir: DataOpt = Path("data")) -> None:
    """Regenerate report.html from the stored data."""
    _setup_logging()
    settings = load_config(config)
    store = Store(data_dir / "flights.db")
    _write_report(settings, store, data_dir)
    store.close()


@app.command()
def status(
    config: ConfigOpt = Path("config.yaml"), data_dir: DataOpt = Path("data"), top: int = 10
) -> None:
    """Print recent runs and the cheapest dates."""
    settings = load_config(config)
    store = Store(data_dir / "flights.db")
    for r in store.recent_runs(5):
        per = ", ".join(f"{k}={v['status']}/{v['requests']}req" for k, v in r["sources"].items())
        typer.echo(f"run {r['id']} {r['started_at'][:16]} {r['mode']:7} {r['status']:8} {per}")
    for route in settings.routes:
        typer.echo(f"\n{route.name}: cheapest dates")
        best = sorted(store.latest_best(route.name), key=lambda b: b["total_krw"])[:top]
        for b in best:
            ids = [b["out_offer_id"], b["back_offer_id"], b["bundle_offer_id"]]
            legs = [leg for i in ids if (o := store.offer(i)) for leg in o.legs]
            desc = " / ".join(f"{leg.flight_no} {leg.departure:%H:%M}" for leg in legs)
            typer.echo(f"  {b['date']} ({b['date']:%a}) ₩{b['total_krw']:>9,} {b['kind']:6} {desc}")
    store.close()
