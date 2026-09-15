import pickle
import zlib
from datetime import datetime
from pathlib import Path

from nmea2log.model import AttitudeSample, EngineSample, PositionFix
from nmea2log.sample_cache import (
    CACHE_FORMAT_VERSION,
    SampleCache,
    _decode_samples,
    _decode_samples_tuple,
    _encode_samples,
    _encode_samples_tuple,
)


def _sample_tuple(lat: float = 52.30) -> tuple:
    """Shaped like the real 9-tuple _collect_samples returns (see cli.py): a mix of
    dict-by-source and flat-list parts. Only the first (fixes) part is populated -- enough to
    exercise the cache's columnar encode/decode without every test needing to spell out all 9."""
    return (
        {10: [PositionFix(datetime(2026, 7, 15, 9, 0), lat, 4.90)]},
        {},
        [],
        [],
        {},
        {},
        {},
        [],
        {},
    )


def test_get_returns_none_for_an_unknown_file(tmp_path: Path):
    ebl = tmp_path / "000_000.ebl"
    ebl.write_bytes(b"hello")
    cache = SampleCache(tmp_path / "cache.pkl")

    assert cache.get(ebl) is None


def test_put_then_get_returns_the_cached_entry(tmp_path: Path):
    ebl = tmp_path / "000_000.ebl"
    ebl.write_bytes(b"hello")
    cache = SampleCache(tmp_path / "cache.pkl")
    when = datetime(2026, 7, 15, 9, 0)

    cache.put(ebl, _sample_tuple(), when)
    result = cache.get(ebl)

    assert result == (_sample_tuple(), when)


def test_get_matches_regardless_of_relative_vs_absolute_path(tmp_path: Path, monkeypatch):
    """Regression test for a real bug found in practice: caching keyed on the raw path string
    missed the cache every time the same file was referred to by a relative path in one run and
    an absolute path in another (e.g. --ebl-dir Actisense vs. an absolute --ebl-dir), even though
    it's the exact same file -- see _cache_key."""
    ebl = tmp_path / "000_000.ebl"
    ebl.write_bytes(b"hello")
    cache = SampleCache(tmp_path / "cache.pkl")
    cache.put(ebl, _sample_tuple(), None)  # cached via the absolute path

    monkeypatch.chdir(tmp_path)
    relative_path = Path("000_000.ebl")

    assert cache.get(relative_path) == (_sample_tuple(), None)


def test_get_is_invalidated_when_the_file_size_changes(tmp_path: Path):
    """A changed size (the log was still being written to, or was re-downloaded with different
    content) must not silently serve stale cached samples."""
    ebl = tmp_path / "000_000.ebl"
    ebl.write_bytes(b"hello")
    cache = SampleCache(tmp_path / "cache.pkl")
    cache.put(ebl, _sample_tuple(), None)

    ebl.write_bytes(b"hello world, now bigger")

    assert cache.get(ebl) is None


def test_cache_persists_across_instances(tmp_path: Path):
    """put() is durable immediately -- no separate save() step needed (see module docstring: a
    process killed mid-run must not lose already-decoded files)."""
    ebl = tmp_path / "000_000.ebl"
    ebl.write_bytes(b"hello")
    cache_file = tmp_path / "cache.pkl"

    cache = SampleCache(cache_file)
    cache.put(ebl, _sample_tuple(), datetime(2026, 7, 15, 9, 0))

    reloaded = SampleCache(cache_file)
    assert reloaded.get(ebl) == (_sample_tuple(), datetime(2026, 7, 15, 9, 0))


def test_cache_dir_is_empty_when_nothing_was_ever_put(tmp_path: Path):
    cache_dir = tmp_path / "cache.pkl"

    SampleCache(cache_dir)

    assert list(cache_dir.iterdir()) == []


def test_a_cache_from_a_different_format_version_is_ignored(tmp_path: Path):
    """Regression guard: a cache written by older/newer code (different decoded-sample shape)
    must never be silently reused -- it's discarded and rebuilt from scratch instead."""
    ebl = tmp_path / "000_000.ebl"
    ebl.write_bytes(b"hello")
    cache_file = tmp_path / "cache.pkl"
    cache_file.write_bytes(
        pickle.dumps({"version": CACHE_FORMAT_VERSION + 1, "files": {str(ebl): {"size": 5, "samples": ("stale",)}}})
    )

    cache = SampleCache(cache_file)

    assert cache.get(ebl) is None


def test_a_corrupt_cache_file_is_treated_as_empty_instead_of_crashing(tmp_path: Path):
    ebl = tmp_path / "000_000.ebl"
    ebl.write_bytes(b"hello")
    cache_file = tmp_path / "cache.pkl"
    cache_file.write_bytes(b"not a valid pickle stream")

    cache = SampleCache(cache_file)

    assert cache.get(ebl) is None


def test_a_legacy_single_file_cache_is_migrated_with_its_entries_preserved(tmp_path: Path):
    """Regression guard for the one-time upgrade from the old single-big-pickle format to the
    current one-file-per-source-file directory (see sample_cache.py's module docstring, written
    to fix a real phone getting OOM-killed mid-run because that old format had to hold the whole
    cache in memory at once) -- an existing cache built by older code must not be silently thrown
    away on first use by newer code."""
    ebl = tmp_path / "000_000.ebl"
    ebl.write_bytes(b"hello")
    cache_path = tmp_path / "cache.pkl"
    legacy_payload = {
        "version": CACHE_FORMAT_VERSION,
        "files": {
            str(ebl.resolve()): {
                "size": ebl.stat().st_size,
                "samples": _encode_samples_tuple(_sample_tuple()),
                "time_state_after": datetime(2026, 7, 15, 9, 0),
            }
        },
    }
    cache_path.write_bytes(zlib.compress(pickle.dumps(legacy_payload)))

    cache = SampleCache(cache_path)

    assert cache_path.is_dir()  # the plain legacy file became the new directory layout
    assert cache.get(ebl) == (_sample_tuple(), datetime(2026, 7, 15, 9, 0))


def test_a_legacy_cache_from_a_different_format_version_is_migrated_as_empty(tmp_path: Path):
    ebl = tmp_path / "000_000.ebl"
    ebl.write_bytes(b"hello")
    cache_path = tmp_path / "cache.pkl"
    legacy_payload = {
        "version": CACHE_FORMAT_VERSION + 1,
        "files": {str(ebl.resolve()): {"size": ebl.stat().st_size, "samples": ("stale",)}},
    }
    cache_path.write_bytes(zlib.compress(pickle.dumps(legacy_payload)))

    cache = SampleCache(cache_path)

    assert cache.get(ebl) is None


def test_encode_decode_samples_round_trips_a_list_preserving_order():
    """Regression test for the columnar cache format: encoding to parallel value-tuples and
    decoding back must reproduce the exact same objects in the exact same order, not just the
    same set -- a swapped field order would silently corrupt every sample."""
    samples = [
        AttitudeSample(datetime(2026, 7, 15, 9, 0), 1.0, -2.0),
        AttitudeSample(datetime(2026, 7, 15, 9, 1), None, 3.5),
    ]

    assert _decode_samples(_encode_samples(samples)) == samples


def test_encode_samples_empty_list_is_none():
    assert _encode_samples([]) is None
    assert _decode_samples(None) == []


def test_encode_decode_samples_preserves_a_dataclass_with_defaulted_fields():
    """EngineSample has several Optional fields with defaults (oil pressure, warnings, ...) --
    the columnar encoding stores every field explicitly, so decoding must not fall back to the
    class's own defaults instead of the sample's actual (possibly different) values."""
    sample = EngineSample(
        datetime(2026, 7, 15, 9, 0), 0, 8.0, 3600, warnings=frozenset({"low_oil_pressure"})
    )

    assert _decode_samples(_encode_samples([sample])) == [sample]


def test_encode_decode_samples_tuple_round_trips_dict_and_list_parts():
    """The full 9-part samples tuple from _collect_samples mixes dict-by-source parts (fixes,
    attitude, ...) and flat-list parts (engine, rpm, ...) -- both must round-trip correctly."""
    fixes_by_source = {10: [PositionFix(datetime(2026, 7, 15, 9, 0), 52.30, 4.90)]}
    engine_samples = [EngineSample(datetime(2026, 7, 15, 9, 0), 0, 8.0, 3600)]
    samples = (fixes_by_source, {}, engine_samples, [], {}, {}, {}, [], {})

    assert _decode_samples_tuple(_encode_samples_tuple(samples)) == samples
