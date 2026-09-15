import http.client
import json
import urllib.error
import urllib.request
from datetime import datetime

from nmea2log.weather import HourlyWeather, NoWeather, WeatherFetcher


class _FakeResponse:
    def __init__(self, payload: dict):
        self._data = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self._data


def _archive_payload(times, wind_kn, wind_deg, precip_mm, cloud_pct):
    return {
        "hourly": {
            "time": times,
            "wind_speed_10m": wind_kn,
            "wind_direction_10m": wind_deg,
            "precipitation": precip_mm,
            "cloud_cover": cloud_pct,
        }
    }


def test_hour_returns_the_matching_hourly_reading(monkeypatch, tmp_path):
    def fake_urlopen(request, timeout=10):
        return _FakeResponse(
            _archive_payload(
                ["2026-08-05T07:00", "2026-08-05T08:00"],
                [8.4, 8.3],
                [294, 292],
                [0.1, 0.0],
                [20, 7],
            )
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    fetcher = WeatherFetcher(cache_file=tmp_path / "cache.json")

    result = fetcher.hour(47.5, -2.5, datetime(2026, 8, 5, 8, 17))

    assert result == HourlyWeather(wind_kn=8.3, wind_deg=292, precip_mm=0.0, cloud_pct=7)


def test_hour_rounds_down_to_the_start_of_the_hour(monkeypatch, tmp_path):
    """Regression-style check: 08:59 must still match the 08:00 reading, not 09:00 (which the
    API never even returned here)."""
    def fake_urlopen(request, timeout=10):
        return _FakeResponse(_archive_payload(["2026-08-05T08:00"], [8.3], [292], [0.0], [7]))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    fetcher = WeatherFetcher(cache_file=tmp_path / "cache.json")

    result = fetcher.hour(47.5, -2.5, datetime(2026, 8, 5, 8, 59))

    assert result is not None
    assert result.wind_kn == 8.3


def test_day_is_only_fetched_once_for_multiple_hours_the_same_day(monkeypatch, tmp_path):
    call_count = 0

    def fake_urlopen(request, timeout=10):
        nonlocal call_count
        call_count += 1
        return _FakeResponse(
            _archive_payload(
                ["2026-08-05T07:00", "2026-08-05T08:00"], [8.4, 8.3], [294, 292], [0.1, 0.0], [20, 7]
            )
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    cache_file = tmp_path / "cache.json"
    fetcher = WeatherFetcher(cache_file=cache_file)

    fetcher.hour(47.5, -2.5, datetime(2026, 8, 5, 7, 41))
    fetcher.hour(47.5, -2.5, datetime(2026, 8, 5, 10, 52))

    assert call_count == 1
    assert cache_file.exists()

    # a second Fetcher instance (e.g. a later run) must reuse the cache file too, not re-fetch
    fetcher2 = WeatherFetcher(cache_file=cache_file)
    fetcher2.hour(47.5, -2.5, datetime(2026, 8, 5, 9, 0))
    assert call_count == 1


def test_hour_returns_none_when_no_reading_exists_for_that_hour(monkeypatch, tmp_path):
    def fake_urlopen(request, timeout=10):
        return _FakeResponse(_archive_payload(["2026-08-05T07:00"], [8.4], [294], [0.1], [20]))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    fetcher = WeatherFetcher(cache_file=tmp_path / "cache.json")

    result = fetcher.hour(47.5, -2.5, datetime(2026, 8, 5, 23, 0))

    assert result is None


def test_failed_request_is_not_cached(monkeypatch, tmp_path):
    """Regression-style check mirroring geocode.py's own failed-lookup test: a transient failure
    (no internet) must not permanently poison that date -- a later run with a working connection
    has to retry, not keep returning "no weather" forever."""
    call_count = 0

    def fake_urlopen(request, timeout=10):
        nonlocal call_count
        call_count += 1
        raise urllib.error.URLError("getaddrinfo failed")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr("nmea2log.weather.time.sleep", lambda s: None)
    cache_file = tmp_path / "cache.json"
    fetcher = WeatherFetcher(cache_file=cache_file)

    result = fetcher.hour(47.5, -2.5, datetime(2026, 8, 5, 8, 0))

    assert result is None
    assert not cache_file.exists()

    fetcher.hour(47.5, -2.5, datetime(2026, 8, 5, 8, 0))
    assert call_count == 2 * (5 + 1)  # both lookups retried the full _MAX_RETRIES + 1 attempts


def test_remote_disconnected_is_treated_as_a_failed_lookup_not_a_crash(monkeypatch, tmp_path):
    def fake_urlopen(request, timeout=10):
        raise http.client.RemoteDisconnected("Remote end closed connection without response")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr("nmea2log.weather.time.sleep", lambda s: None)
    fetcher = WeatherFetcher(cache_file=tmp_path / "cache.json")

    result = fetcher.hour(47.5, -2.5, datetime(2026, 8, 5, 8, 0))

    assert result is None


def test_no_weather_always_returns_none():
    assert NoWeather().hour(47.5, -2.5, datetime(2026, 8, 5, 8, 0)) is None
