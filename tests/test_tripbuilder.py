from datetime import datetime, timedelta

import pytest

from nmea2000processor.model import DepthSample, EngineSample, PositionFix, SogSample, TripFuelSample
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
    assert trip.fuel_liters_device is None  # geen PGN 127497 meegegeven
    assert trip.avg_speed_kn == pytest.approx(3.0 / 0.514444, rel=1e-3)
    assert trip.max_speed_kn == pytest.approx(3.0 / 0.514444, rel=1e-3)
    assert trip.min_depth_m is None  # geen dieptedata meegegeven
    # motor 0 heeft wel samples, maar geen van de gezondheidsvelden was ingevuld
    assert trip.engine_health[0].oil_pressure_bar_avg is None
    assert trip.engine_health[0].warnings == frozenset()
    # geocoder moet niet vaker dan het aantal havenbezoeken zijn aangeroepen
    assert len(geocoder.calls) == 2


def test_trip_fuel_device_delta():
    fixes, sogs, engine_samples = _build_scenario()
    geocoder = _StubGeocoder()

    # motor-eigen triptmeter: loopt gestaag op, alleen tijdens het varen relevant voor de reis
    trip_fuel_samples = [
        TripFuelSample(_dt(m), 0, 100.0 + m * 0.2) for m in range(0, 54)
    ]

    trips = build_trips(
        fixes, sogs, engine_samples, trip_fuel_samples,
        geocoder=geocoder, speed_threshold_kn=0.5, min_stop_minutes=10,
    )

    assert len(trips) == 1
    trip = trips[0]
    assert trip.fuel_liters_device is not None
    assert trip.fuel_liters_device > 0
    # verschil tussen begin- en eindstand van de reis, niet van de hele periode
    assert trip.fuel_liters_device < (100.0 + 53 * 0.2) - 100.0


def test_min_depth_with_position():
    fixes, sogs, engine_samples = _build_scenario()
    geocoder = _StubGeocoder()

    # diepte varieert tijdens het varen (m 12..41), met een duidelijk dieptepunt op m=25
    depth_samples = []
    for m in range(0, 54):
        if 12 <= m < 42:
            depth = 2.0 if m == 25 else 8.0
        else:
            depth = 15.0  # in de haven, niet relevant voor deze reis
        depth_samples.append(DepthSample(_dt(m), depth))

    trips = build_trips(
        fixes, sogs, engine_samples, None, depth_samples,
        geocoder=geocoder, speed_threshold_kn=0.5, min_stop_minutes=10,
    )

    assert len(trips) == 1
    trip = trips[0]
    assert trip.min_depth_m == pytest.approx(2.0)
    # de ondiepste positie hoort bij fix m=25, ergens op de rechte lijn tussen de havens
    assert trip.min_depth_lat is not None
    assert trip.min_depth_lon is not None


def test_engine_health_and_warnings():
    fixes, sogs, _unused_engine = _build_scenario()
    geocoder = _StubGeocoder()

    engine_samples = []
    for m in range(0, 54):
        fuel_rate = 8.0 if 12 <= m < 42 else 0.5
        warnings = frozenset({"Low Oil Pressure"}) if m == 20 else frozenset()
        engine_samples.append(
            EngineSample(
                _dt(m), 0, fuel_rate, 3600 * 100 + m * 60,
                oil_pressure_pa=300000.0,
                oil_temperature_k=350.0,
                coolant_temperature_k=355.0,
                alternator_voltage_v=14.2,
                engine_load_pct=60.0 if m != 30 else 90.0,
                warnings=warnings,
            )
        )

    trips = build_trips(
        fixes, sogs, engine_samples, geocoder=geocoder, speed_threshold_kn=0.5, min_stop_minutes=10
    )

    assert len(trips) == 1
    health = trips[0].engine_health[0]
    assert health.oil_pressure_bar_avg == pytest.approx(3.0)
    assert health.coolant_temperature_c_avg == pytest.approx(355.0 - 273.15)
    assert health.alternator_voltage_v_avg == pytest.approx(14.2)
    assert health.engine_load_pct_max == pytest.approx(90.0)
    # de waarschuwing die op m=20 (tijdens het varen) actief was, moet zichtbaar zijn
    assert "Low Oil Pressure" in health.warnings


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


def test_large_data_gap_keeps_harbor_but_excludes_gap_from_duration():
    """Regressietest voor een echte bug, gevonden met echte data: een groot gat in de data
    (bv. het apparaat/de log stond een tijd stil) liet de gerapporteerde vaartijd doorlopen tot
    ver na het gat (in de praktijk: 3:21 i.p.v. de echte ~0:45, terwijl de draaiurenteller --
    die niet van GPS-classificatie afhangt -- het wel bij het rechte eind had).

    Een eerdere fix loste dat op door reizen bij elk gat in aparte stukken te knippen, maar dat
    brak op zijn beurt de havenkoppeling: een stilligperiode van maar een paar minuten vlak
    vóór het gat (te kort voor min_stop_minutes) telde niet als havenbezoek, dus de aankomst-
    haven van de reis ervoor werd "Onbekend" in plaats van de echte (bevestigde) ligplaats.

    De juiste aanpak: zo'n korte stilligperiode blijft wél een havenbezoek als hij aan het gat
    grenst (_merge_short_stops), maar de *gerapporteerde duur* van de reis (``trip.duration``)
    negeert het gat (_moving_duration) -- de haven blijft dus gekoppeld, de vaartijd niet
    kunstmatig opgerekt."""
    fixes, sogs, engine_samples = _build_scenario()  # 12 min stil -> 30 min varen -> 12 min stil

    # na de reis: een stilligperiode van maar 5 minuten (te kort voor min_stop_minutes=10)
    short_stop_start = 54
    for m in range(short_stop_start, short_stop_start + 5):
        fixes.append(PositionFix(_dt(m), 52.40, 4.95))
        sogs.append(SogSample(_dt(m), 0.0))
        engine_samples.append(EngineSample(_dt(m), 0, 0.5, 3600 * 100 + m * 60))

    # dan een gat van 3 uur zonder enige data (apparaat/log lag stil)
    gap_end_minute = short_stop_start + 5 + 180

    # na het gat: een aparte, korte reis (ruim boven min_trip_distance_nm)
    for i, m in enumerate(range(gap_end_minute, gap_end_minute + 3)):
        frac = i / 2
        fixes.append(PositionFix(_dt(m), 52.40 + 0.01 * frac, 4.95 + 0.01 * frac))
        sogs.append(SogSample(_dt(m), 2.0))
        engine_samples.append(EngineSample(_dt(m), 0, 0.0, 3600 * 100 + m * 60))  # motor uit

    geocoder = _StubGeocoder()
    trips = build_trips(
        fixes, sogs, engine_samples, geocoder=geocoder, speed_threshold_kn=0.5, min_stop_minutes=10
    )

    assert len(trips) == 2  # de hoofdreis, en de korte reis na het gat, apart
    first_trip, second_trip = trips

    # de hoofdreis behoudt zijn echte aankomsthaven (niet "Onbekend"!)...
    assert first_trip.arrive_place == "Haven@52.40,4.95"
    # ...met een vaartijd die niet het gat overbrugt
    assert first_trip.duration < timedelta(hours=1)

    # de reis na het gat vertrekt logischerwijs vanaf diezelfde haven
    assert second_trip.depart_place == "Haven@52.40,4.95"
    assert second_trip.arrive_place == "Onbekend (einde buiten logbestand)"


def test_negligible_distance_trip_is_filtered_out():
    """Reizen met verwaarloosbare afstand (GPS-/snelheidsruis, vaak vlak bij een segmentgrens)
    horen niet als logboekregel te verschijnen."""
    fixes, sogs, engine_samples = _build_scenario()

    # havenbezoek (>= min_stop_minutes), gevolgd door een piepklein "reisje" van een paar meter
    for m in range(54, 54 + 12):
        fixes.append(PositionFix(_dt(m), 52.40, 4.95))
        sogs.append(SogSample(_dt(m), 0.0))
        engine_samples.append(EngineSample(_dt(m), 0, 0.0, 3600 * 100 + m * 60))
    for m in range(66, 66 + 2):
        fixes.append(PositionFix(_dt(m), 52.4001, 4.9501))  # een paar meter verderop
        sogs.append(SogSample(_dt(m), 2.0))
        engine_samples.append(EngineSample(_dt(m), 0, 0.0, 3600 * 100 + m * 60))

    geocoder = _StubGeocoder()
    trips = build_trips(
        fixes, sogs, engine_samples, geocoder=geocoder, speed_threshold_kn=0.5, min_stop_minutes=10
    )

    # alleen de oorspronkelijke reis (52.30 -> 52.40) hoort over te blijven
    assert len(trips) == 1
    assert trips[0].arrive_place == "Haven@52.40,4.95"


def test_min_trip_distance_nm_can_be_disabled():
    fixes, sogs, engine_samples = _build_scenario()
    for m in range(54, 54 + 12):
        fixes.append(PositionFix(_dt(m), 52.40, 4.95))
        sogs.append(SogSample(_dt(m), 0.0))
        engine_samples.append(EngineSample(_dt(m), 0, 0.0, 3600 * 100 + m * 60))
    for m in range(66, 66 + 2):
        fixes.append(PositionFix(_dt(m), 52.4001, 4.9501))
        sogs.append(SogSample(_dt(m), 2.0))
        engine_samples.append(EngineSample(_dt(m), 0, 0.0, 3600 * 100 + m * 60))

    geocoder = _StubGeocoder()
    trips = build_trips(
        fixes, sogs, engine_samples,
        geocoder=geocoder, speed_threshold_kn=0.5, min_stop_minutes=10, min_trip_distance_nm=0.0,
    )

    assert len(trips) == 2  # met de filter uitgeschakeld telt ook het piepkleine reisje mee


def test_small_data_gap_does_not_split_trip():
    """Een klein gat (bv. een paar seconden tussen twee logbestanden) mag een reis niet
    onnodig opknippen -- alleen gaten van minstens max_gap_minutes doen dat."""
    fixes, sogs, engine_samples = _build_scenario()

    # gat van 2 minuten (43 -> 45) middenin de vaarperiode, ruim onder max_gap_minutes (10)
    fixes = [f for f in fixes if not (43 <= (f.time - _dt(0)).total_seconds() / 60 < 45)]
    sogs = [s for s in sogs if not (43 <= (s.time - _dt(0)).total_seconds() / 60 < 45)]

    geocoder = _StubGeocoder()
    trips = build_trips(
        fixes, sogs, engine_samples, geocoder=geocoder, speed_threshold_kn=0.5, min_stop_minutes=10
    )

    assert len(trips) == 1  # ongewijzigd gedrag: het kleine gat wordt genegeerd
