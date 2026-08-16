from __future__ import annotations

import json
from pathlib import Path

import polars as pl
import pytest

from weather_analysis.geography import (
    AdministrativeGeography,
    build_temperature_surface,
    inverse_distance_temperature,
    load_administrative_geography,
    point_in_geometry,
)


ROOT = Path(__file__).parents[1]


def square_geography() -> AdministrativeGeography:
    geometry = {
        "type": "Polygon",
        "coordinates": [[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0],
                         [0.0, 1.0], [0.0, 0.0]]],
    }
    return AdministrativeGeography(
        {
            "type": "FeatureCollection",
            "features": [{
                "type": "Feature",
                "properties": {"code": "01", "nom": "Test", "region": "84"},
                "geometry": geometry,
            }],
        },
        {"type": "FeatureCollection", "features": []},
        {},
    )


def test_committed_geography_and_provenance_are_valid():
    root = ROOT / "data/reference/geography"
    geography = load_administrative_geography(root)
    assert len(geography.departments["features"]) == 109
    assert len(geography.regions["features"]) == 26
    assert geography.region_for_department("01") == "84"
    assert geography.region_for_department("2A") == "94"
    assert geography.region_for_department("44") == "52"
    metadata = json.loads((root / "source-metadata.json").read_text())
    assert metadata["release_selector"] == "2026"
    assert all("/2026/geojson/" in item["source_url"] for item in metadata["files"])


def test_loader_reports_missing_geography(tmp_path):
    with pytest.raises(FileNotFoundError, match="metadata is missing"):
        load_administrative_geography(tmp_path)


def test_point_mask_and_confidence_limited_interpolation():
    geography = square_geography()
    geometry = geography.departments["features"][0]["geometry"]
    assert point_in_geometry(0.5, 0.5, geometry)
    assert not point_in_geometry(1.5, 0.5, geometry)

    stations = pl.DataFrame({
        "LAT": [0.45, 0.5, 0.55], "LON": [0.45, 0.55, 0.5],
        "period_mean_temperature": [10.0, 12.0, 14.0],
    })
    surface = build_temperature_surface(
        stations, geography, ("01",), grid_degrees=0.25
    )
    assert not surface.is_empty()
    assert all(0.0 <= value <= 1.0 for value in surface["LAT"])
    assert all(0.0 <= value <= 1.0 for value in surface["LON"])
    exact = inverse_distance_temperature(
        0.45, 0.45, [(0.45, 0.45, 10.0), (0.5, 0.55, 12.0), (0.55, 0.5, 14.0)]
    )
    assert exact == 10.0
    assert inverse_distance_temperature(5.0, 5.0, [
        (0.45, 0.45, 10.0), (0.5, 0.55, 12.0), (0.55, 0.5, 14.0),
    ]) is None
