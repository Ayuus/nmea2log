from datetime import datetime

from nmea2000processor.cli import (
    _dominant_source_only,
    _merge_by_source,
    _select_primary_gps_source,
    build_arg_parser,
)
from nmea2000processor.model import PositionFix, SogSample


def test_dominant_source_only_picks_largest_group():
    by_source = {
        10: [PositionFix(datetime(2026, 7, 15, 9, 0), 52.30, 4.90)] * 5,
        11: [PositionFix(datetime(2026, 7, 15, 9, 0), 52.31, 4.91)] * 3,
    }

    result = _dominant_source_only(by_source)

    assert result == by_source[10]


def test_dominant_source_only_empty():
    assert _dominant_source_only({}) == []


def test_merge_by_source_combines_across_calls():
    target: dict = {}
    _merge_by_source(target, {10: [1, 2], 11: [3]})
    _merge_by_source(target, {10: [4], 12: [5]})

    assert target == {10: [1, 2, 4], 11: [3], 12: [5]}


def test_dominant_source_only_after_merge_reflects_full_session():
    """Regressietest voor een echte bug: bij meerdere bestanden moet de dominante bron over de
    hele sessie bepaald worden, niet per bestand -- anders kan een minderheids-GPS-bron per
    toeval 'winnen' in een van de bestanden en zo toch valse sprongen veroorzaken."""
    target: dict = {}
    # bestand 1: bron 10 licht in de minderheid
    _merge_by_source(target, {10: [1, 2], 11: [1, 2, 3]})
    # bestand 2: bron 10 juist ruim in de meerderheid -> over de hele sessie wint bron 10
    _merge_by_source(target, {10: [3, 4, 5, 6, 7], 11: [4]})

    result = _dominant_source_only(target)

    assert result == [1, 2, 3, 4, 5, 6, 7]


def _fixes(n: int) -> list:
    return [PositionFix(datetime(2026, 7, 15, 9, 0), 52.30, 4.90)] * n


def _sogs(n: int) -> list:
    return [SogSample(datetime(2026, 7, 15, 9, 0), 3.0)] * n


def test_select_primary_gps_source_uses_sog_from_same_source():
    """Positie en snelheid moeten van dezelfde fysieke bron komen als die bron ze allebei
    stuurt -- ook als een andere bron toevallig meer snelheidsberichten stuurde."""
    fixes_by_source = {10: _fixes(100), 11: _fixes(90)}
    sogs_by_source = {10: _sogs(5), 11: _sogs(50)}  # bron 11 stuurt veruit de meeste SOG

    fixes, sogs, primary = _select_primary_gps_source(fixes_by_source, sogs_by_source)

    assert primary == 10  # bron 10 heeft de meeste positieberichten, dus is leidend
    assert fixes == fixes_by_source[10]
    assert sogs == sogs_by_source[10]  # niet bron 11, ook al stuurde die meer SOG


def test_select_primary_gps_source_falls_back_when_primary_has_no_sog():
    """Als de gekozen positiebron zelf geen snelheid stuurt, valt de code terug op de
    snelheidsbron met de meeste berichten (een ander fysiek apparaat, maar beter dan niets)."""
    fixes_by_source = {10: _fixes(100)}  # bron 10 stuurt geen SOG
    sogs_by_source = {14: _sogs(20)}

    fixes, sogs, primary = _select_primary_gps_source(fixes_by_source, sogs_by_source)

    assert primary == 10
    assert sogs == sogs_by_source[14]


def test_select_primary_gps_source_no_fixes():
    fixes, sogs, primary = _select_primary_gps_source({}, {10: _sogs(3)})

    assert fixes == []
    assert sogs == _sogs(3)
    assert primary is None


def test_config_file_sets_argparse_defaults(tmp_path, monkeypatch):
    config_path = tmp_path / "nmea2log.ini"
    config_path.write_text(
        "[nmea2log]\nmin_trip_distance_nm = 0.3\nutc_offset = 2\nno_geocode = true\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    args = build_arg_parser().parse_args([])

    assert args.min_trip_distance_nm == 0.3
    assert args.utc_offset == 2.0
    assert args.no_geocode is True


def test_config_file_default_is_overridden_by_explicit_cli_arg(tmp_path, monkeypatch):
    config_path = tmp_path / "nmea2log.ini"
    config_path.write_text("[nmea2log]\nmin_trip_distance_nm = 0.3\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    args = build_arg_parser().parse_args(["--min-trip-distance-nm", "0.9"])

    assert args.min_trip_distance_nm == 0.9
