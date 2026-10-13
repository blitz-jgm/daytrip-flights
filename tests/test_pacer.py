import pytest

from daytrip_flights.pacer import BudgetExceeded, Pacer, SourceBlocked


class FakeClock:
    def __init__(self):
        self.sleeps: list[float] = []

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)


def make(max_requests=10, retries=2):
    clock = FakeClock()
    pacer = Pacer(
        "google",
        max_requests=max_requests,
        delay=(4.0, 8.0),
        retries=retries,
        backoff_base=5.0,
        sleep=clock.sleep,
        uniform=lambda a, b: a,  # deterministic: lower bound
    )
    return pacer, clock


def test_counts_requests_and_jitters_between_them():
    pacer, clock = make()
    assert pacer.call(lambda: 1) == 1
    assert pacer.call(lambda: 2) == 2
    assert pacer.requests == 2
    assert clock.sleeps == [4.0]  # no wait before the very first request


def test_retries_with_exponential_backoff_then_succeeds():
    pacer, clock = make(retries=2)
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("boom")
        return "ok"

    assert pacer.call(flaky) == "ok"
    assert pacer.requests == 3
    # backoff 5*2^0, jitter 4, backoff 5*2^1, jitter 4
    assert clock.sleeps == [5.0, 4.0, 10.0, 4.0]


def test_gives_up_after_retries():
    pacer, _ = make(retries=1)
    with pytest.raises(RuntimeError):
        pacer.call(lambda: (_ for _ in ()).throw(RuntimeError("down")))
    assert pacer.requests == 2


def test_blocked_status_opens_circuit():
    pacer, _ = make()

    def blocked():
        raise RuntimeError("Google Flights returned HTTP 429. rate-limited")

    with pytest.raises(SourceBlocked):
        pacer.call(blocked)
    assert pacer.requests == 1  # no retry on a block
    with pytest.raises(SourceBlocked):
        pacer.call(lambda: "never")  # circuit stays open for the rest of the run
    assert pacer.requests == 1


def test_budget_cap():
    pacer, _ = make(max_requests=2)
    pacer.call(lambda: 1)
    pacer.call(lambda: 1)
    with pytest.raises(BudgetExceeded):
        pacer.call(lambda: 1)
