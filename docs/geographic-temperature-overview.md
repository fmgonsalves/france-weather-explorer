# Geographic station temperature overview

The Department overview is a station-based exploratory map. It deliberately
does not assign one temperature to a department or region.

## Measured station values

The overview always uses air temperature (`T`) and applies the dashboard's
committed date range, local/UTC day basis, and quality setting. The department,
station selection, and selected metric do not limit this cross-department view.

For every station and civil or UTC day:

1. retain populated hourly `T` observations allowed by the quality setting;
2. require at least 18 valid hourly observations;
3. calculate that station-day's arithmetic mean; and
4. average all qualifying station-day means with equal daily weight.

The marker hover reports the station identity, department, altitude, period
mean, qualifying-day count, and valid-hour count. A station without a qualifying
day is omitted. Department outlines remain visible when a period has no data.

## Estimated surface

The optional surface uses inverse-distance weighting with power 2 on an
approximately 0.05-degree (roughly 5 km) grid. Each estimate uses at most the
eight nearest qualifying stations. A grid point is shown only when it lies
inside a materialized department and at least three stations are within 75 km.

The surface is an aid for seeing broad spatial patterns. It does not model
terrain, coastlines, exposure, observation uncertainty, or atmospheric physics,
and it must not be interpreted as an official gridded product. Exact station
markers use the same color scale and remain the authoritative values on the map.

## Administrative boundaries and reproducibility

The committed boundaries are the 2026 `100m` generalized GeoJSON release from
the French government's [Contours administratifs](https://www.data.gouv.fr/datasets/contours-administratifs)
dataset. The dataset is derived primarily from IGN Admin Express and published
under the Open Data Commons Open Database License (ODbL).

`data/reference/geography/source-metadata.json` records the dated download URLs,
file sizes, feature counts, join properties, and SHA-256 checksums. The loader
checks these hashes before using the files. Administrative codes are strings:
weather `department` joins to department `code`, and department `region` joins
to region `code`.

For an annual refresh, download both files from the same named release, validate
their feature properties and hashes, update the provenance metadata, run the
full test suite, and review a dashboard map before committing the three files.
