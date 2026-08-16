# Potential Analysis Capabilities

This document collects possible analytical capabilities for the Météo-France
weather application. These are candidate features rather than committed work or
an implementation schedule. They are recorded here so that useful ideas and the
methodological questions behind them are not lost.

## Current foundation

The application currently provides:

- one-department-at-a-time exploration of materialized Météo-France data;
- station selection and map-based navigation;
- hourly and daily station time series;
- daily mean, minimum, maximum, and precipitation-total resampling;
- station-specific historical daily comparisons;
- coverage diagnostics and quality-code filtering;
- CSV export of the displayed analysis;
- partitioned analytical Parquet queried through read-only DuckDB views; and
- a Dash frontend whose query layer is independent of Dash.

The ideas below should continue to preserve station identity. Departmental
statistics should not be produced by naively pooling a changing set of stations.

## Climate context and anomalies

Add an analysis that compares a selected station and period with a defined
reference climatology.

Possible outputs include:

- daily, monthly, seasonal, and annual temperature anomalies;
- a selectable 1991–2020 or full-history reference period;
- percentile rank for each displayed day;
- calendar heatmaps of anomalies;
- record-high and record-low indicators;
- counts of unusually warm, cold, wet, or dry days; and
- a table of records approached or exceeded.

The reference period and minimum-data requirements must be stated explicitly.
Météo-France uses 1991–2020 for its current climate normals and calculates more
than simple averages, including distribution statistics, records, and threshold
counts.

## Weather-event detection

Identify continuous weather episodes rather than only isolated observations.

Candidate events include:

- hot spells and warm nights;
- frost periods;
- heavy-rainfall episodes;
- consecutive dry or wet days;
- strong-wind episodes; and
- unusually large day-to-day temperature changes.

An event result could contain its start, end, duration, peak value, accumulated
precipitation or degree-hours, contributing observations, and missing-data
warnings. Thresholds should initially be described as application-defined
analytical rules unless they implement a documented official definition.

## Long-term trends

For stations with sufficiently long and continuous records, provide:

- annual and seasonal mean temperature;
- annual and seasonal precipitation totals;
- frost-day, hot-day, and warm-night counts;
- rolling 5-, 10-, or 30-year summaries;
- comparisons between early and recent periods; and
- estimated trends with uncertainty intervals.

Station moves, metadata changes, missing years, and measurement continuity must
be visible. Trend estimates should remain station-specific until a defensible
spatial aggregation method has been selected.

## Station comparison

Extend multi-station exploration with explicit comparison tools:

- synchronized small multiples;
- the difference between two station series;
- scatterplots and correlation matrices;
- differences by hour, month, or season;
- elevation, coastal, inland, airport, and urban contrasts;
- overlap and missingness diagnostics; and
- markers for station name, coordinate, or altitude changes.

Comparisons should only use overlapping timestamps unless the analysis clearly
states another alignment rule.

## Seasonal-cycle analysis

Summarize the typical shape of the year or day rather than repeating a historical
envelope over selected dates.

Possible views include:

- mean, median, minimum, and maximum by day of year;
- percentile bands by calendar day;
- hour-of-day by month heatmaps;
- profiles split by decade or reference period;
- the current year overlaid on the historical seasonal cycle; and
- contributing-year and completeness counts.

Leap day, daylight-saving transitions, missing observations, and partial current
years need explicit handling.

## Precipitation analyses

Treat rainfall as an accumulation and event process rather than applying the same
summaries used for temperature.

Candidate capabilities include:

- daily, monthly, seasonal, and annual totals;
- running annual accumulation compared with historical years;
- maximum 1-hour and 24-hour totals;
- wet-day frequency;
- consecutive wet and dry days;
- rainfall-intensity distributions; and
- event duration and accumulated rainfall.

Return-period or frequency analysis may be useful later, but it requires a
separately reviewed statistical method and record-eligibility rules.

## Data quality and station readiness

Add an analysis focused on whether a station and period are suitable for another
calculation.

Useful outputs include:

- metric coverage by station, month, and year;
- missing-hour calendar heatmaps;
- quality-code distributions;
- station metadata changes;
- longest continuous record;
- years satisfying a configurable completeness threshold; and
- an eligibility explanation for trend, climatology, or event analyses.

This capability can make methodological exclusions visible instead of silently
removing observations.

## Cross-department comparison

Once enough departments are materialized, possible comparisons include:

- representative stations from multiple departments;
- ranked annual anomalies, rainfall totals, or threshold counts;
- department-by-month heatmaps;
- side-by-side seasonal profiles; and
- small-multiple station maps and trend charts.

Selecting representative stations is simpler and more transparent than claiming
to calculate a department-wide weather value. A true department statistic should
wait until stable-station rules, spatial weighting, altitude, and changing station
composition have been addressed.

## Derived comfort and environmental metrics

The hourly facts may support derived indicators such as:

- apparent-temperature or heat-index measures;
- wind chill;
- dew-point depression;
- humid or uncomfortable hour counts;
- heating and cooling degree days; and
- freeze-thaw cycles.

Each derived metric needs a documented formula, valid input range, unit handling,
and behavior when one of its required measurements is missing or doubtful.

## Supporting daily analytical dataset

Several candidates would benefit from a canonical station-day fact dataset rather
than repeatedly scanning and aggregating hourly observations.

A possible `station_daily` schema would include:

- department, network category, station identifier, and local date;
- daily mean, minimum, and maximum temperature;
- daily precipitation total;
- selected wind, humidity, and pressure summaries;
- contributing and expected hourly counts;
- completeness percentage;
- summarized quality information;
- source and processing lineage; and
- dataset and transformation versions.

It should be partitioned by department and year alongside the hourly dataset.
Parquet would remain canonical, with DuckDB views exposing the derived facts.

## Performance options

The existing department/year Parquet layout allows DuckDB to apply partition,
filter, and column projection pruning. Further options include:

- materializing reusable station-day facts;
- sorting output within partitions by station and observation time;
- reviewing Parquet row-group sizes for selective station/date queries;
- caching stable metadata and repeated historical summaries; and
- using background callbacks only for analyses that become genuinely slow.

Performance changes should follow measured query behavior rather than being added
preemptively.

## Methodological decisions shared by several ideas

Before publishing climatologies, trends, records, or department summaries, define:

- the reference period;
- minimum daily, monthly, and annual completeness;
- the treatment of quality codes;
- UTC versus Europe/Paris day boundaries;
- duplicate and daylight-saving-hour behavior;
- station metadata continuity rules;
- whether partial current periods are eligible;
- how leap day is handled;
- whether missing data may be interpolated; and
- whether any spatial aggregation is station-weighted, area-weighted, or based on
  a stable representative subset.

These rules should be versioned and included in exported results.

## Suggested first candidate

A strong first extension would be a **Climate context** analysis containing:

- station-level daily and monthly anomalies;
- a selectable 1991–2020 or full-history baseline;
- percentile bands;
- record indicators; and
- a calendar heatmap.

It builds directly on the existing historical-comparison query while establishing
the daily facts and completeness rules needed by many other candidates.

## References

- [Météo-France: new 1991–2020 climate normals](https://meteofrance.com/actualites-et-dossiers/actualites/climat/de-nouvelles-normales-pour-qualifier-le-climat-en-france)
- [DuckDB: reading and writing Parquet](https://duckdb.org/docs/stable/data/parquet/overview)
- [DuckDB: Parquet performance tips](https://duckdb.org/docs/stable/data/parquet/tips)
- [Dash: performance and caching](https://dash.plotly.com/performance)
