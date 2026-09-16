"""Regenerates examples/demo-logbook.html, using the real write_html_logbook() renderer but
entirely made-up boat identity, dates and trip stats -- no real personal data. Run from the repo
root: `python examples/generate_demo_logbook.py`.
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from nmea2log.tripbuilder import TripLeg, NavSample, EngineHealth, BatteryHealth
from nmea2log.html_writer import write_html_logbook

# Fictional Wadden Sea cruise -- real, well-known public harbours (not identifying of any
# individual boat/owner), but a fictional boat, fictional MMSI/call sign, fictional dates and
# fictional trip statistics throughout.
STOPS = [
    ("Enkhuizen", 52.7040, 5.2913),
    ("Medemblik", 52.7690, 5.1050),
    ("Den Oever", 52.9330, 5.0300),
    ("Oudeschild (Texel)", 53.0400, 4.8460),
    ("West-Terschelling", 53.3610, 5.2230),
    ("Enkhuizen", 52.7040, 5.2913),
]

# (distance_nm, duration_hours, avg_speed, max_speed, fuel_liters, min_depth_m)
STATS = [
    (11.5, 2.3, 5.0, 6.8, 4.2, 2.8),
    (14.0, 2.6, 5.4, 7.1, 5.1, 3.5),
    (16.8, 3.1, 5.4, 7.4, 6.0, 4.1),
    (34.2, 5.8, 5.9, 7.9, 12.4, 6.2),
    (36.0, 6.4, 5.6, 7.6, 13.1, 5.4),
]

# Extra via-points per leg so the drawn track follows open water instead of a straight
# point-to-point line -- a pure straight line cuts across land in several places here (e.g.
# Enkhuizen-Medemblik crosses the Andijk peninsula, Medemblik-Den Oever crosses the
# Wieringermeer polder) -- found by checking each leg's rendered map against OpenStreetMap.
# Each entry is a list of (lat, lon) waypoints inserted between depart and arrive.
VIA_POINTS = [
    [(52.76, 5.33)],                   # Enkhuizen -> Medemblik: around the Andijk peninsula
    [(52.85, 5.25)],                   # Medemblik -> Den Oever: around the Wieringermeer coast
    [(53.02, 4.92)],                   # Den Oever -> Oudeschild: through the open Waddenzee
    [(53.305, 5.205)],                 # Oudeschild -> West-Terschelling: through the Vliestroom gap
    [(53.075, 5.341), (52.85, 5.30)],  # West-Terschelling -> Enkhuizen: through the Kornwerderzand
    # lock (the only gap in the Afsluitdijk near here) -- a waypoint that isn't actually at the
    # lock leaves the straight segment either side of it cutting across the dijk itself, found by
    # checking against OpenStreetMap after the first fix still crossed the dijk.
]

start = datetime(2025, 6, 14, 8, 0, 0)

trips = []
t = start
for i in range(5):
    depart_name, depart_lat, depart_lon = STOPS[i]
    arrive_name, arrive_lat, arrive_lon = STOPS[i + 1]
    dist, dur_h, avg_kn, max_kn, fuel, min_depth = STATS[i]

    depart_time = t
    arrive_time = depart_time + timedelta(hours=dur_h)

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
            engine_hours_total={0: 1200.0 + i * dur_h},
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

    t = arrive_time + timedelta(hours=20)  # overnight stay before the next leg

out_path = REPO_ROOT / "examples" / "demo-logbook.html"

write_html_logbook(
    trips,
    out_path,
    boat_name="Zeezwaluw",
    mmsi="244012345",
    call_sign="PA1234",
    utc_offset_hours=2.0,
    latest_data_at=trips[-1].arrive_time,
)

print(f"wrote {out_path} ({out_path.stat().st_size} bytes)")
