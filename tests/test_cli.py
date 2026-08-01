from datetime import datetime

from nmea2000processor.cli import _dominant_source_only, _merge_by_source
from nmea2000processor.model import PositionFix


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
