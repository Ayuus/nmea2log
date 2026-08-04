import struct
from datetime import date, datetime, timedelta

import pytest

from nmea2000processor.pgn_decode import (
    decode_engine_dynamic,
    decode_position_rapid,
    decode_sog,
    decode_system_time,
    decode_trip_fuel_engine,
    decode_water_depth,
)


def test_decode_position_rapid():
    lat_raw = round(52.3676 / 1e-7)
    lon_raw = round(4.9041 / 1e-7)
    data = struct.pack("<ii", lat_raw, lon_raw)

    lat, lon = decode_position_rapid(data)

    assert lat == pytest.approx(52.3676, abs=1e-6)
    assert lon == pytest.approx(4.9041, abs=1e-6)


def test_decode_position_rapid_not_available():
    data = struct.pack("<ii", 0x7FFFFFFF, 0x7FFFFFFF)
    assert decode_position_rapid(data) is None


def test_decode_sog():
    sog_raw = round(3.5 / 0.01)  # 3,5 m/s
    # sid(1B) + cogRef/reserved(1B) + cog(2B) + sog(2B) + reserved(2B)
    data = struct.pack("<BBHHH", 0, 0, 0, sog_raw, 0xFFFF)

    assert decode_sog(data) == pytest.approx(3.5)


def test_decode_engine_dynamic_full_fields():
    data = struct.pack(
        "<BHHHhhIHHBHHbb",
        0,  # instance
        3000,  # oil pressure raw -> 300000 Pa
        3531,  # oil temperature raw -> 353.1 K
        35315,  # coolant temperature raw -> 353.15 K
        1420,  # alternator voltage raw -> 14.20 V
        45,  # fuel rate raw -> 4.5 L/hour
        36000,  # total engine hours (s)
        0xFFFF,  # coolant pressure (n/a, not read out)
        0xFFFF,  # fuel pressure (n/a, not read out)
        0xFF,  # reserved
        0b0000000000000100,  # discrete status 1: bit 2 = Low Oil Pressure
        0b0000000000001000,  # discrete status 2: bit 3 = Maintenance Needed
        45,  # engine load (%)
        0x7F,  # engine torque (n/a, not read out)
    )

    result = decode_engine_dynamic(data)

    assert result["instance"] == 0
    assert result["fuel_rate_lph"] == pytest.approx(4.5)
    assert result["total_hours_s"] == 36000
    assert result["oil_pressure_pa"] == pytest.approx(300000.0)
    assert result["oil_temperature_k"] == pytest.approx(353.1)
    assert result["coolant_temperature_k"] == pytest.approx(353.15)
    assert result["alternator_voltage_v"] == pytest.approx(14.20)
    assert result["engine_load_pct"] == pytest.approx(45.0)
    assert result["warnings"] == frozenset({"Low Oil Pressure", "Maintenance Needed"})


def test_decode_engine_dynamic_not_available():
    data = struct.pack(
        "<BHHHhhIHHBHHbb",
        1, 0xFFFF, 0xFFFF, 0xFFFF, 0x7FFF, 0x7FFF, 500, 0xFFFF, 0xFFFF, 0xFF, 0xFFFF, 0xFFFF, 0x7F, 0x7F,
    )

    result = decode_engine_dynamic(data)

    assert result["instance"] == 1
    assert result["fuel_rate_lph"] is None
    assert result["total_hours_s"] == 500
    assert result["oil_pressure_pa"] is None
    assert result["warnings"] == frozenset()


def test_decode_trip_fuel_engine():
    # instance(1B) + tripFuelUsed(2B) + fuelRateAverage(2B) + fuelRateEconomy(2B) + instantaneousFuelEconomy(2B)
    data = struct.pack("<BHhhh", 0, 123, 50, 45, 60)

    instance, trip_fuel_l = decode_trip_fuel_engine(data)

    assert instance == 0
    assert trip_fuel_l == pytest.approx(123.0)


def test_decode_trip_fuel_engine_not_available():
    data = struct.pack("<BHhhh", 2, 0xFFFF, 0x7FFF, 0x7FFF, 0x7FFF)

    instance, trip_fuel_l = decode_trip_fuel_engine(data)

    assert instance == 2
    assert trip_fuel_l is None


def test_decode_water_depth():
    # sid(1B) + depth(4B, res 0,01) + offset(2B) + range(1B)
    data = struct.pack("<BIhB", 0, 250, 0, 0)

    assert decode_water_depth(data) == pytest.approx(2.50)


def test_decode_water_depth_not_available():
    data = struct.pack("<BIhB", 0, 0xFFFFFFFF, 0, 0)

    assert decode_water_depth(data) is None


def test_decode_system_time():
    when = datetime(2026, 7, 15, 9, 30, 15, 500000)
    epoch_days = (when.date() - date(1970, 1, 1)).days
    seconds_of_day = (when - datetime.combine(when.date(), datetime.min.time())).total_seconds()
    time_raw = round(seconds_of_day / 0.0001)
    # sid(1B) + source(4bit)/reserved(4bit) + date(2B) + time(4B)
    data = struct.pack("<BBH", 0, 0, epoch_days) + struct.pack("<I", time_raw)

    result = decode_system_time(data)

    assert result == when


def test_decode_system_time_not_available():
    data = struct.pack("<BBH", 0, 0, 0xFFFF) + struct.pack("<I", 0)

    assert decode_system_time(data) is None
