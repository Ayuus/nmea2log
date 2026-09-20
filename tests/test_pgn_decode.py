import math
import struct
from datetime import date, datetime, timedelta

import pytest

from nmea2log.pgn_decode import (
    decode_gnss_dops,
    decode_battery_status,
    decode_cog,
    decode_engine_dynamic,
    decode_engine_rapid,
    decode_position_rapid,
    decode_sea_temperature,
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


def test_decode_cog():
    cog_raw = round(math.radians(225.0) / 0.0001)  # 225 degrees
    # sid(1B) + cogRef/reserved(1B) + cog(2B) + sog(2B) + reserved(2B)
    data = struct.pack("<BBHHH", 0, 0, cog_raw, 0, 0xFFFF)

    assert decode_cog(data) == pytest.approx(225.0, abs=0.01)


def test_decode_cog_not_available():
    data = struct.pack("<BBHHH", 0, 0, 0xFFFF, 0, 0xFFFF)

    assert decode_cog(data) is None


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


def test_decode_engine_rapid():
    # instance(1B) + engineSpeed(2B, 0.25 rpm/bit) + boostPressure(2B) + tiltTrim(1B signed)
    rpm_raw = round(2400.0 / 0.25)
    data = struct.pack("<BHHb", 1, rpm_raw, 0xFFFF, 0x7F)

    instance, rpm = decode_engine_rapid(data)

    assert instance == 1
    assert rpm == pytest.approx(2400.0)


def test_decode_engine_rapid_not_available():
    data = struct.pack("<BHHb", 0, 0xFFFF, 0xFFFF, 0x7F)

    instance, rpm = decode_engine_rapid(data)

    assert instance == 0
    assert rpm is None


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


def test_decode_sea_temperature():
    # sid(1B) + instance(1B) + source(1B, 0 = sea temperature) + actualTemp(2B, res 0.01K) + setTemp(2B)
    temp_raw = round((21.82 + 273.15) / 0.01)
    data = struct.pack("<BBBHH", 0, 0, 0, temp_raw, 0xFFFF)

    assert decode_sea_temperature(data) == pytest.approx(21.82, abs=1e-6)


def test_decode_sea_temperature_ignores_other_sources():
    # source 1 = outside (air) temperature, not sea -- should be filtered out, not returned
    temp_raw = round((21.82 + 273.15) / 0.01)
    data = struct.pack("<BBBHH", 0, 0, 1, temp_raw, 0xFFFF)

    assert decode_sea_temperature(data) is None


def test_decode_sea_temperature_not_available():
    data = struct.pack("<BBBHH", 0, 0, 0, 0xFFFF, 0xFFFF)

    assert decode_sea_temperature(data) is None


def test_decode_battery_status():
    # instance(1B) + voltage(2B, res 0.01V, signed) + current(2B, res 0.1A, signed) + temp(2B) + sid(1B)
    voltage_raw = round(13.45 / 0.01)
    data = struct.pack("<BhhHB", 0, voltage_raw, 0x7FFF, 0xFFFF, 0)

    instance, voltage_v = decode_battery_status(data)

    assert instance == 0
    assert voltage_v == pytest.approx(13.45)


def test_decode_battery_status_not_available():
    data = struct.pack("<BhhHB", 1, 0x7FFF, 0x7FFF, 0xFFFF, 0)

    instance, voltage_v = decode_battery_status(data)

    assert instance == 1
    assert voltage_v is None


def test_decode_gnss_dops_reads_a_normal_3d_solution():
    # Real payload from a boat's GNSS receiver during normal operation: HDOP 0.70, VDOP 1.10.
    data = bytes.fromhex("9bd746006e00ff7f")

    assert decode_gnss_dops(data) == (2, pytest.approx(0.70))


def test_decode_gnss_dops_reads_the_receivers_no_fix_placeholder():
    # Real payload, same receiver in the first seconds after power-on, before it had a fix: HDOP 99.00.
    data = bytes.fromhex("04d7ac26ac26ff7f")

    assert decode_gnss_dops(data) == (2, pytest.approx(99.0))


def test_decode_gnss_dops_reads_not_available_at_power_off():
    # Real payload, same receiver in the last second before it lost power: mode and HDOP both "not available".
    data = bytes.fromhex("01ffff7fff7fff7f")

    assert decode_gnss_dops(data) == (None, None)


def test_decode_gnss_dops_none_when_payload_too_short():
    assert decode_gnss_dops(b"") is None
