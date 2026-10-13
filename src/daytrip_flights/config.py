"""config.yaml schema. Routes and time windows live here so new routes need no code changes."""

from __future__ import annotations

from datetime import time
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


class TimeWindow(BaseModel):
    """Inclusive local-time departure window, e.g. "06:00-09:00"."""

    start: time
    end: time

    @classmethod
    def parse(cls, text: str) -> TimeWindow:
        a, b = (part.strip() for part in text.split("-"))
        return cls(start=time.fromisoformat(a), end=time.fromisoformat(b))

    @model_validator(mode="after")
    def _ordered(self) -> TimeWindow:
        if self.end < self.start:
            raise ValueError("window end is before start (overnight windows are not supported)")
        return self

    def contains(self, t: time) -> bool:
        return self.start <= t <= self.end

    def __str__(self) -> str:
        return f"{self.start:%H:%M}-{self.end:%H:%M}"


def _window(v: Any) -> Any:
    return TimeWindow.parse(v) if isinstance(v, str) else v


class Route(BaseModel):
    """A same-day trip: origins -> destination in out_window, destination -> origins in back_window.

    out_window is in origin-local time, back_window in destination-local time.
    """

    name: str
    origins: list[str]
    destination: str
    out_window: TimeWindow
    back_window: TimeWindow
    days_ahead: int = 90

    _parse_out = field_validator("out_window", mode="before")(_window)
    _parse_back = field_validator("back_window", mode="before")(_window)


class Budget(BaseModel):
    """Per-source, per-run request limits. Defaults are deliberately conservative."""

    max_requests_per_run: int = 400
    delay_min_s: float = 4.0
    delay_max_s: float = 8.0
    retries: int = 3
    backoff_base_s: float = 5.0


class Tiers(BaseModel):
    """Full 90-day scan at most once per `full_scan_every_hours`; other runs refresh a subset."""

    full_scan_every_hours: int = 24
    refresh_next_days: int = 14
    refresh_top_dates: int = 15
    bundle_top_dates: int = 10
    bundle_outbounds_per_date: int = 2
    no_service_after_empty: int = 5
    no_service_cache_days: int = 7


class Notify(BaseModel):
    """When to send. Channels are switched on by their secrets in the environment (.env)."""

    alert_below_typical_pct: float = 10.0  # Telegram: this far below the median of earlier runs
    alert_min_runs: int = 8  # ... once there are this many runs, so "typical" means something


class Settings(BaseModel):
    routes: list[Route]
    sources: list[str] = Field(default_factory=lambda: ["google", "naver"])
    timezone: str = "Asia/Seoul"
    currency: str = "KRW"
    budget: Budget = Field(default_factory=Budget)
    tiers: Tiers = Field(default_factory=Tiers)
    notify: Notify = Field(default_factory=Notify)
    resume_max_age_hours: float = 6.0


def load_config(path: str | Path) -> Settings:
    with open(path, encoding="utf-8") as f:
        return Settings.model_validate(yaml.safe_load(f))
