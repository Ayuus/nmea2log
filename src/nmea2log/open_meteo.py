"""Shared machinery for the two Open-Meteo lookups (weather.py, marine.py): a local cache keyed on
date + rounded position, a throttled, retrying HTTP request per (day, position), and the
"closest hourly reading" lookup. Uses only the Python standard library (urllib).

The underlying data is a fixed historical reanalysis for a past date, so once fetched a cache
entry never goes stale and never needs re-checking -- unlike port names.

Not a measurement from the boat itself: the data is regional model output for the nearest grid
cell to the given position (found in practice: a request for 46.4968,-1.7899 came back for
46.502636,-1.733551, several km away) -- indicative of the conditions, not what an instrument on
board would have recorded.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional

from ._net import FailureBreaker, urlopen_ipv4_first
from .log import log

# The first attempt plus 2 retries: the same as Nominatim and Overpass (see geocode.py). More never paid off:
# when the service is down, the extra attempts fail too (see FailureBreaker).
_MAX_RETRIES = 2
# Open-Meteo has no documented per-second limit like Nominatim's, but requests fired back-to-back
# (one per day/position, no gap) were found in practice to get reset by the server about a third
# of the time (WinError 10054 / connection reset) -- spacing them out the same way Geocoder does
# for Nominatim clears that up.
_MIN_INTERVAL_S = 1.0

HourlyValues = Dict[str, Optional[float]]
DayData = Dict[str, HourlyValues]


def _day_key(lat: float, lon: float, day: date) -> str:
    # Rounded to 2 decimals (~1 km) -- finer than the model's own grid resolution, so this only
    # ever creates a new cache entry for a position that could plausibly get different data.
    return f"{day.isoformat()}:{round(lat, 2)},{round(lon, 2)}"


def hourly_column(hourly: dict, name: str) -> List[Optional[float]]:
    """One of the response's parallel per-hour arrays, or [] if the response doesn't have it."""
    return hourly.get(name, [])


def value_at(column: List[Optional[float]], i: int) -> Optional[float]:
    """The i-th entry of a per-hour array, None if the array is shorter than the time axis."""
    return column[i] if i < len(column) else None


class OpenMeteoDayFetcher:
    """Subclasses set ``_API_URL``/``_LOG_TAG``/``_HOURLY_FIELDS`` (and ``_EXTRA_PARAMS`` if the
    API needs more request parameters) and implement ``_parse_hourly``; callers use the subclass'
    own ``hour()`` (see weather.py/marine.py), built on ``_hour_values`` here."""

    _API_URL: str
    _LOG_TAG: str
    _HOURLY_FIELDS: str
    _EXTRA_PARAMS: Dict[str, str] = {}

    def __init__(
        self,
        *,
        cache_file: Optional[Path] = None,
        user_agent: str = "nmea2log/0.1 (personal sailing logbook)",
    ) -> None:
        self.cache_file = cache_file
        self.user_agent = user_agent
        self._cache: Dict[str, Optional[DayData]] = {}
        self._last_request = 0.0
        self._breaker = FailureBreaker(
            "Open-Meteo", self._LOG_TAG,
            "The days after this show no data and are not cached, so a later run fetches them again.",
        )
        if cache_file is not None and cache_file.exists():
            self._cache = json.loads(cache_file.read_text(encoding="utf-8"))

    def _parse_hourly(self, hourly: dict) -> DayData:
        """Turns the response's ``hourly`` object (parallel arrays plus a ``time`` axis) into
        ``{hour timestamp: {field: value}}`` -- what gets cached."""
        raise NotImplementedError

    def _hour_values(self, lat: float, lon: float, when: datetime) -> Optional[HourlyValues]:
        """The closest available hourly reading to ``when`` (a UTC datetime), or None if the
        day's data could never be fetched (a transient failure -- not cached, see _fetch_day)."""
        day_data = self._day(lat, lon, when.date())
        if not day_data:
            return None
        hour_key = when.replace(minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:00")
        return day_data.get(hour_key)

    def _day(self, lat: float, lon: float, day: date) -> Optional[DayData]:
        key = _day_key(lat, lon, day)
        if key in self._cache:
            return self._cache[key]
        result = self._fetch_day(lat, lon, day)
        # A confirmed empty/malformed response is still worth caching (Open-Meteo has no data for
        # some very recent dates yet) -- only a genuine request failure is left uncached, the same
        # reasoning as Geocoder._lookup: an offline run must not permanently poison this date.
        if result is not None:
            self._cache[key] = result
            self._save_cache()
        return result

    def _fetch_day(self, lat: float, lon: float, day: date) -> Optional[DayData]:
        params = urllib.parse.urlencode(
            {
                "latitude": f"{lat:.4f}",
                "longitude": f"{lon:.4f}",
                "start_date": day.isoformat(),
                "end_date": day.isoformat(),
                "hourly": self._HOURLY_FIELDS,
                **self._EXTRA_PARAMS,
            }
        )
        request = urllib.request.Request(f"{self._API_URL}?{params}", headers={"User-Agent": self.user_agent})
        if self._breaker.off:
            return None  # given up on for this run: like a failed request, so not cached
        payload = None
        for attempt in range(_MAX_RETRIES + 1):
            if attempt:
                time.sleep(2.0)
            wait = _MIN_INTERVAL_S - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            try:
                with urlopen_ipv4_first(request, timeout=15) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                self._last_request = time.monotonic()
                break
            except (urllib.error.URLError, OSError, ValueError) as exc:
                self._last_request = time.monotonic()
                log(
                    f"[{self._LOG_TAG}] Open-Meteo request failed ({exc}) "
                    f"-- attempt {attempt + 1}/{_MAX_RETRIES + 1}",
                    file=sys.stderr,
                )
        self._breaker.record(payload is not None)
        if payload is None:
            return None  # best-effort: the caller shows no data for this day rather than crashing
        return self._parse_hourly(payload.get("hourly", {}))

    def _save_cache(self) -> None:
        if self.cache_file is None:
            return
        self.cache_file.write_text(json.dumps(self._cache, ensure_ascii=False, indent=2), encoding="utf-8")
