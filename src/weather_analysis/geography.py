from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import hashlib
import json
import math
from pathlib import Path
from typing import Iterable

import polars as pl


DEPARTMENT_FILE = "departments-100m.geojson"
REGION_FILE = "regions-100m.geojson"
METADATA_FILE = "source-metadata.json"
SURFACE_GRID_DEGREES = 0.05
SURFACE_MAX_NEIGHBORS = 8
SURFACE_CONFIDENCE_KM = 75.0
SURFACE_MIN_NEIGHBORS = 3


@dataclass(frozen=True)
class AdministrativeGeography:
    departments: dict
    regions: dict
    metadata: dict

    def department_geojson(self, codes: Iterable[str]) -> dict:
        selected = {str(code) for code in codes}
        return {
            "type": "FeatureCollection",
            "features": [
                feature
                for feature in self.departments["features"]
                if str(feature["properties"]["code"]) in selected
            ],
        }

    def region_for_department(self, code: str) -> str | None:
        for feature in self.departments["features"]:
            properties = feature["properties"]
            if str(properties["code"]) == str(code):
                return str(properties["region"])
        return None


def _load_feature_collection(path: Path, required_properties: set[str]) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise FileNotFoundError(f"Administrative geography file is missing: {path}") from None
    except json.JSONDecodeError as error:
        raise ValueError(f"Administrative geography file is invalid JSON: {path}: {error}") from error
    if payload.get("type") != "FeatureCollection" or not isinstance(payload.get("features"), list):
        raise ValueError(f"Administrative geography file is not a GeoJSON FeatureCollection: {path}")
    for feature in payload["features"]:
        properties = feature.get("properties") or {}
        missing = required_properties - properties.keys()
        if missing:
            raise ValueError(
                f"Administrative geography feature in {path} is missing: {', '.join(sorted(missing))}"
            )
        properties["code"] = str(properties["code"])
        if "region" in properties:
            properties["region"] = str(properties["region"])
        if (feature.get("geometry") or {}).get("type") not in {"Polygon", "MultiPolygon"}:
            raise ValueError(f"Administrative geography feature in {path} has unsupported geometry")
    return payload


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@lru_cache(maxsize=8)
def load_administrative_geography(directory: str | Path) -> AdministrativeGeography:
    root = Path(directory).resolve()
    metadata_path = root / METADATA_FILE
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise FileNotFoundError(f"Administrative geography metadata is missing: {metadata_path}") from None
    except json.JSONDecodeError as error:
        raise ValueError(f"Administrative geography metadata is invalid JSON: {error}") from error

    recorded = {
        str(item["path"]): str(item["sha256"])
        for item in metadata.get("files", [])
        if "path" in item and "sha256" in item
    }
    for filename in (DEPARTMENT_FILE, REGION_FILE):
        path = root / filename
        expected = recorded.get(filename)
        if expected is None:
            raise ValueError(f"Administrative geography metadata has no checksum for {filename}")
        actual = _sha256(path)
        if actual != expected:
            raise ValueError(
                f"Administrative geography checksum mismatch for {filename}: "
                f"expected {expected}, found {actual}"
            )

    departments = _load_feature_collection(
        root / DEPARTMENT_FILE, {"code", "nom", "region"}
    )
    regions = _load_feature_collection(root / REGION_FILE, {"code", "nom"})
    return AdministrativeGeography(departments, regions, metadata)


def _rings(geometry: dict) -> list[list[list[list[float]]]]:
    if geometry["type"] == "Polygon":
        return [geometry["coordinates"]]
    return geometry["coordinates"]


def _point_in_ring(longitude: float, latitude: float, ring: list[list[float]]) -> bool:
    inside = False
    previous = ring[-1]
    for current in ring:
        x1, y1 = previous[:2]
        x2, y2 = current[:2]
        if (y1 > latitude) != (y2 > latitude):
            crossing = (x2 - x1) * (latitude - y1) / (y2 - y1) + x1
            if longitude < crossing:
                inside = not inside
        previous = current
    return inside


def point_in_geometry(longitude: float, latitude: float, geometry: dict) -> bool:
    for polygon in _rings(geometry):
        if _point_in_ring(longitude, latitude, polygon[0]) and not any(
            _point_in_ring(longitude, latitude, hole) for hole in polygon[1:]
        ):
            return True
    return False


def _geometry_bounds(geometry: dict) -> tuple[float, float, float, float]:
    points = [point for polygon in _rings(geometry) for ring in polygon for point in ring]
    longitudes = [float(point[0]) for point in points]
    latitudes = [float(point[1]) for point in points]
    return min(longitudes), min(latitudes), max(longitudes), max(latitudes)


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371.0088
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    value = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    )
    return radius * 2 * math.atan2(math.sqrt(value), math.sqrt(1 - value))


def inverse_distance_temperature(
    latitude: float,
    longitude: float,
    stations: list[tuple[float, float, float]],
) -> float | None:
    distances = sorted(
        (
            _haversine_km(latitude, longitude, station_lat, station_lon),
            temperature,
        )
        for station_lat, station_lon, temperature in stations
    )
    if distances and distances[0][0] < 0.001:
        return distances[0][1]
    eligible = [item for item in distances if item[0] <= SURFACE_CONFIDENCE_KM]
    if len(eligible) < SURFACE_MIN_NEIGHBORS:
        return None
    nearest = eligible[:SURFACE_MAX_NEIGHBORS]
    weights = [1.0 / (distance * distance) for distance, _ in nearest]
    return sum(weight * value for weight, (_, value) in zip(weights, nearest)) / sum(weights)


def build_temperature_surface(
    stations: pl.DataFrame,
    geography: AdministrativeGeography,
    department_codes: Iterable[str],
    grid_degrees: float = SURFACE_GRID_DEGREES,
) -> pl.DataFrame:
    schema = {"LAT": pl.Float64, "LON": pl.Float64, "temperature": pl.Float64}
    if stations.is_empty():
        return pl.DataFrame(schema=schema)
    station_values = [
        (float(row["LAT"]), float(row["LON"]), float(row["period_mean_temperature"]))
        for row in stations.iter_rows(named=True)
        if row["LAT"] is not None
        and row["LON"] is not None
        and row["period_mean_temperature"] is not None
    ]
    if len(station_values) < SURFACE_MIN_NEIGHBORS:
        return pl.DataFrame(schema=schema)

    features = geography.department_geojson(department_codes)["features"]
    seen: set[tuple[int, int]] = set()
    rows: list[dict[str, float]] = []
    for feature in features:
        geometry = feature["geometry"]
        min_lon, min_lat, max_lon, max_lat = _geometry_bounds(geometry)
        lon_index_start = math.floor(min_lon / grid_degrees)
        lon_index_end = math.ceil(max_lon / grid_degrees)
        lat_index_start = math.floor(min_lat / grid_degrees)
        lat_index_end = math.ceil(max_lat / grid_degrees)
        for lat_index in range(lat_index_start, lat_index_end + 1):
            latitude = round(lat_index * grid_degrees, 6)
            for lon_index in range(lon_index_start, lon_index_end + 1):
                key = (lat_index, lon_index)
                if key in seen:
                    continue
                longitude = round(lon_index * grid_degrees, 6)
                if not point_in_geometry(longitude, latitude, geometry):
                    continue
                seen.add(key)
                temperature = inverse_distance_temperature(
                    latitude, longitude, station_values
                )
                if temperature is not None:
                    rows.append(
                        {"LAT": latitude, "LON": longitude, "temperature": temperature}
                    )
    return pl.DataFrame(rows, schema=schema)
