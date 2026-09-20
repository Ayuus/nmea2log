import http.client
import json
import urllib.error
import urllib.request
from datetime import datetime

from nmea2log.marine import HourlyMarine, MarineFetcher, NoMarine


class _FakeResponse:
    def __init__(self, payload: dict):
        self._data = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self._data


def _archive_payload(times, wave_height, wave_direction, wave_period, current_kmh, current_direction):
    return {
        "hourly": {
            "time": times,
            "wave_height": wave_height,
            "wave_direction": wave_direction,
            "wave_period": wave_period,
            "ocean_current_velocity": current_kmh,
            "ocean_current_direction": current_direction,
        }
    }


def test_hour_returns_the_matching_hourly_reading(monkeypatch, tmp_path):
    def fake_urlopen(request, timeout=10):
        return _FakeResponse(
            _archive_payload(
                ["2026-08-05T07:00", "2026-08-05T08:00"],
                [0.8, 0.72],
                [249, 254],
                [4.45, 4.7],
                [0.72, 0.72],  # km/h
                [180, 270],
            )
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    fetcher = MarineFetcher(cache_file=tmp_path / "cache.json")

    result = fetcher.hour(47.5, -2.5, datetime(2026, 8, 5, 8, 17))

    assert result == HourlyMarine(
        wave_height_m=0.72,
        wave_direction_deg=254,
        wave_period_s=4.7,
        current_kn=0.72 / 1.852,
        current_direction_deg=270,
    )


def test_current_velocity_is_converted_from_kmh_to_knots(monkeypatch, tmp_path):
    """Regression-style check: Open-Meteo's marine API has no server-side knots conversion for
    ocean current velocity (unlike wind's wind_speed_unit=kn) -- confirmed against the real API,
    a current_velocity_unit=kn param is silently ignored and km/h still comes back. Conversion
    has to happen client-side instead."""
    def fake_urlopen(request, timeout=10):
        return _FakeResponse(
            _archive_payload(["2026-08-05T08:00"], [0.72], [254], [4.7], [1.852], [270])
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    fetcher = MarineFetcher(cache_file=tmp_path / "cache.json")

    result = fetcher.hour(47.5, -2.5, datetime(2026, 8, 5, 8, 0))

    assert result is not None
    assert result.current_kn == 1.0  # 1.852 km/h == 1 kn exactly


def test_hour_rounds_down_to_the_start_of_the_hour(monkeypatch, tmp_path):
    def fake_urlopen(request, timeout=10):
        return _FakeResponse(_archive_payload(["2026-08-05T08:00"], [0.72], [254], [4.7], [0.72], [270]))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    fetcher = MarineFetcher(cache_file=tmp_path / "cache.json")

    result = fetcher.hour(47.5, -2.5, datetime(2026, 8, 5, 8, 59))

    assert result is not None
    assert result.wave_height_m == 0.72


def test_day_is_only_fetched_once_for_multiple_hours_the_same_day(monkeypatch, tmp_path):
    call_count = 0

    def fake_urlopen(request, timeout=10):
        nonlocal call_count
        call_count += 1
        return _FakeResponse(
            _archive_payload(
                ["2026-08-05T07:00", "2026-08-05T08:00"], [0.8, 0.72], [249, 254], [4.45, 4.7],
                [0.7, 0.72], [180, 270],
            )
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    cache_file = tmp_path / "cache.json"
    fetcher = MarineFetcher(cache_file=cache_file)

    fetcher.hour(47.5, -2.5, datetime(2026, 8, 5, 7, 41))
    fetcher.hour(47.5, -2.5, datetime(2026, 8, 5, 10, 52))

    assert call_count == 1
    assert cache_file.exists()

    fetcher2 = MarineFetcher(cache_file=cache_file)
    fetcher2.hour(47.5, -2.5, datetime(2026, 8, 5, 9, 0))
    assert call_count == 1


def test_hour_returns_none_when_no_reading_exists_for_that_hour(monkeypatch, tmp_path):
    def fake_urlopen(request, timeout=10):
        return _FakeResponse(_archive_payload(["2026-08-05T07:00"], [0.8], [249], [4.45], [0.7], [180]))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    fetcher = MarineFetcher(cache_file=tmp_path / "cache.json")

    result = fetcher.hour(47.5, -2.5, datetime(2026, 8, 5, 23, 0))

    assert result is None


def test_failed_request_is_not_cached(monkeypatch, tmp_path):
    call_count = 0

    def fake_urlopen(request, timeout=10):
        nonlocal call_count
        call_count += 1
        raise urllib.error.URLError("getaddrinfo failed")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr("nmea2log.open_meteo.time.sleep", lambda s: None)
    cache_file = tmp_path / "cache.json"
    fetcher = MarineFetcher(cache_file=cache_file)

    result = fetcher.hour(47.5, -2.5, datetime(2026, 8, 5, 8, 0))

    assert result is None
    assert not cache_file.exists()

    fetcher.hour(47.5, -2.5, datetime(2026, 8, 5, 8, 0))
    assert call_count == 2 * (5 + 1)  # both lookups retried the full _MAX_RETRIES + 1 attempts


def test_remote_disconnected_is_treated_as_a_failed_lookup_not_a_crash(monkeypatch, tmp_path):
    def fake_urlopen(request, timeout=10):
        raise http.client.RemoteDisconnected("Remote end closed connection without response")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr("nmea2log.open_meteo.time.sleep", lambda s: None)
    fetcher = MarineFetcher(cache_file=tmp_path / "cache.json")

    result = fetcher.hour(47.5, -2.5, datetime(2026, 8, 5, 8, 0))

    assert result is None


def test_no_marine_always_returns_none():
    assert NoMarine().hour(47.5, -2.5, datetime(2026, 8, 5, 8, 0)) is None
