"""Telegram alerts for real deals and a daily e-mail digest.

Both channels are optional and switched on by their secrets in the environment (`.env`):
TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID, and SMTP_USER + SMTP_PASSWORD (Gmail app password by
default). Sending happens after a run and never fails it; a failed send is retried next run.
"""

from __future__ import annotations

import html
import logging
import smtplib
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from jinja2 import Environment, PackageLoader, select_autoescape

from daytrip_flights.config import Settings
from daytrip_flights.report.render import build_context
from daytrip_flights.store import Store

log = logging.getLogger(__name__)


def esc(text: str) -> str:
    return html.escape(str(text), quote=False)


def krw(v: int) -> str:
    return f"₩{v:,}"


# ---- channels ------------------------------------------------------------------------------


class Telegram:
    def __init__(self, token: str, chat_id: str, post: Callable[..., Any] = httpx.post) -> None:
        self.token = token
        self.chat_id = chat_id
        self._post = post

    def send(self, text: str) -> None:
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        body = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        try:
            r = self._post(url, json=body, timeout=30)
        except httpx.HTTPError as exc:  # the message would contain the URL, i.e. the token
            raise RuntimeError(f"telegram: {type(exc).__name__}") from None
        if r.status_code != 200:
            raise RuntimeError(f"telegram: HTTP {r.status_code}: {r.text[:200]}")


class Email:
    def __init__(
        self, host: str, port: int, user: str, password: str, to: str, sender: str
    ) -> None:
        self.host, self.port, self.user, self.password = host, port, user, password
        self.to, self.sender = to, sender

    def send(self, msg: EmailMessage) -> None:
        msg["From"] = self.sender
        msg["To"] = self.to
        if self.port == 465:
            smtp: smtplib.SMTP = smtplib.SMTP_SSL(self.host, self.port, timeout=30)
        else:
            smtp = smtplib.SMTP(self.host, self.port, timeout=30)
        with smtp:
            if self.port != 465:
                smtp.starttls()
            smtp.login(self.user, self.password)
            smtp.send_message(msg)


def channels_from_env(env: Mapping[str, str]) -> tuple[Telegram | None, Email | None]:
    tg = None
    if env.get("TELEGRAM_BOT_TOKEN") and env.get("TELEGRAM_CHAT_ID"):
        tg = Telegram(env["TELEGRAM_BOT_TOKEN"], env["TELEGRAM_CHAT_ID"])
    mail = None
    if env.get("SMTP_USER") and env.get("SMTP_PASSWORD"):
        user = env["SMTP_USER"]
        mail = Email(
            host=env.get("SMTP_HOST") or "smtp.gmail.com",
            port=int(env.get("SMTP_PORT") or 587),
            user=user,
            password=env["SMTP_PASSWORD"],
            to=env.get("EMAIL_TO") or user,
            sender=env.get("EMAIL_FROM") or user,
        )
    return tg, mail


# ---- when to alert -------------------------------------------------------------------------


def decide_alert(
    runs: list[dict[str, Any]], below_pct: float, min_runs: int
) -> dict[str, Any] | None:
    """Alert when the latest run's cheapest price is a new low, or clearly below typical.

    Both are judged against earlier runs only, so a deal is not measured against itself.
    """
    if len(runs) < 2:
        return None
    latest = runs[-1]["total"]
    earlier = [r["total"] for r in runs[:-1]]
    typical = round(median(earlier))
    if latest < min(earlier):
        return {"reason": "new_low", "prev_low": min(earlier), "typical": typical}
    if len(runs) >= min_runs and latest <= typical * (1 - below_pct / 100):
        return {"reason": "below_typical", "prev_low": min(earlier), "typical": typical}
    return None


def _title(route: dict[str, Any]) -> str:
    return f"{'/'.join(route['origins'])} → {route['destination']}"


def _legs_text(day: dict[str, Any]) -> list[str]:
    return [
        f"{esc(leg['flight_no'])} {leg['origin']} {leg['dep']} → {leg['destination']} "
        f"{leg['arr']} · {leg['source']}"
        for leg in day["legs"]
    ]


def alert_text(route: dict[str, Any], alert: dict[str, Any]) -> str:
    latest = route["cheapest"]["runs"][-1]
    day = next((d for d in route["days"] if d["date"] == latest["date"]), None)
    if alert["reason"] == "new_low":
        why = f"📉 new lowest price, was {krw(alert['prev_low'])}"
    else:
        pct = round(100 * (1 - latest["total"] / alert["typical"]))
        why = f"📉 {pct}% below typical ({krw(alert['typical'])})"
    lines = [
        f"✈️ <b>Day-trip deal · {esc(_title(route))}</b>",
        f"<b>{krw(latest['total'])}</b> · {latest['date']} ({latest['weekday']})",
        why,
    ]
    if day:
        lines += _legs_text(day)
        links = " | ".join(
            f'<a href="{html.escape(lk["url"])}">{esc(lk["label"])}</a>' for lk in day["links"]
        )
        if links:
            lines.append(links)
    return "\n".join(lines)


def test_text(ctx: dict[str, Any]) -> str:
    """For `daytrip notify-test`: proves the bot can reach the chat."""
    lines = ["✅ daytrip-flights: Telegram is set up."]
    for route in ctx["routes"]:
        if route["top"]:
            d = route["top"][0]
            lines.append(
                f"{esc(_title(route))}: cheapest now {krw(d['total'])}"
                f" on {d['date']} ({d['weekday']})"
            )
    return "\n".join(lines)


# ---- daily digest --------------------------------------------------------------------------


def _weekdays(days: list[dict[str, Any]]) -> list[tuple[str, int]]:
    by: dict[str, list[int]] = {}
    for d in days:
        by.setdefault(d["weekday"], []).append(d["total"])
    return sorted(((wd, round(median(v))) for wd, v in by.items()), key=lambda x: x[1])


def digest_message(ctx: dict[str, Any], report_path: Path) -> EmailMessage:
    routes = [r for r in ctx["routes"] if r["top"]]
    for r in routes:
        r["weekdays"] = _weekdays(r["days"])[:3] if len(r["days"]) >= 14 else []
    env = Environment(
        loader=PackageLoader("daytrip_flights", "report"),
        autoescape=select_autoescape(["html", "j2"]),
    )
    env.filters["krw"] = krw
    msg = EmailMessage()
    best = routes[0]["top"][0] if routes else None
    msg["Subject"] = (
        f"Day-trip flights: from {krw(best['total'])} · {best['date']} ({best['weekday']})"
        if best
        else "Day-trip flights: no results yet"
    )
    text = [f"Day-trip flights · {ctx['generated_kst']} ({ctx['timezone']})", ""]
    for r in routes:
        text.append(_title(r))
        if r["cheapest"]:
            c = r["cheapest"]
            text.append(
                f"Now {krw(c['latest'])} · lowest seen {krw(c['low'])} · typical {krw(c['median'])}"
            )
        for d in r["top"]:
            flights = " / ".join(f"{leg['flight_no']} {leg['dep']}" for leg in d["legs"])
            text.append(f"  {d['date']} ({d['weekday']})  {krw(d['total'])}  {flights}")
        text.append("")
    text.append("The full interactive report is attached (open it in a browser).")
    msg.set_content("\n".join(text))
    msg.add_alternative(env.get_template("email.html.j2").render({**ctx, "routes": routes}), "html")
    if report_path.exists():
        msg.add_attachment(
            report_path.read_text(encoding="utf-8"), subtype="html", filename="report.html"
        )
    return msg


# ---- after a run ---------------------------------------------------------------------------


def notify_after_run(
    settings: Settings,
    store: Store,
    run_id: int,
    report_path: Path,
    telegram: Telegram | None = None,
    email: Email | None = None,
) -> None:
    if telegram is None and email is None:
        return
    run = store.run(run_id)
    if run["mode"] == "limited" or run["status"] not in ("done", "partial"):
        return
    now = datetime.fromisoformat(run["finished_at"] or run["started_at"])
    ctx = build_context(settings, store, now=now)

    if telegram is not None:
        for route in ctx["routes"]:
            c = route["cheapest"]
            if not c or c["runs"][-1]["run_id"] != run_id:
                continue  # this run found nothing for the route
            alert = decide_alert(
                c["runs"], settings.notify.alert_below_typical_pct, settings.notify.alert_min_runs
            )
            if alert is None:
                continue
            key = f"{c['runs'][-1]['date']}:{c['runs'][-1]['total']}"
            if store.notified("telegram", route["name"], key):
                continue
            try:
                telegram.send(alert_text(route, alert))
            except Exception as exc:
                log.warning("telegram alert not sent: %s", exc)
                continue
            store.add_notification("telegram", route["name"], key, datetime.now(UTC))
            log.info("telegram alert sent for %s: %s", route["name"], key)

    if email is not None and run["mode"] == "full":
        day = now.astimezone(ZoneInfo(settings.timezone)).date().isoformat()
        if not store.notified("email", "", day):
            try:
                email.send(digest_message(ctx, report_path))
            except Exception as exc:
                log.warning("e-mail digest not sent: %s", exc)
                return
            store.add_notification("email", "", day, datetime.now(UTC))
            log.info("e-mail digest sent for %s", day)
