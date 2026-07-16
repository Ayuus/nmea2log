import struct

import pytest

from nmea2000processor.pgn_decode import (
    decode_engine_dynamic,
    decode_position_rapid,
    decode_sog,
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


def test_decode_engine_dynamic():
    fuel_raw = 45  # 0,1 L/uur per eenheid -> 4,5 L/uur
    total_hours_s = 36000  # 10 uur

    data = struct.pack(
        "<BHHHhhIHHBHHbb",
        0,  # instance
        0xFFFF,  # oil pressure (n.v.t.)
        0xFFFF,  # oil temperature (n.v.t.)
        0xFFFF,  # temperature (n.v.t.)
        0x7FFF,  # alternator potential (n.v.t.)
        fuel_raw,  # fuel rate
        total_hours_s,  # total engine hours
        0xFFFF,  # coolant pressure (n.v.t.)
        0xFFFF,  # fuel pressure (n.v.t.)
        0xFF,  # reserved
        0xFFFF,  # discrete status 1 (n.v.t.)
        0xFFFF,  # discrete status 2 (n.v.t.)
        0x7F,  # engine load (n.v.t.)
        0x7F,  # engine torque (n.v.t.)
    )

    instance, fuel_lph, hours_s = decode_engine_dynamic(data)

    assert instance == 0
    assert fuel_lph == pytest.approx(4.5)
    assert hours_s == total_hours_s


def test_decode_engine_dynamic_fuel_not_available():
    data = struct.pack(
        "<BHHHhhIHHBHHbb",
        1, 0xFFFF, 0xFFFF, 0xFFFF, 0x7FFF, 0x7FFF, 500, 0xFFFF, 0xFFFF, 0xFF, 0xFFFF, 0xFFFF, 0x7F, 0x7F,
    )

    instance, fuel_lph, hours_s = decode_engine_dynamic(data)

    assert instance == 1
    assert fuel_lph is None
    assert hours_s == 500
