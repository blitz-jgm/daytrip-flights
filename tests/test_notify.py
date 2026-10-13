import re
from datetime import UTC, datetime, timedelta

from factories import D, oneway

from daytrip_flights import notify
from daytrip_flights.config import Settings
from daytrip_flights.store import Store

T0 = datetime(2026, 11, 9, 18, 0, tzinfo=UTC)  # 2026-11-10 03:00 KST


def settings(**notify_cfg):
    return Settings.model_validate(
        {
            "routes": [
                {
                    "name": "seoul-fukuoka",
                    "origins": ["ICN"],
                    "destination": "FUK",
                    "out_window": "06:00-09:00",
                    "back_window": "18:00-21:00",
                }
            ],
            "notify": notify_cfg,
        }
    )


def add_run(store, price, mode="full", at=T0, status="done"):
    run = store.start_run(mode, at)
    o, b = store.add_offers(
        run,
        [
            oneway("out", "7C1401", "ICN", "FUK", "06:30", price // 2),
            oneway("back", "LJ266", "FUK", "ICN", "18:00", price - price // 2, source="naver"),
        ],
    )
    store.add_daily_best(run, "seoul-fukuoka", D, "combo", price, o, b, None)
    store.finish_run(run, at + timedelta(minutes=20), status, {})
    return run


class FakeTelegram:
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    def send(self, text):
        if self.fail:
            raise RuntimeError("telegram down")
        self.sent.append(text)


class FakeEmail:
    def __init__(self):
        self.sent = []

    def send(self, msg):
        self.sent.append(msg)


def runs(*totals):
    return [{"total": t, "date": "2026-11-10", "run_id": i + 1} for i, t in enumerate(totals)]


# ---- when to alert -----------------------------------------------------------------------


def test_new_low_alerts_after_a_single_earlier_run():
    alert = notify.decide_alert(runs(400000, 380000), below_pct=10, min_runs=8)
    assert alert["reason"] == "new_low" and alert["prev_low"] == 400000


def test_no_alert_without_history_or_without_a_deal():
    assert notify.decide_alert(runs(380000), below_pct=10, min_runs=8) is None
    assert notify.decide_alert(runs(380000, 380000), below_pct=10, min_runs=8) is None
    assert notify.decide_alert(runs(380000, 390000), below_pct=10, min_runs=8) is None


def test_below_typical_needs_enough_runs():
    history = [400000, 300000] + [400000] * 5  # 7 earlier runs: typical 400k, low 300k
    assert notify.decide_alert(runs(*history, 350000), below_pct=10, min_runs=9) is None
    alert = notify.decide_alert(runs(*history, 350000), below_pct=10, min_runs=8)
    assert alert["reason"] == "below_typical" and alert["typical"] == 400000
    # 361k is only 9.75% below typical
    assert notify.decide_alert(runs(*history, 361000), below_pct=10, min_runs=8) is None


# ---- after a run -------------------------------------------------------------------------


def test_alert_is_sent_once_per_deal(tmp_path):
    store, tg = Store(tmp_path / "f.db"), FakeTelegram()
    add_run(store, 400000)
    run = add_run(store, 380000, mode="refresh", at=T0 + timedelta(hours=3))
    notify.notify_after_run(settings(), store, run, tmp_path / "r.html", telegram=tg)
    assert len(tg.sent) == 1
    assert "₩380,000" in tg.sent[0] and "new lowest" in tg.sent[0]
    assert "2026-11-10 (Tue)" in tg.sent[0] and "7C1401" in tg.sent[0]
    assert "https://example.test" in tg.sent[0]
    # the same deal in the next run is not sent again
    again = add_run(store, 380000, mode="refresh", at=T0 + timedelta(hours=6))
    notify.notify_after_run(settings(), store, again, tmp_path / "r.html", telegram=tg)
    assert len(tg.sent) == 1


def test_smoke_test_runs_send_nothing(tmp_path):
    store, tg, mail = Store(tmp_path / "f.db"), FakeTelegram(), FakeEmail()
    add_run(store, 400000)
    smoke = add_run(store, 100000, mode="limited", at=T0 + timedelta(hours=1))
    notify.notify_after_run(settings(), store, smoke, tmp_path / "r.html", telegram=tg, email=mail)
    assert tg.sent == [] and mail.sent == []


def test_digest_after_full_scan_once_per_day(tmp_path):
    store, mail = Store(tmp_path / "f.db"), FakeEmail()
    report = tmp_path / "report.html"
    report.write_text("<!doctype html><script>1</script>", encoding="utf-8")
    refresh = add_run(store, 400000, mode="refresh")
    notify.notify_after_run(settings(), store, refresh, report, email=mail)
    assert mail.sent == []
    full = add_run(store, 380000, at=T0 + timedelta(hours=3))
    notify.notify_after_run(settings(), store, full, report, email=mail)
    assert len(mail.sent) == 1
    # a second full scan on the same local day sends no second digest
    again = add_run(store, 370000, at=T0 + timedelta(hours=6))
    notify.notify_after_run(settings(), store, again, report, email=mail)
    assert len(mail.sent) == 1


def test_digest_email_is_static_html_with_the_report_attached(tmp_path):
    store, mail = Store(tmp_path / "f.db"), FakeEmail()
    report = tmp_path / "report.html"
    report.write_text("<!doctype html><p>full report</p>", encoding="utf-8")
    add_run(store, 400000)
    full = add_run(store, 380000, at=T0 + timedelta(hours=3))
    notify.notify_after_run(settings(), store, full, report, email=mail)
    (msg,) = mail.sent
    assert "₩380,000" in msg["Subject"]
    html = msg.get_body(("html",)).get_content()
    text = msg.get_body(("plain",)).get_content()
    assert not re.search(r"<script", html, re.I)  # mail apps strip scripts
    assert "₩380,000" in html and "7C1401" in html and "https://example.test" in html
    assert "₩380,000" in text
    (att,) = list(msg.iter_attachments())
    assert att.get_filename() == "report.html"
    assert "full report" in att.get_content()


def test_a_failing_channel_never_breaks_the_run(tmp_path, caplog):
    store = Store(tmp_path / "f.db")
    add_run(store, 400000)
    run = add_run(store, 380000, at=T0 + timedelta(hours=3))
    notify.notify_after_run(
        settings(), store, run, tmp_path / "missing.html", telegram=FakeTelegram(fail=True)
    )
    assert "telegram down" in caplog.text
    # a failed send is not recorded, so the next run tries again
    tg = FakeTelegram()
    notify.notify_after_run(settings(), store, run, tmp_path / "missing.html", telegram=tg)
    assert len(tg.sent) == 1


# ---- configuration -----------------------------------------------------------------------


def test_channels_come_from_env_and_are_off_when_unset():
    assert notify.channels_from_env({}) == (None, None)
    tg, mail = notify.channels_from_env(
        {
            "TELEGRAM_BOT_TOKEN": "t",
            "TELEGRAM_CHAT_ID": "42",
            "SMTP_USER": "me@example.test",
            "SMTP_PASSWORD": "p",
        }
    )
    assert tg.chat_id == "42"
    assert (mail.host, mail.port, mail.to) == ("smtp.gmail.com", 587, "me@example.test")


def test_telegram_text_escapes_html():
    assert notify.esc("A<B & C>") == "A&lt;B &amp; C&gt;"


def test_setup_check_message_names_the_current_best(tmp_path):
    store = Store(tmp_path / "f.db")
    add_run(store, 380000)
    ctx = notify.build_context(settings(), store, now=T0 + timedelta(hours=1))
    text = notify.test_text(ctx)
    assert "Telegram is set up" in text and "₩380,000" in text
    assert "Telegram is set up" in notify.test_text({"routes": []})
