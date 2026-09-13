import http.client
import json
import urllib.error
import urllib.request

from nmea2000processor.geocode import Geocoder, NoGeocoder


class _FakeResponse:
    def __init__(self, payload: dict, status: int = 200):
        self._data = json.dumps(payload).encode("utf-8")
        self.status = status

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


def test_place_name_omits_accept_language_by_default(monkeypatch, tmp_path):
    """Default language="" means "each place's own native/local name", which for Nominatim means
    not sending accept-language at all (see Geocoder's own doc comment) -- not sending it as an
    empty string, which Nominatim could plausibly treat differently than omitting it outright."""
    captured_urls = []

    def fake_urlopen(request, timeout=10):
        captured_urls.append(request.full_url)
        return _FakeResponse({"address": {"village": "Port-Louis"}})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    geocoder = Geocoder(cache_file=tmp_path / "cache.json")

    geocoder.place_name(47.7108, -3.3551)

    nominatim_calls = [u for u in captured_urls if "nominatim.openstreetmap.org" in u]
    assert nominatim_calls  # sanity: the reverse lookup actually happened
    assert "accept-language" not in nominatim_calls[0]


def test_place_name_sends_accept_language_when_a_language_is_given(monkeypatch, tmp_path):
    captured_urls = []

    def fake_urlopen(request, timeout=10):
        captured_urls.append(request.full_url)
        return _FakeResponse({"address": {"village": "Port-Louis"}})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    geocoder = Geocoder(cache_file=tmp_path / "cache.json", language="fr")

    geocoder.place_name(47.7108, -3.3551)

    nominatim_calls = [u for u in captured_urls if "nominatim.openstreetmap.org" in u]
    assert nominatim_calls
    assert "accept-language=fr" in nominatim_calls[0]


def test_place_name_prefers_a_real_village_over_an_obscure_leisure_match(monkeypatch, tmp_path):
    """Regression test for a real case: Nominatim's own reverse lookup matched a marina it itself
    scores as obscure (importance 0.0000555, four orders of magnitude below a well-known match,
    see the next test) -- "Darse de Castéro", a single named quay -- instead of the actual harbour
    village right there, "Port Haliguen", which was also present in the address."""
    def fake_urlopen(request, timeout=10):
        if "overpass-api.de" in request.full_url:
            return _FakeResponse({"elements": []})
        return _FakeResponse(
            {
                "lat": "47.488900",
                "lon": "-3.101200",
                "category": "leisure",
                "type": "marina",
                "importance": 0.0000555,
                "name": "Darse de Castéro",
                "address": {"leisure": "Darse de Castéro", "village": "Port Haliguen"},
            }
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    geocoder = Geocoder(cache_file=tmp_path / "cache.json")

    assert geocoder.place_name(47.4889, -3.1012) == "Port Haliguen"


def test_place_name_prefers_a_real_village_over_an_obscure_leisure_match_even_as_a_mapped_area(
    monkeypatch, tmp_path
):
    """Regression test for a real case: "Port de Plaisance de Pornichet" is a real mapped area
    (a way, not a bare point) yet still scores as obscure (importance 0.000059) -- being an
    actually mapped area on its own doesn't make a match well-known, so the village name
    ("Pornichet") is preferred here too, the same as for a bare-point obscure match."""
    def fake_urlopen(request, timeout=10):
        if "overpass-api.de" in request.full_url:
            return _FakeResponse({"elements": []})
        return _FakeResponse(
            {
                "lat": "47.257900",
                "lon": "-2.350700",
                "category": "leisure",
                "type": "marina",
                "importance": 0.000059,
                "name": "Port de Plaisance de Pornichet",
                "address": {"leisure": "Port de Plaisance de Pornichet", "town": "Pornichet"},
            }
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    geocoder = Geocoder(cache_file=tmp_path / "cache.json")

    assert geocoder.place_name(47.2579, -2.3507) == "Pornichet"


def test_place_name_keeps_a_well_known_marinas_own_name(monkeypatch, tmp_path):
    """A leisure match Nominatim itself scores as well-known (importance far above the obscure
    threshold) keeps its own name even when a village is also present in the address. Real case:
    Port Olona (0.173) and Port du Crouesty (0.186) both score three orders of magnitude above
    the obscure matches above and must stay unaffected."""
    def fake_urlopen(request, timeout=10):
        if "overpass-api.de" in request.full_url:
            return _FakeResponse({"elements": []})
        return _FakeResponse(
            {
                "lat": "47.544600",
                "lon": "-2.894200",
                "category": "leisure",
                "type": "marina",
                "importance": 0.186,
                "name": "Port du Crouesty",
                "address": {"leisure": "Port du Crouesty", "village": "Kerners"},
            }
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    geocoder = Geocoder(cache_file=tmp_path / "cache.json")

    assert geocoder.place_name(47.5446, -2.8942) == "Port du Crouesty"


def test_place_name_keeps_an_obscure_leisure_matchs_own_name_when_no_village_present(
    monkeypatch, tmp_path
):
    """An obscure leisure match still wins when there's no real village/town/city to prefer
    instead -- this only ever defers to an address key that's actually there."""
    def fake_urlopen(request, timeout=10):
        if "overpass-api.de" in request.full_url:
            return _FakeResponse({"elements": []})
        return _FakeResponse(
            {
                "lat": "47.488900",
                "lon": "-3.101200",
                "category": "leisure",
                "type": "marina",
                "importance": 0.0000555,
                "name": "Darse de Castéro",
                "address": {"leisure": "Darse de Castéro"},
            }
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    geocoder = Geocoder(cache_file=tmp_path / "cache.json")

    assert geocoder.place_name(47.4889, -3.1012) == "Darse de Castéro"


def test_place_name_keeps_leisure_matchs_own_name_when_importance_is_missing(monkeypatch, tmp_path):
    """A leisure match with no "importance" field at all (some real Nominatim responses omit it)
    must not be treated as obscure by default -- that would silently prefer the village for every
    match missing the field, not just genuinely obscure ones."""
    def fake_urlopen(request, timeout=10):
        if "overpass-api.de" in request.full_url:
            return _FakeResponse({"elements": []})
        return _FakeResponse(
            {
                "lat": "47.544600",
                "lon": "-2.894200",
                "category": "leisure",
                "type": "marina",
                "name": "Port du Crouesty",
                "address": {"leisure": "Port du Crouesty", "village": "Kerners"},
            }
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    geocoder = Geocoder(cache_file=tmp_path / "cache.json")

    assert geocoder.place_name(47.5446, -2.8942) == "Port du Crouesty"


def test_failed_lookup_is_not_cached(monkeypatch, tmp_path):
    """Regression test for a real bug found in practice: a transient network failure (no
    internet on the boat, DNS lookup failing) got permanently written to the cache file, so even
    a later run with a working connection kept returning the same stale "Onbekend" placeholder
    forever instead of retrying."""
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

    assert "Onbekend" in name
    assert not cache_file.exists()  # nothing was ever written -- there's nothing to cache
    assert geocoder._cache == {}
    assert call_count == 3  # the first attempt plus 2 retries (see _NOMINATIM_MAX_RETRIES)

    # a later lookup (e.g. once back online) must retry from scratch, not just keep returning
    # the failure -- another 3 attempts, not reusing/counting against the first lookup's own.
    geocoder.place_name(47.4889, -3.1012)
    assert call_count == 6


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

    assert "Onbekend" in name


def test_place_name_retries_the_nominatim_lookup_after_a_transient_failure(monkeypatch, tmp_path):
    """Regression test for a real case found in practice: the exact same coordinate that failed
    with a dropped connection mid-run resolved correctly on every one of 4 immediate, separate
    retries moments later -- a single failure on the main Nominatim request must not give up
    outright the way it used to (unlike the Overpass islet check just below, which already
    retried)."""
    nominatim_call_count = 0

    def fake_urlopen(request, timeout=10):
        nonlocal nominatim_call_count
        if "overpass-api.de" in request.full_url:
            return _FakeResponse({"elements": []})  # no islet nearby -- not the thing under test
        nominatim_call_count += 1
        if nominatim_call_count == 1:
            raise urllib.error.URLError("Connection reset by peer")
        return _FakeResponse({"address": {"village": "Arzal"}})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr("nmea2000processor.geocode.time.sleep", lambda s: None)
    geocoder = Geocoder(cache_file=tmp_path / "cache.json")

    assert geocoder.place_name(47.5027, -2.3857) == "Arzal"
    assert nominatim_call_count == 2


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
    assert saved == {"47.839,-4.176": "Loctudy"}


def test_cache_entries_from_an_older_precision_are_migrated_and_stay_usable(monkeypatch, tmp_path):
    """Regression test for a real bug: raising the cache's own default precision (see
    Geocoder._key's own doc comment, coarsened to absorb small run-to-run drift in a stay's
    averaged position) left every entry already on disk keyed at the *old*, finer precision, so
    none of them ever matched a freshly-computed key again -- every lookup for an already-known
    place missed the cache and re-hit Nominatim/Overpass regardless. An entry written under the
    old 4-decimal-place key must still be found (no network call at all) once loaded by a
    Geocoder using today's coarser precision, and the file on disk gets rewritten to the new key
    so this migration only ever has to run once."""
    def fake_urlopen(request, timeout=10):
        raise AssertionError("must not make a real request for an already-cached place")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    cache_file = tmp_path / "cache.json"
    cache_file.write_text(
        json.dumps({"47.8387,-4.1759": "Loctudy"}), encoding="utf-8"
    )

    geocoder = Geocoder(cache_file=cache_file)  # default precision=3, the entry above was 4

    assert geocoder.place_name(47.8387, -4.1759) == "Loctudy"
    migrated = json.loads(cache_file.read_text(encoding="utf-8"))
    assert migrated == {"47.839,-4.176": "Loctudy"}


def test_place_name_prefers_a_nearby_islet_over_nominatims_own_match(monkeypatch, tmp_path):
    """Regression test for a real case: anchored a couple hundred meters off a named islet, so
    Nominatim's plain reverse lookup matched an unrelated nearby pier (and used *its* address
    hierarchy, a real but different nearby hamlet) instead of the islet itself. An islet always
    wins here, even though it isn't necessarily the closer of the two (see
    _nearby_landmark_name's own docstring for why marinas/bridges don't get this same treatment)."""
    def fake_urlopen(request, timeout=10):
        if "overpass-api.de" in request.full_url:
            return _FakeResponse(
                {
                    "elements": [
                        {
                            "type": "node",
                            "tags": {"place": "islet", "name": "Île de la Jument"},
                            "lat": 47.5686912,
                            "lon": -2.8875040,
                        }
                    ]
                }
            )
        return _FakeResponse({"address": {"hamlet": "Le Graniol", "village": "Kerners"}})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    geocoder = Geocoder(cache_file=tmp_path / "cache.json")

    assert geocoder.place_name(47.5706676, -2.8852789) == "Île de la Jument"


def test_place_name_prefers_a_nearby_lock_over_nominatims_own_match(monkeypatch, tmp_path):
    """Regression test for a real case: a lock complex (Arzal, on the Vilaine) came back from
    Nominatim's plain reverse lookup as just the containing village, with the lock itself --
    tagged lock=yes/lock_name in OSM -- entirely ignored, even though French waterway locks are
    commonly given their own lock_name Nominatim's address has no equivalent field for at all.
    Mapped as a way (the lock chamber), so its own "center" is used, unlike an islet's coastline
    way (see test_place_name_ignores_the_islets_coastline_way_even_when_only_it_is_found)."""
    def fake_urlopen(request, timeout=10):
        if "overpass-api.de" in request.full_url:
            return _FakeResponse(
                {
                    "elements": [
                        {
                            "type": "way",
                            "tags": {
                                "waterway": "canal", "lock": "yes",
                                "lock_name": "Écluse du barrage d'Arzal",
                            },
                            "center": {"lat": 47.500091, "lon": -2.381719},
                        }
                    ]
                }
            )
        return _FakeResponse({"address": {"hamlet": "Le Barrage", "village": "Arzal"}})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    geocoder = Geocoder(cache_file=tmp_path / "cache.json")

    assert geocoder.place_name(47.5002, -2.381898) == "Écluse du barrage d'Arzal"


def test_place_name_ignores_the_islets_coastline_way_even_when_only_it_is_found(monkeypatch, tmp_path):
    """Regression test for a real case: anchored 38 m from a pier -- genuinely at that harbour --
    yet a large islet's coastline way still had *some* point of its shape within the search
    radius, so Overpass returned it too. Its own "center" (the way's geometric centroid, not the
    nearest point of its actual coastline) was reported nearly 680 m away and would have wrongly
    won outright over the harbour the boat was actually moored at, if trusted. A way is never
    used for this, even as a fallback when no node is found -- only a plain name node has a
    single, exact coordinate that can be trusted for "is this genuinely nearby"."""
    def fake_urlopen(request, timeout=10):
        if "overpass-api.de" in request.full_url:
            return _FakeResponse(
                {
                    "elements": [
                        {
                            "type": "way",
                            "tags": {"place": "islet", "name": "Île Garo"},
                            "center": {"lat": 47.8440, "lon": -4.1820},
                        }
                    ]
                }
            )
        return _FakeResponse({"address": {"village": "Loctudy"}})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    geocoder = Geocoder(cache_file=tmp_path / "cache.json")

    assert geocoder.place_name(47.8387, -4.1759) == "Loctudy"


def test_place_name_retries_when_overpass_times_out_server_side(monkeypatch, tmp_path):
    """Regression test for a real case: under load, Overpass answers HTTP 200 with valid,
    parseable JSON even when the query itself only partially ran server-side -- signalled by a
    top-level "remark" key, with "elements" empty or incomplete. Found in practice: this looked
    exactly like a confirmed "no islet nearby" and got cached as that permanently, silently
    losing a real islet match (Île de la Jument) to Kerners on a later run, instead of being
    retried like any other failed attempt."""
    overpass_call_count = 0

    def fake_urlopen(request, timeout=10):
        nonlocal overpass_call_count
        if "overpass-api.de" in request.full_url:
            overpass_call_count += 1
            if overpass_call_count == 1:
                return _FakeResponse(
                    {"elements": [], "remark": "runtime error: Query timed out in \"around\" ..."}
                )
            return _FakeResponse(
                {
                    "elements": [
                        {
                            "type": "node",
                            "tags": {"place": "islet", "name": "Île de la Jument"},
                            "lat": 47.5687,
                            "lon": -2.8875,
                        }
                    ]
                }
            )
        return _FakeResponse({"address": {"village": "Kerners"}})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr("nmea2000processor.geocode.time.sleep", lambda s: None)
    geocoder = Geocoder(cache_file=tmp_path / "cache.json")

    assert geocoder.place_name(47.5706676, -2.8852789) == "Île de la Jument"
    assert overpass_call_count == 2


def test_place_name_prefers_the_islets_own_name_node_over_its_coastline_way(monkeypatch, tmp_path):
    """Regression test for a real case: an islet is usually mapped as both a plain name node (its
    own short name) and a separate coastline way outlining its shape -- which OSM convention
    allows to carry a different, more elaborate name (an alt_name tacked on, e.g. a local cove
    name). Found in practice: "Île de la Jument" (node) vs "Île de la Jument (Er Gazeg)" (way,
    291 m away vs the node's 276 m) -- the way's name read as needlessly specific for a boat
    simply anchored off the island, not literally in that one cove. The node wins even when it
    isn't the closer of the two -- the way is ignored entirely, not just deprioritized."""
    def fake_urlopen(request, timeout=10):
        if "overpass-api.de" in request.full_url:
            return _FakeResponse(
                {
                    "elements": [
                        {
                            "type": "way",
                            "tags": {"place": "islet", "name": "Île de la Jument (Er Gazeg)"},
                            "center": {"lat": 47.5686589, "lon": -2.887956},
                        },
                        {
                            "type": "node",
                            "tags": {"place": "islet", "name": "Île de la Jument"},
                            "lat": 47.5688692,
                            "lon": -2.8872895,
                        },
                    ]
                }
            )
        return _FakeResponse({"address": {"hamlet": "Le Graniol", "village": "Kerners"}})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    geocoder = Geocoder(cache_file=tmp_path / "cache.json")

    assert geocoder.place_name(47.5705, -2.8852) == "Île de la Jument"


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
                {
                    "elements": [
                        {
                            "tags": {"place": "islet", "name": "Île de la Jument"},
                            "lat": 47.5687,
                            "lon": -2.8875,
                        }
                    ]
                }
            )
        return _FakeResponse({"address": {"village": "Kerners"}})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr("nmea2000processor.geocode.time.sleep", lambda s: None)
    geocoder = Geocoder(cache_file=tmp_path / "cache.json")

    assert geocoder.place_name(47.5706676, -2.8852789) == "Île de la Jument"
    assert overpass_call_count == 2


def test_landmark_check_retries_twice_before_giving_up(monkeypatch, tmp_path):
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

    assert overpass_call_count == 3  # the first attempt plus 2 retries


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
    assert err.count("Overpass landmark check failed") == 3


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
