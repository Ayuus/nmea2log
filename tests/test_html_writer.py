import json
import re
from datetime import datetime, timedelta
from pathlib import Path

from nmea2000processor.html_writer import write_html_logbook
from nmea2000processor.model import PositionFix
from nmea2000processor.tripbuilder import BatteryHealth, EngineHealth, NavSample, TripLeg
from nmea2000processor.marine import HourlyMarine
from nmea2000processor.weather import HourlyWeather


class _StubGeocoder:
    def __init__(self) -> None:
        self.calls = []

    def place_name(self, lat: float, lon: float) -> str:
        self.calls.append((lat, lon))
        return f"Port@{lat:.2f},{lon:.2f}"


def _trip(**overrides) -> TripLeg:
    defaults = dict(
        depart_time=datetime(2026, 7, 15, 9, 0),
        arrive_time=datetime(2026, 7, 15, 10, 30),
        depart_place="Marina A",
        arrive_place="Marina B",
        depart_lat=52.3,
        depart_lon=4.9,
        arrive_lat=52.4,
        arrive_lon=4.95,
        duration=timedelta(hours=1, minutes=30),
        distance_nm=12.0,
        avg_speed_kn=None,
        max_speed_kn=None,
        fuel_liters=6.0,
        fuel_liters_device=None,
        engine_hours={0: 1.5},
        engine_hours_total={0: 123.4},
        engine_health={},
        battery_health={},
        typical_rpm={},
        typical_rpm_speed_kn={},
        min_depth_m=None,
        min_depth_lat=None,
        min_depth_lon=None,
        avg_water_temp_c=None,
        min_water_temp_c=None,
        max_water_temp_c=None,
        roll_variation_deg=None,
        pitch_variation_deg=None,
        roll_range_deg=None,
        pitch_range_deg=None,
        track=[],
    )
    defaults.update(overrides)
    return TripLeg(**defaults)


def test_write_html_logbook_includes_boat_name_in_title_and_heading(tmp_path: Path):
    out_path = tmp_path / "logbook.html"

    write_html_logbook([_trip()], out_path, boat_name="Zeevalk")

    html = out_path.read_text(encoding="utf-8")
    assert "<title>Zeevalk - Vaarlogboek</title>" in html
    assert "Zeevalk" in html and "Vaarlogboek" in html


def test_write_html_logbook_has_a_home_screen_icon(tmp_path: Path):
    """Without an apple-touch-icon, iOS's own "Add to Home Screen" falls back to an ugly
    screenshot of the page as the icon (found in practice)."""
    out_path = tmp_path / "logbook.html"

    write_html_logbook([_trip()], out_path, boat_name="Zeevalk")

    html = out_path.read_text(encoding="utf-8")
    assert '<link rel="icon" href="data:image/svg+xml;base64,' in html
    assert '<link rel="apple-touch-icon" href="data:image/svg+xml;base64,' in html
    assert '<meta name="apple-mobile-web-app-capable" content="yes">' in html
    assert '<meta name="apple-mobile-web-app-title" content="Zeevalk - Vaarlogboek">' in html


def test_write_html_logbook_shows_mmsi_and_call_sign(tmp_path: Path):
    out_path = tmp_path / "logbook.html"

    write_html_logbook([_trip()], out_path, mmsi="244003579", call_sign="PI 3201")

    html = out_path.read_text(encoding="utf-8")
    assert "MMSI: 244003579" in html
    assert 'data-i18n="vessel_call_sign">Roepnaam</span>: PI 3201' in html


def test_write_html_logbook_omits_vessel_info_when_not_given(tmp_path: Path):
    out_path = tmp_path / "logbook.html"

    write_html_logbook([_trip()], out_path)

    html = out_path.read_text(encoding="utf-8")
    # Not "Roepnaam" not in html -- that word is now always present regardless, embedded in the
    # page's own I18N JS object for the language switcher (see write_html_logbook) -- the actual
    # thing being tested is that the vessel-info block itself doesn't render at all.
    assert '<div class="vessel-info">' not in html


def test_write_html_logbook_without_boat_name(tmp_path: Path):
    out_path = tmp_path / "logbook.html"

    write_html_logbook([_trip()], out_path, boat_name=None)

    html = out_path.read_text(encoding="utf-8")
    assert "<title>Vaarlogboek</title>" in html


def test_write_html_logbook_shows_totals(tmp_path: Path):
    trip_a = _trip(distance_nm=10.0, fuel_liters=5.0, engine_hours={0: 1.0})
    trip_b = _trip(distance_nm=6.0, fuel_liters=3.0, engine_hours={0: 0.5, 1: 2.0})
    out_path = tmp_path / "logbook.html"

    write_html_logbook([trip_a, trip_b], out_path)

    html = out_path.read_text(encoding="utf-8")
    assert "16,0 nm" in html  # total distance
    assert "8,0 L" in html  # total fuel
    assert "Gelogde motoruren, motor 0" in html
    assert "Gelogde motoruren, motor 1" in html


def test_write_html_logbook_engine_hour_totals_are_3rd_and_4th_cards(tmp_path: Path):
    trip = _trip(engine_hours={0: 1.0}, engine_hours_total={0: 42.0})
    out_path = tmp_path / "logbook.html"

    write_html_logbook([trip], out_path)

    html = out_path.read_text(encoding="utf-8")
    stat_labels = re.findall(r'<div class="stat-label">(?:<span[^>]*>)?([^<]*)', html)
    assert stat_labels[2] == "Motoruren-teller"
    assert stat_labels[3] == "Gelogde motoruren"


def test_write_html_logbook_year_totals_use_the_latest_engine_hour_meter_reading(tmp_path: Path):
    """Regression test for a real bug: the per-year totals were computed from a trip list built
    in "weeks descending" (display) order, not chronological order, so _compute_totals -- which
    relies on its input being chronological to pick out the *latest* engine-hour-meter reading --
    picked up a trip from the year's *earliest* week instead, understating "Motoruren-teller"
    (found in practice: the year section's own total didn't match the correct one shown at the
    top of the page, which uses the full, correctly-sorted trip list)."""
    early = _trip(
        depart_time=datetime(2026, 1, 5, 9, 0), arrive_time=datetime(2026, 1, 5, 10, 0),
        engine_hours_total={0: 100.0},
    )
    middle = _trip(
        depart_time=datetime(2026, 4, 1, 9, 0), arrive_time=datetime(2026, 4, 1, 10, 0),
        engine_hours_total={0: 150.0},
    )
    latest = _trip(
        depart_time=datetime(2026, 7, 15, 9, 0), arrive_time=datetime(2026, 7, 15, 10, 0),
        engine_hours_total={0: 200.0},
    )
    out_path = tmp_path / "logbook.html"

    write_html_logbook([early, middle, latest], out_path, utc_offset_hours=0)

    html = out_path.read_text(encoding="utf-8")
    # Appears twice: once in the top-of-page totals (all years), once in the 2026 section's own
    # totals -- both must reflect the truly latest trip's reading, not the earliest week's.
    assert html.count("200,0 h") == 2


def test_write_html_logbook_shows_hours_underway_and_avg_speed(tmp_path: Path):
    trip_a = _trip(distance_nm=10.0, duration=timedelta(hours=2))
    trip_b = _trip(distance_nm=6.0, duration=timedelta(hours=2))
    out_path = tmp_path / "logbook.html"

    write_html_logbook([trip_a, trip_b], out_path)

    html = out_path.read_text(encoding="utf-8")
    assert "Totale vaaruren" in html
    assert "4,0 h" in html  # 2h + 2h
    assert "Gem. snelheid" in html
    assert "4,0 kn" in html  # 16 nm / 4 h, distance-weighted


def test_write_html_logbook_total_hours_matches_the_sum_of_rounded_trip_durations(tmp_path: Path):
    """Regression test for a real inconsistency: "Totale vaaruren" used to sum each trip's *raw*
    duration, while the "Duur" column floor-rounded each trip's own value for display -- so a
    user adding up the visible per-row durations by hand could get a different total than the
    card showed (found in practice). Both now go through the same per-trip rounded-minutes
    value, and floor became round while at it (less biased for an individual trip's own
    display)."""
    trips = [_trip(duration=timedelta(minutes=5, seconds=40)) for _ in range(10)]
    out_path = tmp_path / "logbook.html"

    write_html_logbook(trips, out_path)

    html = out_path.read_text(encoding="utf-8")
    assert "<td>0:06</td>" in html  # each trip's own duration, rounded up from 5:40
    # 10 * 0:06 = 1:00 -- not "0,9 h", which is what summing the *unrounded* 5:40s would give.
    assert "1,0 h" in html


def test_write_html_logbook_totals_omit_engine_label_with_one_engine(tmp_path: Path):
    trip = _trip(engine_hours={0: 1.5})
    out_path = tmp_path / "logbook.html"

    write_html_logbook([trip], out_path)

    html = out_path.read_text(encoding="utf-8")
    assert 'data-i18n="totals_hours_logged">Gelogde motoruren</span></div>' in html
    assert "engine 0" not in html.lower()


def test_write_html_logbook_shows_current_engine_hour_meter(tmp_path: Path):
    # a later trip's reading should win over an earlier one, since it's the most recent
    trip_a = _trip(
        depart_time=datetime(2026, 7, 15, 9, 0), arrive_time=datetime(2026, 7, 15, 10, 0),
        engine_hours_total={0: 100.0},
    )
    trip_b = _trip(
        depart_time=datetime(2026, 7, 16, 9, 0), arrive_time=datetime(2026, 7, 16, 10, 0),
        engine_hours_total={0: 102.5},
    )
    out_path = tmp_path / "logbook.html"

    write_html_logbook([trip_a, trip_b], out_path)

    html = out_path.read_text(encoding="utf-8")
    assert 'data-i18n="totals_engine_hour_meter">Motoruren-teller</span></div>' in html
    assert "102,5 h" in html
    assert "100,0 h" not in html


def test_write_html_logbook_groups_by_year_and_week(tmp_path: Path):
    trip_2025 = _trip(depart_time=datetime(2025, 6, 10, 9, 0), arrive_time=datetime(2025, 6, 10, 10, 0))
    trip_2026 = _trip(depart_time=datetime(2026, 7, 15, 9, 0), arrive_time=datetime(2026, 7, 15, 10, 0))
    out_path = tmp_path / "logbook.html"

    write_html_logbook([trip_2025, trip_2026], out_path)

    html = out_path.read_text(encoding="utf-8")
    assert "<h2>2025</h2>" in html
    assert "<h2>2026</h2>" in html
    # 2026 is a more recent year and must appear before 2025 in the document
    assert html.index("<h2>2026</h2>") < html.index("<h2>2025</h2>")


def test_write_html_logbook_year_sections_use_calendar_year_not_iso_week_year(tmp_path: Path):
    """Regression test for a real bug: year sections used to group by the ISO *week's* year
    (date.isocalendar()[0]), not the trip's own calendar year -- those disagree for a few days
    every year (e.g. 2025-12-31 falls in ISO week 1 of *2026*; 2027-01-01 falls in ISO week 53 of
    *2026*), so a trip right at a year boundary could silently land in the wrong year's section
    and update that year's totals (e.g. "Motoruren-teller") even though that year was otherwise
    already done (found in practice: a New Year's Day trip would have changed the *previous*
    year's engine-hour-meter reading). Both trips below fall in the exact same ISO (year, week)
    -- (2026, 1) -- but on either side of the calendar year boundary, and must end up in their
    own, separate year sections."""
    new_years_eve = _trip(
        depart_time=datetime(2025, 12, 31, 9, 0), arrive_time=datetime(2025, 12, 31, 10, 0),
        engine_hours_total={0: 100.0},
    )
    new_years_day = _trip(
        depart_time=datetime(2026, 1, 2, 9, 0), arrive_time=datetime(2026, 1, 2, 10, 0),
        engine_hours_total={0: 5.0},
    )
    out_path = tmp_path / "logbook.html"

    write_html_logbook([new_years_eve, new_years_day], out_path, utc_offset_hours=0)

    html = out_path.read_text(encoding="utf-8")
    assert "<h2>2025</h2>" in html
    assert "<h2>2026</h2>" in html
    # each year's own "Motoruren-teller" must reflect only its own trip's reading, not whichever
    # trip happens to be chronologically last overall
    section_2025 = html[html.index("<h2>2025</h2>") : html.index("<h2>2025</h2>") + 2000]
    section_2026 = html[html.index("<h2>2026</h2>") : html.index("<h2>2026</h2>") + 2000]
    assert "100,0 h" in section_2025
    assert "5,0 h" in section_2026


def test_write_html_logbook_yearly_and_overall_totals_sum_correctly_and_stay_isolated_by_year(
    tmp_path: Path,
):
    """Coverage for the year-isolation guarantee from a different angle than the ISO-week-boundary
    regression test above: with two ordinary trips in each of two different years, each year's own
    "Totale afstand" and "Gelogde motoruren" must equal the sum of *only that year's* trips -- a
    trip from the other year must never leak into it. The overall, all-years totals at the very
    top of the page (before either year's own section) must always include every trip from every
    year -- unlike a year's own section, there's no reason those should ever be "frozen"."""
    trip_2025a = _trip(
        depart_time=datetime(2025, 3, 1, 9, 0), arrive_time=datetime(2025, 3, 1, 10, 0),
        distance_nm=5.0, engine_hours={0: 1.1},
    )
    trip_2025b = _trip(
        depart_time=datetime(2025, 6, 1, 9, 0), arrive_time=datetime(2025, 6, 1, 10, 0),
        distance_nm=7.0, engine_hours={0: 1.3},
    )
    trip_2026a = _trip(
        depart_time=datetime(2026, 4, 1, 9, 0), arrive_time=datetime(2026, 4, 1, 10, 0),
        distance_nm=11.0, engine_hours={0: 2.1},
    )
    trip_2026b = _trip(
        depart_time=datetime(2026, 9, 1, 9, 0), arrive_time=datetime(2026, 9, 1, 10, 0),
        distance_nm=13.0, engine_hours={0: 2.7},
    )
    out_path = tmp_path / "logbook.html"

    write_html_logbook(
        [trip_2025a, trip_2025b, trip_2026a, trip_2026b], out_path, utc_offset_hours=0,
    )

    html = out_path.read_text(encoding="utf-8")
    # years are shown most-recent-first: overall totals, then <h2>2026</h2>, then <h2>2025</h2>
    year_2026_at = html.index("<h2>2026</h2>")
    year_2025_at = html.index("<h2>2025</h2>")
    overall_section = html[:year_2026_at]
    section_2026 = html[year_2026_at:year_2025_at]
    section_2025 = html[year_2025_at:]

    # 2025: 5.0 + 7.0 = 12.0 nm distance, 1.1 + 1.3 = 2.4 h logged engine hours
    assert "12,0 nm" in section_2025
    assert "2,4 h" in section_2025
    # must not also show 2026's own totals
    assert "24,0 nm" not in section_2025
    assert "4,8 h" not in section_2025

    # 2026: 11.0 + 13.0 = 24.0 nm distance, 2.1 + 2.7 = 4.8 h logged engine hours
    assert "24,0 nm" in section_2026
    assert "4,8 h" in section_2026
    # must not also show 2025's own totals
    assert "12,0 nm" not in section_2026
    assert "2,4 h" not in section_2026

    # overall (all years combined): 12.0 + 24.0 = 36.0 nm, 2.4 + 4.8 = 7.2 h -- must include
    # every trip regardless of year, unlike either year's own section above
    assert "36,0 nm" in overall_section
    assert "7,2 h" in overall_section


def test_write_html_logbook_sequence_number_resets_each_year(tmp_path: Path):
    """Volgnummer counts trips chronologically within a year (1, 2, 3, ...), independent of the
    display order (most recent week first) -- and starts back at 1 for the next year."""
    trip_2025_a = _trip(depart_time=datetime(2025, 6, 10, 9, 0), arrive_time=datetime(2025, 6, 10, 10, 0))
    trip_2025_b = _trip(depart_time=datetime(2025, 6, 17, 9, 0), arrive_time=datetime(2025, 6, 17, 10, 0))
    trip_2026_a = _trip(depart_time=datetime(2026, 7, 15, 9, 0), arrive_time=datetime(2026, 7, 15, 10, 0))
    out_path = tmp_path / "logbook.html"

    write_html_logbook([trip_2025_a, trip_2025_b, trip_2026_a], out_path)

    html = out_path.read_text(encoding="utf-8")
    rows = re.findall(r'<tr class="trip-row"[^>]*><td>(\d+)</td>', html)
    # display order: 2026's only trip (seq 1), then 2025's b (chronologically 2nd -> seq 2, but
    # shown first within 2025 since its week is later), then 2025's a (chronologically 1st -> seq 1)
    assert rows == ["1", "2", "1"]


def test_write_html_logbook_trips_within_a_week_are_most_recent_first(tmp_path: Path):
    """Regression test: trips within a week used to show oldest-first while weeks themselves show
    newest-first, alternating direction between the two levels in a way that read as confusing
    (found in practice). Both levels now go the same direction."""
    earlier = _trip(depart_time=datetime(2026, 7, 14, 9, 0), arrive_time=datetime(2026, 7, 14, 10, 0))
    later = _trip(depart_time=datetime(2026, 7, 16, 9, 0), arrive_time=datetime(2026, 7, 16, 10, 0))
    out_path = tmp_path / "logbook.html"

    write_html_logbook([earlier, later], out_path)

    html = out_path.read_text(encoding="utf-8")
    assert html.index("2026-07-16") < html.index("2026-07-14")


def test_write_html_logbook_one_table_per_year_with_week_divider_rows(tmp_path: Path):
    """Regression test: weeks used to each get their own <table>, so columns from different
    weeks could end up different widths and not line up. All of a year's trips must now share a
    single table (so column widths are computed together), with a divider row between weeks
    instead of a separate table."""
    week_a = _trip(depart_time=datetime(2026, 7, 6, 9, 0), arrive_time=datetime(2026, 7, 6, 10, 0))
    week_b = _trip(depart_time=datetime(2026, 7, 15, 9, 0), arrive_time=datetime(2026, 7, 15, 10, 0))
    out_path = tmp_path / "logbook.html"

    write_html_logbook([week_a, week_b], out_path)

    html = out_path.read_text(encoding="utf-8")
    assert html.count('<table class="trips">') == 1
    assert html.count('class="week-row"') == 2
    week_label = 'data-i18n="week_label_prefix">Week</span>'
    assert f"{week_label} 28" in html and f"{week_label} 29" in html


def test_write_html_logbook_shows_per_year_totals(tmp_path: Path):
    trip_2025 = _trip(
        depart_time=datetime(2025, 6, 10, 9, 0), arrive_time=datetime(2025, 6, 10, 10, 0), distance_nm=7.0,
    )
    trip_2026a = _trip(
        depart_time=datetime(2026, 7, 15, 9, 0), arrive_time=datetime(2026, 7, 15, 10, 0), distance_nm=10.0,
    )
    trip_2026b = _trip(
        depart_time=datetime(2026, 8, 1, 9, 0), arrive_time=datetime(2026, 8, 1, 10, 0), distance_nm=6.0,
    )
    out_path = tmp_path / "logbook.html"

    write_html_logbook([trip_2025, trip_2026a, trip_2026b], out_path)

    html = out_path.read_text(encoding="utf-8")
    # overall total (23.0) and the 2026-only total (16.0) must both appear
    assert "23,0 nm" in html
    assert "16,0 nm" in html
    # the per-year total must land after that year's heading, not before it
    assert html.index("<h2>2026</h2>") < html.index("16,0 nm")


def test_write_html_logbook_map_button_only_with_track(tmp_path: Path):
    track = [NavSample(datetime(2026, 7, 15, 9, 0), 52.30, 4.90, 3.0, None)]
    trip_with_track = _trip(track=track)
    trip_without_track = _trip(depart_time=datetime(2026, 7, 16, 9, 0), arrive_time=datetime(2026, 7, 16, 10, 0))
    out_path = tmp_path / "logbook.html"

    write_html_logbook([trip_with_track, trip_without_track], out_path)

    html = out_path.read_text(encoding="utf-8")
    assert html.count('class="show-map"') == 1
    assert '"points": [[52.3, 4.9]]' in html.replace(" ", "").replace("\n", "") or "52.3" in html


def _track_every_10_minutes(count: int, cog_deg=200.0):
    return [
        NavSample(datetime(2026, 7, 15, 9, 0) + timedelta(minutes=10 * i), 52.30 + 0.001 * i, 4.90, 3.0, None, None, cog_deg)
        for i in range(count)
    ]


def test_write_html_logbook_embeds_log_points_for_the_map(tmp_path: Path):
    """The map shows a numbered marker per periodic log entry (see the log-marker JS in
    write_html_logbook), reusing the exact same entries as the Details popup's own log table --
    embedded here as TRIPS[idx].log so JS doesn't have to re-derive them (and risk landing on a
    different set of points than the table shows for the same trip)."""
    track = _track_every_10_minutes(10)  # 0..90 minutes, matches the periodic-log tests below
    trip = _trip(track=track)
    out_path = tmp_path / "logbook.html"

    write_html_logbook([trip], out_path, utc_offset_hours=0)

    html = out_path.read_text(encoding="utf-8")
    trips_json = json.loads(re.search(r"const TRIPS = (\{.*?\});", html).group(1))
    log_points = trips_json["0"]["log"]
    assert len(log_points) == 4  # same 09:00/09:30/10:00/10:30 split as the log table
    assert log_points[0]["time"] == "09:00"
    assert log_points[0]["cog"] == "200°"
    assert log_points[0]["sog"] == "5,8 kn"
    assert log_points[-1]["time"] == "10:30"


def test_write_html_logbook_embeds_max_speed_marker_for_the_map(tmp_path: Path):
    """A trip's single fastest moment gets its own marker position + ready-made tooltip fields
    embedded as TRIPS[idx].maxSpeed, looked up from the track by matching max_speed_at -- so JS
    doesn't have to re-derive which of the track's own points that was, and can't drift out of
    sync with the "Topsnelheid" table cell's own tooltip."""
    max_speed_time = datetime(2026, 7, 15, 9, 20)
    track = _track_every_10_minutes(10)
    trip = _trip(track=track, max_speed_kn=18.5, max_speed_at=max_speed_time, max_speed_rpm={0: 3200.0})
    out_path = tmp_path / "logbook.html"

    write_html_logbook([trip], out_path, utc_offset_hours=0)

    html = out_path.read_text(encoding="utf-8")
    trips_json = json.loads(re.search(r"const TRIPS = (\{.*?\});", html).group(1))
    max_speed = trips_json["0"]["maxSpeed"]
    matching_sample = next(s for s in track if s.time == max_speed_time)
    assert max_speed["lat"] == round(matching_sample.lat, 6)
    assert max_speed["lon"] == round(matching_sample.lon, 6)
    assert max_speed["time"] == "09:20"
    assert "18,5" in max_speed["speed"]
    assert "3200" in max_speed["rpm"]
    # a distinct marker/color from the plain numbered log points (asked for explicitly)
    assert "max-speed-marker" in html


def test_write_html_logbook_max_speed_marker_lists_rpm_per_engine(tmp_path: Path):
    max_speed_time = datetime(2026, 7, 15, 9, 20)
    track = _track_every_10_minutes(10)
    trip = _trip(
        track=track, max_speed_kn=18.5, max_speed_at=max_speed_time, max_speed_rpm={0: 3200.0, 1: 2950.0}
    )
    out_path = tmp_path / "logbook.html"

    write_html_logbook([trip], out_path, utc_offset_hours=0)

    html = out_path.read_text(encoding="utf-8")
    trips_json = json.loads(re.search(r"const TRIPS = (\{.*?\});", html).group(1))
    rpm_text = trips_json["0"]["maxSpeed"]["rpm"]
    assert "3200" in rpm_text
    assert "2950" in rpm_text


def test_write_html_logbook_no_max_speed_marker_without_max_speed_at(tmp_path: Path):
    track = _track_every_10_minutes(10)
    trip = _trip(track=track, max_speed_kn=18.5, max_speed_at=None)
    out_path = tmp_path / "logbook.html"

    write_html_logbook([trip], out_path, utc_offset_hours=0)

    html = out_path.read_text(encoding="utf-8")
    trips_json = json.loads(re.search(r"const TRIPS = (\{.*?\});", html).group(1))
    assert trips_json["0"]["maxSpeed"] is None


def _log_table_html(html: str) -> str:
    """Isolates the Details popup's own log table from the rest of the page -- the main trips
    table has its own Vertrek/Aankomst *columns* with the same time values, so a plain substring
    search over the whole page can accidentally match a cell there instead of the log table
    (found in practice: a trip's own arrival time cell in the main table)."""
    start = html.index('<table class="log-table"')
    end = html.index("</table>", start)
    return html[start:end]


def test_write_html_logbook_shows_periodic_log_entries(tmp_path: Path):
    """Default interval is 30 minutes: a 90-minute track sampled every 10 minutes should collapse
    to entries at 0/30/60/90 (start, two 30-min steps, and the trip's own end)."""
    track = _track_every_10_minutes(10)  # 0..90 minutes
    trip = _trip(track=track)
    out_path = tmp_path / "logbook.html"

    write_html_logbook([trip], out_path, utc_offset_hours=0)

    html = out_path.read_text(encoding="utf-8")
    assert 'class="show-log"' in html
    assert 'id="log-0"' in html  # dialog id must match the button's data-trip
    assert 'class="log-dialog"' in html
    assert 'class="close-log"' in html
    log_table = _log_table_html(html)
    assert log_table.count("</td><td>09:") + log_table.count("</td><td>10:") == 4
    assert "200&deg;" in log_table
    assert "5,8 kn" in log_table  # 3.0 m/s -> ~5.8 kn
    # first/last rows are labeled Vertrek/Aankomst, not numbered; the two in between are 1 and 2
    assert '<td><span data-i18n="map_marker_departure">Vertrek</span></td>' in log_table
    assert '<td><span data-i18n="map_marker_arrival">Aankomst</span></td>' in log_table
    assert "<td>1</td>" in log_table and "<td>2</td>" in log_table


def test_write_html_logbook_makes_the_details_popup_draggable(tmp_path: Path):
    """Regression test: the Details popup used to sit fixed-centered on top of an open trip map,
    with no way to move it aside (asked for explicitly)."""
    trip = _trip(track=_track_every_10_minutes(10))
    out_path = tmp_path / "logbook.html"

    write_html_logbook([trip], out_path, utc_offset_hours=0)

    html = out_path.read_text(encoding="utf-8")
    assert "function enableDialogDrag(dialog)" in html
    assert "enableDialogDrag(dialog);" in html


def test_write_html_logbook_log_interval_is_configurable(tmp_path: Path):
    track = _track_every_10_minutes(10)  # 0..90 minutes
    trip = _trip(track=track)
    out_path = tmp_path / "logbook.html"

    write_html_logbook([trip], out_path, log_interval_minutes=60, utc_offset_hours=0)

    html = out_path.read_text(encoding="utf-8")
    # every 60 minutes -> 09:00, 10:00 (>= next_due), 10:30 (trip end) = 3 rows
    log_table = _log_table_html(html)
    assert log_table.count("</td><td>09:") + log_table.count("</td><td>10:") == 3


def test_write_html_logbook_log_entries_are_clock_aligned(tmp_path: Path):
    """Regression test: entries must land on the next whole/half hour (09:30, 10:00, ...), not on
    times offset from the trip's own arbitrary departure minute (09:37, 10:07, ... if counted
    from a 09:07 departure) -- a real logbook's periodic entries read on the clock, not relative
    to whenever the boat happened to leave."""
    track = [
        NavSample(datetime(2026, 7, 15, 9, 7) + timedelta(minutes=i), 52.30, 4.90, 3.0, None, None, 90.0)
        for i in range(61)  # one point per minute, 09:07 .. 10:07
    ]
    trip = _trip(track=track, depart_time=track[0].time, arrive_time=track[-1].time)
    out_path = tmp_path / "logbook.html"

    write_html_logbook([trip], out_path, utc_offset_hours=0)

    log_table = _log_table_html(out_path.read_text(encoding="utf-8"))
    assert "</td><td>09:30</td>" in log_table
    assert "</td><td>10:00</td>" in log_table
    assert "</td><td>09:37</td>" not in log_table
    # 10:07 IS present -- but only once, as the trip's own (mandatory) end time, not as a second
    # clock-aligned entry landing exactly on the old drift-from-departure schedule
    assert log_table.count("</td><td>10:07</td>") == 1


def test_write_html_logbook_no_log_table_with_a_single_track_point(tmp_path: Path):
    track = [NavSample(datetime(2026, 7, 15, 9, 0), 52.30, 4.90, 3.0)]
    trip = _trip(track=track)
    out_path = tmp_path / "logbook.html"

    write_html_logbook([trip], out_path)

    html = out_path.read_text(encoding="utf-8")
    assert 'class="show-log"' not in html


def test_write_html_logbook_shows_weather_columns_in_the_log_when_a_weather_fetcher_is_given(
    tmp_path: Path,
):
    class _FakeWeather:
        def hour(self, lat, lon, when):
            return HourlyWeather(wind_kn=8.3, wind_deg=292, precip_mm=0.1, cloud_pct=7)

    track = _track_every_10_minutes(10)  # 0..90 minutes
    trip = _trip(track=track)
    out_path = tmp_path / "logbook.html"

    write_html_logbook([trip], out_path, utc_offset_hours=0, weather=_FakeWeather())

    log_table = _log_table_html(out_path.read_text(encoding="utf-8"))
    assert "Wind" in log_table and "Neerslag" in log_table and "Bewolking" in log_table
    assert "8,3 kn WNW (Bft 3)" in log_table  # 292 deg rounds to WNW; 8.3 kn is Beaufort force 3
    assert "0,1 mm" in log_table
    assert "7%" in log_table


def test_write_html_logbook_no_weather_columns_without_a_weather_fetcher(tmp_path: Path):
    track = _track_every_10_minutes(10)
    trip = _trip(track=track)
    out_path = tmp_path / "logbook.html"

    write_html_logbook([trip], out_path, utc_offset_hours=0)

    log_table = _log_table_html(out_path.read_text(encoding="utf-8"))
    assert "Wind" in log_table  # the column header is always shown
    # but every data cell for it is empty -- no stray "kn"/"mm"/"%" values with nothing behind them
    assert "kn WNW" not in log_table


def test_write_html_logbook_shows_marine_columns_in_the_log_when_a_marine_fetcher_is_given(
    tmp_path: Path,
):
    class _FakeMarine:
        def hour(self, lat, lon, when):
            return HourlyMarine(
                wave_height_m=0.72, wave_direction_deg=254, wave_period_s=4.7,
                current_kn=0.5, current_direction_deg=180,
            )

    track = _track_every_10_minutes(10)
    trip = _trip(track=track)
    out_path = tmp_path / "logbook.html"

    write_html_logbook([trip], out_path, utc_offset_hours=0, marine=_FakeMarine())

    log_table = _log_table_html(out_path.read_text(encoding="utf-8"))
    assert "Golven" in log_table and "Stroming" in log_table
    assert "0,7 m, 4,7 s, WZW" in log_table  # 254 deg rounds to the WZW compass point
    assert "0,5 kn Z" in log_table  # 180 deg rounds to the Z compass point


def test_write_html_logbook_no_marine_columns_without_a_marine_fetcher(tmp_path: Path):
    track = _track_every_10_minutes(10)
    trip = _trip(track=track)
    out_path = tmp_path / "logbook.html"

    write_html_logbook([trip], out_path, utc_offset_hours=0)

    log_table = _log_table_html(out_path.read_text(encoding="utf-8"))
    assert "Golven" in log_table  # the column header is always shown
    # but every data cell for it is empty -- no stray "m,"/"kn" values with nothing behind them
    assert "m, 4,7 s" not in log_table
    assert "0,5 kn Z" not in log_table


def test_write_html_logbook_shows_the_latest_trips_arrival_as_last_updated(tmp_path: Path):
    """Regression test: this used to show *generation* time (datetime.now()), which looked
    misleadingly fresh on a run that couldn't fetch any new data and just regenerated the file
    from what was already cached -- found in practice. It must instead reflect how current the
    data itself is: the most recent trip's own arrival time."""
    older = _trip(depart_time=datetime(2026, 8, 10, 9, 0), arrive_time=datetime(2026, 8, 10, 10, 0))
    newest = _trip(depart_time=datetime(2026, 8, 11, 13, 0), arrive_time=datetime(2026, 8, 11, 14, 32))
    out_path = tmp_path / "logbook.html"

    write_html_logbook([older, newest], out_path, utc_offset_hours=0)

    html = out_path.read_text(encoding="utf-8")
    assert 'data-i18n="last_updated">Laatst bijgewerkt</span>: 2026-08-11 14:32' in html


def test_write_html_logbook_latest_data_at_overrides_the_last_trips_arrival(tmp_path: Path):
    """Regression test: even the most recent *completed* trip's arrival can lag behind the
    latest raw data -- e.g. days spent anchored/idle after the last trip closed off, still
    logging System Time (PGN 126992) the whole time without ever forming a new trip. The
    caller's own latest_data_at (see cli.py's ebl_time_state) must win over trip data here."""
    trip = _trip(depart_time=datetime(2026, 8, 9, 9, 0), arrive_time=datetime(2026, 8, 9, 17, 18))
    out_path = tmp_path / "logbook.html"

    write_html_logbook(
        [trip], out_path, utc_offset_hours=0, latest_data_at=datetime(2026, 8, 16, 8, 5)
    )

    html = out_path.read_text(encoding="utf-8")
    assert (
        '<div class="last-updated"><span data-i18n="last_updated">Laatst bijgewerkt</span>: '
        "2026-08-16 08:05</div>" in html
    )




def test_write_html_logbook_shows_the_last_known_position_under_last_updated(tmp_path: Path):
    """Asked for explicitly: a trip that ends "outside the log file" (still underway when the data
    ran out, e.g. GPS lost approaching a harbor) has no arrival place name to show in the trips
    table -- this is the only place on the page that still shows *where* the boat last was.
    Deliberately built from latest_position (e.g. cli.py's own all_fixes[-1]), not from the last
    trip's own arrive_lat/arrive_lon/arrive_time -- those can lag behind the true latest fix by
    days if the boat has been sitting anchored/idle since the last trip closed (same reasoning as
    latest_data_at, see write_html_logbook's own docstring), asked for explicitly after an earlier
    version used the last trip's own arrival instead. With no geocoder given (NoGeocoder, the
    default) the position shows as plain coordinates, same fallback depart_place/arrive_place
    already use without one."""
    trip = _trip()  # present only to prove the position doesn't come from here
    out_path = tmp_path / "logbook.html"

    write_html_logbook(
        [trip], out_path, utc_offset_hours=0,
        latest_position=PositionFix(time=datetime(2026, 9, 4, 11, 37), lat=47.13877, lon=-2.36295),
    )

    html = out_path.read_text(encoding="utf-8")
    assert 'data-i18n="last_position">Laatste positie</span>: ' in html
    assert "47.1388, -2.3630" in html  # NoGeocoder's own plain "{lat:.4f}, {lon:.4f}" format
    assert "(11:37)" in html
    assert 'href="https://www.google.com/maps?q=47.13877,-2.36295"' in html


def test_write_html_logbook_geocodes_the_last_position_into_a_place_name(tmp_path: Path):
    """Asked for explicitly: shown as a place, the same way depart_place/arrive_place already are
    (via geocode.Geocoder.place_name()), not as raw coordinates -- a real geocoder plugged in must
    actually get used for this position too, not just for the trips themselves."""
    geocoder = _StubGeocoder()
    out_path = tmp_path / "logbook.html"

    write_html_logbook(
        [_trip()], out_path, utc_offset_hours=0,
        latest_position=PositionFix(time=datetime(2026, 9, 4, 11, 37), lat=47.13877, lon=-2.36295),
        geocoder=geocoder,
    )

    html = out_path.read_text(encoding="utf-8")
    assert ">Port@47.14,-2.36<" in html
    assert geocoder.calls == [(47.13877, -2.36295)]


def test_write_html_logbook_no_last_position_without_one_given(tmp_path: Path):
    out_path = tmp_path / "logbook.html"

    write_html_logbook([_trip()], out_path)

    html = out_path.read_text(encoding="utf-8")
    # "last_position" itself still appears once, inside the embedded I18N JS object (every
    # language's full translation table is always embedded, regardless) -- what must be absent is
    # the *rendered* span, since no latest_position was given at all.
    assert 'data-i18n="last_position"' not in html


def test_write_html_logbook_has_a_noscript_fallback_for_the_map_buttons(tmp_path: Path):
    """The route map is entirely JS-driven (Leaflet). Most email clients strip <script> tags from
    attachments, so a Map button silently does nothing if this file is opened from an email --
    <noscript> is the standard way to show a fallback message in exactly that situation (it
    triggers whenever the renderer doesn't execute scripts, not just when a <script> tag is
    literally missing), found in practice as "clicking Map does nothing" when emailed."""
    out_path = tmp_path / "logbook.html"

    write_html_logbook([_trip()], out_path)

    html = out_path.read_text(encoding="utf-8")
    assert "<noscript>" in html
    assert "JavaScript" in html


def test_write_html_logbook_embeds_trip_uid_as_data_attribute(tmp_path: Path):
    trip_a = _trip(depart_time=datetime(2026, 7, 15, 9, 0), arrive_time=datetime(2026, 7, 15, 10, 0))
    trip_b = _trip(depart_time=datetime(2026, 7, 16, 9, 0), arrive_time=datetime(2026, 7, 16, 10, 0))
    out_path = tmp_path / "logbook.html"

    write_html_logbook([trip_a, trip_b], out_path, trip_uids=["uid-a", "uid-b"])

    html = out_path.read_text(encoding="utf-8")
    assert 'data-uid="uid-a"' in html
    assert 'data-uid="uid-b"' in html


def test_write_html_logbook_without_trip_uids(tmp_path: Path):
    out_path = tmp_path / "logbook.html"

    write_html_logbook([_trip()], out_path)  # trip_uids omitted entirely

    html = out_path.read_text(encoding="utf-8")
    assert "data-uid" not in html


def test_write_html_logbook_remarks_enabled_by_default(tmp_path: Path):
    """The Remarks feature is on by default (a fixed, single-site API path, not something that
    needs per-machine configuration -- see _DEFAULT_REMARKS_API_URL's own docstring for why this
    changed from an opt-in ini setting: that only ever got applied on the desktop CLI, silently
    leaving every phone-built logbook without a remarks column at all)."""
    out_path = tmp_path / "logbook.html"

    write_html_logbook([_trip()], out_path, trip_uids=["uid-a"])

    html = out_path.read_text(encoding="utf-8")
    assert 'data-i18n="header_remarks"' in html  # header
    assert 'class="show-remarks" data-trip="0"' in html


def test_write_html_logbook_remarks_disabled_when_api_url_is_empty(tmp_path: Path):
    out_path = tmp_path / "logbook.html"

    write_html_logbook([_trip()], out_path, trip_uids=["uid-a"], remarks_api_url="")

    html = out_path.read_text(encoding="utf-8")
    assert 'class="show-remarks"' not in html
    assert 'data-i18n="header_remarks"' not in html  # header not shown either


def test_write_html_logbook_shows_remarks_button_when_enabled(tmp_path: Path):
    out_path = tmp_path / "logbook.html"

    write_html_logbook(
        [_trip()], out_path, trip_uids=["uid-a"], remarks_api_url="/wp-json/nmea2log/v1/remarks",
    )

    html = out_path.read_text(encoding="utf-8")
    assert "Opmerkingen" in html  # header
    assert 'class="show-remarks" data-trip="0"' in html
    assert 'id="remarks-0"' in html
    assert 'data-trip-uid="uid-a"' in html
    assert '"/wp-json/nmea2log/v1/remarks"' in html  # embedded as the JS REMARKS_API_URL const


def test_write_html_logbook_no_remarks_button_without_a_trip_uid(tmp_path: Path):
    out_path = tmp_path / "logbook.html"

    # trip_uids omitted -> no stable id to key a remark on
    write_html_logbook([_trip()], out_path, remarks_api_url="/wp-json/nmea2log/v1/remarks")

    html = out_path.read_text(encoding="utf-8")
    assert 'class="show-remarks"' not in html


def test_write_html_logbook_remarks_prefill_fetch_sends_the_wp_nonce(tmp_path: Path):
    """Regression test for a real bug: the page-load GET that pre-fills each remark's textarea
    was sent without the X-WP-Nonce header. WordPress's cookie-auth layer rejects *any* REST
    request from a logged-in session that lacks a valid nonce with 401 -- even a GET, and even
    though the endpoint's own permission check would have allowed it -- so remarks always looked
    empty on page load even though saving (whose POST did send the header) worked fine (found in
    practice: "opslaan werkt niet, na refresh van pagina is opmerking leeg")."""
    out_path = tmp_path / "logbook.html"

    write_html_logbook(
        [_trip()], out_path, trip_uids=["uid-a"], remarks_api_url="/wp-json/nmea2log/v1/remarks",
    )

    html = out_path.read_text(encoding="utf-8")
    assert "fetch(REMARKS_API_URL, {credentials: 'same-origin', headers: {'X-WP-Nonce': WP_REST_NONCE}})" in html


def test_write_html_logbook_shows_max_speed_per_trip_and_overall(tmp_path: Path):
    trip_a = _trip(
        depart_time=datetime(2026, 7, 15, 9, 0), arrive_time=datetime(2026, 7, 15, 10, 0),
        avg_speed_kn=5.0, max_speed_kn=8.2,
    )
    trip_b = _trip(
        depart_time=datetime(2026, 7, 16, 9, 0), arrive_time=datetime(2026, 7, 16, 10, 0),
        avg_speed_kn=6.0, max_speed_kn=11.6,
    )
    out_path = tmp_path / "logbook.html"

    write_html_logbook([trip_a, trip_b], out_path)

    html = out_path.read_text(encoding="utf-8")
    assert "8,2 kn" in html
    assert "11,6 kn" in html
    assert 'data-i18n="totals_top_speed">Topsnelheid</span></div>' in html


def test_write_html_logbook_shows_low_battery_warning(tmp_path: Path):
    trip = _trip(battery_health={0: BatteryHealth(avg_voltage_v=12.6, min_voltage_v=11.8)})
    out_path = tmp_path / "logbook.html"

    write_html_logbook([trip], out_path, battery_warning_voltage=12.2)

    html = out_path.read_text(encoding="utf-8")
    assert "low battery 11,8 V" in html


def test_write_html_logbook_warnings_cell_shows_a_count(tmp_path: Path):
    """The cell itself shows just a count, not the warning text (which would blow up the column
    width with several warnings) -- the actual text is in a hover tooltip, same pattern as the
    other tooltip columns."""
    trip = _trip(
        engine_health={
            0: EngineHealth(None, None, None, None, None, warnings=frozenset({"low oil pressure", "overheat"}))
        },
        battery_health={0: BatteryHealth(avg_voltage_v=12.6, min_voltage_v=11.8)},
    )
    out_path = tmp_path / "logbook.html"

    write_html_logbook([trip], out_path, battery_warning_voltage=12.2)

    html = out_path.read_text(encoding="utf-8")
    assert 'class="temp-hover warning-count">3<' in html  # 2 engine warnings + 1 battery
    assert "low oil pressure" in html
    assert "overheat" in html
    assert "low battery 11,8 V" in html


def test_write_html_logbook_warnings_tooltip_shows_when_each_one_first_triggered(tmp_path: Path):
    trip = _trip(
        engine_health={
            0: EngineHealth(
                None, None, None, None, None,
                warnings=frozenset({"low oil pressure", "overheat"}),
                warning_first_seen={
                    "low oil pressure": datetime(2026, 7, 15, 9, 12),
                    "overheat": datetime(2026, 7, 15, 9, 40),
                },
            )
        },
        battery_health={
            0: BatteryHealth(avg_voltage_v=12.6, min_voltage_v=11.8, min_voltage_at=datetime(2026, 7, 15, 9, 55))
        },
    )
    out_path = tmp_path / "logbook.html"

    write_html_logbook([trip], out_path, battery_warning_voltage=12.2)

    html = out_path.read_text(encoding="utf-8")
    # time first, no parentheses, and chronological (09:12 before 09:40 before 09:55) rather than
    # alphabetized -- "overheat" would otherwise sort before "low oil pressure"
    assert "09:12 low oil pressure, 09:40 overheat, 09:55 low battery 11,8 V" in html


def test_write_html_logbook_no_warnings_cell_without_warnings(tmp_path: Path):
    out_path = tmp_path / "logbook.html"

    write_html_logbook([_trip()], out_path)

    html = out_path.read_text(encoding="utf-8")
    assert 'class="temp-hover warning-count"' not in html


def test_write_html_logbook_warnings_shown_in_parentheses_after_engine_hours(tmp_path: Path):
    """Warnings live in the engine-hours cell now, not their own column -- asked for explicitly:
    a whole column empty for almost every trip wasn't worth the width, and this reads just as
    clearly right next to the hours it belongs to."""
    trip = _trip(
        engine_hours={0: 1.5},
        engine_health={
            0: EngineHealth(None, None, None, None, None, warnings=frozenset({"overheat"}))
        },
    )
    out_path = tmp_path / "logbook.html"

    write_html_logbook([trip], out_path)

    html = out_path.read_text(encoding="utf-8")
    assert "data-i18n=\"header_warnings\"" not in html  # no separate Alarm column
    assert '1,5 h (<span class="temp-hover warning-count">1' in html


def test_write_html_logbook_details_popup_shows_water_temperature(tmp_path: Path):
    """Water temperature moved from its own always-visible column into the shared Details popup
    (alongside motion and the periodic log) to keep the trips table narrow (found in practice:
    one column each for these blew the table width out badly)."""
    trip = _trip(avg_water_temp_c=21.8, min_water_temp_c=21.7, max_water_temp_c=21.9)
    out_path = tmp_path / "logbook.html"

    write_html_logbook([trip], out_path)

    html = out_path.read_text(encoding="utf-8")
    assert 'class="show-log"' in html  # the "Details" button
    assert "Watertemperatuur" in html
    assert "21,8°C" in html


def test_write_html_logbook_shows_speed_at_typical_rpm_tooltip(tmp_path: Path):
    trip = _trip(typical_rpm={0: 2250.0}, typical_rpm_speed_kn={0: (12.6, 13.4, 13.0, None)})
    out_path = tmp_path / "logbook.html"

    write_html_logbook([trip], out_path)

    html = out_path.read_text(encoding="utf-8")
    assert "2250" in html
    assert "gem. 13,0 kn bij dat toerental (12,6-13,4 kn)" in html


def test_write_html_logbook_shows_fuel_consumption_at_typical_rpm_tooltip(tmp_path: Path):
    trip = _trip(typical_rpm={0: 2250.0}, typical_rpm_speed_kn={0: (12.6, 13.4, 13.0, 0.84)})
    out_path = tmp_path / "logbook.html"

    write_html_logbook([trip], out_path)

    html = out_path.read_text(encoding="utf-8")
    assert "gem. 13,0 kn bij dat toerental (12,6-13,4 kn), gem. verbruik 0,84 L/nm" in html


def test_write_html_logbook_shows_max_speed_time_and_rpm_tooltip(tmp_path: Path):
    trip = _trip(
        max_speed_kn=17.5,
        max_speed_at=datetime(2026, 7, 15, 9, 25),
        max_speed_rpm={0: 3400.0},
    )
    out_path = tmp_path / "logbook.html"

    write_html_logbook([trip], out_path)

    html = out_path.read_text(encoding="utf-8")
    assert "17,5 kn" in html
    assert "om 09:25 bij 3400 rpm" in html


def test_write_html_logbook_details_popup_shows_motion(tmp_path: Path):
    """Motion (roll/pitch) moved from its own always-visible column into the shared Details
    popup, which has room to spell out which number is which directly instead of needing a
    numbers-only cell plus a hover tooltip to explain it."""
    trip = _trip(roll_variation_deg=2.5, pitch_variation_deg=0.7)
    out_path = tmp_path / "logbook.html"

    write_html_logbook([trip], out_path)

    html = out_path.read_text(encoding="utf-8")
    assert "Beweging" in html
    assert 'data-i18n="motion_roll">slingeren</span> ±2,5°' in html
    assert 'data-i18n="motion_pitch">stampen</span> ±0,7°' in html


def test_write_html_logbook_shows_motion_peak_in_the_details_popup(tmp_path: Path):
    """The standard deviation alone can look deceptively small for a trip that's mostly calm
    with one rough patch, so the peak-to-peak range shows up too, right alongside it."""
    trip = _trip(roll_variation_deg=2.5, pitch_variation_deg=0.7, roll_range_deg=23.0, pitch_range_deg=5.3)
    out_path = tmp_path / "logbook.html"

    write_html_logbook([trip], out_path)

    html = out_path.read_text(encoding="utf-8")
    peak_span = '<span data-i18n="motion_peak">piek</span>'
    assert f'data-i18n="motion_roll">slingeren</span> ±2,5° ({peak_span} 23,0°)' in html
    assert f'data-i18n="motion_pitch">stampen</span> ±0,7° ({peak_span} 5,3°)' in html


def test_write_html_logbook_water_temp_shows_range_when_notable(tmp_path: Path):
    trip = _trip(avg_water_temp_c=17.0, min_water_temp_c=15.0, max_water_temp_c=19.0)
    out_path = tmp_path / "logbook.html"

    write_html_logbook([trip], out_path)

    html = out_path.read_text(encoding="utf-8")
    assert "15,0" in html and "19,0" in html


def test_write_html_logbook_no_details_button_without_any_data(tmp_path: Path):
    """No water temperature, no motion data, and (with the default empty track) no periodic log
    either -- there's nothing for a Details popup to show, so the button shouldn't appear at
    all."""
    out_path = tmp_path / "logbook.html"

    write_html_logbook([_trip()], out_path)

    html = out_path.read_text(encoding="utf-8")
    assert 'class="show-log"' not in html
    # Not "Watertemperatuur"/"Beweging" not in html -- those words are now always present
    # regardless, embedded in the page's own I18N JS object for the language switcher (see
    # write_html_logbook) -- the actual thing being tested is that neither detail row renders.
    assert 'data-i18n="header_water_temp"' not in html
    assert 'data-i18n="header_motion"' not in html


def test_write_html_logbook_escapes_place_names(tmp_path: Path):
    trip = _trip(depart_place="Marina <A> & Co")
    out_path = tmp_path / "logbook.html"

    write_html_logbook([trip], out_path)

    html = out_path.read_text(encoding="utf-8")
    assert "Marina <A> & Co" not in html
    assert "&lt;A&gt;" in html and "&amp;" in html
