from datetime import UTC, date, datetime, time

from daytrip_flights.models import FlightOffer, Leg

D = date(2026, 11, 10)


def leg(no: str, a: str, b: str, dep: str, day: date = D) -> Leg:
    t = time.fromisoformat(dep)
    return Leg(
        flight_no=no,
        airline=no[:2],
        origin=a,
        destination=b,
        departure=datetime.combine(day, t),
        arrival=datetime.combine(day, t),
    )


def oneway(
    kind: str,
    no: str,
    a: str,
    b: str,
    dep: str,
    price: int,
    source: str = "google",
    day: date = D,
    krw: bool = True,
) -> FlightOffer:
    lg = leg(no, a, b, dep, day)
    return FlightOffer(
        source=source,
        route="seoul-fukuoka",
        date=day,
        ticket_type="oneway",
        out_leg=lg if kind == "out" else None,
        back_leg=lg if kind == "back" else None,
        price=price,
        currency="KRW" if krw else "JPY",
        price_krw=price if krw else None,
        fx_rate=1.0 if krw else None,
        booking_url="https://example.test",
        collected_at=datetime(2026, 10, 13, tzinfo=UTC),
    )


def bundle(
    out_no: str,
    out_dep: str,
    back_no: str,
    back_dep: str,
    price: int,
    source: str = "google",
    day: date = D,
    origin: str = "ICN",
) -> FlightOffer:
    return FlightOffer(
        source=source,
        route="seoul-fukuoka",
        date=day,
        ticket_type="roundtrip",
        out_leg=leg(out_no, origin, "FUK", out_dep, day),
        back_leg=leg(back_no, "FUK", origin, back_dep, day),
        price=price,
        currency="KRW",
        price_krw=price,
        fx_rate=1.0,
        booking_url="https://example.test",
        collected_at=datetime(2026, 10, 13, tzinfo=UTC),
    )
