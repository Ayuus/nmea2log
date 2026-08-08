import json
import urllib.error
import urllib.request

from nmea2000processor.geocode import Geocoder, NoGeocoder


class _FakeResponse:
    def __init__(self, payload: dict):
        self._data = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self._data


def test_place_name_prefers_village_over_quarter(monkeypatch, tmp_path):
    def fake_urlopen(request, timeout=10):
        return _FakeResponse({"address": {"village": "Port-Louis", "quarter": "Le Driasker"}})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    geocoder = Geocoder(cache_file=tmp_path / "cache.json")

    assert geocoder.place_name(47.7108, -3.3551) == "Port-Louis"


def test_failed_lookup_is_not_cached(monkeypatch, tmp_path):
    """Regression test for a real bug found in practice: a transient network failure (no
    internet on the boat, DNS lookup failing) got permanently written to the cache file, so even
    a later run with a working connection kept returning the same stale "geocoding failed"
    message forever instead of retrying."""
    call_count = 0

    def fake_urlopen(request, timeout=10):
        nonlocal call_count
        call_count += 1
        raise urllib.error.URLError("getaddrinfo failed")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr("nmea2000processor.geocode.time.sleep", lambda s: None)
    cache_file = tmp_path / "cache.json"
    geocoder = Geocoder(cache_file=cache_file)

    name = geocoder.place_name(47.4889, -3.1012)

    assert "geocoding failed" in name
    assert not cache_file.exists()  # nothing was ever written -- there's nothing to cache
    assert geocoder._cache == {}

    # a later lookup (e.g. once back online) must retry, not just keep returning the failure
    geocoder.place_name(47.4889, -3.1012)
    assert call_count == 2


def test_successful_lookup_is_cached_and_not_looked_up_again(monkeypatch, tmp_path):
    call_count = 0

    def fake_urlopen(request, timeout=10):
        nonlocal call_count
        call_count += 1
        return _FakeResponse({"address": {"village": "Loctudy"}})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr("nmea2000processor.geocode.time.sleep", lambda s: None)
    cache_file = tmp_path / "cache.json"
    geocoder = Geocoder(cache_file=cache_file)

    assert geocoder.place_name(47.8387, -4.1759) == "Loctudy"
    assert geocoder.place_name(47.8387, -4.1759) == "Loctudy"

    assert call_count == 1
    saved = json.loads(cache_file.read_text(encoding="utf-8"))
    assert saved == {"47.8387,-4.1759": "Loctudy"}


def test_no_geocoder_returns_coordinates():
    assert NoGeocoder().place_name(52.3676, 4.9041) == "52.3676, 4.9041"
