"""Look up historical hourly wind/precipitation/cloud cover for GPS positions via Open-Meteo's
free historical weather API (https://open-meteo.com/en/docs/historical-weather-api). Caching,
throttling and retries are shared with marine.py (see open_meteo.py).

Not a measurement from the boat itself: this is regional weather-model data for the nearest grid
cell to the given position (found in practice: a request for 46.4968,-1.7899 came back for
46.502636,-1.733551, several km away) -- indicative of the conditions, not what an instrument on
board would have recorded.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from .open_meteo import DayData, OpenMeteoDayFetcher, hourly_column, value_at


@dataclass(frozen=True)
class HourlyWeather:
    wind_kn: Optional[float]
    wind_deg: Optional[float]
    precip_mm: Optional[float]
    cloud_pct: Optional[float]


class WeatherFetcher(OpenMeteoDayFetcher):
    _API_URL = "https://archive-api.open-meteo.com/v1/archive"
    _LOG_TAG = "weather"
    _HOURLY_FIELDS = "wind_speed_10m,wind_direction_10m,precipitation,cloud_cover"
    _EXTRA_PARAMS = {"wind_speed_unit": "kn"}

    def hour(self, lat: float, lon: float, when: datetime) -> Optional[HourlyWeather]:
        values = self._hour_values(lat, lon, when)
        if values is None:
            return None
        return HourlyWeather(
            wind_kn=values.get("wind_kn"),
            wind_deg=values.get("wind_deg"),
            precip_mm=values.get("precip_mm"),
            cloud_pct=values.get("cloud_pct"),
        )

    def _parse_hourly(self, hourly: dict) -> DayData:
        wind_kn = hourly_column(hourly, "wind_speed_10m")
        wind_deg = hourly_column(hourly, "wind_direction_10m")
        precip_mm = hourly_column(hourly, "precipitation")
        cloud_pct = hourly_column(hourly, "cloud_cover")
        return {
            t: {
                "wind_kn": value_at(wind_kn, i),
                "wind_deg": value_at(wind_deg, i),
                "precip_mm": value_at(precip_mm, i),
                "cloud_pct": value_at(cloud_pct, i),
            }
            for i, t in enumerate(hourly.get("time", []))
        }


class NoWeather:
    """Skips the online weather lookup -- no weather columns shown."""

    def hour(self, lat: float, lon: float, when: datetime) -> Optional[HourlyWeather]:
        return None
