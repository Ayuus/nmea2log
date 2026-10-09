"""Regenerates examples/demo-logbook.html, using the real write_html_logbook() renderer but
entirely made-up boat identity, dates and trip stats -- no real personal data. Run from the repo
root: `python examples/generate_demo_logbook.py`.

The same fictional cruise is used for the screenshots in the Android and iOS apps' docs: put it
somewhere else and under another boat name with `--output` / `--boat-name`, e.g.
`python examples/generate_demo_logbook.py --boat-name "Sea Swallow" --output /tmp/logbook.html`.
"""
import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from nmea2log.tripbuilder import TripLeg, NavSample, EngineHealth, BatteryHealth
from nmea2log.html_writer import write_html_logbook

import demo_cruise  # the cruise itself (harbours, route, times), shared with generate_demo_ebl.py
from demo_cruise import STOPS, VIA_POINTS

# (distance_nm, duration_hours, avg_speed, max_speed, fuel_liters, min_depth_m) of each leg, from the route of demo_cruise
STATS = []
for _leg, _dur in enumerate(demo_cruise.DURATIONS_H):
    _dist = round(demo_cruise.leg_distance_nm(_leg), 1)
    _avg = round(_dist / _dur, 1)
    STATS.append((_dist, _dur, _avg, round(_avg * 1.25, 1), round(_dist * demo_cruise.FUEL_PER_NM, 1), demo_cruise.MIN_DEPTH_M[_leg]))

trips = []
for i in range(5):
    depart_name, depart_lat, depart_lon = STOPS[i]
    arrive_name, arrive_lat, arrive_lon = STOPS[i + 1]
    dist, dur_h, avg_kn, max_kn, fuel, min_depth = STATS[i]

    depart_time, arrive_time = demo_cruise.leg_times(i)

    waypoints = [(depart_lat, depart_lon), *VIA_POINTS[i], (arrive_lat, arrive_lon)]
    # A handful of NavSamples per leg, spread evenly along the whole via-point polyline (not
    # just depart->arrive) and along the whole trip duration, so the track hugs open water.
    points_per_leg = 4
    track = []
    total_legs = len(waypoints) - 1
    total_steps = total_legs * points_per_leg
    for s in range(total_steps + 1):
        overall_frac = s / total_steps
        leg_idx = min(s // points_per_leg, total_legs - 1)
        leg_frac = (s - leg_idx * points_per_leg) / points_per_leg
        lat0, lon0 = waypoints[leg_idx]
        lat1, lon1 = waypoints[leg_idx + 1]
        track.append(
            NavSample(
                time=depart_time + timedelta(hours=dur_h * overall_frac),
                lat=lat0 + (lat1 - lat0) * leg_frac,
                lon=lon0 + (lon1 - lon0) * leg_frac,
                sog_ms=avg_kn * 0.514444,
            )
        )

    trips.append(
        TripLeg(
            depart_time=depart_time,
            arrive_time=arrive_time,
            depart_place=depart_name,
            arrive_place=arrive_name,
            depart_lat=depart_lat,
            depart_lon=depart_lon,
            arrive_lat=arrive_lat,
            arrive_lon=arrive_lon,
            duration=timedelta(hours=dur_h),
            distance_nm=dist,
            avg_speed_kn=avg_kn,
            max_speed_kn=max_kn,
            fuel_liters=fuel,
            fuel_liters_device=None,
            engine_hours={0: dur_h},
            engine_hours_total={0: demo_cruise.ENGINE_HOURS_AT_START + sum(demo_cruise.DURATIONS_H[: i + 1])},
            engine_health={
                0: EngineHealth(
                    oil_pressure_bar_avg=3.2,
                    oil_temperature_c_avg=88.0,
                    coolant_temperature_c_avg=82.0,
                    alternator_voltage_v_avg=14.1,
                    engine_load_pct_max=68.0,
                    warnings=frozenset(),
                )
            },
            typical_rpm={0: 1800.0},
            typical_rpm_speed_kn={0: (avg_kn - 0.4, avg_kn + 0.4, avg_kn, fuel / dist)},
            battery_health={0: BatteryHealth(avg_voltage_v=12.8, min_voltage_v=12.4)},
            min_depth_m=min_depth,
            min_depth_lat=(depart_lat + arrive_lat) / 2,
            min_depth_lon=(depart_lon + arrive_lon) / 2,
            avg_water_temp_c=17.5,
            min_water_temp_c=16.8,
            max_water_temp_c=18.2,
            roll_variation_deg=2.1,
            pitch_variation_deg=1.4,
            roll_range_deg=8.5,
            pitch_range_deg=5.2,
            track=track,
        )
    )


parser = argparse.ArgumentParser(description="Writes the fictional demo logbook.")
parser.add_argument("--boat-name", default="Zeezwaluw", help="the (fictional) boat's name")
parser.add_argument("--output", type=Path, default=REPO_ROOT / "examples" / "demo-logbook.html")
args = parser.parse_args()
out_path = args.output

write_html_logbook(
    trips,
    out_path,
    boat_name=args.boat_name,
    mmsi="244012345",
    call_sign="PA1234",
    utc_offset_hours=2.0,
    latest_data_at=trips[-1].arrive_time,
)

print(f"wrote {out_path} ({out_path.stat().st_size} bytes)")
