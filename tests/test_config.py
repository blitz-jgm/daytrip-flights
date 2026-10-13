from datetime import time

import pytest

from daytrip_flights.config import Route, TimeWindow, load_config


def test_time_window_parses_and_is_inclusive():
    w = TimeWindow.parse("06:00-09:00")
    assert w.start == time(6, 0) and w.end == time(9, 0)
    assert w.contains(time(6, 0))
    assert w.contains(time(9, 0))
    assert not w.contains(time(9, 1))
    assert not w.contains(time(5, 59))


def test_time_window_rejects_reversed():
    with pytest.raises(ValueError):
        TimeWindow.parse("21:00-18:00")


def test_route_accepts_window_strings():
    r = Route(
        name="seoul-fukuoka",
        origins=["ICN", "GMP"],
        destination="FUK",
        out_window="06:00-09:00",
        back_window="18:00-21:00",
    )
    assert r.out_window.contains(time(7, 30))
    assert r.back_window.contains(time(20, 5))
    assert r.days_ahead == 90


def test_load_config_reads_yaml(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(
        """
routes:
  - name: fukuoka-seoul
    origins: [FUK]
    destination: ICN
    out_window: "07:00-10:00"
    back_window: "17:00-20:00"
    days_ahead: 30
budget:
  max_requests_per_run: 50
"""
    )
    cfg = load_config(p)
    assert cfg.routes[0].origins == ["FUK"]
    assert cfg.routes[0].days_ahead == 30
    assert cfg.budget.max_requests_per_run == 50
    assert cfg.budget.delay_min_s >= 1  # conservative defaults stay in place
    assert cfg.sources == ["google", "naver"]
