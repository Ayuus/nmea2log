from datetime import datetime, timedelta

from nmea2000processor.model import EngineSample, PositionFix, SogSample
from nmea2000processor.tripbuilder import build_trips


class _StubGeocoder:
    def __init__(self) -> None:
        self.calls = []

    def place_name(self, lat: float, lon: float) -> str:
        self.calls.append((lat, lon))
        return f"Haven@{lat:.2f},{lon:.2f}"


def _dt(minute: int) -> datetime:
    return datetime(2026, 7, 15, 8, 0, 0) + timedelta(minutes=minute)


def _build_scenario():
    """12 min stilliggen -> 30 min varen -> 12 min stilliggen, met motordata erbij."""
    fixes = []
    sogs = []

    for m in range(0, 12):
        fixes.append(PositionFix(_dt(m), 52.30, 4.90))
        sogs.append(SogSample(_dt(m), 0.0))

    for i, m in enumerate(range(12, 42)):
        frac = i / 29
        fixes.append(PositionFix(_dt(m), 52.30 + 0.10 * frac, 4.90 + 0.05 * frac))
        sogs.append(SogSample(_dt(m), 3.0))  # ~5,8 kn, boven de standaarddrempel

    for m in range(42, 54):
        fixes.append(PositionFix(_dt(m), 52.40, 4.95))
        sogs.append(SogSample(_dt(m), 0.0))

    engine_samples = []
    for m in range(0, 54):
        fuel_rate = 8.0 if 12 <= m < 42 else 0.5
        engine_samples.append(EngineSample(_dt(m), 0, fuel_rate, 3600 * 100 + m * 60))

    return fixes, sogs, engine_samples


def test_build_trips_single_leg():
    fixes, sogs, engine_samples = _build_scenario()
    geocoder = _StubGeocoder()

    trips = build_trips(
        fixes, sogs, engine_samples, geocoder=geocoder, speed_threshold_kn=0.5, min_stop_minutes=10
    )

    assert len(trips) == 1
    trip = trips[0]
    assert trip.depart_place.startswith("Haven@52.30")
    assert trip.arrive_place.startswith("Haven@52.40")
    assert trip.distance_nm > 5
    assert trip.fuel_liters > 0
    assert 0 in trip.engine_hours
    assert trip.engine_hours[0] > 0
    # geocoder moet niet vaker dan het aantal havenbezoeken zijn aangeroepen
    assert len(geocoder.calls) == 2


def test_short_stop_does_not_split_trip():
    """Een korte stop (< min_stop_minutes) hoort niet als apart havenbezoek te tellen."""
    fixes, sogs, engine_samples = _build_scenario()

    # voeg een korte stop van 3 minuten toe halverwege de reis
    extra_fixes = [PositionFix(_dt(25) + timedelta(seconds=s), 52.35, 4.925) for s in range(0, 180, 20)]
    extra_sogs = [SogSample(_dt(25) + timedelta(seconds=s), 0.0) for s in range(0, 180, 20)]

    fixes = fixes[:39] + extra_fixes + fixes[39:]
    sogs = sogs[:39] + extra_sogs + sogs[39:]

    geocoder = _StubGeocoder()
    trips = build_trips(
        fixes, sogs, engine_samples, geocoder=geocoder, speed_threshold_kn=0.5, min_stop_minutes=10
    )

    assert len(trips) == 1


def test_no_samples_returns_no_trips():
    assert build_trips([], [], []) == []
