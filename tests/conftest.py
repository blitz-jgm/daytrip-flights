from datetime import date
from pathlib import Path

import pytest

from daytrip_flights.config import Route
from daytrip_flights.pacer import Pacer

FIXTURES = Path(__file__).parent / "fixtures"
FIXTURE_DATE = date(2026, 11, 10)


@pytest.fixture
def route() -> Route:
    return Route(
        name="seoul-fukuoka",
        origins=["ICN", "GMP"],
        destination="FUK",
        out_window="06:00-09:00",
        back_window="18:00-21:00",
    )


@pytest.fixture
def pacer() -> Pacer:
    return Pacer(
        "test", max_requests=100, delay=(0, 0), retries=0, backoff_base=0, sleep=lambda s: None
    )


def fixture_text(*parts: str) -> str:
    return FIXTURES.joinpath(*parts).read_text(encoding="utf-8")
