"""Haalt één OpenStreetMap-tegel op die de hele reis dekt, voor een klein kaartplaatje-met-
routelijn ingebed in het ODS-logboek (zie ``ods_writer.py``). De tegel zelf wordt ongewijzigd
ingebed (geen pixelbewerking nodig); de routelijn wordt er als aparte vectorlijn overheen
getekend door ODF zelf. Alleen de standaardbibliotheek (``urllib``).

Vereist internet en respecteert het gebruiksbeleid van tile.openstreetmap.org (eigen
User-Agent, resultaten worden lokaal gecachet zodat dezelfde tegel niet steeds opnieuw wordt
opgehaald). Kan uitgezet worden met ``--no-route-thumbnails`` als je geen internet hebt of de
tegel-server niet wilt belasten; het logboek werkt dan gewoon door (route-kolom valt terug op
een gewone tekstlink)."""

from __future__ import annotations

import math
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from .tripbuilder import NavSample

TILE_SIZE = 256
DEFAULT_CACHE_DIR = Path(".map_tile_cache")
_MAX_DECIMATED_POINTS = 200
_USER_AGENT = "nmea2log (https://github.com/Ayuus/nmea2log; personal boat logbook tool)"
_TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"


def _lonlat_to_tile_xy(lon: float, lat: float, zoom: int) -> Tuple[float, float]:
    lat_rad = math.radians(max(min(lat, 85.05), -85.05))
    n = 2.0 ** zoom
    x = (lon + 180.0) / 360.0 * n
    y = (1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi) / 2.0 * n
    return x, y


@dataclass
class RouteThumbnail:
    png_bytes: bytes
    points_px: List[Tuple[float, float]]  # pixelposities binnen de 256x256-tegel


def _choose_tile(track: List[NavSample]) -> Tuple[int, int, int]:
    """Kiest de hoogste zoom waarbij de hele reis nog binnen 1 tegel (256x256) past."""
    lats = [s.lat for s in track]
    lons = [s.lon for s in track]
    lat_min, lat_max = min(lats), max(lats)
    lon_min, lon_max = min(lons), max(lons)

    zoom = 2
    tile_x = tile_y = 0
    for candidate in range(15, 1, -1):
        x0, y0 = _lonlat_to_tile_xy(lon_min, lat_max, candidate)
        x1, y1 = _lonlat_to_tile_xy(lon_max, lat_min, candidate)
        if int(x0) == int(x1) and int(y0) == int(y1):
            zoom, tile_x, tile_y = candidate, int(x0), int(y0)
            break
    else:
        x0, y0 = _lonlat_to_tile_xy(lon_min, lat_max, zoom)
        tile_x, tile_y = int(x0), int(y0)
    return zoom, tile_x, tile_y


def _tile_cache_path(cache_dir: Path, zoom: int, x: int, y: int) -> Path:
    return cache_dir / str(zoom) / str(x) / f"{y}.png"


def _fetch_tile(zoom: int, x: int, y: int, cache_dir: Path) -> Optional[bytes]:
    cache_path = _tile_cache_path(cache_dir, zoom, x, y)
    if cache_path.exists():
        return cache_path.read_bytes()

    url = _TILE_URL.format(z=zoom, x=x, y=y)
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            data = response.read()
    except (urllib.error.URLError, OSError):
        return None

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(data)
    return data


def build_route_thumbnail(
    track: List[NavSample], cache_dir: Path = DEFAULT_CACHE_DIR
) -> Optional[RouteThumbnail]:
    if not track:
        return None

    zoom, tile_x, tile_y = _choose_tile(track)
    png_bytes = _fetch_tile(zoom, tile_x, tile_y, cache_dir)
    if png_bytes is None:
        return None

    stride = max(1, len(track) // _MAX_DECIMATED_POINTS)
    decimated = track[::stride]
    if decimated[-1] is not track[-1]:
        decimated = decimated + [track[-1]]

    points_px = []
    for sample in decimated:
        x, y = _lonlat_to_tile_xy(sample.lon, sample.lat, zoom)
        points_px.append(((x - tile_x) * TILE_SIZE, (y - tile_y) * TILE_SIZE))

    return RouteThumbnail(png_bytes=png_bytes, points_px=points_px)
