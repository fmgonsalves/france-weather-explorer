# Weather Analysis

Tools for downloading and analyzing Météo-France weather observations.

## Météo-France hourly archives

Inspect the main hourly archive without changing local files:

```bash
uv run weather-analysis meteo-france status
```

Download only missing, partial, or size-mismatched resources:

```bash
uv run weather-analysis meteo-france download
```

Filter resources by department and overlapping publication period:

```bash
uv run weather-analysis meteo-france status \
  --department 44 \
  --department 75 \
  --start-year 2010 \
  --end-year 2025
```

Available shared options are:

- `--category main|complementary`
- repeatable `--department CODE`
- `--start-year YEAR`
- `--end-year YEAR`
- `--output-dir PATH`

The default main archive directory is `data/meteo_france/hourly_raw`. The
default complementary archive directory is
`data/meteo_france/hourly_complementary`.

Downloads are first written to a `.part` file. Existing partial content is
overwritten, and the file is renamed to `.csv.gz` only after its size matches
the storage server's HTTP `Content-Length` (or the data.gouv.fr catalog size
when that header is unavailable). Catalog/server discrepancies are reported
without rejecting an otherwise valid download. Neither command extracts
archive contents.

> **Current update-detection limitation:** archive freshness is determined by
> byte size. If Météo-France replaces a remote archive with different contents
> but exactly the same size, the existing local file will still be classified
> as complete and will not be downloaded again. Reliable recurring refreshes
> should add an ETag, `Last-Modified`, checksum, or explicit forced-refresh
> mechanism.

When a local size differs from the catalog, `status` uses a read-only HTTP
`HEAD` request to verify it against the storage server. `download` removes a
redundant `.part` copy only after the corresponding final archive is verified.

`status` and `download` return a nonzero exit code if selected resources remain
missing, partial, or invalid. A catalog request failure also returns nonzero.

## Inspecting compressed archives

Inspect headers and group schemas without extracting the gzip archives:

```bash
uv run weather-analysis meteo-france inspect --mode schema
```

Stream every row to build archive-level counts and coverage:

```bash
uv run weather-analysis meteo-france inspect --mode inventory
```

Profile the smallest representative archive from every distinct schema:

```bash
uv run weather-analysis meteo-france inspect --mode profile
```

Run a complete row-level exploration for exactly one department:

```bash
uv run weather-analysis meteo-france inspect \
  --mode explore \
  --department 44 \
  --report-dir data/meteo_france/metadata/department=44
```

Department exploration reports station continuity, missingness, quality-code
distributions, numeric ranges, duplicates, archive overlaps, metadata conflicts,
and full-year temperature coverage. It remains read-only with respect to source
archives and cleans its derived temporary key batches after successful report
generation.

Inspection accepts the same category, department, year, and input-directory
filters as the download commands, plus `--report-dir` and `--workers`. Reports
default to `data/meteo_france/metadata`. Only files ending exactly in
`.csv.gz` are read; `.part` files are ignored and no CSV is extracted.

## Analytical Parquet dataset

Preview local source changes without writing:

```bash
uv run weather-analysis meteo-france dataset status --department 44
```

Summarize every materialized department and the shared catalog without selecting
one department:

```bash
uv run weather-analysis meteo-france dataset status
```

The global view lists only departments needing attention by default. Add
`--verbose` to show every materialized department, and
`--include-unmaterialized` to list downloaded raw departments which have not yet
been transformed. Raw-only departments are informational and do not make the
dataset unhealthy.

Create missing partitions or atomically rebuild only years affected by changed
source archives:

```bash
uv run weather-analysis meteo-france dataset sync --department 44
```

Synchronization reports each processing stage and, in an interactive terminal,
shows archive progress plus processed-row throughput. Use `--no-progress` for
quiet scripts and automation.

Shared department and source-file metadata is assembled from every department
manifest, so synchronizing one department preserves the others. Check this
global metadata alongside a department's raw-file status with `dataset status`.
Normal status output reports department counts; add `--verbose` to display the
complete expected and observed department lists. Inconsistencies always print
the missing or unexpected department codes.
If status reports metadata drift, repair only the shared metadata—without
reprocessing weather observations—with:

```bash
uv run weather-analysis meteo-france dataset repair-metadata
```

The repair validates manifest, fact, and station-dimension department sets,
stages deterministic replacements, refreshes DuckDB, and rolls back on failure.
It also writes a compact audit report under
`data/meteo_france/analytics/v1/metadata/repairs`.

Curated schema changes require an explicit rebuild:

```bash
uv run weather-analysis meteo-france dataset rebuild \
  --department 44 \
  --schema-version 1
```

The canonical output is under `data/meteo_france/analytics/v1`. Hourly core and
sparse surface, marine, and snow facts are Zstandard-compressed Parquet,
partitioned by department and UTC observation year. Daily and departmental
weather aggregates are intentionally deferred.

The persistent DuckDB catalog contains views over Parquet:

```python
import duckdb

connection = duckdb.connect(
    "data/meteo_france/analytics/v1/weather.duckdb",
    read_only=True,
)
connection.execute("SET TimeZone='UTC'")

temperatures = connection.execute("""
    SELECT
        observation_time_utc,
        local_date,
        local_hour,
        NUM_POSTE,
        NOM_USUEL,
        T,
        QT
    FROM hourly_core
    WHERE department = '44'
      AND year = 2025
      AND NUM_POSTE = '44020001'
    ORDER BY observation_time_utc
""").fetchdf()
```

DuckDB `TIMESTAMPTZ` values display in the connection's configured timezone.
Set `TimeZone='UTC'` when displaying `observation_time_utc`; use the persisted
`local_date`, `local_hour`, offset, and DST fields for civil-time analysis.

Available catalog views are `hourly_core`, `hourly_surface`, `hourly_marine`,
`hourly_snow`, `stations`, `station_metadata_history`, `metrics`, `departments`,
`source_files`, and `processing_runs`.

## Department 44 dashboard

Run the local Dash application over the analytical DuckDB catalog:

```bash
uv run weather-analysis dashboard dash
```

The dashboard defaults to department 44, Nantes-Bouguenais, and the latest
complete calendar year (2025). It provides station maps, hourly or daily time
series, coverage diagnostics, quality-code filtering, and CSV export for `T`,
`TD`, `U`, `RR1`, `FF`, and `PSTAT`. The selected date range is always plotted
consistently; it does not switch to a different profile when the range exceeds
one year. A separate historical-comparison tab aligns each selected station's
daily values with its complete record by calendar day. It shows absolute and
average historical min–max envelopes for most metrics, and a daily-total range
and average for precipitation. The selected dates are excluded from that
baseline by default and can be included from the tab-specific control.

Use a different local address, port, or analytical directory with:

```bash
uv run weather-analysis dashboard dash \
  --host 127.0.0.1 \
  --port 8050 \
  --analytics-dir data/meteo_france/analytics/v1
```

The dashboard opens `weather.duckdb` read-only. Restart it after synchronizing
the analytical dataset so its cached station and metric metadata are refreshed.
