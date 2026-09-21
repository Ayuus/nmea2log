import json
import urllib.error
import urllib.request
from datetime import datetime

import pytest

from nmea2log import _net, open_meteo
from nmea2log.geocode import Geocoder
from nmea2log.marine import MarineFetcher
from nmea2log.weather import WeatherFetcher


class _FakeResponse:
    status = 200

    def __init__(self, payload: dict):
        self._data = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self._data


def _breaker(limit: int = 3) -> _net.FailureBreaker:
    return _net.FailureBreaker("Some service", "tag", "What happens next.", limit=limit)


def test_the_breaker_switches_off_after_the_limit_of_failures_in_a_row(capsys):
    breaker = _breaker()

    breaker.record(False)
    breaker.record(False)
    assert not breaker.off
    breaker.record(False)

    assert breaker.off
    assert "[tag] Some service switched off for the rest of this run: 3 lookups in a row failed" in capsys.readouterr().err


def test_a_lookup_that_worked_starts_the_count_over():
    breaker = _breaker()
    breaker.record(False)
    breaker.record(False)
    breaker.record(True)
    breaker.record(False)
    breaker.record(False)

    assert not breaker.off


def test_the_breaker_says_so_only_once(capsys):
    breaker = _breaker(limit=2)
    for _ in range(5):
        breaker.record(False)

    assert capsys.readouterr().err.count("switched off") == 1


@pytest.fixture
def no_waiting(monkeypatch):
    monkeypatch.setattr("nmea2log.geocode.time.sleep", lambda s: None)
    monkeypatch.setattr("nmea2log.open_meteo.time.sleep", lambda s: None)


def _requests_to(monkeypatch, host_part: str, fails, answer) -> list:
    """urlopen fake: requests to a URL containing ``host_part`` fail while ``fails()`` is true and otherwise get
    ``answer``; everything else answers "Kerners". Returns the list of URLs asked for ``host_part``."""
    asked = []

    def fake_urlopen(request, timeout=10):
        if host_part in request.full_url:
            asked.append(request.full_url)
            if fails():
                raise urllib.error.URLError("Connection refused")
            return _FakeResponse(answer)
        return _FakeResponse({"address": {"village": "Kerners"}, "elements": []})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return asked


def test_nominatim_is_left_alone_after_three_lookups_in_a_row_failed(monkeypatch, tmp_path, no_waiting):
    asked = _requests_to(monkeypatch, "nominatim", lambda: True, {})
    geocoder = Geocoder(cache_file=tmp_path / "cache.json")

    names = [geocoder.place_name(47.5 + i * 0.1, -2.88) for i in range(5)]

    assert len(asked) == 3 * 3  # three lookups with their three attempts each, then no more requests
    assert all(name.startswith("Onbekend (") for name in names)
    assert not (tmp_path / "cache.json").exists()  # nothing cached: a later run looks them up again


def test_nominatim_comes_back_in_a_later_run(monkeypatch, tmp_path, no_waiting):
    state = {"down": True}
    _requests_to(monkeypatch, "nominatim", lambda: state["down"], {"address": {"village": "Arzal"}})
    cache_file = tmp_path / "cache.json"
    first = Geocoder(cache_file=cache_file)
    for i in range(4):
        first.place_name(47.5 + i * 0.1, -2.88)

    state["down"] = False
    later = Geocoder(cache_file=cache_file)

    assert later.place_name(47.9, -2.88) == "Arzal"


@pytest.mark.parametrize("fetcher_class, host, tag", [(WeatherFetcher, "archive-api", "weather"), (MarineFetcher, "marine-api", "marine")])
def test_open_meteo_is_left_alone_after_three_days_in_a_row_failed(monkeypatch, tmp_path, no_waiting, fetcher_class, host, tag, capsys):
    asked = _requests_to(monkeypatch, host, lambda: True, {})
    fetcher = fetcher_class(cache_file=tmp_path / "cache.json")

    results = [fetcher.hour(47.5, -2.5, datetime(2026, 8, 1 + i, 8, 0)) for i in range(5)]

    assert results == [None] * 5
    assert len(asked) == 3 * (open_meteo._MAX_RETRIES + 1)  # three days with all their attempts, then no more requests
    assert f"[{tag}] Open-Meteo switched off for the rest of this run" in capsys.readouterr().err
    assert not (tmp_path / "cache.json").exists()


def test_open_meteo_days_that_worked_reset_the_count(monkeypatch, tmp_path, no_waiting):
    state = {"down": True}
    payload = {"hourly": {"time": ["2026-08-05T08:00"], "wind_speed_10m": [5], "wind_direction_10m": [180],
                          "precipitation": [0], "cloud_cover": [10]}}
    asked = _requests_to(monkeypatch, "archive-api", lambda: state["down"], payload)
    fetcher = WeatherFetcher(cache_file=tmp_path / "cache.json")

    fetcher.hour(47.5, -2.5, datetime(2026, 8, 1, 8, 0))
    fetcher.hour(47.5, -2.5, datetime(2026, 8, 2, 8, 0))
    state["down"] = False
    assert fetcher.hour(47.5, -2.5, datetime(2026, 8, 5, 8, 0)) is not None
    state["down"] = True
    fetcher.hour(47.5, -2.5, datetime(2026, 8, 6, 8, 0))
    fetcher.hour(47.5, -2.5, datetime(2026, 8, 7, 8, 0))
    before = len(asked)
    fetcher.hour(47.5, -2.5, datetime(2026, 8, 8, 8, 0))  # the third failure since the success: still tried

    assert len(asked) == before + open_meteo._MAX_RETRIES + 1
