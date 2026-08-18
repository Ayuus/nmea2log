import http.client
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


def test_remote_disconnected_is_treated_as_a_failed_lookup_not_a_crash(monkeypatch, tmp_path):
    """Regression test for a real crash: the Nominatim server dropping the connection mid-response
    raises http.client.RemoteDisconnected, which -- despite being a connection failure like any
    other -- isn't a urllib.error.URLError (it's an OSError/ConnectionResetError instead), so it
    wasn't caught and crashed the whole run instead of being treated as a normal, non-cacheable
    lookup failure."""
    def fake_urlopen(request, timeout=10):
        raise http.client.RemoteDisconnected("Remote end closed connection without response")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr("nmea2000processor.geocode.time.sleep", lambda s: None)
    geocoder = Geocoder(cache_file=tmp_path / "cache.json")

    name = geocoder.place_name(47.4889, -3.1012)

    assert "geocoding failed" in name


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

    assert call_count == 2  # Nominatim + the nearby-landmark check (see _nearby_landmark_name), once
    saved = json.loads(cache_file.read_text(encoding="utf-8"))
    assert saved == {"47.8387,-4.1759": "Loctudy"}


def test_place_name_prefers_a_nearby_marina_over_nominatims_own_match(monkeypatch, tmp_path):
    """Regression test for a real case: moored just outside a marina's own mapped basin, so
    Nominatim's plain reverse lookup matched an unrelated nearby feature (and used *its* address
    hierarchy, "Port au Loup") instead of the marina itself. The separate nearby-landmark check
    (see _nearby_landmark_name) must win over that plain match."""
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


def test_place_name_prefers_a_nearby_islet_over_nominatims_own_match(monkeypatch, tmp_path):
    """Regression test for a real case: anchored a couple hundred meters off a named islet, so
    Nominatim's plain reverse lookup matched an unrelated nearby pier (and used *its* address
    hierarchy, a real but different nearby hamlet) instead of the islet itself."""
    def fake_urlopen(request, timeout=10):
        if "overpass-api.de" in request.full_url:
            return _FakeResponse(
                {
                    "elements": [
                        {
                            "tags": {"name": "Île de la Jument"},
                            "center": {"lat": 47.5686912, "lon": -2.8875040},
                        }
                    ]
                }
            )
        return _FakeResponse({"address": {"hamlet": "Le Graniol", "village": "Kerners"}})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    geocoder = Geocoder(cache_file=tmp_path / "cache.json")

    assert geocoder.place_name(47.5706676, -2.8852789) == "Île de la Jument"


def test_place_name_falls_back_to_nominatim_when_no_landmark_nearby(monkeypatch, tmp_path):
    def fake_urlopen(request, timeout=10):
        if "overpass-api.de" in request.full_url:
            return _FakeResponse({"elements": []})
        return _FakeResponse({"address": {"village": "Kerners"}})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    geocoder = Geocoder(cache_file=tmp_path / "cache.json")

    assert geocoder.place_name(47.5707, -2.8853) == "Kerners"


def test_place_name_ignores_a_failed_landmark_check(monkeypatch, tmp_path):
    """A failed Overpass lookup (timeout, unreachable, ...) must not break geocoding entirely --
    just falls back to the plain Nominatim result, same as if no landmark had been found."""
    def fake_urlopen(request, timeout=10):
        if "overpass-api.de" in request.full_url:
            raise urllib.error.URLError("timed out")
        return _FakeResponse({"address": {"village": "Kerners"}})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr("nmea2000processor.geocode.time.sleep", lambda s: None)
    geocoder = Geocoder(cache_file=tmp_path / "cache.json")

    assert geocoder.place_name(47.5707, -2.8853) == "Kerners"


def test_place_name_ignores_a_remote_disconnected_landmark_check(monkeypatch, tmp_path):
    """Regression test for a real crash: the Overpass server dropping the connection mid-response
    raises http.client.RemoteDisconnected -- an OSError/ConnectionResetError, not a
    urllib.error.URLError -- which crashed the whole run instead of falling back to Nominatim's
    plain result, same as any other failed landmark check."""
    def fake_urlopen(request, timeout=10):
        if "overpass-api.de" in request.full_url:
            raise http.client.RemoteDisconnected("Remote end closed connection without response")
        return _FakeResponse({"address": {"village": "Kerners"}})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr("nmea2000processor.geocode.time.sleep", lambda s: None)
    geocoder = Geocoder(cache_file=tmp_path / "cache.json")

    assert geocoder.place_name(47.5707, -2.8853) == "Kerners"


def test_place_name_retries_the_landmark_check_after_a_transient_failure(monkeypatch, tmp_path):
    """Regression test for a real case: the free public Overpass instance answered a plain
    around-query with a 504 under load once, then succeeded under a second (< 1 s later) --
    a single failure must not give up and fall back to Nominatim's own, less specific match."""
    overpass_call_count = 0

    def fake_urlopen(request, timeout=10):
        nonlocal overpass_call_count
        if "overpass-api.de" in request.full_url:
            overpass_call_count += 1
            if overpass_call_count == 1:
                raise urllib.error.HTTPError(request.full_url, 504, "Gateway Timeout", None, None)
            return _FakeResponse(
                {"elements": [{"tags": {"name": "Île de la Jument"}, "center": {"lat": 47.5687, "lon": -2.8875}}]}
            )
        return _FakeResponse({"address": {"village": "Kerners"}})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr("nmea2000processor.geocode.time.sleep", lambda s: None)
    geocoder = Geocoder(cache_file=tmp_path / "cache.json")

    assert geocoder.place_name(47.5706676, -2.8852789) == "Île de la Jument"
    assert overpass_call_count == 2


def test_landmark_check_retries_ten_times_before_giving_up(monkeypatch, tmp_path):
    overpass_call_count = 0

    def fake_urlopen(request, timeout=10):
        nonlocal overpass_call_count
        if "overpass-api.de" in request.full_url:
            overpass_call_count += 1
            raise urllib.error.URLError("timed out")
        return _FakeResponse({"address": {"village": "Kerners"}})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr("nmea2000processor.geocode.time.sleep", lambda s: None)
    geocoder = Geocoder(cache_file=tmp_path / "cache.json")

    geocoder.place_name(47.5707, -2.8853)

    assert overpass_call_count == 11  # the first attempt plus 10 retries


def test_landmark_check_logs_each_failed_attempt(monkeypatch, tmp_path, capsys):
    def fake_urlopen(request, timeout=10):
        if "overpass-api.de" in request.full_url:
            raise urllib.error.URLError("timed out")
        return _FakeResponse({"address": {"village": "Kerners"}})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr("nmea2000processor.geocode.time.sleep", lambda s: None)
    geocoder = Geocoder(cache_file=tmp_path / "cache.json")

    geocoder.place_name(47.5707, -2.8853)

    err = capsys.readouterr().err
    assert err.count("Overpass landmark check failed") == 11


def test_result_is_not_cached_when_the_landmark_check_fails_entirely(monkeypatch, tmp_path):
    """Regression test: a failed landmark check (not just "found nothing") must not be cached --
    otherwise this run's plain Nominatim fallback, possibly the wrong name (that's the whole
    reason the landmark check exists), would get permanently stuck in the cache even once
    Overpass is reachable again on a later run. Mirrors test_failed_lookup_is_not_cached for
    Nominatim's own request."""
    def fake_urlopen(request, timeout=10):
        if "overpass-api.de" in request.full_url:
            raise urllib.error.URLError("timed out")
        return _FakeResponse({"address": {"village": "Kerners"}})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr("nmea2000processor.geocode.time.sleep", lambda s: None)
    cache_file = tmp_path / "cache.json"
    geocoder = Geocoder(cache_file=cache_file)

    name = geocoder.place_name(47.5707, -2.8853)

    assert name == "Kerners"  # still usable for this run
    assert not cache_file.exists()  # but never written to the cache
    assert geocoder._cache == {}


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
