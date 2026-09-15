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
from .translations import LANGUAGE_FLAGS, LANGUAGES, MONTH_ABBR, MONTH_ABBR_NL, NL as T
from .tripbuilder import NavSample, TripLeg
from .geocode import NoGeocoder
from .marine import NoMarine
from .model import PositionFix
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
# A relative path, so it resolves against whatever site the logbook is opened from -- works
# without a host configured too, as long as logbook.html is uploaded (see --upload) to the same
# site as the WordPress plugin (see wordpress-plugin/nmea2log-remarks.php), which every real run
# always is. On by default for exactly that reason -- unlike --upload, which needs per-machine
# credentials and so can't have a working built-in default, this is the same fixed path for every
# run on every platform, with nothing left to configure. Used to be an empty, disabled-by-default
# ini setting instead, but that only ever got applied on the desktop CLI (nmea2log.ini isn't read
# on Android at all, see android_entry.py's run_pipeline docstring) -- found in practice: every
# phone-built logbook silently had no remarks column at all, only ever a desktop-built one did.
# Still overridable with --remarks-api-url (empty disables it) for local testing/development.
_DEFAULT_REMARKS_API_URL = "/wp-json/nmea2log/v1/remarks"


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


def _i18n_span(key: str) -> str:
    """A translatable label: the given key's Dutch text (this file's own server-side rendering
    stays Dutch, unchanged), tagged with data-i18n so the client-side language switcher (see the
    <script> in write_html_logbook) can swap it to another already-embedded language without
    regenerating the file."""
    return f'<span data-i18n="{escape(key)}">{escape(T[key])}</span>'


def _i18n_tpl_html(key: str, **args: object) -> str:
    """Like _i18n_span, but for a key with {placeholder}s (e.g. rpm_tooltip_single) -- the
    substituted Dutch sentence is shown directly (and is all a no-JS/email viewer ever sees), while
    data-i18n-tpl/data-i18n-args let the switcher re-run the *same* substitution against another
    language's own template string, instead of only ever being able to swap whole fixed labels."""
    text = T[key].format(**args)
    args_json = json.dumps({k: str(v) for k, v in args.items()})
    return (
        f'<span data-i18n-tpl="{escape(key)}" data-i18n-args="{escape(args_json, {chr(34): "&quot;"})}">'
        f"{escape(text)}</span>"
    )


def _localized_date(d: date) -> str:
    """strftime's %b is locale-independent (always English month abbreviations) unless the
    process locale is changed, which is fragile/platform-dependent -- MONTH_ABBR_NL avoids that.
    The month abbreviation is wrapped in its own data-i18n-month span (keyed by %b's English
    output, the same key every language's own month table in translations.py uses) so the
    language switcher can re-translate just that word without touching the day number next to
    it."""
    text = f"{d:%b %d}"
    month, day = text.split(" ", 1)
    return f'<span data-i18n-month="{month}">{MONTH_ABBR_NL[month]}</span> {day}'


def _week_label(iso_year: int, iso_week: int) -> str:
    """Returns HTML (not plain text, unlike its own name might suggest) -- both the "Week" prefix
    and the month abbreviations inside _localized_date are individually re-translatable, so the
    caller must not escape() this like a plain string."""
    monday = date.fromisocalendar(iso_year, iso_week, 1)
    sunday = monday + timedelta(days=6)
    return f"{_i18n_span('week_label_prefix')} {iso_week} ({_localized_date(monday)} - {_localized_date(sunday)})"


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
    """HTML (not plain text -- the roll/pitch/peak words are individually re-translatable, see
    _i18n_span), spelling out which number is roll vs. pitch (slingeren/stampen) and the
    peak-to-peak range directly -- this now lives inside the Details popup (see
    _details_cell_html), which has room for that instead of needing a numbers-only cell plus a
    hover tooltip to explain it."""
    parts = []
    if trip.roll_variation_deg is not None:
        text = f"{_i18n_span('motion_roll')} ±{_nl_num(trip.roll_variation_deg)}°"
        if trip.roll_range_deg is not None:
            text += f" ({_i18n_span('motion_peak')} {_nl_num(trip.roll_range_deg)}°)"
        parts.append(text)
    if trip.pitch_variation_deg is not None:
        text = f"{_i18n_span('motion_pitch')} ±{_nl_num(trip.pitch_variation_deg)}°"
        if trip.pitch_range_deg is not None:
            text += f" ({_i18n_span('motion_peak')} {_nl_num(trip.pitch_range_deg)}°)"
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
            tooltip_html = _i18n_tpl_html(
                "rpm_tooltip_single_fuel",
                avg=_nl_num(avg_kn), min=_nl_num(min_kn), max=_nl_num(max_kn),
                fuel=_nl_num(avg_fuel_l_per_nm, 2),
            )
        else:
            tooltip_html = _i18n_tpl_html(
                "rpm_tooltip_single", avg=_nl_num(avg_kn), min=_nl_num(min_kn), max=_nl_num(max_kn)
            )
    else:
        parts = []
        for instance, (min_kn, max_kn, avg_kn, avg_fuel_l_per_nm) in sorted(trip.typical_rpm_speed_kn.items()):
            if avg_fuel_l_per_nm is not None:
                parts.append(
                    _i18n_tpl_html(
                        "rpm_tooltip_per_engine_fuel",
                        instance=instance, avg=_nl_num(avg_kn), min=_nl_num(min_kn), max=_nl_num(max_kn),
                        fuel=_nl_num(avg_fuel_l_per_nm, 2),
                    )
                )
            else:
                parts.append(
                    _i18n_tpl_html(
                        "rpm_tooltip_per_engine",
                        instance=instance, avg=_nl_num(avg_kn), min=_nl_num(min_kn), max=_nl_num(max_kn),
                    )
                )
        tooltip_html = ", ".join(parts)
    return (
        f'<span class="temp-hover">{visible}'
        f'<span class="temp-tooltip" style="background:#eef4fb;color:#1a4a7a;">'
        f"⚙️ {tooltip_html}</span></span>"
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
        tooltip_html = _i18n_tpl_html("max_speed_tooltip_single", time=time_text, rpm=f"{rpm:.0f}")
    elif trip.max_speed_rpm:
        tooltip_html = ", ".join(
            _i18n_tpl_html("max_speed_tooltip_per_engine", time=time_text, instance=instance, rpm=f"{rpm:.0f}")
            for instance, rpm in sorted(trip.max_speed_rpm.items())
        )
    else:
        return visible
    return (
        f'<span class="temp-hover">{visible}'
        f'<span class="temp-tooltip" style="background:#eef4fb;color:#1a4a7a;">'
        f"🚀 {tooltip_html}</span></span>"
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


def _max_speed_marker(trip: TripLeg, offset_hours: float) -> Optional[dict]:
    """Position + ready-made tooltip fields for the trip's max-speed marker on the map -- a
    distinct color/icon from the plain numbered log points, so the single fastest moment of a trip
    stands out at a glance instead of needing the Details popup opened. Includes RPM per engine,
    same reasoning as the "Topsnelheid" table cell's own tooltip (_max_speed_html): a bare speed
    number doesn't say whether it was a brief downwind surge at low RPM or genuinely flat-out.

    Looked up by matching ``max_speed_at`` against the track's own samples, not stored separately
    on TripLeg -- ``_speed_stats_kn`` already picks that time from one of ``group``'s own points
    (see tripbuilder.py), so it's always an exact match, never an interpolation."""
    if trip.max_speed_at is None or trip.max_speed_kn is None:
        return None
    sample = next((s for s in trip.track if s.time == trip.max_speed_at), None)
    if sample is None:
        return None
    if len(trip.max_speed_rpm) == 1:
        rpm_text = f"{next(iter(trip.max_speed_rpm.values())):.0f} rpm"
    elif trip.max_speed_rpm:
        rpm_text = ", ".join(
            f"#{instance}: {rpm:.0f} rpm" for instance, rpm in sorted(trip.max_speed_rpm.items())
        )
    else:
        rpm_text = None
    return {
        "lat": round(sample.lat, 6),
        "lon": round(sample.lon, 6),
        "time": f"{_to_local(trip.max_speed_at, offset_hours):%H:%M}",
        "speed": f"{_nl_num(trip.max_speed_kn)} kn",
        "rpm": rpm_text,
    }


def _trip_title(trip: TripLeg, depart_local: datetime) -> str:
    """Shared with the map popup's own title (see _trip_row_html) -- the Details popup needs the
    same "which trip is this" context, since unlike the always-visible table row it replaces a
    cell of, a popup can be scrolled away from the row that opened it."""
    return f"{depart_local:%Y-%m-%d %H:%M} {trip.depart_place} -> {trip.arrive_place}"


def _details_row_html(icon: str, label_key: str, value: str) -> str:
    return (
        f'<div class="detail-row"><span class="detail-icon">{icon}</span>'
        f'<span class="detail-label">{_i18n_span(label_key)}:</span> {value}</div>'
    )


_COMPASS_POINTS = (
    "N", "NNO", "NO", "ONO", "O", "OZO", "ZO", "ZZO",
    "Z", "ZZW", "ZW", "WZW", "W", "WNW", "NW", "NNW",
)


def _compass_abbr(deg: float) -> str:
    return _COMPASS_POINTS[round(deg / 22.5) % 16]


# Upper bound (knots, exclusive) of Beaufort forces 0-11 -- the standard WMO scale. A speed at or
# above the last threshold (64 kn) is force 12, the scale's own open-ended top end.
_BEAUFORT_THRESHOLDS_KN = (1, 4, 7, 11, 17, 22, 28, 34, 41, 48, 56, 64)


def _beaufort(kn: float) -> int:
    for force, threshold in enumerate(_BEAUFORT_THRESHOLDS_KN):
        if kn < threshold:
            return force
    return 12


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
                number_text = _i18n_span("map_marker_departure")
            elif i == last_idx:
                number_text = _i18n_span("map_marker_arrival")
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
                wind_text = (
                    f"{_nl_num(hourly.wind_kn)} kn {_compass_abbr(hourly.wind_deg)} "
                    f"(Bft {_beaufort(hourly.wind_kn)})"
                )
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
            f"<th>{_i18n_span('log_header_number')}</th>"
            f"<th>{_i18n_span('log_header_time')}</th><th>{_i18n_span('log_header_position')}</th>"
            f"<th>{_i18n_span('log_header_cog')}</th><th>{_i18n_span('log_header_sog')}</th>"
            f"<th>{_i18n_span('log_header_wind')}</th><th>{_i18n_span('log_header_precip')}</th>"
            f"<th>{_i18n_span('log_header_cloud')}</th>"
            f"<th>{_i18n_span('log_header_wave')}</th><th>{_i18n_span('log_header_current')}</th>"
            f'</tr></thead><tbody>{"".join(rows)}</tbody></table>'
        )

    water_temp = _water_temp_detail_text(trip)
    if water_temp:
        sections.append(_details_row_html("🌡️", "header_water_temp", escape(water_temp)))
    motion = _motion_detail_text(trip)
    if motion:
        sections.append(_details_row_html("〰️", "header_motion", motion))

    if not sections:
        return ""
    depart_local = _to_local(trip.depart_time, offset_hours)
    title = f'<div class="trip-map-title">{escape(_trip_title(trip, depart_local))}</div>'
    dialog = (
        f'<dialog class="log-dialog" id="log-{idx}">{title}{"".join(sections)}'
        f'<button type="button" class="close-log">{_i18n_span("log_close_button")}</button></dialog>'
    )
    return f'<button type="button" class="show-log" data-trip="{idx}">{_i18n_span("details_button")}</button>{dialog}'


def _remarks_cell_html(trip_uid: Optional[str], idx: int, remarks_api_url: str) -> str:
    """Button + <dialog> popup (same pattern as Details/Map). The remark text itself isn't known at
    generation time -- unlike everything else in this file, it doesn't come from the decoded
    NMEA2000 data at all, but from WordPress (see wordpress-plugin/), fetched by the REMARKS_*
    script after the page loads -- so this only builds the empty shell; JS fills in the button
    label and textarea once that fetch resolves. Needs a stable trip_uid to key the remark on
    (see trip_ids.py) -- without one there's nothing to attach a saved remark to, so no button.

    No login form here: reading and saving both ride on the WordPress session that got you past
    the login-gated logbook page in the first place (see logboek-index.php), not a
    separate credential entered in this dialog."""
    if not remarks_api_url or not trip_uid:
        return ""
    dialog = (
        f'<dialog class="log-dialog remarks-dialog" id="remarks-{idx}" data-trip-uid="{escape(trip_uid)}">'
        '<textarea class="remarks-textarea" rows="12" cols="60"></textarea>'
        '<p class="remarks-error" hidden></p>'
        '<div class="remarks-buttons">'
        f'<button type="button" class="remarks-save">{_i18n_span("remarks_save_button")}</button>'
        # .remarks-cancel-label (not just the button's own textContent) -- unlike every other
        # translated button, a read-only visitor's fetch-completion JS below permanently relabels
        # this one to "Close" by *changing which key this inner span's data-i18n points at*, not
        # by overwriting text directly -- so a later language switch still re-translates it
        # correctly (to "Close" in the new language, not back to "Cancel").
        f'<button type="button" class="remarks-cancel"><span class="remarks-cancel-label" '
        f'data-i18n="remarks_cancel_button">{escape(T["remarks_cancel_button"])}</span></button>'
        "</div>"
        "</dialog>"
    )
    return f'<button type="button" class="show-remarks" data-trip="{idx}">{_i18n_span("remarks_button_placeholder")}</button>{dialog}'


def _totals_html(totals: _Totals) -> str:
    avg_l_per_nm = totals.fuel_liters / totals.distance_nm if totals.distance_nm > 0 else None
    avg_l_per_hour = totals.fuel_liters / totals.moving_hours if totals.moving_hours > 0 else None
    # Distance-weighted, not a plain average of each trip's avg_speed_kn -- that would give a
    # short trip the same weight as a long one, which isn't representative of the whole period.
    avg_speed_kn = totals.distance_nm / totals.moving_hours if totals.moving_hours > 0 else None

    items = [
        (_i18n_span("totals_trips"), str(totals.trip_count)),
        (_i18n_span("totals_distance"), f"{_nl_num(totals.distance_nm)} nm"),
    ]
    # In 3rd/4th place specifically (found in practice: wanted near the top, not buried after
    # every fuel/speed stat).
    if len(totals.engine_hours_current) == 1:
        hours = next(iter(totals.engine_hours_current.values()))
        items.append((_i18n_span("totals_engine_hour_meter"), f"{_nl_num(hours)} h"))
    else:
        for instance, hours in sorted(totals.engine_hours_current.items()):
            label_html = _i18n_tpl_html("totals_engine_hour_meter_engine", instance=instance)
            items.append((label_html, f"{_nl_num(hours)} h"))
    if len(totals.engine_hours) == 1:
        hours = next(iter(totals.engine_hours.values()))
        items.append((_i18n_span("totals_hours_logged"), f"{_nl_num(hours)} h"))
    else:
        for instance, hours in sorted(totals.engine_hours.items()):
            label_html = _i18n_tpl_html("totals_hours_logged_engine", instance=instance)
            items.append((label_html, f"{_nl_num(hours)} h"))
    items.append((_i18n_span("totals_hours"), f"{_nl_num(totals.moving_hours)} h"))
    items.append((_i18n_span("totals_fuel_calculated"), f"{_nl_num(totals.fuel_liters)} L"))
    if totals.fuel_liters_device is not None:
        items.append((_i18n_span("totals_fuel_engine_meter"), f"{_nl_num(totals.fuel_liters_device)} L"))
    if avg_l_per_nm is not None:
        items.append((_i18n_span("totals_avg_consumption"), f"{_nl_num(avg_l_per_nm, 2)} L/nm"))
    if avg_l_per_hour is not None:
        items.append((_i18n_span("totals_avg_consumption"), f"{_nl_num(avg_l_per_hour)} L/h"))
    if avg_speed_kn is not None:
        items.append((_i18n_span("totals_avg_speed"), f"{_nl_num(avg_speed_kn)} kn"))
    if totals.max_speed_kn is not None:
        items.append((_i18n_span("totals_top_speed"), f"{_nl_num(totals.max_speed_kn)} kn"))

    cards = "".join(
        f'<div class="stat"><div class="stat-label">{label_html}</div>'
        f'<div class="stat-value">{escape(value)}</div></div>'
        for label_html, value in items
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
        f'<button class="show-map" data-trip="{idx}">{_i18n_span("map_button_show")}</button>'
        if trip.track
        else ""
    )
    # Warnings shown in parentheses right after the engine hours they belong to, not their own
    # column -- found in practice, asked for explicitly: a whole column that's empty for the
    # overwhelming majority of trips wasn't worth the width, and this reads just as clearly right
    # next to the hours themselves.
    engine_hours_cell = escape(_engine_hours_text(trip))
    warnings_html = _warnings_html(trip, battery_warning_voltage, offset)
    if warnings_html:
        engine_hours_cell += f" ({warnings_html})"

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
        engine_hours_cell,
        _typical_rpm_html(trip),
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
    return f'<tr class="trip-row" data-trip="{idx}"{uid_attr}>{row}</tr>{map_row}'


_HEADER_FULL_KEYS = {
    "header_departure_abbr": "header_departure_full",
    "header_arrival_abbr": "header_arrival_full",
}
# Shown abbreviated always, with just a native title="" tooltip on hover -- unlike Vertr./Aank.
# above, expanding this one on a wide viewport isn't worth it: with this many columns the table
# needs horizontal scrolling regardless of viewport width anyway (found in practice), so it would
# only ever waste column width without actually helping anyone see more of the table at once.
_HEADER_ABBR_TITLE_KEYS = {"header_seq_abbr": "header_seq_full"}
# Headers whose meaning isn't obvious from the label alone get a hover tooltip (same CSS-only
# mechanism as the table cells, see .temp-hover/.temp-tooltip) instead of a longer header.
_HEADER_TOOLTIP_KEYS: Dict[str, str] = {}


def _header_cell_html(key: str) -> str:
    """key is a translations.py key (e.g. "header_seq_abbr"), not the resolved Dutch text --
    every piece shown here is individually wrapped so the language switcher (see the <script> in
    write_html_logbook) can re-translate a header cell without needing to know this function's own
    HTML structure."""
    title_key = _HEADER_ABBR_TITLE_KEYS.get(key)
    if title_key is not None:
        return (
            f'<span title="{escape(T[title_key])}" data-i18n="{escape(key)}" '
            f'data-i18n-title="{escape(title_key)}">{escape(T[key])}</span>'
        )
    full_key = _HEADER_FULL_KEYS.get(key)
    if full_key is None:
        base = _i18n_span(key)
    else:
        # Shows the abbreviation by default; a wide-enough viewport swaps to the full word (see
        # the .hdr-full / .hdr-abbr media query in write_html_logbook's <style>).
        base = (
            f'<span class="hdr-full" data-i18n="{escape(full_key)}">{escape(T[full_key])}</span>'
            f'<span class="hdr-abbr" data-i18n="{escape(key)}">{escape(T[key])}</span>'
        )
    tooltip_key = _HEADER_TOOLTIP_KEYS.get(key)
    if tooltip_key is None:
        return base
    return (
        f'<span class="temp-hover">{base}'
        f'<span class="temp-tooltip" style="background:#eef4fb;color:#1a4a7a;" '
        f'data-i18n="{escape(tooltip_key)}">{escape(T[tooltip_key])}</span></span>'
    )


_HEADER_KEYS = [
    "header_seq_abbr",
    "header_date",
    "header_departure_abbr",
    "header_from",
    "header_arrival_abbr",
    "header_to",
    "header_duration",
    "header_distance",
    "header_avg_speed",
    "header_max_speed",
    "header_fuel",
    "header_l_per_nm",
    "header_engine_hours",
    "header_rpm",
    "header_route",
    "header_details",
]


def _headers_for(remarks_api_url: str) -> List[str]:
    """The Remarks column only exists at all when the feature is configured (see
    _DEFAULT_REMARKS_API_URL) -- unlike Route/Details, whose *column* always exists even though
    individual trips without track data leave that cell empty, "remarks enabled" is a whole-
    document setting, not a per-trip one, so an unused column isn't shown at all rather than
    always being present-but-empty."""
    return _HEADER_KEYS + ["header_remarks"] if remarks_api_url else _HEADER_KEYS


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
    log_interval_minutes: float = _DEFAULT_LOG_INTERVAL_MINUTES,
    remarks_api_url: str = _DEFAULT_REMARKS_API_URL,
    weather=None,
    marine=None,
    geocoder=None,
    latest_position: Optional[PositionFix] = None,
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

    ``remarks_api_url``: URL of the WordPress REST endpoint that stores per-trip remarks (see
    ``wordpress-plugin/``). Empty (default) disables the whole Remarks column.

    ``weather``: a weather.WeatherFetcher (or weather.NoWeather, the default) -- adds wind/
    precipitation/cloud-cover columns to each trip's own periodic log table (see
    _details_cell_html), one lookup per log row rather than one per trip, since wind especially
    can change a lot over a longer trip.

    ``marine``: a marine.MarineFetcher (or marine.NoMarine, the default) -- adds wave and ocean-
    current columns to the same periodic log table, from a separate Open-Meteo dataset than
    ``weather``.

    ``latest_position``: the single most recent GPS fix in the *whole* dataset (e.g. cli.py's own
    ``all_fixes[-1]``), shown under "Laatst bijgewerkt" as a place name (via ``geocoder``) -- asked
    for explicitly, since a trip that's still underway when the data runs out has no arrival place
    of its own to show in the trips table (a raw "Unknown (end outside log file)"), leaving no
    indication anywhere on the page of where the boat actually last was. Deliberately *not* derived
    from the last trip's own arrive_lat/arrive_lon/arrive_time: those can lag behind the true latest
    fix by days if the boat has been sitting anchored/idle (still logging position) since the last
    trip closed off, same reasoning as ``latest_data_at`` above. None (the default) shows nothing.

    ``geocoder``: a geocode.Geocoder (or geocode.NoGeocoder, the default) -- reverse-geocodes
    ``latest_position`` into a place name the same way every trip's own depart_place/arrive_place
    already is, so a mid-sea position reads the same as everywhere else on this page (e.g. "op het
    water, bij Pornichet") instead of a bare, un-styled lat/lon."""
    if geocoder is None:
        geocoder = NoGeocoder()
    trips = list(trips)
    uid_by_trip = {id(trip): uid for trip, uid in zip(trips, trip_uids)} if trip_uids is not None else {}
    trips = sorted(trips, key=lambda t: t.depart_time)

    if latest_data_at is None and trips:
        latest_data_at = max(t.arrive_time for t in trips)

    last_updated_html = ""
    last_position_html = ""
    if latest_data_at is not None:
        # No single trip necessarily covers latest_data_at (it can be later than every trip's own
        # arrival, e.g. while anchored) -- the most recent trip's own offset is still the best
        # available estimate of the current local timezone, since the boat is very unlikely to
        # have jumped somewhere wildly different since then.
        offset = _trip_utc_offset_hours(trips[-1], utc_offset_hours) if trips else (utc_offset_hours or 0.0)
        latest_local = _to_local(latest_data_at, offset)
        last_updated_html = (
            f'<div class="last-updated">{_i18n_span("last_updated")}: {latest_local:%Y-%m-%d %H:%M}</div>'
        )
        if latest_position is not None:
            place = geocoder.place_name(latest_position.lat, latest_position.lon)
            position_time_local = _to_local(latest_position.time, offset)
            # A small in-page map popup (asked for explicitly), not the external Google Maps link
            # this used to be -- that link never worked from inside the Android app's WebView
            # (target="_blank" has nowhere to go there, same class of problem the Overzicht map's
            # own dialog already solves for trips). data-lat/lon/place/time feed the click handler
            # below rather than a URL, so no geocoding/formatting logic needs duplicating in JS.
            last_position_html = (
                f'<div class="last-updated">{_i18n_span("last_position")}: '
                f'<a href="#" class="show-last-position" '
                f'data-lat="{latest_position.lat:.5f}" data-lon="{latest_position.lon:.5f}" '
                f'data-place="{escape(place)}">{escape(place)}</a>'
                f" ({position_time_local:%Y-%m-%d %H:%M})</div>"
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
    # Accumulated across every year below (each year's own indices are disjoint, since every trip
    # belongs to exactly one calendar_year) -- fed into trip_data/YEAR_TRIP_INDICES after the loop,
    # for the "Overzicht" season map (see the <dialog id="overview-dialog"> further down).
    seq_by_index_all: Dict[int, int] = {}
    year_trip_indices: Dict[int, List[int]] = {}
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
        seq_by_index_all.update(seq_by_index)
        # Only trips with an actual GPS track have anything to place on the overview map (same
        # condition trip_data/the per-trip "Kaart" button already use below) -- ordered by seq
        # (ascending/chronological), so the map's own marker numbers read left-to-right the same
        # way the table's "Nr." column does.
        year_trip_indices[calendar_year] = [
            i for i in year_indices_chronological if trips[i].track
        ]
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
                f"{_week_label(iso_year, iso_week)}</td></tr>"
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
        # No overview link at all when there's nothing to show on it (no trip that year has a GPS
        # track) -- a link that opens an empty map would just be confusing.
        overview_link_html = (
            f' <a href="#" class="show-overview" data-year="{calendar_year}">{_i18n_span("overview_link")}</a>'
            if year_trip_indices[calendar_year]
            else ""
        )
        sections.append(
            f'<section class="year"><h2>{calendar_year}{overview_link_html}</h2>'
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
            "maxSpeed": _max_speed_marker(trip, _trip_utc_offset_hours(trip, utc_offset_hours)),
            # Only used by the "Overzicht" season map (see the <dialog id="overview-dialog">
            # further down) -- seq is the same 1-based per-year number as the table's own "Nr."
            # column (seq_by_index_all, built alongside year_trip_indices above).
            "seq": seq_by_index_all.get(idx),
            "date": _to_local(trip.depart_time, _trip_utc_offset_hours(trip, utc_offset_hours)).strftime("%Y-%m-%d"),
            "departPlace": trip.depart_place,
            "arrivePlace": trip.arrive_place,
        }
        for idx, trip in enumerate(trips)
        if trip.track
    }
    trips_json = json.dumps(trip_data).replace("</", "<\\/")
    year_trip_indices_json = json.dumps(year_trip_indices).replace("</", "<\\/")

    title = f"{boat_name} - {T['logbook_title_suffix']}" if boat_name else T["logbook_title_suffix"]
    heading = (
        f"{escape(boat_name)} &mdash; {_i18n_span('logbook_title_suffix')}"
        if boat_name
        else _i18n_span("logbook_title_suffix")
    )

    vessel_info_lines = []
    if mmsi:
        vessel_info_lines.append(f"MMSI: {escape(mmsi)}")
    if call_sign:
        vessel_info_lines.append(f"{_i18n_span('vessel_call_sign')}: {escape(call_sign)}")
    vessel_info_html = (
        '<div class="vessel-info">' + "".join(f"<div>{line}</div>" for line in vessel_info_lines) + "</div>"
        if vessel_info_lines
        else ""
    )

    # Flag buttons for the client-side language switcher (see the <script> below) -- flags/order
    # come from translations.py's LANGUAGE_FLAGS so this file doesn't hardcode which languages are
    # available. "nl" starts active since the page itself is always server-rendered in Dutch;
    # applyLanguage() below corrects this to the visitor's saved/browser language once the page's
    # own JS runs.
    lang_switcher_html = '<div class="lang-switcher">' + "".join(
        f'<button type="button" class="lang-flag{" lang-flag-text" if flag.isascii() else ""}'
        f'{" active" if lang == "nl" else ""}" data-lang="{lang}" aria-label="{lang}">{flag}</button>'
        for lang, flag in LANGUAGE_FLAGS.items()
    ) + "</div>"

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
  .header-row {{ display: flex; justify-content: space-between; align-items: flex-start; flex-wrap: wrap; gap: 0.5em 1.5em; }}
  .header-right {{ display: flex; flex-direction: column; align-items: flex-end; gap: 0.4em; }}
  .vessel-info {{ color: #444; font-size: 1.1em; text-align: right; line-height: 1.4; }}
  /* Top-right language switcher (asked for explicitly): a row of flag buttons, not a dropdown --
     only 4 languages, so all of them fit and stay one tap away instead of needing a menu opened
     first. */
  .lang-switcher {{ display: flex; gap: 0.3em; }}
  .lang-flag {{
    cursor: pointer; border: 1px solid #ccc; background: white; border-radius: 4px;
    padding: 0.15em 0.4em; font-size: 1.2em; line-height: 1.3;
  }}
  /* Every language switcher button is plain text (nl/en/fr/de), not an emoji flag -- found in
     practice: a regional-indicator flag emoji renders as literal, unpaired letters (not a flag
     glyph at all) on at least one real device/browser this site is viewed from, see
     translations.py's LANGUAGE_FLAGS for the full history. Without this class, plain text sits in
     the same box as an emoji would but reads as unstyled leftover text rather than a matching
     button; a fixed min-width plus a subtle background gives it the same visual footprint an
     emoji flag would have had -- normal weight, not bold, on request. */
  .lang-flag-text {{
    min-width: 1.6em; text-align: center; font-size: 0.75em;
    background: #eef3fa; letter-spacing: 0.02em;
  }}
  .lang-flag.active {{ border-color: #1a6ecc; box-shadow: 0 0 0 1px #1a6ecc inset; }}
  .last-updated {{ color: #666; font-size: 0.85em; margin-bottom: 1em; }}
  /* flex, not relying on vertical-align (found in practice: it aligned the "Overzicht" link -- a
     much smaller font-size than the year number -- to the surrounding text's baseline/x-height,
     not the year number's own visual center) -- the only content h2 ever has is the year number
     plus that one optional link, so this is safe to apply unconditionally. */
  h2 {{
    margin-top: 2em; border-bottom: 2px solid #1a6ecc; padding-bottom: 0.2em;
    display: flex; align-items: center; gap: 0.5em;
  }}
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
  /* #h-scroll-bar below is the only horizontal scrollbar meant to be visible -- overflow-x: auto
     above still makes .table-scroll itself scrollable (by touch, trackpad, or #h-scroll-bar's own
     JS-driven scrollLeft), but without this its *own* native scrollbar would also show at the
     bottom of every table on a platform that draws always-visible (non-overlay) scrollbars, e.g.
     Windows Chrome/Edge -- found in practice: exactly the "scrollbar's back inside the table"
     regression #h-scroll-bar was added to fix in the first place, just via a second, forgotten
     scrollbar rather than the original one ever having moved. */
  .table-scroll {{ scrollbar-width: none; }}
  .table-scroll::-webkit-scrollbar {{ display: none; }}
  /* A horizontal scrollbar that lives at the bottom of .table-scroll itself sits below every row
     of a long table -- reaching it means scrolling all the way down past the table first, asked
     to fix explicitly. #h-scroll-bar (see the <script> below) is a second, empty-looking strip
     pinned to the bottom of the *viewport* instead, kept in sync with whichever year's table is
     currently scrolled into view, so the horizontal scrollbar is always within reach regardless
     of where in a long table the page itself is scrolled to. Hidden by default (display: none)
     -- JS only shows it once a table that actually overflows horizontally is in view; nothing to
     scroll otherwise. */
  #h-scroll-bar {{
    position: fixed; left: 0; right: 0; bottom: 0; height: 14px;
    overflow-x: auto; overflow-y: hidden;
    background: #f0f0f0; border-top: 1px solid #ddd; z-index: 50; display: none;
  }}
  #h-scroll-spacer {{ height: 1px; }}
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
  /* Draggable (see the drag* JS below) so the Details popup can be moved aside instead of sitting
     centered on top of an open trip map underneath it (found in practice, asked for explicitly).
     The title bar is the natural drag handle for the Details popup; remarks-dialog has no title
     of its own, so its empty padding around the textarea/buttons works as the drag area there. */
  .log-dialog .trip-map-title {{ cursor: move; }}
  .log-dialog.dragging {{ cursor: move; user-select: none; }}
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
  /* Orange, not the log points' blue -- needs to stand out at a glance against both the blue
     route line and the blue numbered log markers. */
  .max-speed-marker {{
    background: #e67e22; color: white; border: 1px solid white; border-radius: 50%;
    width: 16px; height: 16px; display: flex; align-items: center; justify-content: center;
    font-size: 10px;
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
  .show-overview {{ font-size: 0.6em; font-weight: 400; color: #1a6ecc; }}
  .overview-dialog {{ width: min(900px, 90vw); }}
  .overview-map {{ height: 70vh; width: 100%; }}
  .last-position-dialog {{ width: min(500px, 90vw); }}
  .last-position-map {{ height: 40vh; width: 100%; }}
  /* iconSize/iconAnchor in the matching L.divIcon() call below must stay in sync with width/
     height here -- Leaflet positions the icon from those JS numbers, not this CSS. */
  .overview-marker {{
    background: #1a6ecc; color: white; border: 2px solid white; border-radius: 50%;
    width: 26px; height: 26px; display: flex; align-items: center; justify-content: center;
    font-size: 12px; font-weight: 700; cursor: pointer; box-shadow: 0 1px 3px rgba(0,0,0,0.4);
  }}
  /* Briefly marks the row a marker's own click just scrolled to -- fades back out on its own
     (transition, not a fixed background) so it doesn't look like a permanent selection state. */
  .trip-row-highlight {{ background: #fff3cd !important; transition: background 2s ease-out; }}
  .trip-row-highlight.fade-out {{ background: transparent !important; }}
  .remarks-error {{ color: #c0392b; font-size: 0.85em; margin: 0 0 0.6em; }}
  .remarks-buttons {{ display: flex; gap: 0.5em; }}
  .remarks-save {{ cursor: pointer; border: 1px solid #1a6ecc; background: #1a6ecc; color: white; border-radius: 4px; padding: 0.3em 0.8em; }}
  .remarks-cancel {{ cursor: pointer; border: 1px solid #ccc; background: white; border-radius: 4px; padding: 0.3em 0.8em; }}
</style>
</head>
<body>
<div id="h-scroll-bar"><div id="h-scroll-spacer"></div></div>
<noscript>
  <div class="noscript-warning">
    {escape(T["noscript_warning"])}
  </div>
</noscript>
<div class="header-row"><h1>{heading}</h1><div class="header-right">{vessel_info_html}{lang_switcher_html}</div></div>
{last_updated_html}
{last_position_html}
{_totals_html(_compute_totals(trips))}
{"".join(sections)}
<dialog class="log-dialog overview-dialog" id="overview-dialog">
  <div class="trip-map-title" id="overview-dialog-title"></div>
  <div class="overview-map" id="overview-map"></div>
  <button type="button" class="close-log">{_i18n_span("log_close_button")}</button>
</dialog>
<dialog class="log-dialog last-position-dialog" id="last-position-dialog">
  <div class="trip-map-title" id="last-position-dialog-title"></div>
  <div class="last-position-map" id="last-position-map"></div>
  <button type="button" class="close-log">{_i18n_span("log_close_button")}</button>
</dialog>
<script>
const TRIPS = {trips_json};
const YEAR_TRIP_INDICES = {year_trip_indices_json};
const REMARKS_API_URL = {json.dumps(remarks_api_url)};
// Filled in by the server (see wordpress-plugin/logboek-index.php) when this file is
// served through the login gate, which -- unlike this Python-generated static file -- can call
// WordPress's own wp_create_nonce('wp_rest'). A POST to the REST API needs this even though the
// browser already sends the WordPress login cookie automatically (same-origin): the nonce is
// WordPress's CSRF protection on top of that cookie, required for any state-changing (non-GET)
// REST request. If this file is opened some other way (not through the gate, or locally), the
// placeholder never gets replaced and saving fails cleanly with a REMARKS_SAVE_FAILED message
// below.
const WP_REST_NONCE = "%%WP_REST_NONCE%%";

// Language switcher (asked for explicitly): this page is always server-rendered in Dutch (see
// html_writer.py's T = NL), but every translatable bit of UI chrome also carries a data-i18n/
// data-i18n-tpl/data-i18n-month attribute (see _i18n_span()/_i18n_tpl_html() in that file) --
// I18N/MONTH_ABBR_BY_LANG embed all four languages so switching is instant and needs no server
// round-trip. Free-form data (place names, trip stats, fault/warning text from the decoded NMEA
// data itself) is never touched by this -- only fixed labels/buttons/headers are.
const I18N = {json.dumps(LANGUAGES)};
const MONTH_ABBR_BY_LANG = {json.dumps(MONTH_ABBR)};
const BOAT_NAME = {json.dumps(boat_name or "")};
let currentLang = 'nl';

function applyLanguage(lang) {{
  if (!I18N[lang]) return;
  currentLang = lang;
  document.documentElement.lang = lang;
  document.title = (BOAT_NAME ? BOAT_NAME + ' - ' : '') + I18N[lang].logbook_title_suffix;
  document.querySelectorAll('[data-i18n]').forEach(function(el) {{
    var text = I18N[lang][el.dataset.i18n];
    if (text !== undefined) el.textContent = text;
  }});
  document.querySelectorAll('[data-i18n-title]').forEach(function(el) {{
    var text = I18N[lang][el.dataset.i18nTitle];
    if (text !== undefined) el.title = text;
  }});
  document.querySelectorAll('[data-i18n-tpl]').forEach(function(el) {{
    var tpl = I18N[lang][el.dataset.i18nTpl];
    if (tpl === undefined) return;
    var args = {{}};
    try {{ args = JSON.parse(el.dataset.i18nArgs || '{{}}'); }} catch (e) {{}}
    el.textContent = tpl.replace(/\\{{(\\w+)\\}}/g, function(whole, key) {{
      return Object.prototype.hasOwnProperty.call(args, key) ? args[key] : whole;
    }});
  }});
  document.querySelectorAll('[data-i18n-month]').forEach(function(el) {{
    var months = MONTH_ABBR_BY_LANG[lang];
    var text = months && months[el.dataset.i18nMonth];
    if (text !== undefined) el.textContent = text;
  }});
  document.querySelectorAll('.lang-flag').forEach(function(btn) {{
    btn.classList.toggle('active', btn.dataset.lang === lang);
  }});
  try {{ localStorage.setItem('logbookLang', lang); }} catch (e) {{}}
}}

document.querySelectorAll('.lang-flag').forEach(function(btn) {{
  btn.addEventListener('click', function() {{ applyLanguage(btn.dataset.lang); }});
}});

(function() {{
  var savedLang = null;
  try {{ savedLang = localStorage.getItem('logbookLang'); }} catch (e) {{}}
  if (savedLang && I18N[savedLang]) {{
    applyLanguage(savedLang);
    return;
  }}
  var browserLang = ((navigator.language || 'nl').split('-')[0] || 'nl').toLowerCase();
  applyLanguage(I18N[browserLang] ? browserLang : 'nl');
}})();
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
      L.marker(TRIPS[idx].departPos).addTo(map)
        .bindTooltip(entryTooltip(I18N[currentLang].map_marker_departure, log[0]));
      L.marker(TRIPS[idx].arrivePos).addTo(map)
        .bindTooltip(entryTooltip(I18N[currentLang].map_marker_arrival, log[log.length - 1]));
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
      // Orange lightning-bolt marker for the trip's single fastest moment -- a distinct color/icon
      // from the plain numbered log points (asked for explicitly), so it stands out without
      // opening the Details popup. Absent (maxSpeed is null) whenever a trip has no RPM/speed data
      // to place it from at all (see _max_speed_marker in html_writer.py).
      var maxSpeed = TRIPS[idx].maxSpeed;
      if (maxSpeed) {{
        var maxSpeedIcon = L.divIcon({{
          className: 'max-speed-marker', html: '&#9889;', iconSize: [16, 16], iconAnchor: [8, 8]
        }});
        var maxSpeedParts = [I18N[currentLang].map_marker_max_speed, maxSpeed.time, maxSpeed.speed];
        if (maxSpeed.rpm) maxSpeedParts.push(maxSpeed.rpm);
        L.marker([maxSpeed.lat, maxSpeed.lon], {{icon: maxSpeedIcon}}).addTo(map)
          .bindTooltip(maxSpeedParts.join(' &middot; '));
      }}
      map.fitBounds(line.getBounds(), {{padding: [20, 20]}});
      setTimeout(function() {{ map.invalidateSize(); }}, 0);
    }}
  }});
}});
// Movable (not fixed-centered) so a popup can be dragged aside instead of sitting on top of an
// open trip map underneath it (found in practice, asked for explicitly). The title bar is the
// natural drag handle for the Details popup (see .log-dialog .trip-map-title); remarks-dialog
// has no title of its own, so its empty padding around the textarea/buttons works as the drag
// area there.
function enableDialogDrag(dialog) {{
  var dragging = false, moved = false, startX, startY, startLeft, startTop;
  dialog.addEventListener('pointerdown', function(e) {{
    // .leaflet-container (asked for explicitly, found in practice): without this, starting a
    // click on the overview map's own markers/tiles/zoom buttons set pointer capture on the
    // *dialog* below instead (this listener not otherwise excluding a plain <span>, which a
    // Leaflet marker's own divIcon content is) -- that redirected the click away from the
    // marker's own handler entirely, onto the dialog, which then read it as a backdrop click and
    // just closed the dialog instead of opening that trip's map. Panning/clicking the map itself
    // was always the expected gesture there anyway, not dragging the whole popup.
    if (e.target.closest('button, textarea, input, select, a, td, th, .leaflet-container')) return;
    var rect = dialog.getBoundingClientRect();
    dialog.style.position = 'fixed';
    dialog.style.margin = '0';
    dialog.style.left = rect.left + 'px';
    dialog.style.top = rect.top + 'px';
    startX = e.clientX; startY = e.clientY;
    startLeft = rect.left; startTop = rect.top;
    dragging = true; moved = false;
    dialog.classList.add('dragging');
    dialog.setPointerCapture(e.pointerId);
  }});
  dialog.addEventListener('pointermove', function(e) {{
    if (!dragging) return;
    var dx = e.clientX - startX, dy = e.clientY - startY;
    if (Math.abs(dx) > 3 || Math.abs(dy) > 3) moved = true;
    var maxLeft = Math.max(0, window.innerWidth - dialog.offsetWidth);
    var maxTop = Math.max(0, window.innerHeight - dialog.offsetHeight);
    dialog.style.left = Math.min(Math.max(0, startLeft + dx), maxLeft) + 'px';
    dialog.style.top = Math.min(Math.max(0, startTop + dy), maxTop) + 'px';
  }});
  dialog.addEventListener('pointerup', function() {{
    dragging = false;
    dialog.classList.remove('dragging');
  }});
  // Suppress the backdrop-click-closes handler below for the click that follows an actual drag
  // -- otherwise dragging by grabbing the dialog's own empty padding (remarks-dialog's only drag
  // area) closed it again right after moving it, since that click's target is still the dialog
  // element itself, same as a real backdrop click. Capture phase + stopImmediatePropagation so
  // this runs before that handler regardless of listener registration order.
  dialog.addEventListener('click', function(e) {{
    if (moved && e.target === dialog) {{ e.stopImmediatePropagation(); moved = false; }}
  }}, true);
  // Reset to centered each time it's reopened, so a dialog dragged aside doesn't stay stuck off
  // to the side (possibly off-screen after a resize) the next time it's opened.
  dialog.addEventListener('close', function() {{
    dialog.style.removeProperty('position');
    dialog.style.removeProperty('margin');
    dialog.style.removeProperty('left');
    dialog.style.removeProperty('top');
  }});
}}
// Lets the Android back button (see MainActivity's OnBackPressedCallback) close whatever popup
// is currently open -- or, if a trip was just opened *from* the Overzicht map (which closes that
// dialog on the way there, see closeOverviewAndHighlight below), reopen Overzicht instead of
// falling through to the default "leave the app" behavior (found in practice, asked for
// explicitly: with nothing left open at that point, back had nothing to undo but exiting). Kept
// as one shared, always-defined entry point rather than duplicating the "what's open" check on
// the Kotlin side, so this stays correct regardless of which dialog -- or none -- is involved.
window.__handleBackPress = function() {{
  var openDialog = document.querySelector('dialog[open]');
  if (openDialog) {{ openDialog.close(); return true; }}
  if (window.__pendingOverviewYear && window.__reopenOverview) {{
    var year = window.__pendingOverviewYear;
    window.__pendingOverviewYear = null;
    window.__reopenOverview(year);
    return true;
  }}
  return false;
}};
document.querySelectorAll('.show-log').forEach(function(btn) {{
  var dialog = document.getElementById('log-' + btn.dataset.trip);
  btn.addEventListener('click', function() {{ dialog.showModal(); }});
  dialog.querySelector('.close-log').addEventListener('click', function() {{ dialog.close(); }});
  // clicking the backdrop closes the dialog too -- a click that lands on the dialog element
  // itself (not any of its content) can only be the backdrop, since the content fills the box.
  dialog.addEventListener('click', function(e) {{
    if (e.target === dialog) dialog.close();
  }});
  enableDialogDrag(dialog);
}});
// "Overzicht" (season map, one per year): a single shared dialog/map instance reused across
// every year link, rather than one Leaflet map per year up front -- built (and, on a second
// open, just cleared and repopulated) lazily, same reasoning as the per-trip maps above only
// initializing on first open.
(function() {{
  var overviewDialog = document.getElementById('overview-dialog');
  var overviewLinks = document.querySelectorAll('.show-overview');
  if (!overviewDialog || !overviewLinks.length) return;
  var overviewMap = null;
  var overviewMarkers = [];
  var overviewLines = [];

  function closeOverviewAndHighlight(idx, year) {{
    // Remembered so the back button can reopen this Overzicht instead of exiting the app (see
    // window.__handleBackPress above) -- cleared again by the two "user actually dismissed
    // Overzicht" paths below (Sluiten button, backdrop click), so back only reopens it right
    // after a jump like this one, not after a deliberate close.
    window.__pendingOverviewYear = year;
    overviewDialog.close();
    var row = document.querySelector('.trip-row[data-trip="' + idx + '"]');
    if (!row) return;
    // Opens that trip's own map too (asked for explicitly), not just scrolling to the row --
    // reuses the existing .show-map click handler (further down) rather than duplicating its
    // lazy-init logic here, so this stays in sync with whatever that handler does. Only clicked
    // if not already open, so this can't accidentally *close* an already-open map.
    var mapButton = row.querySelector('.show-map');
    if (mapButton && !mapButton.classList.contains('active')) {{
      mapButton.click();
    }}
    // Scrolls to the *map* row, not the trip row -- asked for explicitly, covers both the map
    // having just been opened above and it having already been open before this click. The map
    // sits in its own row right after the trip row (see _trip_row_html in html_writer.py), so
    // scrollIntoView on the trip row alone doesn't promise the map (taller, and what the user
    // actually asked to see) ends up in view too -- only whatever it was called on. Falls back to
    // the trip row itself only if there's genuinely no map row (shouldn't happen here, since only
    // trips with a track ever get a marker on the overview map in the first place).
    var mapRow = document.querySelector('.trip-map-row[data-trip="' + idx + '"]');
    // Deferred a tick: the .show-map handler above schedules its own map.invalidateSize()/
    // fitBounds() via setTimeout(fn, 0) when the map has just been opened, which can still be
    // adjusting the page's layout in the same tick this scroll would otherwise start in --
    // letting that settle first keeps this scroll's own target position from being measured
    // against a layout that's still about to shift under it.
    setTimeout(function() {{
      (mapRow || row).scrollIntoView({{behavior: 'smooth', block: 'center'}});
    }}, 50);
    row.classList.remove('fade-out');
    row.classList.add('trip-row-highlight');
    // Two classes (not just removing trip-row-highlight after a timeout): the CSS transition on
    // .trip-row-highlight only animates when a *property value* changes, not when the class
    // itself disappears, so a plain removal would snap back to no-highlight instantly instead of
    // fading. fade-out changes the actual background value (to transparent) while the transition
    // rule is still in effect, so it eases out instead.
    setTimeout(function() {{ row.classList.add('fade-out'); }}, 50);
    setTimeout(function() {{ row.classList.remove('trip-row-highlight', 'fade-out'); }}, 2100);
  }}

  function openOverviewForYear(year) {{
    var indices = YEAR_TRIP_INDICES[year] || [];
    var titleTpl = I18N[currentLang].overview_dialog_title || 'Overzicht {{year}}';
    document.getElementById('overview-dialog-title').textContent = titleTpl.replace('{{year}}', year);
    overviewDialog.showModal();
    if (!overviewMap) {{
      overviewMap = L.map('overview-map');
      L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
        maxZoom: 19,
        attribution: '&copy; OpenStreetMap contributors'
      }}).addTo(overviewMap);
    }}
    overviewMarkers.forEach(function(m) {{ overviewMap.removeLayer(m); }});
    overviewLines.forEach(function(l) {{ overviewMap.removeLayer(l); }});
    overviewMarkers = [];
    overviewLines = [];
    var bounds = null;
    indices.forEach(function(idx) {{
      var trip = TRIPS[idx];
      // The real route (same points the per-trip map's own line draws, see the .show-map
      // handler above), not a straight line between depart/arrive -- asked for explicitly.
      if (!trip || !trip.points || !trip.points.length) return;
      var line = L.polyline(trip.points, {{color: '#1a6ecc', weight: 2, opacity: 0.6}}).addTo(overviewMap);
      overviewLines.push(line);
      bounds = bounds ? bounds.extend(line.getBounds()) : line.getBounds();
      // The route's own middle point (not the geometric midpoint of depart/arrive, which can
      // land nowhere near a winding route) -- close enough to "the middle of the trip" for a
      // marker anchor without needing to walk the track by distance/time.
      var mid = trip.points[Math.floor(trip.points.length / 2)];
      var icon = L.divIcon({{
        className: '', html: '<span class="overview-marker">' + trip.seq + '</span>',
        iconSize: [26, 26], iconAnchor: [13, 13]
      }});
      var marker = L.marker(mid, {{icon: icon}}).addTo(overviewMap);
      marker.bindTooltip(
        trip.date + ' &middot; ' + trip.departPlace + ' &rarr; ' + trip.arrivePlace
      );
      marker.on('click', function() {{ closeOverviewAndHighlight(idx, year); }});
      overviewMarkers.push(marker);
    }});
    setTimeout(function() {{
      overviewMap.invalidateSize();
      if (bounds) overviewMap.fitBounds(bounds, {{padding: [24, 24]}});
    }}, 0);
  }}
  // Exposed so window.__handleBackPress (see above) can reopen this dialog for whichever year the
  // user last jumped away from, without this whole IIFE's private state (overviewMap and friends)
  // needing to move out to module scope just for that one call.
  window.__reopenOverview = openOverviewForYear;

  overviewLinks.forEach(function(link) {{
    link.addEventListener('click', function(e) {{
      e.preventDefault();
      // A fresh, deliberate open -- not a "return" -- so any pending reopen from an earlier jump
      // (e.g. a different year, still sitting there if the user backed out some other way) is
      // stale now and shouldn't fire later.
      window.__pendingOverviewYear = null;
      openOverviewForYear(link.dataset.year);
    }});
  }});
  overviewDialog.querySelector('.close-log').addEventListener('click', function() {{
    window.__pendingOverviewYear = null;
    overviewDialog.close();
  }});
  overviewDialog.addEventListener('click', function(e) {{
    if (e.target === overviewDialog) {{
      window.__pendingOverviewYear = null;
      overviewDialog.close();
    }}
  }});
  enableDialogDrag(overviewDialog);
}})();
// "Laatste positie" (single-point popup, asked for explicitly instead of the external Google
// Maps link this used to be -- see last_position_html above): one shared dialog/map, same lazy-
// init-on-first-open pattern as Overzicht, just for a single marker instead of a whole season's
// routes, and zoomed in close (there's no route/bounds to fit here) rather than a wide view.
(function() {{
  var link = document.querySelector('.show-last-position');
  var dialog = document.getElementById('last-position-dialog');
  if (!link || !dialog) return;
  var map = null;
  var marker = null;

  link.addEventListener('click', function(e) {{
    e.preventDefault();
    var lat = parseFloat(link.dataset.lat);
    var lon = parseFloat(link.dataset.lon);
    document.getElementById('last-position-dialog-title').textContent = link.dataset.place;
    dialog.showModal();
    if (!map) {{
      map = L.map('last-position-map');
      L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
        maxZoom: 19,
        attribution: '&copy; OpenStreetMap contributors'
      }}).addTo(map);
      marker = L.marker([lat, lon]).addTo(map);
    }} else {{
      marker.setLatLng([lat, lon]);
    }}
    setTimeout(function() {{
      map.invalidateSize();
      // Close enough to make out the actual anchorage/berth, not just which town -- there's only
      // ever one point here, so there's no route/bounds to fitBounds() to instead.
      map.setView([lat, lon], 15);
    }}, 0);
  }});
  dialog.querySelector('.close-log').addEventListener('click', function() {{ dialog.close(); }});
  dialog.addEventListener('click', function(e) {{
    if (e.target === dialog) dialog.close();
  }});
  enableDialogDrag(dialog);
}})();
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
    enableDialogDrag(dialog);
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
          errorEl.textContent = response.status === 403
            ? I18N[currentLang].remarks_save_forbidden : I18N[currentLang].remarks_save_failed;
          errorEl.hidden = false;
          return;
        }}
        dialog.close();
      }}).catch(function() {{
        errorEl.textContent = I18N[currentLang].remarks_save_failed;
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
          // Repoints the label's own data-i18n key (rather than just setting textContent) so a
          // later language switch keeps re-translating it as "Close", not back to "Cancel" --
          // see the comment on .remarks-cancel-label in html_writer.py.
          var cancelLabel = dialog.querySelector('.remarks-cancel-label');
          cancelLabel.dataset.i18n = 'remarks_close_button';
          cancelLabel.textContent = I18N[currentLang].remarks_close_button;
        }});
      }}
    }})
    .catch(function() {{
      Object.keys(remarksButtonsByUid).forEach(function(tripUid) {{
        remarksButtonsByUid[tripUid].title = I18N[currentLang].remarks_unavailable;
      }});
    }});
}}
// #h-scroll-bar (see its own CSS comment above): a horizontal scrollbar pinned to the bottom of
// the viewport, kept in sync with whichever year's .table-scroll is currently in view, instead of
// each table's own scrollbar living below all of its rows. syncing guards against the two sides
// (this bar, and whichever .table-scroll is "current") each reacting to a scroll event the other
// side itself just caused, which would otherwise fight/jitter.
(function() {{
  var bar = document.getElementById('h-scroll-bar');
  var spacer = document.getElementById('h-scroll-spacer');
  var tableScrolls = Array.prototype.slice.call(document.querySelectorAll('.table-scroll'));
  if (!bar || !spacer || !tableScrolls.length) return;
  var current = null;
  var syncing = false;

  // The .table-scroll whose own vertical midpoint is closest to the viewport's -- a simple,
  // cheap-to-recompute-on-scroll stand-in for "which table is the owner mostly looking at right
  // now", good enough since year sections don't interleave (each one's rows are contiguous).
  function pickCurrent() {{
    var viewportMid = window.innerHeight / 2;
    var best = null, bestDist = Infinity;
    tableScrolls.forEach(function(ts) {{
      var rect = ts.getBoundingClientRect();
      if (rect.bottom < 0 || rect.top > window.innerHeight) return;  // fully off-screen
      var dist = Math.abs((rect.top + rect.bottom) / 2 - viewportMid);
      if (dist < bestDist) {{ bestDist = dist; best = ts; }}
    }});
    return best;
  }}

  function updateBar() {{
    var picked = pickCurrent();
    if (!picked || picked.scrollWidth <= picked.clientWidth + 1) {{
      bar.style.display = 'none';
      current = null;
      return;
    }}
    if (picked !== current) {{
      current = picked;
      spacer.style.width = current.scrollWidth + 'px';
      syncing = true;
      bar.scrollLeft = current.scrollLeft;
      syncing = false;
    }}
    bar.style.display = 'block';
  }}

  bar.addEventListener('scroll', function() {{
    if (syncing || !current) return;
    syncing = true;
    current.scrollLeft = bar.scrollLeft;
    syncing = false;
  }});
  tableScrolls.forEach(function(ts) {{
    ts.addEventListener('scroll', function() {{
      if (syncing || ts !== current) return;
      syncing = true;
      bar.scrollLeft = ts.scrollLeft;
      syncing = false;
    }});
  }});
  window.addEventListener('scroll', updateBar);
  window.addEventListener('resize', updateBar);
  updateBar();
}})();
</script>
</body>
</html>
"""
    path.write_text(html, encoding="utf-8")
