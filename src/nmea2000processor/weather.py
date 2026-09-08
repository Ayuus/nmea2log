"""Look up historical hourly wind/precipitation/cloud cover for GPS positions via Open-Meteo's
free historical weather API (https://open-meteo.com/en/docs/historical-weather-api). Uses only the
Python standard library (urllib), no extra dependency. Results are cached locally, keyed on date +
rounded position -- unlike port names, the underlying data is a fixed historical reanalysis for a
past date, so once fetched a cache entry never goes stale and never needs re-checking.

Not a measurement from the boat itself: this is regional weather-model data for the nearest grid
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
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Dict, Optional

from ._net import urlopen_ipv4_first
from .log import log

_ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
_MAX_RETRIES = 5
_HOURLY_FIELDS = "wind_speed_10m,wind_direction_10m,precipitation,cloud_cover"
# Open-Meteo has no documented per-second limit like Nominatim's, but requests fired back-to-back
# (one per day/position, no gap) were found in practice to get reset by the server about a third
# of the time (WinError 10054 / connection reset) -- spacing them out the same way Geocoder does
# for Nominatim clears that up.
_MIN_INTERVAL_S = 1.0


@dataclass(frozen=True)
class HourlyWeather:
    wind_kn: Optional[float]
    wind_deg: Optional[float]
    precip_mm: Optional[float]
    cloud_pct: Optional[float]


def _day_key(lat: float, lon: float, day: date) -> str:
    # Rounded to 2 decimals (~1 km) -- finer than the weather model's own grid resolution, so this
    # only ever creates a new cache entry for a position that could plausibly get different data.
    return f"{day.isoformat()}:{round(lat, 2)},{round(lon, 2)}"


class WeatherFetcher:
    def __init__(
        self,
        *,
        cache_file: Optional[Path] = None,
        user_agent: str = "nmea2000processor/0.1 (personal sailing logbook)",
    ) -> None:
        self.cache_file = cache_file
        self.user_agent = user_agent
        self._cache: Dict[str, Optional[Dict[str, Dict[str, Optional[float]]]]] = {}
        self._last_request = 0.0
        if cache_file is not None and cache_file.exists():
            self._cache = json.loads(cache_file.read_text(encoding="utf-8"))

    def hour(self, lat: float, lon: float, when: datetime) -> Optional[HourlyWeather]:
        """The closest available hourly reading to ``when`` (a UTC datetime), or None if the
        day's data could never be fetched (a transient failure -- not cached, see _fetch_day)."""
        day_data = self._day(lat, lon, when.date())
        if not day_data:
            return None
        hour_key = when.replace(minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:00")
        values = day_data.get(hour_key)
        if values is None:
            return None
        return HourlyWeather(
            wind_kn=values.get("wind_kn"),
            wind_deg=values.get("wind_deg"),
            precip_mm=values.get("precip_mm"),
            cloud_pct=values.get("cloud_pct"),
        )

    def _day(self, lat: float, lon: float, day: date) -> Optional[Dict[str, Dict[str, Optional[float]]]]:
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

    def _fetch_day(self, lat: float, lon: float, day: date) -> Optional[Dict[str, Dict[str, Optional[float]]]]:
        params = urllib.parse.urlencode(
            {
                "latitude": f"{lat:.4f}",
                "longitude": f"{lon:.4f}",
                "start_date": day.isoformat(),
                "end_date": day.isoformat(),
                "hourly": _HOURLY_FIELDS,
                "wind_speed_unit": "kn",
            }
        )
        request = urllib.request.Request(
            f"{_ARCHIVE_URL}?{params}", headers={"User-Agent": self.user_agent}
        )
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
                    f"[weather] Open-Meteo request failed ({exc}) "
                    f"-- attempt {attempt + 1}/{_MAX_RETRIES + 1}",
                    file=sys.stderr,
                )
        if payload is None:
            return None  # best-effort: caller shows no weather for this day rather than crashing

        hourly = payload.get("hourly", {})
        times = hourly.get("time", [])
        wind_kn = hourly.get("wind_speed_10m", [])
        wind_deg = hourly.get("wind_direction_10m", [])
        precip_mm = hourly.get("precipitation", [])
        cloud_pct = hourly.get("cloud_cover", [])
        return {
            t: {
                "wind_kn": wind_kn[i] if i < len(wind_kn) else None,
                "wind_deg": wind_deg[i] if i < len(wind_deg) else None,
                "precip_mm": precip_mm[i] if i < len(precip_mm) else None,
                "cloud_pct": cloud_pct[i] if i < len(cloud_pct) else None,
            }
            for i, t in enumerate(times)
        }

    def _save_cache(self) -> None:
        if self.cache_file is None:
            return
        self.cache_file.write_text(
            json.dumps(self._cache, ensure_ascii=False, indent=2), encoding="utf-8"
        )


class NoWeather:
    """Skips the online weather lookup -- no weather columns shown."""

    def hour(self, lat: float, lon: float, when: datetime) -> Optional[HourlyWeather]:
        return None
