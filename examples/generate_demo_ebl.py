"""Writes the demo .ebl files: what an Actisense W2K-2 would have logged on the fictional cruise of the demo logbook
(examples/demo_cruise.py), with positions, speed and course, a diesel engine (revolutions, fuel, hours, oil and coolant),
water and air temperature, humidity, wind, depth, heel and a battery. Everything is made up and computed; there is no real
boat or real recording behind it. Run from the repo root:

    python examples/generate_demo_ebl.py            # writes examples/demo-data/Actisense/EBL000001/000001_00N.ebl
    python examples/generate_demo_ebl.py --output /tmp/demo   # or somewhere else

The files are the same bytes every time (a fixed random seed), so a test can compare them with the ones in the repository.
Importing them in the Android or iOS app (or `nmea2log --ebl-dir examples/demo-data/Actisense`) builds the five trips of
examples/demo-logbook.html. One file per trip, a quarter of an hour of the boat lying still on either side of it.
"""

import argparse
import math
import random
import struct
import sys
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import demo_cruise  # noqa: E402

ESC, SOH, NL = 0x1B, 0x01, 0x0A
STEP_S = 5  # one position every 5 seconds
KNOT = 0.514444  # m/s
ENGINE_SOURCE, GPS_SOURCE, SENSOR_SOURCE = 5, 11, 20

BEFORE_ENGINE_OFF_S = 8 * 60  # lying still, engine off, at the start of a file
IDLE_BEFORE_S = 4 * 60  # engine idling before departure
IDLE_AFTER_S = 3 * 60  # idling after arrival
AFTER_ENGINE_OFF_S = 8 * 60  # lying still, engine off, at the end of a file (a stop of 11 minutes in all)
RAMP_S = 120  # speeding up and slowing down


# -- the bytes of an .ebl file: BST-95 records (a CAN frame each), wrapped in ESC SOH ... ESC NL with the ESC bytes doubled --

def frame_bytes(message: bytes) -> bytes:
    stuffed = bytearray()
    for b in message:
        stuffed.append(b)
        if b == ESC:
            stuffed.append(ESC)
    return bytes([ESC, SOH]) + bytes(stuffed) + bytes([ESC, NL])


def can_id(priority: int, pgn: int, source: int) -> int:
    dp = (pgn >> 16) & 1
    pf = (pgn >> 8) & 0xFF
    ps = 0xFF if pf < 240 else pgn & 0xFF
    return source | (ps << 8) | (pf << 16) | (dp << 24) | (priority << 26)


def bst95(cid: int, payload: bytes) -> bytes:
    body = struct.pack("<H", 0) + struct.pack("<I", cid) + payload
    return bytes([0x07, 0x95, len(body)]) + body


def u8(x: float) -> int:
    return max(0, min(0xFE, round(x)))


def u16(x: float) -> int:
    return max(0, min(0xFFFE, round(x)))


def s16(x: float) -> int:
    return max(-0x7FFE, min(0x7FFE, round(x)))


class Writer:
    """Collects the records of one file."""

    def __init__(self) -> None:
        self.data = bytearray()
        self._fast_sequence = 0

    def single(self, priority: int, pgn: int, source: int, payload: bytes) -> None:
        self.data += frame_bytes(bst95(can_id(priority, pgn, source), payload))

    def fast(self, priority: int, pgn: int, source: int, payload: bytes) -> None:
        """A message of more than 8 bytes as NMEA 2000 fast packet frames: byte 0 = sequence counter << 5 | frame number,
        the first frame carries the length and 6 bytes, the others 7 bytes each."""
        sequence = self._fast_sequence << 5
        self._fast_sequence = (self._fast_sequence + 1) % 8
        chunks = [payload[:6]] + [payload[i : i + 7] for i in range(6, len(payload), 7)]
        for number, chunk in enumerate(chunks):
            head = bytes([sequence | number]) + (bytes([len(payload)]) if number == 0 else b"")
            frame = (head + chunk).ljust(8, b"\xff")
            self.single(priority, pgn, source, frame)


# -- the messages (the layouts nmea2log/pgn_decode.py reads) --

def system_time(w: Writer, when: datetime) -> None:
    days = (when.date() - date(1970, 1, 1)).days
    secs = (when - datetime.combine(when.date(), datetime.min.time())).total_seconds()
    w.single(3, 126992, 0, struct.pack("<BBH", 0, 0, days) + struct.pack("<I", round(secs / 0.0001)))


def position(w: Writer, lat: float, lon: float) -> None:
    w.single(2, 129025, GPS_SOURCE, struct.pack("<ii", round(lat * 1e7), round(lon * 1e7)))


def cog_sog(w: Writer, cog_deg: float, sog_ms: float) -> None:
    w.single(2, 129026, GPS_SOURCE, struct.pack("<BBHHH", 0, 0, u16(math.radians(cog_deg) * 1e4), u16(sog_ms * 100), 0xFFFF))


def gnss_dops(w: Writer, hdop: float) -> None:
    # SID, desired mode 3 (auto) + actual mode 2 (3D), HDOP/VDOP/TDOP in 0.01
    w.single(6, 129539, GPS_SOURCE, struct.pack("<BBhhh", 0, 3 | (2 << 3), s16(hdop * 100), s16(hdop * 130), 0x7FFF))


def engine_rapid(w: Writer, rpm: float) -> None:
    w.single(2, 127488, ENGINE_SOURCE, struct.pack("<BHHbBB", 0, u16(rpm * 4), 0xFFFF, 0x7F, 0xFF, 0xFF))


def engine_dynamic(w: Writer, oil_bar: float, oil_c: float, coolant_c: float, alternator_v: float, fuel_lph: float,
                   hours_s: int, load_pct: float) -> None:
    payload = struct.pack(
        "<BHHHhhIHHBHHbb",
        0,
        u16(oil_bar * 1000),  # 100 Pa
        u16((oil_c + 273.15) * 10),  # 0.1 K
        u16((coolant_c + 273.15) * 100),  # 0.01 K
        s16(alternator_v * 100),
        s16(fuel_lph * 10),
        hours_s,
        0xFFFF,  # coolant pressure
        0xFFFF,  # fuel pressure
        0xFF,
        0,  # status 1
        0,  # status 2
        round(load_pct),
        0x7F,  # torque: not available
    )
    w.fast(2, 127489, ENGINE_SOURCE, payload)


def water_depth(w: Writer, depth_m: float) -> None:
    w.single(3, 128267, SENSOR_SOURCE, struct.pack("<BIhB", 0, round(depth_m * 100), 0, 0xFF))


def temperature(w: Writer, source: int, celsius: float) -> None:
    w.single(5, 130312, SENSOR_SOURCE, struct.pack("<BBBHHBB", 0, source, source, u16((celsius + 273.15) * 100), 0xFFFF, 0xFF, 0xFF)[:8])


def humidity(w: Writer, percent: float) -> None:
    w.single(5, 130313, SENSOR_SOURCE, struct.pack("<BBBhhB", 0, 0, 1, s16(percent / 0.004), 0x7FFF, 0xFF)[:8])


def wind(w: Writer, speed_ms: float, angle_deg: float) -> None:
    w.single(2, 130306, SENSOR_SOURCE, struct.pack("<BHHBBB", 0, u16(speed_ms * 100), u16(math.radians(angle_deg) * 1e4), 2, 0xFF, 0xFF))


def speed_through_water(w: Writer, stw_ms: float) -> None:
    w.single(2, 128259, SENSOR_SOURCE, struct.pack("<BHHBBB", 0, u16(stw_ms * 100), 0xFFFF, 0xFF, 0xFF, 0xFF))


def battery(w: Writer, volts: float) -> None:
    w.single(6, 127508, SENSOR_SOURCE, struct.pack("<BhhHB", 0, s16(volts * 100), 0x7FFF, 0xFFFF, 0)[:8])


def attitude(w: Writer, pitch_deg: float, roll_deg: float) -> None:
    w.single(2, 127257, SENSOR_SOURCE, struct.pack("<BhhhB", 0, 0x7FFF, s16(math.radians(pitch_deg) * 1e4), s16(math.radians(roll_deg) * 1e4), 0xFF))


# -- the boat --

def bearing(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    lat1, lat2 = math.radians(a[0]), math.radians(b[0])
    dlon = math.radians(b[1] - a[1])
    y = math.sin(dlon) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def along(points: List[Tuple[float, float]], fraction: float) -> Tuple[float, float]:
    """The point at ``fraction`` (0..1) of the length of the route."""
    lengths = [demo_cruise.distance_nm(points[k], points[k + 1]) for k in range(len(points) - 1)]
    target = max(0.0, min(1.0, fraction)) * sum(lengths)
    for k, length in enumerate(lengths):
        if target <= length or k == len(lengths) - 1:
            f = 0.0 if length == 0 else min(1.0, target / length)
            return points[k][0] + (points[k + 1][0] - points[k][0]) * f, points[k][1] + (points[k + 1][1] - points[k][1]) * f
        target -= length
    return points[-1]


def smoothstep(x: float) -> float:
    x = max(0.0, min(1.0, x))
    return x * x * (3 - 2 * x)


def build_leg(leg: int) -> bytes:
    """One file: the boat lying still, the engine started, the leg, the engine stopped, the boat lying still again."""
    rng = random.Random(1000 + leg)
    depart, arrive = demo_cruise.leg_times(leg)
    duration_s = round((arrive - depart).total_seconds())
    route = demo_cruise.waypoints(leg)
    length_nm = demo_cruise.leg_distance_nm(leg)
    average_kn = length_nm / (duration_s / 3600.0)
    fuel_target_l = length_nm * demo_cruise.FUEL_PER_NM
    min_depth = demo_cruise.MIN_DEPTH_M[leg]
    phase = rng.uniform(0, 2 * math.pi)

    start = depart - timedelta(seconds=BEFORE_ENGINE_OFF_S + IDLE_BEFORE_S)
    total_s = BEFORE_ENGINE_OFF_S + IDLE_BEFORE_S + duration_s + IDLE_AFTER_S + AFTER_ENGINE_OFF_S
    steps = total_s // STEP_S

    # the speed under way: ramps, a slow swell in it, scaled so the route is covered exactly in the time of the leg
    raw = []
    for k in range(duration_s // STEP_S + 1):
        t = k * STEP_S
        ramp = smoothstep(t / RAMP_S) * smoothstep((duration_s - t) / RAMP_S)
        raw.append(ramp * (1.0 + 0.08 * math.sin(2 * math.pi * t / 1100 + phase) + 0.03 * math.sin(2 * math.pi * t / 230)))
    scale = length_nm / (sum(raw) * STEP_S / 3600.0)
    speeds_kn = [r * scale for r in raw]
    covered = [0.0]
    for v in speeds_kn[:-1]:
        covered.append(covered[-1] + v * STEP_S / 3600.0)

    fuel_rates = [0.55 * (v / average_kn) ** 1.6 + 0.0 for v in speeds_kn]  # relative, scaled below
    idle_fuel_l = 0.6 * (IDLE_BEFORE_S + IDLE_AFTER_S) / 3600.0
    running_total = sum(fuel_rates) * STEP_S / 3600.0
    fuel_scale = (fuel_target_l - idle_fuel_l) / running_total
    hours_before = demo_cruise.ENGINE_HOURS_AT_START + sum(
        d + (IDLE_BEFORE_S + IDLE_AFTER_S) / 3600.0 for d in demo_cruise.DURATIONS_H[:leg]
    )

    w = Writer()
    last_course = bearing(route[0], route[1])
    engine_started_s = BEFORE_ENGINE_OFF_S
    engine_stopped_s = BEFORE_ENGINE_OFF_S + IDLE_BEFORE_S + duration_s + IDLE_AFTER_S
    for step in range(steps + 1):
        t = step * STEP_S
        when = start + timedelta(seconds=t)
        under_way_t = t - BEFORE_ENGINE_OFF_S - IDLE_BEFORE_S
        moving = 0 <= under_way_t <= duration_s
        index = min(len(speeds_kn) - 1, max(0, under_way_t // STEP_S)) if moving else 0
        if under_way_t < 0:
            lat, lon = route[0]
            speed_kn = 0.0
        elif under_way_t > duration_s:
            lat, lon = route[-1]
            speed_kn = 0.0
        else:
            lat, lon = along(route, covered[index] / length_nm)
            speed_kn = speeds_kn[index]
            ahead = along(route, min(1.0, covered[index] / length_nm + 0.002))
            if ahead != (lat, lon):
                last_course = bearing((lat, lon), ahead)
        sog_ms = speed_kn * KNOT + (rng.uniform(0.0, 0.04) if speed_kn < 0.3 else rng.uniform(-0.03, 0.03))
        lat += rng.gauss(0, 0.5) / 111_000.0
        lon += rng.gauss(0, 0.5) / (111_000.0 * math.cos(math.radians(lat)))
        engine_running = engine_started_s <= t < engine_stopped_s

        if t % 10 == 0:
            system_time(w, when)
        position(w, lat, lon)
        cog_sog(w, (last_course + (rng.gauss(0, 1.5) if speed_kn > 0.5 else 0.0)) % 360.0, max(0.0, sog_ms))
        if t % 10 == 0:
            # the heel of the boat: a few degrees under way, hardly any in harbour
            swing = 2.6 if speed_kn > 0.5 else 0.25
            attitude(w, rng.gauss(0, 0.8 if speed_kn > 0.5 else 0.1) + 1.4 * math.sin(t / 9.0),
                     swing * math.sin(t / 7.0 + phase) + rng.gauss(0, 0.5 if speed_kn > 0.5 else 0.08)
                     + (1.2 * math.sin(t / 311.0) ** 15 if speed_kn > 0.5 else 0.0))
            gnss_dops(w, 0.9 + 0.2 * abs(math.sin(t / 400.0)))
            fraction = max(0.0, min(1.0, under_way_t / duration_s))
            water_c = 17.5 + 0.7 * math.sin(2 * math.pi * fraction * 1.5 + phase)
            temperature(w, 0, min(18.2, max(16.8, water_c)))
            temperature(w, 1, 19.5 + 1.5 * math.sin(t / 3000.0 + phase) + rng.gauss(0, 0.05))
            humidity(w, 74 + 4 * math.sin(t / 2600.0 + phase))
            wind(w, max(0.3, 5.0 + 2.0 * math.sin(t / 700.0 + phase) + rng.gauss(0, 0.3)) if moving else 2.0 + rng.gauss(0, 0.2),
                 (45 + 15 * math.sin(t / 900.0) + rng.gauss(0, 2)) % 360.0)
            if moving:
                speed_through_water(w, max(0.0, sog_ms * 0.97 + rng.gauss(0, 0.02)))
                depth = min_depth + 4.0 + 3.5 * math.sin(fraction * 23.0 + phase)
                depth = max(min_depth, depth)
                if abs(fraction - 0.5) < 0.5 * STEP_S * 2 / duration_s:
                    depth = min_depth  # the shallowest point of the leg
            else:
                depth = min_depth + 1.2
            water_depth(w, depth)
            battery(w, (13.9 if engine_running else 12.55) + rng.gauss(0, 0.03) - (0.15 if not engine_running and t > 0 else 0.0))
        if engine_running:
            running_s = t - engine_started_s
            idling = not moving
            rpm = (850 + rng.gauss(0, 8)) if idling else 1800 + (speed_kn - average_kn) * 70 + rng.gauss(0, 12)
            engine_rapid(w, rpm)
            if t % 10 == 0:
                if idling:
                    fuel_lph = 0.6 + rng.gauss(0, 0.02)
                else:
                    fuel_lph = fuel_rates[index] * fuel_scale + rng.gauss(0, 0.02)
                load = 12 if idling else 38 + 25 * (speed_kn / average_kn)
                engine_dynamic(
                    w,
                    oil_bar=3.2 + rng.gauss(0, 0.05) if not idling else 1.9 + rng.gauss(0, 0.05),
                    oil_c=88 - 45 * math.exp(-running_s / 240.0),
                    coolant_c=82 - 52 * math.exp(-running_s / 220.0),
                    alternator_v=(14.1 if not idling else 13.8) + rng.gauss(0, 0.02),
                    fuel_lph=max(0.0, fuel_lph),
                    hours_s=round(hours_before * 3600 + running_s),
                    load_pct=load,
                )
    return bytes(w.data)


def write_all(output: Path) -> List[Path]:
    folder = output / "Actisense" / "EBL000001"
    folder.mkdir(parents=True, exist_ok=True)
    paths = []
    for leg in range(len(demo_cruise.DURATIONS_H)):
        path = folder / f"000001_{leg + 1:03d}.ebl"
        path.write_bytes(build_leg(leg))
        paths.append(path)
    return paths


def write_zip(zip_path: Path, source: Path) -> None:
    """The folder ``source``/Actisense in a zip (the release asset demo-ebl.zip), the same bytes every time."""
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted((source / "Actisense").rglob("*.ebl")):
            info = zipfile.ZipInfo(path.relative_to(source).as_posix(), date_time=(2025, 6, 18, 12, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, path.read_bytes())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Writes the demo .ebl files.")
    parser.add_argument("--output", type=Path, default=REPO_ROOT / "examples" / "demo-data")
    parser.add_argument("--zip", type=Path, help="also write the files as a zip (the release asset demo-ebl.zip) to this path")
    args = parser.parse_args()
    for written in write_all(args.output):
        print(f"wrote {written} ({written.stat().st_size} bytes)")
    if args.zip:
        write_zip(args.zip, args.output)
        print(f"wrote {args.zip} ({args.zip.stat().st_size} bytes)")
