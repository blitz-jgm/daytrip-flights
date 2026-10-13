# daytrip-flights

[한국어 README](README.ko.md)

Find the cheapest dates for a **same-day round trip** between Seoul and Japan — fly out in
the morning, fly back in the evening. It searches **Google Flights** and **Naver Flights**
every 3 hours, keeps every price it sees in SQLite, and writes a single offline HTML report.

The first route is Seoul (ICN/GMP) → Fukuoka (FUK):
outbound departs **06:00–09:00 KST**, return departs **18:00–21:00 JST**, direct flights,
1 adult, economy, the next 90 days. Routes and time windows live in `config.yaml`.

> **Disclaimer.** This project reads publicly visible flight-search pages. It is not
> affiliated with Google or Naver. **Checking each site's terms of use and any
> responsibility for how you use this tool lie with the user.** Defaults are deliberately
> slow (one request every 4–8 s per source, capped per run); please keep them polite.
> Prices are snapshots — always confirm on the booking site.

## How a date's price is decided

For every date both of these are computed, and the cheaper one wins:

1. **Round-trip ticket (bundle)** — one booking whose both legs fit the time windows.
2. **Two one-way tickets (combo)** — the cheapest in-window outbound + the cheapest
   in-window return. Airlines, sources and Seoul airports may differ (e.g. ICN out, GMP back).

All prices are KRW. An offer in another currency is stored with its original currency but is
never ranked, because no exchange-rate source is configured (fail closed).

## Quick start (Docker)

```bash
cp .env.example .env          # optional
docker compose up -d          # first search starts now, then every 3 hours
open data/report.html         # macOS; use any browser elsewhere
```

One-off commands:

```bash
docker compose run --rm app search --once     # one search run
docker compose run --rm app status            # recent runs + cheapest dates
docker compose run --rm app report            # rebuild report.html
docker compose logs -f app                    # follow the schedule
```

`./data` holds `flights.db` (SQLite) and `report.html`. `config.yaml` is mounted read-only,
so you can edit routes without rebuilding.

## Local development

Requires [uv](https://docs.astral.sh/uv/) (Python 3.12 is installed by uv if missing).

```bash
uv sync
uv run daytrip search --once --days 3     # small live smoke test
uv run daytrip status
uv run pytest                             # offline: uses saved real responses
uv run ruff check . && uv run ruff format --check .
```

## Configuration (`config.yaml`)

```yaml
routes:
  - name: seoul-fukuoka
    origins: [ICN, GMP]          # either Seoul airport
    destination: FUK
    out_window: "06:00-09:00"    # origin local time, inclusive
    back_window: "18:00-21:00"   # destination local time, inclusive
    days_ahead: 90
sources: [google, naver]
```

A Fukuoka resident's day trip to Seoul is the same structure with `origins: [FUK]` and
`destination: ICN`. Add as many routes as you like; each one costs requests, so mind the budget.

## Request budget

A *request* is one search page. Per date and Seoul airport there is one outbound and one
return search per source.

| Run type | When | Dates searched | Google | Naver |
|---|---|---|---|---|
| Full scan | at most once per 24 h | all 90 | ≈ 180 one-way + ≤ 30 bundle | ≈ 180 |
| Refresh | the other 7 runs | next 14 days + 15 cheapest | ≈ 58 one-way + ≤ 30 bundle | ≈ 58 |

GMP has no direct Fukuoka flights today: after 5 empty answers in a row that direction is
skipped and remembered for 7 days, so it costs ~10 requests a week. Daily totals are roughly
**~830 requests to Google and ~590 to Naver**, sequential, 4–8 s apart, with a hard cap
of 400 per source per run. Every run logs its request count, and `status` shows it. (A Google bundle return lookup is
counted as one request, but the library may make up to two extra fallback calls inside it.)

Errors are retried with exponential backoff (3 times). HTTP 403/429 stops that source for the
rest of the run; the other source keeps going.

## Resuming

Every planned search is written to SQLite before it runs and marked when it finishes. If a run
is interrupted (container restart, crash, Ctrl-C), the next `search --once` within 6 hours
continues the same run and only performs the searches that are still open.

## Data

| Table | Content |
|---|---|
| `runs` | start/finish, full or refresh, status, per-source status and request counts |
| `run_tasks` | every planned search: source, route, airport, date, direction, status, error |
| `offers` | every in-window offer: flights, airlines, times, price, currency, booking link, time seen |
| `daily_best` | the winning bundle/combo per date, per run → price history |
| `route_service` | "no direct flights" cache per source and direction |

Nothing is overwritten, so price trends over time stay available.

## Sources and design notes

- **Google Flights** via [`faster-flights`](https://github.com/jamexhuang/flights) 3.8.0
  (a fork of [`fast-flights`](https://github.com/AWeirdDev/flights)). Chosen after a live
  side-by-side test: the original 3.1.0 release is missing a dependency on PyPI, crashes on
  routes without flights, has no flight numbers, and its server-side time filter dropped
  cheaper flights; the fork returned the complete list and supports pricing the return leg of
  a round-trip ticket. It is pinned and wrapped behind a `Source` adapter with fixture tests.
  Bundle totals that fall below the outbound's round-trip "from" price are discarded, because
  that means the library silently fell back to one-way prices.
- **Naver Flights** has no public API. The web page POSTs to an internal search endpoint that
  streams results from ~20 booking partners; this project calls the same endpoint with `httpx`
  (one request per search, no browser). Only the general adult fare is used; card-specific
  discounts are ignored. Naver is used for one-way fares only: its round-trip results are
  capped at the 200 cheapest pairs and none of them fit same-day time windows in testing.
- **Scheduling** uses [supercronic](https://github.com/aptible/supercronic) rather than
  APScheduler: every run is a fresh process, it needs no extra Python dependency, and the
  schedule is a plain crontab.
- No price-calendar API is used; the tiered scan keeps the request volume down instead.

Internal endpoints and page formats change without notice. When a source breaks, the other
keeps working and the report shows the failing source in its status line.

## License

[MIT](LICENSE)
