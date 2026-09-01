"""Writes the whole logbook out as a single, self-contained HTML file: a boat name header,
overall totals (trip count, distance, fuel, engine hours, average consumption), trips grouped
by year and ISO week, a per-trip route map (Leaflet + OpenStreetMap) that opens in a popup when
you click a trip's "Kaart" button, and a "Details" button/popup bundling water temperature,
motion (roll/pitch), and a periodic course/speed/position log (like a traditional paper logbook)
-- these each used to be their own always-visible column, which made the table too wide to
usefully fit a screen (found in practice).

All UI text lives in translations.py, not here -- see that module's docstring for why.

Everything lives in one file -- there's nothing to keep together or link between. Map tiles and
the Leaflet library load from a CDN when you view the page, so viewing the map requires internet
(the file itself needs none to generate or to open; the periodic log has no such dependency).

Both popups are JavaScript-driven (a <dialog> opened via .showModal()), which doesn't work when
this file is opened straight from an email attachment -- essentially every email client strips
<script> tags for security, so the buttons silently do nothing there. There's no way to fix that
while keeping them interactive (a static, always-visible image/table per trip would work in email
too, but was deliberately not built for the map: it needs a network call per trip to render, and
would make the file much bigger for a season's worth of trips). Instead, a <noscript> banner
explains that the file needs to be opened in a real browser.
"""

from __future__ import annotations

import base64
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
from xml.sax.saxutils import escape

from .logbook_writer import (
    _avg_consumption_l_per_nm,
    _duration_minutes,
    _engine_hours_text,
    _format_duration,
    _nl_num,
    _to_local,
    _trip_utc_offset_hours,
    _typical_rpm_text,
)
from .translations import MONTH_ABBR_NL, NL as T
from .tripbuilder import NavSample, TripLeg
from .marine import NoMarine
from .weather import NoWeather

_LEAFLET_CSS = "https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
_LEAFLET_JS = "https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
# Google's own Material Symbols "directions_boat" icon (Apache-2.0) -- used as both the favicon
# and the "Add to Home Screen" icon on iPhone, which otherwise falls back to an ugly screenshot of
# the page itself. Embedded as a data URI (not an external CDN link, unlike Leaflet above) so the
# icon shows up even without a network connection -- no reason a static square icon should depend
# on the internet being reachable.
#
# The original icon's cabin roof has a small rectangular notch (a step up and back down) that
# reads as an odd thickening in the middle of an otherwise straight top line at small icon sizes;
# flattened into a plain "h468" straight across instead (found in practice, asked for explicitly).
_ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 200">
<rect width="200" height="200" fill="#eef4fb"/>
<g transform="translate(30,170) scale(0.14583)">
<path fill="#1a4a7a" d="M178-80h-58v-60h58q40 0 79-11t76-34q35 22 72 32.5t75 10.5q38 0 75-10.5t72-32.5q38 23 77.5 34t78.5 11h57v60h-57q-39 0-78-9t-77-28q-38 19-75 28t-73 9q-36 0-73-9.5T333-117q-38 18-77.5 27.5T178-80Zm226.5-169.5Q365-270 330-307q-33 33-71 52.5T182-230l-71-245q-4-12 2-22.5t18-14.5l55-16v-190q0-25 17.5-42.5T246-778h468q25 0 42.5 17.5T774-718v190l55 16q12 4 18 14.5t2 22.5l-71 245q-39-5-77-24.5T630-307q-35 37-74.5 57.5T480-229q-36 0-75.5-20.5ZM481-289q32 0 58.5-18t47.5-43l41-48 36 38q16 17 34 31t38 25l48-159-304-92-304 92 48 159q20-11 38-25t34-31l36-38 41 48q22 25 49 43t59 18ZM246-547l234-71 234 72v-172H246v171Zm234 125Z"/>
</g>
</svg>"""
_ICON_URL = "data:image/svg+xml;base64," + base64.b64encode(_ICON_SVG.encode("utf-8")).decode("ascii")
_MAX_MAP_POINTS = 500
_KNOT_IN_MS = 0.514444
_DEFAULT_LOG_INTERVAL_MINUTES = 30.0
# Off by default, like --upload -- the Remarks column needs a matching WordPress REST endpoint
# (see wordpress-plugin/) to actually work, which isn't there unless you set it up yourself.
# Typically just "/wp-json/nmea2log/v1/remarks" once set: a relative path resolves against
# whatever site the logbook is opened from, so it doesn't need a host configured too as long as
# logbook.html is uploaded (see --upload) to the same site as the WordPress plugin.
_DEFAULT_REMARKS_API_URL = ""


@dataclass
class _Totals:
    trip_count: int
    distance_nm: float
    fuel_liters: float
    fuel_liters_device: Optional[float]
    moving_hours: float
    engine_hours: Dict[int, float]
    engine_hours_current: Dict[int, float]
    max_speed_kn: Optional[float]


def _compute_totals(trips: List[TripLeg]) -> _Totals:
    distance_nm = sum(trip.distance_nm for trip in trips)
    fuel_liters = sum(trip.fuel_liters for trip in trips)
    device_values = [trip.fuel_liters_device for trip in trips if trip.fuel_liters_device is not None]
    fuel_liters_device = sum(device_values) if device_values else None
    # Summed from each trip's own *rounded* minutes (the same rounding _format_duration uses for
    # the "Duur" column), not the raw unrounded durations -- otherwise this total doesn't exactly
    # equal what a user gets by adding up the visible per-row values by hand (found in practice).
    moving_hours = sum(_duration_minutes(trip.duration) for trip in trips) / 60.0
    engine_hours: Dict[int, float] = {}
    engine_hours_current: Dict[int, float] = {}
    # ``trips`` is already sorted chronologically by the caller, so the last value seen per
    # engine instance is the most recent known reading of its absolute hour meter.
    for trip in trips:
        for instance, hours in trip.engine_hours.items():
            engine_hours[instance] = engine_hours.get(instance, 0.0) + hours
        for instance, hours in trip.engine_hours_total.items():
            engine_hours_current[instance] = hours

    speeds = [trip.max_speed_kn for trip in trips if trip.max_speed_kn is not None]
    max_speed_kn = max(speeds) if speeds else None

    return _Totals(
        trip_count=len(trips),
        distance_nm=distance_nm,
        fuel_liters=fuel_liters,
        fuel_liters_device=fuel_liters_device,
        moving_hours=moving_hours,
        engine_hours=engine_hours,
        engine_hours_current=engine_hours_current,
        max_speed_kn=max_speed_kn,
    )


def _localized_date(d: date) -> str:
    """strftime's %b is locale-independent (always English month abbreviations) unless the
    process locale is changed, which is fragile/platform-dependent -- MONTH_ABBR_NL avoids
    that."""
    text = f"{d:%b %d}"
    month, day = text.split(" ", 1)
    return f"{MONTH_ABBR_NL[month]} {day}"


def _week_label(iso_year: int, iso_week: int) -> str:
    monday = date.fromisocalendar(iso_year, iso_week, 1)
    sunday = monday + timedelta(days=6)
    return f"{T['week_label_prefix']} {iso_week} ({_localized_date(monday)} - {_localized_date(sunday)})"


def _decimated_points(trip: TripLeg) -> List[Tuple[float, float]]:
    stride = max(1, len(trip.track) // _MAX_MAP_POINTS)
    decimated = trip.track[::stride]
    if decimated and decimated[-1] is not trip.track[-1]:
        decimated.append(trip.track[-1])
    return [(round(s.lat, 6), round(s.lon, 6)) for s in decimated]


def _warnings_count(trip: TripLeg, battery_warning_voltage: Optional[float]) -> int:
    """Counts individual warnings (not text segments) directly from the underlying data instead
    of parsing _warnings_tooltip_text's semicolon-joined string, since one engine's segment can
    itself contain several comma-separated warnings."""
    count = sum(len(health.warnings) for health in trip.engine_health.values())
    if battery_warning_voltage is not None:
        count += sum(
            1
            for health in trip.battery_health.values()
            if health.min_voltage_v is not None and health.min_voltage_v < battery_warning_voltage
        )
    return count


def _warnings_tooltip_text(trip: TripLeg, battery_warning_voltage: Optional[float], offset_hours: float) -> str:
    """Same warnings as the CSV's plain-text column (see logbook_writer._all_warnings_text), but
    flattened into one list across every engine/battery and sorted chronologically (time first,
    then the warning text) instead of grouped and alphabetized -- built separately rather than
    reusing that function so the CSV's own text stays exactly as-is for anything already parsing
    it (see this module's docstring: CSV/GPX output is deliberately not touched by HTML-only
    presentation changes)."""
    single_engine = len(trip.engine_health) == 1
    single_battery = len(trip.battery_health) == 1
    entries: List[Tuple[Optional[datetime], str]] = []

    for instance, health in trip.engine_health.items():
        prefix = "" if single_engine else f"engine {instance}: "
        for warning in health.warnings:
            entries.append((health.warning_first_seen.get(warning), f"{prefix}{warning}"))

    if battery_warning_voltage is not None:
        for instance, health in trip.battery_health.items():
            if health.min_voltage_v is None or health.min_voltage_v >= battery_warning_voltage:
                continue
            prefix = "" if single_battery else f"battery {instance}: "
            entries.append((health.min_voltage_at, f"{prefix}low battery {_nl_num(health.min_voltage_v)} V"))

    # Entries without a known time (only possible from data built before warning_first_seen/
    # min_voltage_at existed) sort last instead of crashing on comparing None to a datetime.
    entries.sort(key=lambda entry: (entry[0] is None, entry[0]))
    return ", ".join(
        f"{_to_local(time, offset_hours).strftime('%H:%M')} {text}" if time is not None else text
        for time, text in entries
    )


def _warnings_html(trip: TripLeg, battery_warning_voltage: Optional[float], offset_hours: float) -> str:
    """Just the count in the cell (so a trip with many warnings doesn't blow up the column
    width); hovering shows the actual warning text plus when each one first triggered, same
    pattern as the other tooltip columns."""
    count = _warnings_count(trip, battery_warning_voltage)
    if count == 0:
        return ""
    tooltip = escape(_warnings_tooltip_text(trip, battery_warning_voltage, offset_hours))
    return (
        f'<span class="temp-hover warning-count">{count}'
        f'<span class="temp-tooltip" style="background:#fdecea;color:#7a2b22;">'
        f"⚠️ {tooltip}</span></span>"
    )


def _water_temp_detail_text(trip: TripLeg) -> str:
    """Full text (not a hover badge) since this now lives inside the Details popup (see
    _details_cell_html), which has room to just show it directly instead of needing a compact,
    hover-only cell."""
    if trip.avg_water_temp_c is None:
        return ""
    text = f"{_nl_num(trip.avg_water_temp_c)}°C"
    if trip.max_water_temp_c - trip.min_water_temp_c > 0.5:
        text += f" ({_nl_num(trip.min_water_temp_c)}–{_nl_num(trip.max_water_temp_c)}°C)"
    return text


def _motion_detail_text(trip: TripLeg) -> str:
    """Full text, spelling out which number is roll vs. pitch (slingeren/stampen) and the
    peak-to-peak range directly -- this now lives inside the Details popup (see
    _details_cell_html), which has room for that instead of needing a numbers-only cell plus a
    hover tooltip to explain it."""
    parts = []
    if trip.roll_variation_deg is not None:
        text = f"{T['motion_roll']} ±{_nl_num(trip.roll_variation_deg)}°"
        if trip.roll_range_deg is not None:
            text += f" ({T['motion_peak']} {_nl_num(trip.roll_range_deg)}°)"
        parts.append(text)
    if trip.pitch_variation_deg is not None:
        text = f"{T['motion_pitch']} ±{_nl_num(trip.pitch_variation_deg)}°"
        if trip.pitch_range_deg is not None:
            text += f" ({T['motion_peak']} {_nl_num(trip.pitch_range_deg)}°)"
        parts.append(text)
    return ", ".join(parts)


def _typical_rpm_html(trip: TripLeg) -> str:
    """Plain RPM text in the cell; hovering shows the speed actually recorded while holding that
    RPM. Without this, the RPM number sits right next to the trip's overall *average* speed,
    which is diluted by slower maneuvering in/out of the harbor and reads as if that RPM only
    makes that speed (found in practice: "2250 RPM" next to "11.9 kn avg" looked like 2250 RPM
    made 11.9 kn, when the boat was actually doing 12.6-13.4 kn whenever it held that RPM)."""
    visible_text = _typical_rpm_text(trip)
    if not visible_text:
        return ""
    visible = escape(visible_text)
    if not trip.typical_rpm_speed_kn:
        return visible

    if len(trip.typical_rpm_speed_kn) == 1:
        min_kn, max_kn, avg_kn, avg_fuel_l_per_nm = next(iter(trip.typical_rpm_speed_kn.values()))
        if avg_fuel_l_per_nm is not None:
            tooltip = T["rpm_tooltip_single_fuel"].format(
                avg=_nl_num(avg_kn), min=_nl_num(min_kn), max=_nl_num(max_kn),
                fuel=_nl_num(avg_fuel_l_per_nm, 2),
            )
        else:
            tooltip = T["rpm_tooltip_single"].format(
                avg=_nl_num(avg_kn), min=_nl_num(min_kn), max=_nl_num(max_kn)
            )
    else:
        parts = []
        for instance, (min_kn, max_kn, avg_kn, avg_fuel_l_per_nm) in sorted(trip.typical_rpm_speed_kn.items()):
            if avg_fuel_l_per_nm is not None:
                parts.append(
                    T["rpm_tooltip_per_engine_fuel"].format(
                        instance=instance, avg=_nl_num(avg_kn), min=_nl_num(min_kn), max=_nl_num(max_kn),
                        fuel=_nl_num(avg_fuel_l_per_nm, 2),
                    )
                )
            else:
                parts.append(
                    T["rpm_tooltip_per_engine"].format(
                        instance=instance, avg=_nl_num(avg_kn), min=_nl_num(min_kn), max=_nl_num(max_kn)
                    )
                )
        tooltip = ", ".join(parts)
    tooltip = escape(tooltip)
    return (
        f'<span class="temp-hover">{visible}'
        f'<span class="temp-tooltip" style="background:#eef4fb;color:#1a4a7a;">'
        f"⚙️ {tooltip}</span></span>"
    )


def _max_speed_html(trip: TripLeg, offset_hours: float) -> str:
    """Plain max-speed text in the cell; hovering shows when it happened and what RPM the engine
    was running at that moment -- the bare number alone doesn't say whether it was a brief
    downwind surge at low RPM or genuinely flat-out at full throttle."""
    if trip.max_speed_kn is None:
        return ""
    visible = _nl_num(trip.max_speed_kn) + " kn"
    if trip.max_speed_at is None:
        return visible
    time_text = _to_local(trip.max_speed_at, offset_hours).strftime("%H:%M")

    if len(trip.max_speed_rpm) == 1:
        rpm = next(iter(trip.max_speed_rpm.values()))
        tooltip = T["max_speed_tooltip_single"].format(time=time_text, rpm=f"{rpm:.0f}")
    elif trip.max_speed_rpm:
        tooltip = ", ".join(
            T["max_speed_tooltip_per_engine"].format(time=time_text, instance=instance, rpm=f"{rpm:.0f}")
            for instance, rpm in sorted(trip.max_speed_rpm.items())
        )
    else:
        return visible
    tooltip = escape(tooltip)
    return (
        f'<span class="temp-hover">{visible}'
        f'<span class="temp-tooltip" style="background:#eef4fb;color:#1a4a7a;">'
        f"🚀 {tooltip}</span></span>"
    )


def _next_aligned_time(local_time: datetime, interval: timedelta) -> datetime:
    """Rounds up to the next clock-aligned boundary (e.g. the next :00 or :30 for a 30-minute
    interval) rather than counting from whatever arbitrary minute the trip happened to depart --
    a real (paper) logbook's periodic entries land on tidy clock times, not on e.g. 07:41, 08:11,
    08:41."""
    midnight = local_time.replace(hour=0, minute=0, second=0, microsecond=0)
    remainder = (local_time - midnight) % interval
    return local_time if remainder == timedelta(0) else local_time + (interval - remainder)


def _periodic_log_entries(
    track: List[NavSample], interval_minutes: float, offset_hours: float
) -> List[NavSample]:
    """Picks one track point every ``interval_minutes`` on a clock-aligned grid in local time --
    like the periodic course/speed/position entries a traditional (paper) logbook records during
    a passage -- plus always the very first and last point of the trip, so the log covers the
    full departure-to-arrival span even if it doesn't divide evenly by the interval."""
    if not track:
        return []
    interval = timedelta(minutes=interval_minutes)
    local_start = _to_local(track[0].time, offset_hours)
    next_due = _next_aligned_time(local_start, interval)
    while next_due <= local_start:  # don't immediately re-log the departure point itself
        next_due += interval
    entries = [track[0]]
    for sample in track[1:]:
        local_time = _to_local(sample.time, offset_hours)
        if local_time >= next_due:
            entries.append(sample)
            while next_due <= local_time:  # stay on the clock grid even across a data gap
                next_due += interval
    if entries[-1] is not track[-1]:
        entries.append(track[-1])
    return entries


def _map_log_points(trip: TripLeg, offset_hours: float, interval_minutes: float) -> List[dict]:
    """The same periodic log entries shown in the Details popup's own log table (see
    _details_cell_html), embedded for the map too so each one can get its own numbered marker --
    formatted here in Python, not re-derived in JS, so the two can never show slightly different
    numbers for what's supposed to be the same point."""
    entries = _periodic_log_entries(trip.track, interval_minutes, offset_hours)
    if len(entries) < 2:
        return []
    points = []
    for entry in entries:
        local_time = _to_local(entry.time, offset_hours)
        points.append(
            {
                "lat": round(entry.lat, 6),
                "lon": round(entry.lon, 6),
                "time": f"{local_time:%H:%M}",
                "cog": f"{_nl_num(entry.cog_deg, 0)}°" if entry.cog_deg is not None else None,
                "sog": f"{_nl_num(entry.sog_ms / _KNOT_IN_MS)} kn",
            }
        )
    return points




def _trip_title(trip: TripLeg, depart_local: datetime) -> str:
    """Shared with the map popup's own title (see _trip_row_html) -- the Details popup needs the
    same "which trip is this" context, since unlike the always-visible table row it replaces a
    cell of, a popup can be scrolled away from the row that opened it."""
    return f"{depart_local:%Y-%m-%d %H:%M} {trip.depart_place} -> {trip.arrive_place}"


def _details_row_html(icon: str, label: str, value: str) -> str:
    return (
        f'<div class="detail-row"><span class="detail-icon">{icon}</span>'
        f'<span class="detail-label">{escape(label)}:</span> {value}</div>'
    )


_COMPASS_POINTS = (
    "N", "NNO", "NO", "ONO", "O", "OZO", "ZO", "ZZO",
    "Z", "ZZW", "ZW", "WZW", "W", "WNW", "NW", "NNW",
)


def _compass_abbr(deg: float) -> str:
    return _COMPASS_POINTS[round(deg / 22.5) % 16]


def _details_cell_html(
    trip: TripLeg, idx: int, interval_minutes: float, offset_hours: float, weather, marine
) -> str:
    """Button + <dialog> popup (like the map, not inline) bundling water temperature, motion
    (roll/pitch), and the periodic course/speed/position log together -- these each used to be
    their own always-visible column, which blew the trips table's width out badly (found in
    practice). A popup for this also doesn't push the rest of a possibly very wide, already
    horizontally-scrolled table around when opened, the same reason the log itself was already a
    popup rather than an inline-expanding table: with an inline table, the COG/SOG columns could
    end up scrolled out of view off the right edge of the same .table-scroll region the button
    itself was in. More columns may move in here later."""
    sections = []

    entries = _periodic_log_entries(trip.track, interval_minutes, offset_hours)
    if len(entries) >= 2:
        rows = []
        last_idx = len(entries) - 1
        for i, entry in enumerate(entries):
            # Same numbering as the map's own numbered markers (see _map_log_points/the
            # log-marker JS, which number only the entries strictly between these two) -- the
            # departure/arrival ends get their own map markers already, a redundant numbered
            # circle right on top of those would just be clutter (found in practice).
            if i == 0:
                number_text = escape(T["map_marker_departure"])
            elif i == last_idx:
                number_text = escape(T["map_marker_arrival"])
            else:
                number_text = str(i)
            local_time = _to_local(entry.time, offset_hours)
            cog_text = f"{_nl_num(entry.cog_deg, 0)}&deg;" if entry.cog_deg is not None else ""
            sog_text = f"{_nl_num(entry.sog_ms / _KNOT_IN_MS)} kn"
            position_text = f"{entry.lat:.4f}, {entry.lon:.4f}"
            # Not a measurement from the boat itself -- regional weather-model data for the
            # nearest grid cell at this hour (see weather.py), one lookup per log row rather than
            # one per trip, since wind in particular can change a lot over a longer trip (found in
            # practice: 1.8 to 9.7 kn across a single ~4.5 hour trip).
            hourly = weather.hour(entry.lat, entry.lon, entry.time)
            if hourly is not None and hourly.wind_kn is not None and hourly.wind_deg is not None:
                wind_text = f"{_nl_num(hourly.wind_kn)} kn {_compass_abbr(hourly.wind_deg)}"
            else:
                wind_text = ""
            precip_text = f"{_nl_num(hourly.precip_mm, 1)} mm" if hourly and hourly.precip_mm is not None else ""
            cloud_text = f"{_nl_num(hourly.cloud_pct, 0)}%" if hourly and hourly.cloud_pct is not None else ""
            # Not a measurement from the boat itself either -- regional wave/current-model data
            # for the nearest sea grid cell at this hour, from a separate Open-Meteo dataset than
            # the wind/precipitation/cloud data above (see marine.py).
            hourly_marine = marine.hour(entry.lat, entry.lon, entry.time)
            if (
                hourly_marine is not None
                and hourly_marine.wave_height_m is not None
                and hourly_marine.wave_period_s is not None
                and hourly_marine.wave_direction_deg is not None
            ):
                wave_text = (
                    f"{_nl_num(hourly_marine.wave_height_m)} m, "
                    f"{_nl_num(hourly_marine.wave_period_s)} s, "
                    f"{_compass_abbr(hourly_marine.wave_direction_deg)}"
                )
            else:
                wave_text = ""
            if (
                hourly_marine is not None
                and hourly_marine.current_kn is not None
                and hourly_marine.current_direction_deg is not None
            ):
                current_text = (
                    f"{_nl_num(hourly_marine.current_kn)} kn "
                    f"{_compass_abbr(hourly_marine.current_direction_deg)}"
                )
            else:
                current_text = ""
            rows.append(
                f"<tr><td>{number_text}</td><td>{local_time:%H:%M}</td><td>{position_text}</td>"
                f"<td>{cog_text}</td><td>{sog_text}</td>"
                f"<td>{wind_text}</td><td>{precip_text}</td><td>{cloud_text}</td>"
                f"<td>{wave_text}</td><td>{current_text}</td></tr>"
            )
        sections.append(
            "<table class=\"log-table\"><thead><tr>"
            f"<th>{escape(T['log_header_number'])}</th>"
            f"<th>{escape(T['log_header_time'])}</th><th>{escape(T['log_header_position'])}</th>"
            f"<th>{escape(T['log_header_cog'])}</th><th>{escape(T['log_header_sog'])}</th>"
            f"<th>{escape(T['log_header_wind'])}</th><th>{escape(T['log_header_precip'])}</th>"
            f"<th>{escape(T['log_header_cloud'])}</th>"
            f"<th>{escape(T['log_header_wave'])}</th><th>{escape(T['log_header_current'])}</th>"
            f'</tr></thead><tbody>{"".join(rows)}</tbody></table>'
        )

    water_temp = _water_temp_detail_text(trip)
    if water_temp:
        sections.append(_details_row_html("🌡️", T["header_water_temp"], escape(water_temp)))
    motion = _motion_detail_text(trip)
    if motion:
        sections.append(_details_row_html("〰️", T["header_motion"], escape(motion)))

    if not sections:
        return ""
    depart_local = _to_local(trip.depart_time, offset_hours)
    title = f'<div class="trip-map-title">{escape(_trip_title(trip, depart_local))}</div>'
    dialog = (
        f'<dialog class="log-dialog" id="log-{idx}">{title}{"".join(sections)}'
        f'<button type="button" class="close-log">{escape(T["log_close_button"])}</button></dialog>'
    )
    return f'<button type="button" class="show-log" data-trip="{idx}">{escape(T["details_button"])}</button>{dialog}'


def _remarks_cell_html(trip_uid: Optional[str], idx: int, remarks_api_url: str) -> str:
    """Button + <dialog> popup (same pattern as Details/Map). The remark text itself isn't known at
    generation time -- unlike everything else in this file, it doesn't come from the decoded
    NMEA2000 data at all, but from WordPress (see wordpress-plugin/), fetched by the REMARKS_*
    script after the page loads -- so this only builds the empty shell; JS fills in the button
    label and textarea once that fetch resolves. Needs a stable trip_uid to key the remark on
    (see trip_ids.py) -- without one there's nothing to attach a saved remark to, so no button.

    No login form here: reading and saving both ride on the WordPress session that got you past
    the login-gated logbook page in the first place (see little_endian-index.php), not a
    separate credential entered in this dialog."""
    if not remarks_api_url or not trip_uid:
        return ""
    dialog = (
        f'<dialog class="log-dialog remarks-dialog" id="remarks-{idx}" data-trip-uid="{escape(trip_uid)}">'
        '<textarea class="remarks-textarea" rows="12" cols="60"></textarea>'
        '<p class="remarks-error" hidden></p>'
        '<div class="remarks-buttons">'
        f'<button type="button" class="remarks-save">{escape(T["remarks_save_button"])}</button>'
        f'<button type="button" class="remarks-cancel">{escape(T["remarks_cancel_button"])}</button>'
        "</div>"
        "</dialog>"
    )
    placeholder = escape(T["remarks_button_placeholder"])
    return f'<button type="button" class="show-remarks" data-trip="{idx}">{placeholder}</button>{dialog}'


def _totals_html(totals: _Totals) -> str:
    avg_l_per_nm = totals.fuel_liters / totals.distance_nm if totals.distance_nm > 0 else None
    avg_l_per_hour = totals.fuel_liters / totals.moving_hours if totals.moving_hours > 0 else None
    # Distance-weighted, not a plain average of each trip's avg_speed_kn -- that would give a
    # short trip the same weight as a long one, which isn't representative of the whole period.
    avg_speed_kn = totals.distance_nm / totals.moving_hours if totals.moving_hours > 0 else None

    items = [
        (T["totals_trips"], str(totals.trip_count)),
        (T["totals_distance"], f"{_nl_num(totals.distance_nm)} nm"),
    ]
    # In 3rd/4th place specifically (found in practice: wanted near the top, not buried after
    # every fuel/speed stat).
    if len(totals.engine_hours_current) == 1:
        hours = next(iter(totals.engine_hours_current.values()))
        items.append((T["totals_engine_hour_meter"], f"{_nl_num(hours)} h"))
    else:
        for instance, hours in sorted(totals.engine_hours_current.items()):
            label = T["totals_engine_hour_meter_engine"].format(instance=instance)
            items.append((label, f"{_nl_num(hours)} h"))
    if len(totals.engine_hours) == 1:
        hours = next(iter(totals.engine_hours.values()))
        items.append((T["totals_hours_logged"], f"{_nl_num(hours)} h"))
    else:
        for instance, hours in sorted(totals.engine_hours.items()):
            label = T["totals_hours_logged_engine"].format(instance=instance)
            items.append((label, f"{_nl_num(hours)} h"))
    items.append((T["totals_hours"], f"{_nl_num(totals.moving_hours)} h"))
    items.append((T["totals_fuel_calculated"], f"{_nl_num(totals.fuel_liters)} L"))
    if totals.fuel_liters_device is not None:
        items.append((T["totals_fuel_engine_meter"], f"{_nl_num(totals.fuel_liters_device)} L"))
    if avg_l_per_nm is not None:
        items.append((T["totals_avg_consumption"], f"{_nl_num(avg_l_per_nm, 2)} L/nm"))
    if avg_l_per_hour is not None:
        items.append((T["totals_avg_consumption"], f"{_nl_num(avg_l_per_hour)} L/h"))
    if avg_speed_kn is not None:
        items.append((T["totals_avg_speed"], f"{_nl_num(avg_speed_kn)} kn"))
    if totals.max_speed_kn is not None:
        items.append((T["totals_top_speed"], f"{_nl_num(totals.max_speed_kn)} kn"))

    cards = "".join(
        f'<div class="stat"><div class="stat-label">{escape(label)}</div>'
        f'<div class="stat-value">{escape(value)}</div></div>'
        for label, value in items
    )
    # Column count is set by JS (see layoutTotals in the <script> below) to exactly however many
    # cards fit the trips table's own width -- one row whenever that's enough room, wrapping to
    # more (equal-width, unlike flex-wrap) only once it isn't. A pure-CSS column count can't do
    # this: a fixed number wraps at the wrong item counts, and auto-fill's own per-column minimum
    # can't be tied to the table's actual (data-dependent) width. This static fallback (used
    # without JS, e.g. an emailed attachment) just wraps at a sensible fixed card width instead.
    return f'<section class="totals">{cards}</section>'


def _trip_row_html(
    trip: TripLeg,
    idx: int,
    utc_offset_hours: Optional[float],
    trip_uid: Optional[str] = None,
    battery_warning_voltage: Optional[float] = None,
    log_interval_minutes: float = _DEFAULT_LOG_INTERVAL_MINUTES,
    seq: Optional[int] = None,
    remarks_api_url: str = _DEFAULT_REMARKS_API_URL,
    weather=None,
    marine=None,
) -> str:
    if weather is None:
        weather = NoWeather()
    if marine is None:
        marine = NoMarine()
    offset = _trip_utc_offset_hours(trip, utc_offset_hours)
    depart_local = _to_local(trip.depart_time, offset)
    arrive_local = _to_local(trip.arrive_time, offset)
    avg_consumption_nm = _avg_consumption_l_per_nm(trip)

    map_cell = (
        f'<button class="show-map" data-trip="{idx}">{escape(T["map_button_show"])}</button>'
        if trip.track
        else ""
    )
    cells = [
        str(seq) if seq is not None else "",
        depart_local.strftime("%Y-%m-%d"),
        depart_local.strftime("%H:%M"),
        escape(trip.depart_place),
        arrive_local.strftime("%H:%M"),
        escape(trip.arrive_place),
        _format_duration(trip.duration),
        f"{_nl_num(trip.distance_nm)} nm",
        _nl_num(trip.avg_speed_kn) + " kn" if trip.avg_speed_kn is not None else "",
        _max_speed_html(trip, offset),
        _nl_num(trip.fuel_liters) + " L",
        f"{_nl_num(avg_consumption_nm, 2)} L/nm" if avg_consumption_nm is not None else "",
        escape(_engine_hours_text(trip)),
        _typical_rpm_html(trip),
        _warnings_html(trip, battery_warning_voltage, offset),
        map_cell,
        _details_cell_html(trip, idx, log_interval_minutes, offset, weather, marine),
    ]
    if remarks_api_url:
        cells.append(_remarks_cell_html(trip_uid, idx, remarks_api_url))
    row = "".join(f"<td>{cell}</td>" for cell in cells)
    map_row = ""
    if trip.track:
        title = escape(_trip_title(trip, depart_local))
        map_row = (
            f'<tr class="trip-map-row" data-trip="{idx}" style="display:none">'
            f'<td colspan="{len(_headers_for(remarks_api_url))}"><div class="trip-map-title">{title}</div>'
            f'<div class="map" id="map-{idx}"></div></td></tr>'
        )
    uid_attr = f' data-uid="{escape(trip_uid)}"' if trip_uid else ""
    return f'<tr class="trip-row"{uid_attr}>{row}</tr>{map_row}'


_HEADER_FULL_NAMES = {
    T["header_departure_abbr"]: T["header_departure_full"],
    T["header_arrival_abbr"]: T["header_arrival_full"],
}
# Shown abbreviated always, with just a native title="" tooltip on hover -- unlike Vertr./Aank.
# above, expanding this one on a wide viewport isn't worth it: with this many columns the table
# needs horizontal scrolling regardless of viewport width anyway (found in practice), so it would
# only ever waste column width without actually helping anyone see more of the table at once.
_HEADER_ABBR_TITLES = {T["header_seq_abbr"]: T["header_seq_full"]}
# Headers whose meaning isn't obvious from the label alone get a hover tooltip (same CSS-only
# mechanism as the table cells, see .temp-hover/.temp-tooltip) instead of a longer header.
_HEADER_TOOLTIPS: Dict[str, str] = {}


def _header_cell_html(label: str) -> str:
    title = _HEADER_ABBR_TITLES.get(label)
    if title is not None:
        return f'<span title="{escape(title)}">{escape(label)}</span>'
    full = _HEADER_FULL_NAMES.get(label)
    if full is None:
        base = escape(label)
    else:
        # Shows the abbreviation by default; a wide-enough viewport swaps to the full word (see
        # the .hdr-full / .hdr-abbr media query in write_html_logbook's <style>).
        base = f'<span class="hdr-full">{escape(full)}</span><span class="hdr-abbr">{escape(label)}</span>'
    tooltip = _HEADER_TOOLTIPS.get(label)
    if tooltip is None:
        return base
    return (
        f'<span class="temp-hover">{base}'
        f'<span class="temp-tooltip" style="background:#eef4fb;color:#1a4a7a;">{escape(tooltip)}</span></span>'
    )


_HEADERS = [
    T["header_seq_abbr"],
    T["header_date"],
    T["header_departure_abbr"],
    T["header_from"],
    T["header_arrival_abbr"],
    T["header_to"],
    T["header_duration"],
    T["header_distance"],
    T["header_avg_speed"],
    T["header_max_speed"],
    T["header_fuel"],
    T["header_l_per_nm"],
    T["header_engine_hours"],
    T["header_rpm"],
    T["header_warnings"],
    T["header_route"],
    T["header_details"],
]


def _headers_for(remarks_api_url: str) -> List[str]:
    """The Remarks column only exists at all when the feature is configured (see
    _DEFAULT_REMARKS_API_URL) -- unlike Route/Details, whose *column* always exists even though
    individual trips without track data leave that cell empty, "remarks enabled" is a whole-
    document setting, not a per-trip one, so an unused column isn't shown at all rather than
    always being present-but-empty."""
    return _HEADERS + [T["header_remarks"]] if remarks_api_url else _HEADERS


def write_html_logbook(
    trips: Iterable[TripLeg],
    path: Path,
    boat_name: Optional[str] = None,
    mmsi: Optional[str] = None,
    call_sign: Optional[str] = None,
    utc_offset_hours: Optional[float] = None,
    trip_uids: Optional[List[str]] = None,
    battery_warning_voltage: Optional[float] = None,
    latest_data_at: Optional[datetime] = None,
    fetch_failed: bool = False,
    log_interval_minutes: float = _DEFAULT_LOG_INTERVAL_MINUTES,
    remarks_api_url: str = _DEFAULT_REMARKS_API_URL,
    weather=None,
    marine=None,
) -> None:
    """``trip_uids``: one id per trip, in the same order as ``trips`` *before* sorting -- e.g.
    from ``trip_ids.assign_trip_ids(trips)``. Embedded as an invisible ``data-uid`` attribute on
    each trip row, and used to key the Remarks feature (see ``remarks_api_url``) -- a uid that
    survives a trip-recognition fix reshuffling exact timestamps is the whole reason it exists.

    ``latest_data_at`` (UTC): shown as "Laatst bijgewerkt" -- meant to answer "is this page
    showing a stale file", so cli.py passes the moment this run actually happened, not the latest
    timestamp found in the boat's own data (an earlier version used the latter, which made this
    look unchanged after a fresh run whenever the boat itself hadn't produced new data since the
    previous run -- found in practice, asked for explicitly). Falls back to the most recent trip's
    own arrival time if not given (e.g. a caller with only trips, no access to cli.py's own
    run-time clock).

    ``fetch_failed``: shows that same timestamp in red, on top of it already reflecting the
    data's own age -- a second, more visible cue that this run specifically didn't get new data,
    not just that it happens to have been a while. Not derived from the trip data at all: only
    the caller (see --download-failed) knows whether the fetch step itself succeeded.

    ``remarks_api_url``: URL of the WordPress REST endpoint that stores per-trip remarks (see
    ``wordpress-plugin/``). Empty (default) disables the whole Remarks column.

    ``weather``: a weather.WeatherFetcher (or weather.NoWeather, the default) -- adds wind/
    precipitation/cloud-cover columns to each trip's own periodic log table (see
    _details_cell_html), one lookup per log row rather than one per trip, since wind especially
    can change a lot over a longer trip.

    ``marine``: a marine.MarineFetcher (or marine.NoMarine, the default) -- adds wave and ocean-
    current columns to the same periodic log table, from a separate Open-Meteo dataset than
    ``weather``."""
    trips = list(trips)
    uid_by_trip = {id(trip): uid for trip, uid in zip(trips, trip_uids)} if trip_uids is not None else {}
    trips = sorted(trips, key=lambda t: t.depart_time)

    if latest_data_at is None and trips:
        latest_data_at = max(t.arrive_time for t in trips)

    last_updated_html = ""
    if latest_data_at is not None:
        # No single trip necessarily covers latest_data_at (it can be later than every trip's own
        # arrival, e.g. while anchored) -- the most recent trip's own offset is still the best
        # available estimate of the current local timezone, since the boat is very unlikely to
        # have jumped somewhere wildly different since then.
        offset = _trip_utc_offset_hours(trips[-1], utc_offset_hours) if trips else (utc_offset_hours or 0.0)
        latest_local = _to_local(latest_data_at, offset)
        last_updated_class = "last-updated fetch-failed" if fetch_failed else "last-updated"
        last_updated_html = (
            f'<div class="{last_updated_class}">{escape(T["last_updated"])}: {latest_local:%Y-%m-%d %H:%M}</div>'
        )

    # Keyed by (calendar_year, iso_year, iso_week) -- calendar_year decides which year *section*
    # (and whose totals) a trip belongs to, deliberately kept separate from the ISO week's own
    # year: near a year boundary those two can disagree (e.g. 2025-12-31 falls in ISO week 1 of
    # *2026*; 2027-01-01..03 fall in ISO week 53 of *2026*) -- grouping by the ISO year instead
    # would silently fold a few days of one calendar year's trips into the *other* year's
    # section, corrupting an already-reported total for a year that's otherwise done (found in
    # practice: a trip on New Year's Day would have updated the *previous* year's own
    # Motoruren-teller). iso_year/iso_week are kept alongside purely so _week_label can still
    # compute that week's real date range -- date.fromisocalendar needs the true ISO year, not
    # the calendar one.
    by_week: Dict[Tuple[int, int, int], List[int]] = defaultdict(list)
    for idx, trip in enumerate(trips):
        offset = _trip_utc_offset_hours(trip, utc_offset_hours)
        local_date = _to_local(trip.depart_time, offset).date()
        iso_year, iso_week, _ = local_date.isocalendar()
        by_week[(local_date.year, iso_year, iso_week)].append(idx)

    headers = _headers_for(remarks_api_url)
    header_html = "".join(f"<th>{_header_cell_html(h)}</th>" for h in headers)

    sections: List[str] = []
    for calendar_year in sorted({y for y, _, _ in by_week}, reverse=True):
        weeks_in_year = sorted({(iy, iw) for y, iy, iw in by_week if y == calendar_year}, reverse=True)
        # Chronological (ascending), unlike weeks_in_year/indices below which are ordered for
        # display (most-recent-first) -- _compute_totals relies on its input being chronological
        # to pick out the *latest* engine-hour-meter reading (see its own docstring), and feeding
        # it the display order instead picked up a trip from the year's earliest week rather than
        # its most recent one, understating "Motoruren-teller" by however many hours the engine
        # ran since then (found in practice: the year section's own total didn't match the
        # top-of-page one, which uses the correctly-sorted full trip list).
        year_indices_chronological = sorted(
            i for iso_year, iso_week in weeks_in_year for i in by_week[(calendar_year, iso_year, iso_week)]
        )
        year_trips = [trips[i] for i in year_indices_chronological]
        # A trip's own position within its year's indices, sorted ascending, is exactly its
        # 1-based sequence number for that year -- resets every year since each year's indices
        # are handled separately.
        seq_by_index = {i: n + 1 for n, i in enumerate(year_indices_chronological)}
        # One continuous <table> for the whole year (not one per week): a single table lets the
        # browser compute column widths from *all* the year's rows together, so every week lines
        # up automatically and no column ever ends up narrower than its widest content -- which
        # a separate table per week, or hand-picked fixed column widths, can't guarantee.
        body_rows: List[str] = []
        for iso_year, iso_week in weeks_in_year:
            # by_week's own indices are chronological (see its construction above); reversed so
            # a trip's position within its week matches the same "most recent first" order the
            # weeks themselves are already shown in, instead of alternating direction between the
            # two levels (found in practice: read as confusing, weeks going newest-to-oldest but
            # each week's own trips going oldest-to-newest).
            indices = list(reversed(by_week[(calendar_year, iso_year, iso_week)]))
            body_rows.append(
                f'<tr class="week-row"><td colspan="{len(headers)}">'
                f"{escape(_week_label(iso_year, iso_week))}</td></tr>"
            )
            body_rows.extend(
                _trip_row_html(
                    trips[i],
                    i,
                    utc_offset_hours,
                    uid_by_trip.get(id(trips[i])),
                    battery_warning_voltage,
                    log_interval_minutes,
                    seq_by_index[i],
                    remarks_api_url,
                    weather=weather,
                    marine=marine,
                )
                for i in indices
            )
        sections.append(
            f'<section class="year"><h2>{calendar_year}</h2>'
            f"{_totals_html(_compute_totals(year_trips))}"
            f'<div class="table-scroll"><table class="trips"><thead><tr>{header_html}</tr></thead>'
            f'<tbody>{"".join(body_rows)}</tbody></table></div></section>'
        )

    trip_data = {
        idx: {
            "points": _decimated_points(trip),
            # The departure/arrival markers are placed from these, not points[0]/points[-1] --
            # the stay's own averaged position (see TripLeg.depart_lat/arrive_lat), not the single
            # GPS fix from the moment the boat started/stopped moving, which has real GPS jitter
            # (found in practice: ~10 m off from the actual berth) that averaging cancels out. The
            # route line itself still draws from the plain track, unaffected.
            "departPos": [round(trip.depart_lat, 6), round(trip.depart_lon, 6)],
            "arrivePos": [round(trip.arrive_lat, 6), round(trip.arrive_lon, 6)],
            "log": _map_log_points(
                trip, _trip_utc_offset_hours(trip, utc_offset_hours), log_interval_minutes
            ),
        }
        for idx, trip in enumerate(trips)
        if trip.track
    }
    trips_json = json.dumps(trip_data).replace("</", "<\\/")

    title = f"{boat_name} - {T['logbook_title_suffix']}" if boat_name else T["logbook_title_suffix"]
    heading = (
        f"{escape(boat_name)} &mdash; {T['logbook_title_suffix']}" if boat_name else T["logbook_title_suffix"]
    )

    vessel_info_lines = []
    if mmsi:
        vessel_info_lines.append(f"MMSI: {escape(mmsi)}")
    if call_sign:
        vessel_info_lines.append(f"{T['vessel_call_sign']}: {escape(call_sign)}")
    vessel_info_html = (
        '<div class="vessel-info">' + "".join(f"<div>{line}</div>" for line in vessel_info_lines) + "</div>"
        if vessel_info_lines
        else ""
    )

    html = f"""<!DOCTYPE html>
<html lang="nl">
<head>
<meta charset="utf-8">
<title>{escape(title)}</title>
<link rel="icon" href="{_ICON_URL}">
<link rel="apple-touch-icon" href="{_ICON_URL}">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="{escape(title)}">
<link rel="stylesheet" href="{_LEAFLET_CSS}">
<script src="{_LEAFLET_JS}"></script>
<style>
  body {{ font-family: sans-serif; margin: 0; padding: 1.5em; background: #f7f7f8; color: #1a1a1a; }}
  h1 {{ margin: 0 0 0.2em; }}
  .header-row {{ display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 0.5em 1.5em; }}
  .vessel-info {{ color: #444; font-size: 1.1em; text-align: right; line-height: 1.4; }}
  .last-updated {{ color: #666; font-size: 0.85em; margin-bottom: 1em; }}
  .last-updated.fetch-failed {{ color: #c0392b; font-weight: 600; }}
  h2 {{ margin-top: 2em; border-bottom: 2px solid #1a6ecc; padding-bottom: 0.2em; }}
  /* A grid, not flex-wrap: flex-wrap gives each wrapped *row* its own independent flex-grow
     distribution, so a short last row (fewer cards) stretched those few cards much wider than
     the same cards on a fuller row above (found in practice). Grid's columns are shared by every
     row, so cards stay the same width everywhere; a short last row just leaves its unused
     columns empty instead of stretching into them. The column *count* is set by JS (see
     layoutTotals below) to whatever fits the trips table's own width in one row, wrapping only
     once it doesn't -- auto-fill's own fixed per-column minimum can't do that, since it has no
     way to know the table's actual (data-dependent) width. This fallback is only for when JS
     isn't available (e.g. an emailed attachment). */
  .totals {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: 0.6em; margin: 1em 0 2em; }}
  .stat {{ background: white; border-radius: 8px; padding: 0.6em 0.9em; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
  .stat-label {{ font-size: 0.8em; color: #666; }}
  .stat-value {{ font-size: 1.3em; font-weight: 600; }}
  /* One continuous table per year (see write_html_logbook) with natural (auto) column sizing --
     every week's rows share the same table, so columns line up automatically and none of them
     can end up narrower than its widest content. table-scroll adds a horizontal scrollbar
     instead of ever squeezing/wrapping a column when the table doesn't fit the viewport. */
  .table-scroll {{ overflow-x: auto; margin-bottom: 1em; }}
  table.trips {{ border-collapse: collapse; width: 100%; background: white; }}
  table.trips th, table.trips td {{
    padding: 0.4em 0.6em; border-bottom: 1px solid #eee; text-align: left; font-size: 0.9em;
    white-space: nowrap;
  }}
  table.trips th {{ background: #f0f0f0; }}
  .hdr-full {{ display: none; }}
  @media (min-width: 1000px) {{
    .hdr-full {{ display: inline; }}
    .hdr-abbr {{ display: none; }}
  }}
  tr.week-row td {{
    background: #eef4fb; font-weight: 700; color: #1a4a7a; font-size: 0.95em;
    padding: 1em 0.6em 0.5em; border-top: 2px solid #1a6ecc; border-bottom: none;
  }}
  .show-map, .show-log {{ cursor: pointer; border: 1px solid #1a6ecc; background: white; color: #1a6ecc; border-radius: 4px; padding: 0.2em 0.6em; white-space: nowrap; }}
  .show-map.active {{ background: #1a6ecc; color: white; }}
  /* Scoped to devices with a real hover-capable pointer (a mouse) -- on a touchscreen, tapping
     can leave a lingering synthetic :hover state that combined with these rules made the button
     flip to the wrong color right after tapping (blue map button turning white right after
     opening the map, and vice versa) instead of just tracking .active (found in practice on a
     phone). */
  @media (hover: hover) {{
    .show-map:hover, .show-log:hover {{ background: #1a6ecc; color: white; }}
    /* Hovering an already-active (blue) button previews what clicking it does -- hiding the map,
       i.e. going back to white -- the same way hovering a white one previews opening it (blue). */
    .show-map.active:hover {{ background: white; color: #1a6ecc; }}
  }}
  .trip-map-title {{ font-weight: 600; margin-bottom: 0.4em; }}
  /* Only the Details popup's own title (not the Map's, which reuses .trip-map-title inside a
     table row rather than a .log-dialog) -- matches the log-table header's background so the
     popup reads as one consistent header bar on top. */
  .log-dialog .trip-map-title {{ background: #f0f0f0; padding: 0.4em 0.6em; border-radius: 4px; }}
  /* A popup (like the map) instead of an inline-expanding table on purpose: in a wide, already
     horizontally-scrolled trips table, expanding a table inline pushed the COG/SOG columns off
     the right edge of the same scroll region the button itself was in, making the log look like
     it only ever had a time and position column (found in practice). A dialog isn't constrained
     by that scroll position at all. */
  .log-dialog {{ border: none; border-radius: 8px; padding: 1em 1.2em; box-shadow: 0 4px 20px rgba(0,0,0,0.25); min-width: 20em; max-width: 90vw; }}
  .log-dialog::backdrop {{ background: rgba(0,0,0,0.4); }}
  .log-table {{ width: 100%; border-collapse: collapse; white-space: nowrap; margin-bottom: 0.8em; }}
  .log-table th, .log-table td {{ padding: 0.2em 0.6em; border-bottom: 1px solid #eee; text-align: left; font-size: 0.9em; }}
  .log-table th {{ background: #f0f0f0; }}
  .detail-row {{
    display: flex; align-items: baseline; gap: 0.5em; background: #f5f7fa; border-radius: 6px;
    padding: 0.5em 0.7em; margin-bottom: 0.5em;
  }}
  .detail-icon {{ flex: none; }}
  .detail-label {{ font-weight: 600; }}
  .close-log {{ cursor: pointer; border: 1px solid #ccc; background: white; border-radius: 4px; padding: 0.3em 0.8em; }}
  .close-log:hover {{ background: #f0f0f0; }}
  .temp-hover {{ cursor: default; border-bottom: 1px dotted #999; }}
  .warning-count {{ color: #c0392b; font-weight: 600; }}
  .temp-tooltip {{
    display: none; position: fixed; z-index: 10;
    border-radius: 12px; padding: 0.15em 0.6em; font-size: 0.85em; white-space: nowrap;
    box-shadow: 0 1px 4px rgba(0,0,0,0.25);
  }}
  .temp-hover:hover .temp-tooltip {{ display: block; }}
  /* The map's own <td> spans every column of the (often much wider than the viewport, already
     horizontally-scrolled) trips table, so without a width cap the map itself would render just
     as wide -- on a narrow screen, only a thin vertical slice of that ends up actually visible
     without also scrolling the table sideways while the map is open (found in practice). Capped
     to the viewport's own width, not a fixed pixel value -- a fixed cap narrowed the map even on
     a wide desktop screen where the old (uncapped) width was never a problem in the first place.
     Sticking it to the left edge of .table-scroll keeps the whole map in view regardless of the
     table's own scroll position. */
  .map {{ height: 350px; width: calc(100vw - 3em); position: sticky; left: 0; }}
  /* Without this, the map's own near-viewport-width sizing (see .map above) still counts towards
     this <td>'s natural width in the table's own (auto) column-sizing math, even though the map
     is just visually overflowing a sticky box -- widening the whole table to match (found in
     practice). max-width: 0 tells that sizing pass to ignore this cell's content entirely; the
     map itself still renders at its full intended size regardless, since nothing here clips it. */
  .trip-map-row > td {{ max-width: 0; }}
  .log-marker {{
    background: #1a6ecc; color: white; border: 1px solid white; border-radius: 50%;
    width: 14px; height: 14px; display: flex; align-items: center; justify-content: center;
    font-size: 9px; font-weight: 600;
  }}
  .noscript-warning {{
    background: #fff3cd; color: #664d03; border: 1px solid #ffe69c; border-radius: 8px;
    padding: 0.8em 1.2em; margin-bottom: 1.5em;
  }}
  .show-remarks {{ cursor: pointer; border: 1px solid #1a6ecc; background: white; color: #1a6ecc; border-radius: 4px; padding: 0.2em 0.6em; white-space: nowrap; }}
  .show-remarks:hover {{ background: #1a6ecc; color: white; }}
  .remarks-dialog {{ width: 24em; max-width: 90vw; }}
  .remarks-textarea {{
    width: 100%; box-sizing: border-box; font: inherit; margin-bottom: 0.8em;
    border: 1px solid #ccc; resize: none;
  }}
  /* Without this, the browser's own default focus outline (thick and near-black in some
  browsers) frames the field instead -- shown every time the dialog opens, since JS focuses the
  textarea right away (found in practice: the intentionally thin #ccc border above was there all
  along, but got visually replaced by this outline as soon as the dialog opened). */
  .remarks-textarea:focus {{ outline: 1px solid #1a6ecc; }}
  .remarks-error {{ color: #c0392b; font-size: 0.85em; margin: 0 0 0.6em; }}
  .remarks-buttons {{ display: flex; gap: 0.5em; }}
  .remarks-save {{ cursor: pointer; border: 1px solid #1a6ecc; background: #1a6ecc; color: white; border-radius: 4px; padding: 0.3em 0.8em; }}
  .remarks-cancel {{ cursor: pointer; border: 1px solid #ccc; background: white; border-radius: 4px; padding: 0.3em 0.8em; }}
</style>
</head>
<body>
<noscript>
  <div class="noscript-warning">
    {escape(T["noscript_warning"])}
  </div>
</noscript>
<div class="header-row"><h1>{heading}</h1>{vessel_info_html}</div>
{last_updated_html}
{_totals_html(_compute_totals(trips))}
{"".join(sections)}
<script>
const TRIPS = {trips_json};
const MAP_MARKER_DEPARTURE = {json.dumps(T["map_marker_departure"])};
const MAP_MARKER_ARRIVAL = {json.dumps(T["map_marker_arrival"])};
const REMARKS_API_URL = {json.dumps(remarks_api_url)};
const REMARKS_BUTTON_PLACEHOLDER = {json.dumps(T["remarks_button_placeholder"])};
const REMARKS_CLOSE_BUTTON = {json.dumps(T["remarks_close_button"])};
const REMARKS_SAVE_FORBIDDEN = {json.dumps(T["remarks_save_forbidden"])};
const REMARKS_SAVE_FAILED = {json.dumps(T["remarks_save_failed"])};
const REMARKS_UNAVAILABLE = {json.dumps(T["remarks_unavailable"])};
// Filled in by the server (see wordpress-plugin/little_endian-index.php) when this file is
// served through the login gate, which -- unlike this Python-generated static file -- can call
// WordPress's own wp_create_nonce('wp_rest'). A POST to the REST API needs this even though the
// browser already sends the WordPress login cookie automatically (same-origin): the nonce is
// WordPress's CSRF protection on top of that cookie, required for any state-changing (non-GET)
// REST request. If this file is opened some other way (not through the gate, or locally), the
// placeholder never gets replaced and saving fails cleanly with REMARKS_SAVE_FAILED below.
const WP_REST_NONCE = "%%WP_REST_NONCE%%";
// Picks however many columns fit the trips table's own width in one row (matching it exactly,
// since 1fr columns divide a grid's width evenly), wrapping to more only once that stops fitting
// -- see the .totals CSS rule above for why this can't just be done with auto-fill in CSS alone.
// Re-run on resize so it stays right if the window (or table width, e.g. a name resolving to a
// longer place name after the page already loaded) changes.
function layoutTotals() {{
  // Each year section has its own .table-scroll (content, so width, can differ per year); the
  // one totals block at the very top (spanning every year) isn't inside a .year section at all,
  // so it falls back to the first table on the page as a reasonable stand-in.
  var firstTableScroll = document.querySelector('.table-scroll');
  document.querySelectorAll('.totals').forEach(function(totals) {{
    var count = totals.querySelectorAll('.stat').length;
    if (!count) return;
    var yearSection = totals.closest('.year');
    var tableScroll = (yearSection && yearSection.querySelector('.table-scroll')) || firstTableScroll;
    if (!tableScroll) {{
      totals.style.gridTemplateColumns = '';  // fall back to the CSS auto-fill rule
      return;
    }}
    var gapPx = parseFloat(getComputedStyle(totals).columnGap) || 0;
    // Below this, a longer label ("Motoruren-teller", "Gelogde motoruren") wraps onto 2 lines
    // itself, making 3 total with the value line underneath (found in practice).
    var minCardWidth = 140;
    var maxColumns = Math.max(1, Math.floor((tableScroll.clientWidth + gapPx) / (minCardWidth + gapPx)));
    var columns = Math.min(count, maxColumns);
    totals.style.gridTemplateColumns = 'repeat(' + columns + ', 1fr)';
  }});
}}
layoutTotals();
window.addEventListener('resize', layoutTotals);
// position: fixed + JS placement (instead of position: absolute anchored to the cell) so a
// tooltip on the last row of a table never gets clipped by .table-scroll's overflow-x: auto --
// setting only one overflow axis makes the browser clip the other one too, cutting off anything
// that pokes past the container's bottom edge.
document.querySelectorAll('.temp-hover').forEach(function(el) {{
  var tooltip = el.querySelector('.temp-tooltip');
  if (!tooltip) return;
  el.addEventListener('mouseenter', function() {{
    tooltip.style.display = 'block';
    var rect = el.getBoundingClientRect();
    var tooltipRect = tooltip.getBoundingClientRect();
    var top = rect.bottom + 4;
    if (top + tooltipRect.height > window.innerHeight) {{
      top = rect.top - tooltipRect.height - 4;
    }}
    var left = Math.min(rect.left, window.innerWidth - tooltipRect.width - 8);
    tooltip.style.top = Math.max(top, 4) + 'px';
    tooltip.style.left = Math.max(left, 4) + 'px';
  }});
  el.addEventListener('mouseleave', function() {{
    tooltip.style.display = '';
  }});
}});
document.querySelectorAll('.show-map').forEach(function(btn) {{
  btn.addEventListener('click', function() {{
    var idx = btn.dataset.trip;
    var row = document.querySelector('.trip-map-row[data-trip="' + idx + '"]');
    var visible = row.style.display !== 'none';
    row.style.display = visible ? 'none' : '';
    // Colored, not relabeled: changing the button's own text (e.g. to "Kaart verbergen") made
    // that column -- and with it the whole table -- change width every time the map was toggled
    // (found in practice). The label stays "Kaart"; only the color now shows whether it's open.
    btn.classList.toggle('active', !visible);
    if (!visible && !row.dataset.initialized) {{
      row.dataset.initialized = '1';
      var map = L.map('map-' + idx);
      L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
        maxZoom: 19,
        attribution: '&copy; OpenStreetMap contributors'
      }}).addTo(map);
      var points = TRIPS[idx].points;
      var line = L.polyline(points, {{color: '#1a6ecc', weight: 3}}).addTo(map);
      var log = TRIPS[idx].log || [];
      function entryTooltip(label, entry) {{
        var parts = [label];
        if (entry) {{
          parts.push(entry.time, entry.lat.toFixed(4) + ', ' + entry.lon.toFixed(4));
          if (entry.cog) parts.push(entry.cog);
          parts.push(entry.sog);
        }}
        return parts.join(' &middot; ');
      }}
      // A hover tooltip, not a click popup like before, for consistency with the numbered log
      // markers below (and so it doesn't need dismissing to see the next one). Positioned from
      // departPos/arrivePos (the stay's own averaged position), not the route line's own first/
      // last point -- see the trip_data comment in html_writer.py for why.
      L.marker(TRIPS[idx].departPos).addTo(map).bindTooltip(entryTooltip(MAP_MARKER_DEPARTURE, log[0]));
      L.marker(TRIPS[idx].arrivePos).addTo(map)
        .bindTooltip(entryTooltip(MAP_MARKER_ARRIVAL, log[log.length - 1]));
      // Same numbering as the Details popup's own log table (see _details_cell_html) -- only the
      // entries strictly between departure and arrival get their own numbered marker; those two
      // ends already have the markers just above, a numbered circle right on top would just be
      // clutter (found in practice).
      log.slice(1, -1).forEach(function(entry, i) {{
        var icon = L.divIcon({{
          className: 'log-marker', html: '<span>' + (i + 1) + '</span>', iconSize: [14, 14], iconAnchor: [7, 7]
        }});
        L.marker([entry.lat, entry.lon], {{icon: icon}}).addTo(map).bindTooltip(entryTooltip(String(i + 1), entry));
      }});
      map.fitBounds(line.getBounds(), {{padding: [20, 20]}});
      setTimeout(function() {{ map.invalidateSize(); }}, 0);
    }}
  }});
}});
document.querySelectorAll('.show-log').forEach(function(btn) {{
  var dialog = document.getElementById('log-' + btn.dataset.trip);
  btn.addEventListener('click', function() {{ dialog.showModal(); }});
  dialog.querySelector('.close-log').addEventListener('click', function() {{ dialog.close(); }});
  // clicking the backdrop closes the dialog too -- a click that lands on the dialog element
  // itself (not any of its content) can only be the backdrop, since the content fills the box.
  dialog.addEventListener('click', function(e) {{
    if (e.target === dialog) dialog.close();
  }});
}});
if (REMARKS_API_URL) {{
  var remarksButtonsByUid = {{}};
  document.querySelectorAll('.show-remarks').forEach(function(btn) {{
    var dialog = document.getElementById('remarks-' + btn.dataset.trip);
    var tripUid = dialog.dataset.tripUid;
    var textarea = dialog.querySelector('.remarks-textarea');
    var errorEl = dialog.querySelector('.remarks-error');
    remarksButtonsByUid[tripUid] = btn;

    btn.addEventListener('click', function() {{
      errorEl.hidden = true;
      dialog.showModal();
      textarea.focus();
    }});
    dialog.querySelector('.remarks-cancel').addEventListener('click', function() {{ dialog.close(); }});
    dialog.addEventListener('click', function(e) {{
      if (e.target === dialog) dialog.close();
    }});
    dialog.querySelector('.remarks-save').addEventListener('click', function() {{
      var text = textarea.value;
      // credentials: 'same-origin' sends the WordPress login cookie automatically (this page and
      // the REST API are the same site); X-WP-Nonce is WordPress's separate CSRF check on top of
      // that cookie for any state-changing request (see the WP_REST_NONCE comment above).
      fetch(REMARKS_API_URL, {{
        method: 'POST',
        credentials: 'same-origin',
        headers: {{'Content-Type': 'application/json', 'X-WP-Nonce': WP_REST_NONCE}},
        body: JSON.stringify({{trip_uid: tripUid, text: text}})
      }}).then(function(response) {{
        if (!response.ok) {{
          errorEl.textContent = response.status === 403 ? REMARKS_SAVE_FORBIDDEN : REMARKS_SAVE_FAILED;
          errorEl.hidden = false;
          return;
        }}
        dialog.close();
      }}).catch(function() {{
        errorEl.textContent = REMARKS_SAVE_FAILED;
        errorEl.hidden = false;
      }});
    }});
  }});

  // X-WP-Nonce is needed here too, not just on the POST below: WordPress's cookie-auth layer
  // rejects *any* REST request from a logged-in session that lacks a valid nonce with 401 --
  // before the endpoint's own permission check even runs -- so without it this GET always failed
  // for logged-in visitors (found in practice: remarks always showing empty on page load even
  // though saving itself worked fine, since the POST above did send the header).
  fetch(REMARKS_API_URL, {{credentials: 'same-origin', headers: {{'X-WP-Nonce': WP_REST_NONCE}}}})
    .then(function(response) {{ return response.ok ? response.json() : Promise.reject(); }})
    .then(function(data) {{
      Object.keys(data.remarks).forEach(function(tripUid) {{
        var btn = remarksButtonsByUid[tripUid];
        if (!btn) return;
        document.getElementById('remarks-' + btn.dataset.trip).querySelector('.remarks-textarea').value = data.remarks[tripUid];
      }});
      // can_edit is a property of the logged-in visitor, not of any one trip, so it applies the
      // same way to every dialog: a "Logboek lezer" account (or anyone else without the
      // edit_logboek_remarks capability) gets a read-only textarea and just a Sluiten (Close)
      // button, having only found out they *can't* save by trying it otherwise.
      if (!data.can_edit) {{
        document.querySelectorAll('.remarks-dialog').forEach(function(dialog) {{
          dialog.querySelector('.remarks-textarea').readOnly = true;
          dialog.querySelector('.remarks-save').hidden = true;
          dialog.querySelector('.remarks-cancel').textContent = REMARKS_CLOSE_BUTTON;
        }});
      }}
    }})
    .catch(function() {{
      Object.keys(remarksButtonsByUid).forEach(function(tripUid) {{
        remarksButtonsByUid[tripUid].title = REMARKS_UNAVAILABLE;
      }});
    }});
}}
</script>
</body>
</html>
"""
    path.write_text(html, encoding="utf-8")
