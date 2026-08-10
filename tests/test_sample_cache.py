from datetime import datetime
from pathlib import Path

from nmea2000processor.sample_cache import CACHE_FORMAT_VERSION, SampleCache
import pickle


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

    cache.put(ebl, ({"positions": [1, 2, 3]},), when)
    result = cache.get(ebl)

    assert result == (({"positions": [1, 2, 3]},), when)


def test_get_matches_regardless_of_relative_vs_absolute_path(tmp_path: Path, monkeypatch):
    """Regression test for a real bug found in practice: caching keyed on the raw path string
    missed the cache every time the same file was referred to by a relative path in one run and
    an absolute path in another (e.g. --ebl-dir Actisense vs. an absolute --ebl-dir), even though
    it's the exact same file -- see _cache_key."""
    ebl = tmp_path / "000_000.ebl"
    ebl.write_bytes(b"hello")
    cache = SampleCache(tmp_path / "cache.pkl")
    cache.put(ebl, ({"positions": [1]},), None)  # cached via the absolute path

    monkeypatch.chdir(tmp_path)
    relative_path = Path("000_000.ebl")

    assert cache.get(relative_path) == (({"positions": [1]},), None)


def test_get_is_invalidated_when_the_file_size_changes(tmp_path: Path):
    """A changed size (the log was still being written to, or was re-downloaded with different
    content) must not silently serve stale cached samples."""
    ebl = tmp_path / "000_000.ebl"
    ebl.write_bytes(b"hello")
    cache = SampleCache(tmp_path / "cache.pkl")
    cache.put(ebl, ({"positions": [1]},), None)

    ebl.write_bytes(b"hello world, now bigger")

    assert cache.get(ebl) is None


def test_cache_persists_across_instances(tmp_path: Path):
    ebl = tmp_path / "000_000.ebl"
    ebl.write_bytes(b"hello")
    cache_file = tmp_path / "cache.pkl"

    cache = SampleCache(cache_file)
    cache.put(ebl, ({"positions": [1]},), datetime(2026, 7, 15, 9, 0))
    cache.save()

    reloaded = SampleCache(cache_file)
    assert reloaded.get(ebl) == (({"positions": [1]},), datetime(2026, 7, 15, 9, 0))


def test_save_does_nothing_when_nothing_changed(tmp_path: Path):
    cache_file = tmp_path / "cache.pkl"
    cache = SampleCache(cache_file)

    cache.save()

    assert not cache_file.exists()  # never touched -- nothing was ever put()


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
