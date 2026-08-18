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

    assert call_count == 2  # Nominatim + the nearby-marina check (see _nearby_marina_name), once
    saved = json.loads(cache_file.read_text(encoding="utf-8"))
    assert saved == {"47.8387,-4.1759": "Loctudy"}


def test_place_name_prefers_a_nearby_marina_over_nominatims_own_match(monkeypatch, tmp_path):
    """Regression test for a real case: moored just outside a marina's own mapped basin, so
    Nominatim's plain reverse lookup matched an unrelated nearby feature (and used *its* address
    hierarchy, "Port au Loup") instead of the marina itself. The separate nearby-marina check
    (see _nearby_marina_name) must win over that plain match."""
    def fake_urlopen(request, timeout=10):
        if "overpass-api.de" in request.full_url:
            return _FakeResponse(
                {
                    "elements": [
                        {
                            "tags": {"name": "Port de Piriac-sur-Mer"},
                            "center": {"lat": 47.3824, "lon": -2.5440},
                        }
                    ]
                }
            )
        return _FakeResponse({"address": {"village": "Port au Loup"}})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    geocoder = Geocoder(cache_file=tmp_path / "cache.json")

    assert geocoder.place_name(47.382706, -2.544722) == "Port de Piriac-sur-Mer"


def test_place_name_falls_back_to_nominatim_when_no_marina_nearby(monkeypatch, tmp_path):
    def fake_urlopen(request, timeout=10):
        if "overpass-api.de" in request.full_url:
            return _FakeResponse({"elements": []})
        return _FakeResponse({"address": {"village": "Kerners"}})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    geocoder = Geocoder(cache_file=tmp_path / "cache.json")

    assert geocoder.place_name(47.5707, -2.8853) == "Kerners"


def test_place_name_ignores_a_failed_marina_check(monkeypatch, tmp_path):
    """A failed Overpass lookup (timeout, unreachable, ...) must not break geocoding entirely --
    just falls back to the plain Nominatim result, same as if no marina had been found."""
    def fake_urlopen(request, timeout=10):
        if "overpass-api.de" in request.full_url:
            raise urllib.error.URLError("timed out")
        return _FakeResponse({"address": {"village": "Kerners"}})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    geocoder = Geocoder(cache_file=tmp_path / "cache.json")

    assert geocoder.place_name(47.5707, -2.8853) == "Kerners"


def test_no_geocoder_returns_coordinates():
    assert NoGeocoder().place_name(52.3676, 4.9041) == "52.3676, 4.9041"


def test_place_name_no_prefix_when_match_is_close(monkeypatch, tmp_path):
    def fake_urlopen(request, timeout=10):
        # ~50 m from the query position -- well under the 250 m threshold
        return _FakeResponse({"lat": "47.57070", "lon": "-2.88536", "type": "islet", "address": {"village": "Kerners"}})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    geocoder = Geocoder(cache_file=tmp_path / "cache.json")

    assert geocoder.place_name(47.5707, -2.8853) == "Kerners"


def test_place_name_prefixed_on_the_water_when_match_is_far_and_not_a_mooring(monkeypatch, tmp_path):
    """Regression test for a real case: an anchor position in the Golfe du Morbihan whose
    nearest Nominatim match (a coastal path) was ~1.1 km away -- far enough that just showing
    the name would wrongly imply the boat was right there."""
    def fake_urlopen(request, timeout=10):
        # ~1.1 km away, and not a mooring-type feature
        return _FakeResponse(
            {"lat": "47.5700", "lon": "-2.8704", "type": "locality", "address": {"village": "Penhap"}}
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    geocoder = Geocoder(cache_file=tmp_path / "cache.json")

    assert geocoder.place_name(47.5707, -2.8853) == "op het water, bij Penhap"


def test_place_name_prefixed_alongside_when_match_is_far_and_a_mooring(monkeypatch, tmp_path):
    def fake_urlopen(request, timeout=10):
        return _FakeResponse(
            {"lat": "47.5700", "lon": "-2.8704", "type": "marina", "address": {"village": "Penhap"}}
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    geocoder = Geocoder(cache_file=tmp_path / "cache.json")

    assert geocoder.place_name(47.5707, -2.8853) == "aan de kant, bij Penhap"
