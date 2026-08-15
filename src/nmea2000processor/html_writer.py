"""Writes the whole logbook out as a single, self-contained HTML file: a boat name header,
overall totals (trip count, distance, fuel, engine hours, average consumption), trips grouped
by year and ISO week, a per-trip route map (Leaflet + OpenStreetMap) that opens in a popup when
you click a trip's "Kaart" button, and a per-trip periodic course/speed/position log (like a
traditional paper logbook) that opens the same way via a "Log" button.

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

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
from xml.sax.saxutils import escape

from .logbook_writer import (
    _all_warnings_text,
    _avg_consumption_l_per_nm,
    _engine_hours_text,
    _format_duration,
    _nl_num,
    _to_local,
    _trip_utc_offset_hours,
    _typical_rpm_text,
)
from .translations import MONTH_ABBR_NL, NL as T
from .tripbuilder import NavSample, TripLeg

_LEAFLET_CSS = "https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
_LEAFLET_JS = "https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
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
    moving_hours = sum(trip.duration.total_seconds() for trip in trips) / 3600.0
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


# (background, text) hex pairs for the water-temp badge, cold to warm.
_TEMP_COLORS = (
    (14, "#E6F1FB", "#0C447C"),
    (18, "#E1F5EE", "#085041"),
    (22, "#EAF3DE", "#27500A"),
    (26, "#FAEEDA", "#633806"),
)
_TEMP_COLOR_HOT = ("#FCEBEB", "#791F1F")


def _warnings_count(trip: TripLeg, battery_warning_voltage: Optional[float]) -> int:
    """Counts individual warnings (not text segments) directly from the underlying data instead
    of parsing _all_warnings_text's semicolon-joined string, since one engine's segment can
    itself contain several comma-separated warnings."""
    count = sum(len(health.warnings) for health in trip.engine_health.values())
    if battery_warning_voltage is not None:
        count += sum(
            1
            for health in trip.battery_health.values()
            if health.min_voltage_v is not None and health.min_voltage_v < battery_warning_voltage
        )
    return count


def _warnings_html(trip: TripLeg, battery_warning_voltage: Optional[float]) -> str:
    """Just the count in the cell (so a trip with many warnings doesn't blow up the column
    width); hovering shows the actual warning text, same pattern as the other tooltip columns."""
    count = _warnings_count(trip, battery_warning_voltage)
    if count == 0:
        return ""
    tooltip = escape(_all_warnings_text(trip, battery_warning_voltage))
    return (
        f'<span class="temp-hover warning-count">{count}'
        f'<span class="temp-tooltip" style="background:#fdecea;color:#7a2b22;">'
        f"⚠️ {tooltip}</span></span>"
    )


def _water_temp_badge_html(trip: TripLeg) -> str:
    """Plain text in the table cell (so it doesn't blow up the column width); hovering over it
    shows the colored thermometer badge (with the min-max range, if notable) as a CSS-only
    tooltip -- a native title="" tooltip can't be styled/colored."""
    if trip.avg_water_temp_c is None:
        return ""
    for threshold, bg, text in _TEMP_COLORS:
        if trip.avg_water_temp_c < threshold:
            break
    else:
        bg, text = _TEMP_COLOR_HOT
    range_text = ""
    if trip.max_water_temp_c - trip.min_water_temp_c > 0.5:
        range_text = f" ({_nl_num(trip.min_water_temp_c)}–{_nl_num(trip.max_water_temp_c)}°C)"
    return (
        f'<span class="temp-hover">{_nl_num(trip.avg_water_temp_c)}°C'
        f'<span class="temp-tooltip" style="background:{bg};color:{text};">'
        f"🌡️ {_nl_num(trip.avg_water_temp_c)}°C{range_text}</span></span>"
    )


def _motion_variation_html(trip: TripLeg) -> str:
    """Just the numbers (roll, then pitch standard deviation, in that order) in the table cell so
    the column stays narrow; hovering shows which is which (slingeren/stampen) plus the
    peak-to-peak range -- a trip that's mostly calm with one rough patch still averages out to a
    small standard deviation, so the single worst swing is worth surfacing separately rather than
    only showing the diluted average. The column header itself also explains the number order
    (see _HEADER_TOOLTIPS) since a bare "±2,5°, ±0,7°" means nothing without that context."""
    visible_parts = []
    tooltip_avg_parts = []
    if trip.roll_variation_deg is not None:
        visible_parts.append(f"±{_nl_num(trip.roll_variation_deg)}°")
        tooltip_avg_parts.append(f"{T['motion_roll']} ±{_nl_num(trip.roll_variation_deg)}°")
    if trip.pitch_variation_deg is not None:
        visible_parts.append(f"±{_nl_num(trip.pitch_variation_deg)}°")
        tooltip_avg_parts.append(f"{T['motion_pitch']} ±{_nl_num(trip.pitch_variation_deg)}°")
    if not visible_parts:
        return ""
    visible = escape(", ".join(visible_parts))

    peak_parts = []
    if trip.roll_range_deg is not None:
        peak_parts.append(f"{T['motion_roll']} {T['motion_peak']} {_nl_num(trip.roll_range_deg)}°")
    if trip.pitch_range_deg is not None:
        peak_parts.append(f"{T['motion_pitch']} {T['motion_peak']} {_nl_num(trip.pitch_range_deg)}°")

    tooltip_text = ", ".join(tooltip_avg_parts)
    if peak_parts:
        tooltip_text += " (" + ", ".join(peak_parts) + ")"
    tooltip = escape(tooltip_text)
    return (
        f'<span class="temp-hover">{visible}'
        f'<span class="temp-tooltip" style="background:#eef4fb;color:#1a4a7a;">'
        f"〰️ {tooltip}</span></span>"
    )


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
        min_kn, max_kn, avg_kn = next(iter(trip.typical_rpm_speed_kn.values()))
        tooltip = T["rpm_tooltip_single"].format(
            avg=_nl_num(avg_kn), min=_nl_num(min_kn), max=_nl_num(max_kn)
        )
    else:
        tooltip = ", ".join(
            T["rpm_tooltip_per_engine"].format(
                instance=instance, avg=_nl_num(avg_kn), min=_nl_num(min_kn), max=_nl_num(max_kn)
            )
            for instance, (min_kn, max_kn, avg_kn) in sorted(trip.typical_rpm_speed_kn.items())
        )
    tooltip = escape(tooltip)
    return (
        f'<span class="temp-hover">{visible}'
        f'<span class="temp-tooltip" style="background:#eef4fb;color:#1a4a7a;">'
        f"⚙️ {tooltip}</span></span>"
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


def _log_cell_html(trip: TripLeg, idx: int, interval_minutes: float, offset_hours: float) -> str:
    """Button + <dialog> popup (like the map, not an inline-expanding table) so opening the log
    doesn't push the rest of a possibly very wide, already horizontally-scrolled trips table
    around -- found in practice: with an inline table, the COG/SOG columns could end up scrolled
    out of view off the right edge of the same .table-scroll region the button itself was in,
    making it look like the log only ever had a time and position column."""
    entries = _periodic_log_entries(trip.track, interval_minutes, offset_hours)
    if len(entries) < 2:
        return ""
    rows = []
    for entry in entries:
        local_time = _to_local(entry.time, offset_hours)
        cog_text = f"{_nl_num(entry.cog_deg, 0)}&deg;" if entry.cog_deg is not None else ""
        sog_text = f"{_nl_num(entry.sog_ms / _KNOT_IN_MS)} kn"
        position_text = f"{entry.lat:.4f}, {entry.lon:.4f}"
        rows.append(
            f"<tr><td>{local_time:%H:%M}</td><td>{position_text}</td>"
            f"<td>{cog_text}</td><td>{sog_text}</td></tr>"
        )
    table = (
        "<table class=\"log-table\"><thead><tr>"
        f"<th>{escape(T['log_header_time'])}</th><th>{escape(T['log_header_position'])}</th>"
        f"<th>{escape(T['log_header_cog'])}</th><th>{escape(T['log_header_sog'])}</th>"
        f'</tr></thead><tbody>{"".join(rows)}</tbody></table>'
    )
    dialog = (
        f'<dialog class="log-dialog" id="log-{idx}">{table}'
        f'<button type="button" class="close-log">{escape(T["log_close_button"])}</button></dialog>'
    )
    return f'<button type="button" class="show-log" data-trip="{idx}">{escape(T["log_button"])}</button>{dialog}'


def _remarks_cell_html(trip_uid: Optional[str], idx: int, remarks_api_url: str) -> str:
    """Button + <dialog> popup (same pattern as Log/Map). The remark text itself isn't known at
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
        '<textarea class="remarks-textarea" rows="4" cols="40"></textarea>'
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
        (T["totals_hours"], f"{_nl_num(totals.moving_hours)} h"),
        (T["totals_fuel_calculated"], f"{_nl_num(totals.fuel_liters)} L"),
    ]
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

    # The engine's own absolute hour meter (for maintenance intervals), as of the most recent
    # trip -- distinct from "hours logged", which only counts time run during this logbook's
    # own trips and misses everything the engine ran before logging started.
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

    cards = "".join(
        f'<div class="stat"><div class="stat-label">{escape(label)}</div>'
        f'<div class="stat-value">{escape(value)}</div></div>'
        for label, value in items
    )
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
) -> str:
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
        _nl_num(trip.max_speed_kn) + " kn" if trip.max_speed_kn is not None else "",
        _nl_num(trip.fuel_liters) + " L",
        f"{_nl_num(avg_consumption_nm, 2)} L/nm" if avg_consumption_nm is not None else "",
        escape(_engine_hours_text(trip)),
        _typical_rpm_html(trip),
        _warnings_html(trip, battery_warning_voltage),
        _water_temp_badge_html(trip),
        _motion_variation_html(trip),
        map_cell,
        _log_cell_html(trip, idx, log_interval_minutes, offset),
    ]
    if remarks_api_url:
        cells.append(_remarks_cell_html(trip_uid, idx, remarks_api_url))
    row = "".join(f"<td>{cell}</td>" for cell in cells)
    map_row = ""
    if trip.track:
        title = escape(f"{depart_local:%Y-%m-%d %H:%M} {trip.depart_place} -> {trip.arrive_place}")
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
_HEADER_TOOLTIPS = {T["header_motion"]: T["header_motion_tooltip"]}


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
    T["header_water_temp"],
    T["header_motion"],
    T["header_route"],
    T["header_log"],
]


def _headers_for(remarks_api_url: str) -> List[str]:
    """The Remarks column only exists at all when the feature is configured (see
    _DEFAULT_REMARKS_API_URL) -- unlike Map/Log/Route, whose *column* always exists even though
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
    generated_at: Optional[datetime] = None,
    log_interval_minutes: float = _DEFAULT_LOG_INTERVAL_MINUTES,
    remarks_api_url: str = _DEFAULT_REMARKS_API_URL,
) -> None:
    """``trip_uids``: one id per trip, in the same order as ``trips`` *before* sorting -- e.g.
    from ``trip_ids.assign_trip_ids(trips)``. Embedded as an invisible ``data-uid`` attribute on
    each trip row, and used to key the Remarks feature (see ``remarks_api_url``) -- a uid that
    survives a trip-recognition fix reshuffling exact timestamps is the whole reason it exists.

    ``generated_at``: shown as "Laatst bijgewerkt" under the heading, in the local time of
    whoever generates the file. Defaults to now; a caller passes a fixed value only for
    testing.

    ``remarks_api_url``: URL of the WordPress REST endpoint that stores per-trip remarks (see
    ``wordpress-plugin/``). Empty (default) disables the whole Remarks column."""
    if generated_at is None:
        generated_at = datetime.now()
    trips = list(trips)
    uid_by_trip = {id(trip): uid for trip, uid in zip(trips, trip_uids)} if trip_uids is not None else {}
    trips = sorted(trips, key=lambda t: t.depart_time)

    by_week: Dict[Tuple[int, int], List[int]] = defaultdict(list)
    for idx, trip in enumerate(trips):
        offset = _trip_utc_offset_hours(trip, utc_offset_hours)
        local_date = _to_local(trip.depart_time, offset).date()
        iso_year, iso_week, _ = local_date.isocalendar()
        by_week[(iso_year, iso_week)].append(idx)

    headers = _headers_for(remarks_api_url)
    header_html = "".join(f"<th>{_header_cell_html(h)}</th>" for h in headers)

    sections: List[str] = []
    for iso_year in sorted({y for y, _ in by_week}, reverse=True):
        weeks_in_year = sorted((w for y, w in by_week if y == iso_year), reverse=True)
        year_trips = [trips[i] for w in weeks_in_year for i in by_week[(iso_year, w)]]
        # ``trips`` is already sorted chronologically (see above), so a trip's own position
        # within its year's indices, sorted ascending, is exactly its 1-based sequence number
        # for that year -- resets every year since each year's indices are handled separately.
        seq_by_index = {
            i: n + 1
            for n, i in enumerate(sorted(i for w in weeks_in_year for i in by_week[(iso_year, w)]))
        }
        # One continuous <table> for the whole year (not one per week): a single table lets the
        # browser compute column widths from *all* the year's rows together, so every week lines
        # up automatically and no column ever ends up narrower than its widest content -- which
        # a separate table per week, or hand-picked fixed column widths, can't guarantee.
        body_rows: List[str] = []
        for iso_week in weeks_in_year:
            indices = by_week[(iso_year, iso_week)]
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
                )
                for i in indices
            )
        sections.append(
            f'<section class="year"><h2>{iso_year}</h2>'
            f"{_totals_html(_compute_totals(year_trips))}"
            f'<div class="table-scroll"><table class="trips"><thead><tr>{header_html}</tr></thead>'
            f'<tbody>{"".join(body_rows)}</tbody></table></div></section>'
        )

    trip_data = {
        idx: {"points": _decimated_points(trip)}
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
<link rel="stylesheet" href="{_LEAFLET_CSS}">
<script src="{_LEAFLET_JS}"></script>
<style>
  body {{ font-family: sans-serif; margin: 0; padding: 1.5em; background: #f7f7f8; color: #1a1a1a; }}
  h1 {{ margin: 0 0 0.2em; }}
  .header-row {{ display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 0.5em 1.5em; }}
  .vessel-info {{ color: #444; font-size: 1.1em; text-align: right; line-height: 1.4; }}
  .last-updated {{ color: #666; font-size: 0.85em; margin-bottom: 1em; }}
  h2 {{ margin-top: 2em; border-bottom: 2px solid #1a6ecc; padding-bottom: 0.2em; }}
  .totals {{ display: flex; flex-wrap: wrap; gap: 1em; margin: 1em 0 2em; }}
  .stat {{ background: white; border-radius: 8px; padding: 0.8em 1.2em; box-shadow: 0 1px 3px rgba(0,0,0,0.1); min-width: 140px; }}
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
  .show-map:hover, .show-log:hover {{ background: #1a6ecc; color: white; }}
  .trip-map-title {{ font-weight: 600; margin-bottom: 0.4em; }}
  /* A popup (like the map) instead of an inline-expanding table on purpose: in a wide, already
     horizontally-scrolled trips table, expanding a table inline pushed the COG/SOG columns off
     the right edge of the same scroll region the button itself was in, making the log look like
     it only ever had a time and position column (found in practice). A dialog isn't constrained
     by that scroll position at all. */
  .log-dialog {{ border: none; border-radius: 8px; padding: 1em 1.2em; box-shadow: 0 4px 20px rgba(0,0,0,0.25); }}
  .log-dialog::backdrop {{ background: rgba(0,0,0,0.4); }}
  .log-table {{ border-collapse: collapse; white-space: nowrap; margin-bottom: 0.8em; }}
  .log-table th, .log-table td {{ padding: 0.2em 0.6em; border-bottom: 1px solid #eee; text-align: left; font-size: 0.9em; }}
  .log-table th {{ background: #f0f0f0; }}
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
  .map {{ height: 350px; }}
  .noscript-warning {{
    background: #fff3cd; color: #664d03; border: 1px solid #ffe69c; border-radius: 8px;
    padding: 0.8em 1.2em; margin-bottom: 1.5em;
  }}
  .show-remarks {{ cursor: pointer; border: 1px solid #1a6ecc; background: white; color: #1a6ecc; border-radius: 4px; padding: 0.2em 0.6em; white-space: nowrap; }}
  .show-remarks:hover {{ background: #1a6ecc; color: white; }}
  .remarks-dialog {{ width: 24em; max-width: 90vw; }}
  .remarks-textarea {{ width: 100%; box-sizing: border-box; font: inherit; margin-bottom: 0.8em; }}
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
<div class="last-updated">{escape(T["last_updated"])}: {generated_at:%Y-%m-%d %H:%M}</div>
{_totals_html(_compute_totals(trips))}
{"".join(sections)}
<script>
const TRIPS = {trips_json};
const MAP_SHOW = {json.dumps(T["map_button_show"])};
const MAP_HIDE = {json.dumps(T["map_button_hide"])};
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
    btn.textContent = visible ? MAP_SHOW : MAP_HIDE;
    if (!visible && !row.dataset.initialized) {{
      row.dataset.initialized = '1';
      var map = L.map('map-' + idx);
      L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
        maxZoom: 19,
        attribution: '&copy; OpenStreetMap contributors'
      }}).addTo(map);
      var points = TRIPS[idx].points;
      var line = L.polyline(points, {{color: '#1a6ecc', weight: 3}}).addTo(map);
      L.marker(points[0]).addTo(map).bindPopup(MAP_MARKER_DEPARTURE);
      L.marker(points[points.length - 1]).addTo(map).bindPopup(MAP_MARKER_ARRIVAL);
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

  fetch(REMARKS_API_URL, {{credentials: 'same-origin'}})
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
