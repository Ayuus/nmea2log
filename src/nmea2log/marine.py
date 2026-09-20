"""Look up historical hourly wave/ocean-current data for GPS positions via Open-Meteo's free
marine weather API (https://open-meteo.com/en/docs/marine-weather-api) -- a separate host/dataset
from the general historical weather archive used by weather.py. Caching, throttling and retries
are shared with weather.py (see open_meteo.py).

Not a measurement from the boat itself: this is regional wave/current-model data for the nearest
sea grid cell to the given position (same grid-snapping behaviour as weather.py's archive API,
confirmed against a real request) -- indicative of the conditions, not what an instrument on
board would have recorded.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from .open_meteo import DayData, OpenMeteoDayFetcher, hourly_column, value_at

_KMH_PER_KN = 1.852


@dataclass(frozen=True)
class HourlyMarine:
    wave_height_m: Optional[float]
    wave_direction_deg: Optional[float]
    wave_period_s: Optional[float]
    current_kn: Optional[float]
    current_direction_deg: Optional[float]


class MarineFetcher(OpenMeteoDayFetcher):
    _API_URL = "https://marine-api.open-meteo.com/v1/marine"
    _LOG_TAG = "marine"
    _HOURLY_FIELDS = "wave_height,wave_direction,wave_period,ocean_current_velocity,ocean_current_direction"

    def hour(self, lat: float, lon: float, when: datetime) -> Optional[HourlyMarine]:
        values = self._hour_values(lat, lon, when)
        if values is None:
            return None
        return HourlyMarine(
            wave_height_m=values.get("wave_height_m"),
            wave_direction_deg=values.get("wave_direction_deg"),
            wave_period_s=values.get("wave_period_s"),
            current_kn=values.get("current_kn"),
            current_direction_deg=values.get("current_direction_deg"),
        )

    def _parse_hourly(self, hourly: dict) -> DayData:
        wave_height_m = hourly_column(hourly, "wave_height")
        wave_direction_deg = hourly_column(hourly, "wave_direction")
        wave_period_s = hourly_column(hourly, "wave_period")
        current_kmh = hourly_column(hourly, "ocean_current_velocity")
        current_direction_deg = hourly_column(hourly, "ocean_current_direction")
        return {
            t: {
                "wave_height_m": value_at(wave_height_m, i),
                "wave_direction_deg": value_at(wave_direction_deg, i),
                "wave_period_s": value_at(wave_period_s, i),
                # No server-side knots conversion for ocean current velocity (unlike wind's
                # wind_speed_unit=kn, confirmed by a real request -- current_velocity_unit=kn is
                # silently ignored and km/h still comes back), so converted here instead.
                "current_kn": current_kmh[i] / _KMH_PER_KN if value_at(current_kmh, i) is not None else None,
                "current_direction_deg": value_at(current_direction_deg, i),
            }
            for i, t in enumerate(hourly.get("time", []))
        }


class NoMarine:
    """Skips the online wave/current lookup -- no marine columns shown."""

    def hour(self, lat: float, lon: float, when: datetime) -> Optional[HourlyMarine]:
        return None
