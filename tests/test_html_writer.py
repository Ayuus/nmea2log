import re
from datetime import datetime, timedelta
from pathlib import Path

from nmea2000processor.html_writer import write_html_logbook
from nmea2000processor.tripbuilder import BatteryHealth, EngineHealth, NavSample, TripLeg


def _trip(**overrides) -> TripLeg:
    defaults = dict(
        depart_time=datetime(2026, 7, 15, 9, 0),
        arrive_time=datetime(2026, 7, 15, 10, 30),
        depart_place="Marina A",
        arrive_place="Marina B",
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


def test_write_html_logbook_shows_mmsi_and_call_sign(tmp_path: Path):
    out_path = tmp_path / "logbook.html"

    write_html_logbook([_trip()], out_path, mmsi="244003579", call_sign="PI 3201")

    html = out_path.read_text(encoding="utf-8")
    assert "MMSI: 244003579" in html
    assert "Roepnaam: PI 3201" in html


def test_write_html_logbook_omits_vessel_info_when_not_given(tmp_path: Path):
    out_path = tmp_path / "logbook.html"

    write_html_logbook([_trip()], out_path)

    html = out_path.read_text(encoding="utf-8")
    assert "MMSI" not in html
    assert "Roepnaam" not in html


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
    assert "Totale uren" in html
    assert "4,0 h" in html  # 2h + 2h
    assert "Gem. snelheid" in html
    assert "4,0 kn" in html  # 16 nm / 4 h, distance-weighted


def test_write_html_logbook_totals_omit_engine_label_with_one_engine(tmp_path: Path):
    trip = _trip(engine_hours={0: 1.5})
    out_path = tmp_path / "logbook.html"

    write_html_logbook([trip], out_path)

    html = out_path.read_text(encoding="utf-8")
    assert "Gelogde motoruren</div>" in html
    assert "engine 0" not in html.lower()


def test_write_html_logbook_engine_hour_meter_and_logged_hours_have_explanatory_tooltips(tmp_path: Path):
    """Regression test for a real point of confusion: "Motoruren-teller" is the engine's own
    lifetime hour meter reading (as of the most recent trip), not a total over the logged period
    -- so it can be much larger than "Totale uren" or "Gelogde motoruren" right next to it, which
    only cover this logbook's own trips (found in practice: read as if the numbers didn't add
    up, without a tooltip explaining that distinction)."""
    trip = _trip(engine_hours={0: 1.5}, engine_hours_total={0: 500.0})
    out_path = tmp_path / "logbook.html"

    write_html_logbook([trip], out_path)

    html = out_path.read_text(encoding="utf-8")
    assert 'title="Actuele stand van de motoruren-teller' in html
    assert 'title="Som van de motoruren tijdens de gelogde reizen' in html


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
    assert "Motoruren-teller</div>" in html
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
    assert "Week 28" in html and "Week 29" in html


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
    assert html.count("<tr><td>09:") + html.count("<tr><td>10:") == 4
    assert "200&deg;" in html
    assert "5,8 kn" in html  # 3.0 m/s -> ~5.8 kn


def test_write_html_logbook_log_interval_is_configurable(tmp_path: Path):
    track = _track_every_10_minutes(10)  # 0..90 minutes
    trip = _trip(track=track)
    out_path = tmp_path / "logbook.html"

    write_html_logbook([trip], out_path, log_interval_minutes=60, utc_offset_hours=0)

    html = out_path.read_text(encoding="utf-8")
    # every 60 minutes -> 09:00, 10:00 (>= next_due), 10:30 (trip end) = 3 rows
    assert html.count("<tr><td>09:") + html.count("<tr><td>10:") == 3


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

    html = out_path.read_text(encoding="utf-8")
    assert "<tr><td>09:30</td>" in html
    assert "<tr><td>10:00</td>" in html
    assert "<tr><td>09:37</td>" not in html
    # 10:07 IS present -- but only once, as the trip's own (mandatory) end time, not as a second
    # clock-aligned entry landing exactly on the old drift-from-departure schedule
    assert html.count("<tr><td>10:07</td>") == 1


def test_write_html_logbook_no_log_table_with_a_single_track_point(tmp_path: Path):
    track = [NavSample(datetime(2026, 7, 15, 9, 0), 52.30, 4.90, 3.0)]
    trip = _trip(track=track)
    out_path = tmp_path / "logbook.html"

    write_html_logbook([trip], out_path)

    html = out_path.read_text(encoding="utf-8")
    assert 'class="show-log"' not in html


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
    assert "Laatst bijgewerkt: 2026-08-11 14:32" in html


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
    assert '<div class="last-updated">Laatst bijgewerkt: 2026-08-16 08:05</div>' in html


def test_write_html_logbook_marks_last_updated_red_when_fetch_failed(tmp_path: Path):
    out_path = tmp_path / "logbook.html"

    write_html_logbook([_trip()], out_path, utc_offset_hours=0, fetch_failed=True)

    html = out_path.read_text(encoding="utf-8")
    assert 'class="last-updated fetch-failed"' in html


def test_write_html_logbook_last_updated_not_red_by_default(tmp_path: Path):
    out_path = tmp_path / "logbook.html"

    write_html_logbook([_trip()], out_path, utc_offset_hours=0)

    html = out_path.read_text(encoding="utf-8")
    assert 'class="last-updated"' in html
    assert 'class="last-updated fetch-failed"' not in html


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


def test_write_html_logbook_remarks_disabled_by_default(tmp_path: Path):
    out_path = tmp_path / "logbook.html"

    write_html_logbook([_trip()], out_path, trip_uids=["uid-a"])

    html = out_path.read_text(encoding="utf-8")
    assert 'class="show-remarks"' not in html
    assert "<th>Opmerkingen</th>" not in html  # header not shown either


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
    assert "Topsnelheid</div>" in html


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


def test_write_html_logbook_no_warnings_cell_without_warnings(tmp_path: Path):
    out_path = tmp_path / "logbook.html"

    write_html_logbook([_trip()], out_path)

    html = out_path.read_text(encoding="utf-8")
    assert 'class="temp-hover warning-count"' not in html


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
    trip = _trip(typical_rpm={0: 2250.0}, typical_rpm_speed_kn={0: (12.6, 13.4, 13.0)})
    out_path = tmp_path / "logbook.html"

    write_html_logbook([trip], out_path)

    html = out_path.read_text(encoding="utf-8")
    assert "2250" in html
    assert "gem. 13,0 kn bij dat toerental (12,6-13,4 kn)" in html


def test_write_html_logbook_details_popup_shows_motion(tmp_path: Path):
    """Motion (roll/pitch) moved from its own always-visible column into the shared Details
    popup, which has room to spell out which number is which directly instead of needing a
    numbers-only cell plus a hover tooltip to explain it."""
    trip = _trip(roll_variation_deg=2.5, pitch_variation_deg=0.7)
    out_path = tmp_path / "logbook.html"

    write_html_logbook([trip], out_path)

    html = out_path.read_text(encoding="utf-8")
    assert "Beweging" in html
    assert "slingeren ±2,5°" in html
    assert "stampen ±0,7°" in html


def test_write_html_logbook_shows_motion_peak_in_the_details_popup(tmp_path: Path):
    """The standard deviation alone can look deceptively small for a trip that's mostly calm
    with one rough patch, so the peak-to-peak range shows up too, right alongside it."""
    trip = _trip(roll_variation_deg=2.5, pitch_variation_deg=0.7, roll_range_deg=23.0, pitch_range_deg=5.3)
    out_path = tmp_path / "logbook.html"

    write_html_logbook([trip], out_path)

    html = out_path.read_text(encoding="utf-8")
    assert "slingeren ±2,5° (piek 23,0°)" in html
    assert "stampen ±0,7° (piek 5,3°)" in html


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
    assert "Watertemperatuur" not in html
    assert "Beweging" not in html


def test_write_html_logbook_escapes_place_names(tmp_path: Path):
    trip = _trip(depart_place="Marina <A> & Co")
    out_path = tmp_path / "logbook.html"

    write_html_logbook([trip], out_path)

    html = out_path.read_text(encoding="utf-8")
    assert "Marina <A> & Co" not in html
    assert "&lt;A&gt;" in html and "&amp;" in html
