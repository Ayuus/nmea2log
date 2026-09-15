"""Look up historical hourly wave/ocean-current data for GPS positions via Open-Meteo's free
marine weather API (https://open-meteo.com/en/docs/marine-weather-api) -- a separate host/dataset
from the general historical weather archive used by weather.py. Uses only the Python standard
library (urllib), no extra dependency. Results are cached locally, keyed on date + rounded
position -- unlike port names, the underlying data is a fixed historical reanalysis for a past
date, so once fetched a cache entry never goes stale and never needs re-checking.

Not a measurement from the boat itself: this is regional wave/current-model data for the nearest
sea grid cell to the given position (same grid-snapping behaviour as weather.py's archive API,
confirmed against a real request) -- indicative of the conditions, not what an instrument on
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

_ARCHIVE_URL = "https://marine-api.open-meteo.com/v1/marine"
_MAX_RETRIES = 5
_HOURLY_FIELDS = "wave_height,wave_direction,wave_period,ocean_current_velocity,ocean_current_direction"
_KMH_PER_KN = 1.852
# Same throttle weather.py needed after being found, in practice, to get connection-reset by
# Open-Meteo about a third of the time when requests were fired back-to-back with no gap.
_MIN_INTERVAL_S = 1.0


@dataclass(frozen=True)
class HourlyMarine:
    wave_height_m: Optional[float]
    wave_direction_deg: Optional[float]
    wave_period_s: Optional[float]
    current_kn: Optional[float]
    current_direction_deg: Optional[float]


def _day_key(lat: float, lon: float, day: date) -> str:
    # Rounded to 2 decimals (~1 km) -- finer than the model's own grid resolution, so this only
    # ever creates a new cache entry for a position that could plausibly get different data.
    return f"{day.isoformat()}:{round(lat, 2)},{round(lon, 2)}"


class MarineFetcher:
    def __init__(
        self,
        *,
        cache_file: Optional[Path] = None,
        user_agent: str = "nmea2log/0.1 (personal sailing logbook)",
    ) -> None:
        self.cache_file = cache_file
        self.user_agent = user_agent
        self._cache: Dict[str, Optional[Dict[str, Dict[str, Optional[float]]]]] = {}
        self._last_request = 0.0
        if cache_file is not None and cache_file.exists():
            self._cache = json.loads(cache_file.read_text(encoding="utf-8"))

    def hour(self, lat: float, lon: float, when: datetime) -> Optional[HourlyMarine]:
        """The closest available hourly reading to ``when`` (a UTC datetime), or None if the
        day's data could never be fetched (a transient failure -- not cached, see _fetch_day)."""
        day_data = self._day(lat, lon, when.date())
        if not day_data:
            return None
        hour_key = when.replace(minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:00")
        values = day_data.get(hour_key)
        if values is None:
            return None
        return HourlyMarine(
            wave_height_m=values.get("wave_height_m"),
            wave_direction_deg=values.get("wave_direction_deg"),
            wave_period_s=values.get("wave_period_s"),
            current_kn=values.get("current_kn"),
            current_direction_deg=values.get("current_direction_deg"),
        )

    def _day(self, lat: float, lon: float, day: date) -> Optional[Dict[str, Dict[str, Optional[float]]]]:
        key = _day_key(lat, lon, day)
        if key in self._cache:
            return self._cache[key]
        result = self._fetch_day(lat, lon, day)
        # A confirmed empty/malformed response is still worth caching (Open-Meteo has no data for
        # some very recent dates yet) -- only a genuine request failure is left uncached, the same
        # reasoning as weather.py/geocode.py: an offline run must not permanently poison this date.
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
                    f"[marine] Open-Meteo request failed ({exc}) "
                    f"-- attempt {attempt + 1}/{_MAX_RETRIES + 1}",
                    file=sys.stderr,
                )
        if payload is None:
            return None  # best-effort: caller shows no marine data for this day rather than crashing

        hourly = payload.get("hourly", {})
        times = hourly.get("time", [])
        wave_height_m = hourly.get("wave_height", [])
        wave_direction_deg = hourly.get("wave_direction", [])
        wave_period_s = hourly.get("wave_period", [])
        current_kmh = hourly.get("ocean_current_velocity", [])
        current_direction_deg = hourly.get("ocean_current_direction", [])
        return {
            t: {
                "wave_height_m": wave_height_m[i] if i < len(wave_height_m) else None,
                "wave_direction_deg": wave_direction_deg[i] if i < len(wave_direction_deg) else None,
                "wave_period_s": wave_period_s[i] if i < len(wave_period_s) else None,
                # No server-side knots conversion for ocean current velocity (unlike wind's
                # wind_speed_unit=kn, confirmed by a real request -- current_velocity_unit=kn is
                # silently ignored and km/h still comes back), so converted here instead.
                "current_kn": (
                    current_kmh[i] / _KMH_PER_KN
                    if i < len(current_kmh) and current_kmh[i] is not None
                    else None
                ),
                "current_direction_deg": (
                    current_direction_deg[i] if i < len(current_direction_deg) else None
                ),
            }
            for i, t in enumerate(times)
        }

    def _save_cache(self) -> None:
        if self.cache_file is None:
            return
        self.cache_file.write_text(
            json.dumps(self._cache, ensure_ascii=False, indent=2), encoding="utf-8"
        )


class NoMarine:
    """Skips the online wave/current lookup -- no marine columns shown."""

    def hour(self, lat: float, lon: float, when: datetime) -> Optional[HourlyMarine]:
        return None
