"""Small, dependency-free helpers for representative MLIT GeoJSON layers."""

from __future__ import annotations

import math
from typing import Any, Iterable

ZOOM = 15

REPRESENTATIVE_LAYERS: dict[int, dict[str, Any]] = {
    3: {
        "name": "地価公示・地価調査のポイント",
        "endpoint": "XPT002",
        "geometry": "point",
        "use": "price_reference",
    },
    4: {
        "name": "都市計画区域・区域区分",
        "endpoint": "XKT001",
        "geometry": "polygon",
        "use": "urban_planning",
    },
    5: {
        "name": "用途地域",
        "endpoint": "XKT002",
        "geometry": "polygon",
        "use": "zoning",
    },
    11: {
        "name": "医療機関",
        "endpoint": "XKT010",
        "geometry": "point",
        "use": "nearby_facilities",
    },
}


def latlon_to_tile_fraction(
    lat: float, lon: float, zoom: int = ZOOM
) -> tuple[int, int, float, float]:
    """Convert WGS84 latitude/longitude to Web Mercator tile coordinates."""
    if not -85.05112878 <= lat <= 85.05112878:
        raise ValueError("latはWeb Mercatorの範囲内で指定してください。")
    if not -180 <= lon <= 180:
        raise ValueError("lonは-180〜180の範囲で指定してください。")
    scale = 2**zoom
    lat_rad = math.radians(lat)
    x_float = scale * ((lon + 180) / 360)
    y_float = scale * (
        1 - (math.log(math.tan(lat_rad) + 1 / math.cos(lat_rad)) / math.pi)
    ) / 2
    x = math.floor(x_float)
    y = math.floor(y_float)
    return x, y, x_float - x, y_float - y


def surrounding_tiles(
    x: int, y: int, x_fraction: float, y_fraction: float
) -> list[tuple[int, int]]:
    """Return the center tile and the three nearest adjacent tiles."""
    x_delta = -1 if x_fraction < 0.5 else 1
    y_delta = -1 if y_fraction < 0.5 else 1
    return [(x, y), (x + x_delta, y), (x + x_delta, y + y_delta), (x, y + y_delta)]


def _haversine_meters(lat: float, lon: float, target_lat: float, target_lon: float) -> float:
    radius = 6_371_008.8
    lat1 = math.radians(lat)
    lat2 = math.radians(target_lat)
    delta_lat = lat2 - lat1
    delta_lon = math.radians(target_lon - lon)
    a = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2
    )
    return radius * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _point_in_ring(lon: float, lat: float, ring: list[list[float]]) -> bool:
    inside = False
    if len(ring) < 3:
        return False
    previous = ring[-1]
    for current in ring:
        x1, y1 = previous[:2]
        x2, y2 = current[:2]
        crosses = (y1 > lat) != (y2 > lat)
        if crosses:
            intersection = (x2 - x1) * (lat - y1) / (y2 - y1) + x1
            if lon < intersection:
                inside = not inside
        previous = current
    return inside


def _geometry_contains_point(geometry: dict[str, Any], lat: float, lon: float) -> bool:
    geometry_type = geometry.get("type")
    coordinates = geometry.get("coordinates")
    if not isinstance(coordinates, list):
        return False
    if geometry_type == "Polygon":
        return bool(coordinates and _point_in_ring(lon, lat, coordinates[0]))
    if geometry_type == "MultiPolygon":
        return any(
            polygon and _point_in_ring(lon, lat, polygon[0])
            for polygon in coordinates
        )
    return False


def filter_geojson_features(
    features: Iterable[dict[str, Any]],
    *,
    geometry_mode: str,
    lat: float,
    lon: float,
    distance_m: float,
) -> list[dict[str, Any]]:
    """Filter point layers by radius and polygon layers by point intersection."""
    matches: list[dict[str, Any]] = []
    for feature in features:
        geometry = feature.get("geometry")
        if not isinstance(geometry, dict):
            continue
        if geometry_mode == "point":
            coordinates = geometry.get("coordinates")
            if geometry.get("type") != "Point" or not isinstance(coordinates, list):
                continue
            try:
                target_lon = float(coordinates[0])
                target_lat = float(coordinates[1])
            except (IndexError, TypeError, ValueError):
                continue
            if _haversine_meters(lat, lon, target_lat, target_lon) <= distance_m:
                matches.append(feature)
        elif _geometry_contains_point(geometry, lat, lon):
            matches.append(feature)
    return matches
