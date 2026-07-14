from __future__ import annotations

from datetime import date

import plotly.graph_objects as go
import polars as pl
from plotly.subplots import make_subplots

from weather_analysis.queries.models import DashboardFilters


COLORS = ["#155eef", "#d92d20", "#039855", "#7a5af8", "#dc6803"]


def empty_figure(message: str, height: int = 360) -> go.Figure:
    figure = go.Figure()
    figure.add_annotation(text=message, x=0.5, y=0.5, xref="paper", yref="paper", showarrow=False)
    figure.update_layout(template="plotly_white", height=height, margin=dict(l=30, r=20, t=50, b=30))
    return figure


def _base_layout(figure: go.Figure, title: str, height: int = 420) -> go.Figure:
    figure.update_layout(
        template="plotly_white", title=title, height=height, hovermode="x unified",
        margin=dict(l=50, r=24, t=60, b=45), legend_title_text="Station",
        font=dict(family="Inter, system-ui, sans-serif", color="#344054"),
    )
    return figure


def time_series_figure(frame: pl.DataFrame, filters: DashboardFilters) -> go.Figure:
    if frame.is_empty():
        return empty_figure("No observations match these filters")
    figure = go.Figure()
    x_column = "observation_time" if filters.resolution == "hourly" else "observation_date"
    for index, station in enumerate(frame.partition_by("NUM_POSTE", maintain_order=True)):
        station_id = station["NUM_POSTE"][0]
        name = station["NOM_USUEL"][0]
        color = COLORS[index % len(COLORS)]
        if filters.resolution == "hourly":
            quality = station["quality_code"].to_list()
            count = station["contributing_count"].to_list()
            custom = [[station_id, quality[i], count[i]] for i in range(station.height)]
            figure.add_trace(go.Scattergl(
                x=station[x_column].to_list(), y=station["value"].to_list(), mode="lines",
                name=f"{name} ({station_id})", line=dict(color=color, width=1.4),
                connectgaps=False, customdata=custom,
                hovertemplate=(
                    "%{x}<br>%{y:.2f} " + filters.metric_spec.unit +
                    "<br>Station: %{customdata[0]}<br>Quality: %{customdata[1]}" +
                    "<br>Contributing: %{customdata[2]}<extra></extra>"
                ),
            ))
            continue

        statistics = set(station["statistic"].to_list())
        if {"minimum", "maximum"}.issubset(statistics):
            minimum = station.filter(pl.col("statistic") == "minimum")
            maximum = station.filter(pl.col("statistic") == "maximum")
            figure.add_trace(go.Scatter(
                x=minimum[x_column].to_list(), y=minimum["value"].to_list(), mode="lines",
                line=dict(width=0), showlegend=False, hoverinfo="skip",
                legendgroup=station_id,
            ))
            figure.add_trace(go.Scatter(
                x=maximum[x_column].to_list(), y=maximum["value"].to_list(), mode="lines",
                line=dict(width=0), fill="tonexty", fillcolor=_rgba(color, 0.12),
                showlegend=False, hoverinfo="skip", legendgroup=station_id,
            ))

        styles = {
            "average": ("Average", "solid", 2.4),
            "minimum": ("Minimum", "dot", 1.5),
            "maximum": ("Maximum", "dash", 1.5),
            "total": ("Total", "solid", 2.4),
        }
        for statistic in filters.daily_statistics:
            values = station.filter(pl.col("statistic") == statistic)
            if values.is_empty():
                continue
            label, dash, width = styles[statistic]
            custom = [[station_id, label, count] for count in values["contributing_count"]]
            figure.add_trace(go.Scatter(
                x=values[x_column].to_list(), y=values["value"].to_list(), mode="lines",
                name=f"{name} — {label}", line=dict(color=color, dash=dash, width=width),
                connectgaps=False, customdata=custom, legendgroup=station_id,
                hovertemplate=(
                    "%{x}<br>%{y:.2f} " + filters.metric_spec.unit +
                    "<br>Station: %{customdata[0]}<br>Statistic: %{customdata[1]}" +
                    "<br>Contributing hours: %{customdata[2]}<extra></extra>"
                ),
            ))
    title = f"{filters.metric_spec.label} time series"
    if filters.resolution == "daily":
        title += " — daily resampling"
    _base_layout(figure, title)
    figure.update_yaxes(title=f"{filters.metric_spec.label} ({filters.metric_spec.unit})")
    figure.update_xaxes(rangeslider_visible=True)
    return figure


def _rgba(hex_color: str, alpha: float) -> str:
    value = hex_color.lstrip("#")
    red, green, blue = (int(value[index:index + 2], 16) for index in (0, 2, 4))
    return f"rgba({red},{green},{blue},{alpha})"


def historical_comparison_figure(
    frame: pl.DataFrame, filters: DashboardFilters
) -> go.Figure:
    if frame.is_empty() or frame["absolute_minimum"].null_count() == frame.height:
        return empty_figure("No historical baseline remains for this selection")
    stations = frame.partition_by("NUM_POSTE", maintain_order=True)
    titles = [f"{part['NOM_USUEL'][0]} ({part['NUM_POSTE'][0]})" for part in stations]
    figure = make_subplots(
        rows=len(stations), cols=1, shared_xaxes=True, shared_yaxes=True,
        vertical_spacing=min(0.08, 0.18 / max(1, len(stations))), subplot_titles=titles,
    )
    outer = "rgba(71,84,103,0.12)"
    inner = "rgba(21,94,239,0.20)"
    for row, station in enumerate(stations, start=1):
        dates = station["observation_date"].to_list()
        show_legend = row == 1
        band_custom = [
            [
                item["absolute_minimum"], item["absolute_maximum"],
                item["average_minimum"], item["average_maximum"],
            ]
            for item in station.iter_rows(named=True)
        ]
        figure.add_trace(go.Scatter(
            x=dates, y=station["absolute_minimum"].to_list(), mode="lines",
            line=dict(width=0), showlegend=False, hoverinfo="skip", legendgroup="absolute",
        ), row=row, col=1)
        figure.add_trace(go.Scatter(
            x=dates, y=station["absolute_maximum"].to_list(), mode="lines",
            line=dict(width=0), fill="tonexty", fillcolor=outer,
            name="Historical absolute range", showlegend=show_legend,
            legendgroup="absolute", customdata=band_custom,
            hovertemplate=(
                "Absolute range: %{customdata[0]:.2f}–%{customdata[1]:.2f}"
                "<extra></extra>"
            ),
        ), row=row, col=1)

        if filters.metric == "RR1":
            reference = station["historical_average_total"].to_list()
            figure.add_trace(go.Scatter(
                x=dates, y=reference, mode="lines", name="Historical average daily total",
                line=dict(color="#667085", width=1.8, dash="dash"),
                showlegend=show_legend, legendgroup="historical-average",
                customdata=band_custom,
                hovertemplate=(
                    "Historical average total: %{y:.2f} " + filters.metric_spec.unit +
                    "<extra></extra>"
                ),
            ), row=row, col=1)
            _add_selected_line(
                figure, row, dates, station, "selected_total", "Selected daily total",
                "#155eef", "solid", 2.6, show_legend, filters,
            )
        else:
            figure.add_trace(go.Scatter(
                x=dates, y=station["average_minimum"].to_list(), mode="lines",
                line=dict(width=0), showlegend=False, hoverinfo="skip", legendgroup="average-range",
            ), row=row, col=1)
            figure.add_trace(go.Scatter(
                x=dates, y=station["average_maximum"].to_list(), mode="lines",
                line=dict(width=0), fill="tonexty", fillcolor=inner,
                name="Historical average min–max", showlegend=show_legend,
                legendgroup="average-range", customdata=band_custom,
                hovertemplate=(
                    "Average min–max: %{customdata[2]:.2f}–%{customdata[3]:.2f}"
                    "<extra></extra>"
                ),
            ), row=row, col=1)
            _add_selected_line(
                figure, row, dates, station, "selected_average", "Selected daily average",
                "#101828", "solid", 2.6, show_legend, filters,
            )
            _add_selected_line(
                figure, row, dates, station, "selected_minimum", "Selected daily minimum",
                "#039855", "dot", 1.7, show_legend, filters,
            )
            _add_selected_line(
                figure, row, dates, station, "selected_maximum", "Selected daily maximum",
                "#dc6803", "dash", 1.7, show_legend, filters,
            )
        _add_hover_metadata(figure, row, dates, station)

    baseline = "excluding" if filters.exclude_selected_period_from_baseline else "including"
    figure.update_layout(
        template="plotly_white",
        title=f"Historical daily comparison — baseline {baseline} selected period",
        height=max(480, 290 * len(stations)), hovermode="x unified",
        margin=dict(l=60, r=30, t=85, b=50), legend=dict(traceorder="normal"),
        font=dict(family="Inter, system-ui, sans-serif", color="#344054"),
    )
    figure.update_yaxes(title_text=filters.metric_spec.unit, matches="y")
    figure.update_xaxes(showgrid=True)
    return figure


def _add_selected_line(
    figure: go.Figure, row: int, dates: list, station: pl.DataFrame,
    column: str, label: str, color: str, dash: str, width: float,
    show_legend: bool, filters: DashboardFilters,
) -> None:
    figure.add_trace(go.Scatter(
        x=dates, y=station[column].to_list(), mode="lines", name=label,
        line=dict(color=color, dash=dash, width=width), connectgaps=False,
        showlegend=show_legend, legendgroup=column,
        hovertemplate=(
            label + ": %{y:.2f} " + filters.metric_spec.unit + "<extra></extra>"
        ),
    ), row=row, col=1)


def _add_hover_metadata(
    figure: go.Figure, row: int, dates: list, station: pl.DataFrame
) -> None:
    values = station.iter_rows(named=True)
    metadata = [
        [item["selected_contributing_hours"], item["historical_years"]]
        for item in values
    ]
    anchors = [
        (minimum + maximum) / 2
        if minimum is not None and maximum is not None
        else minimum if minimum is not None else maximum
        for minimum, maximum in zip(
            station["absolute_minimum"].to_list(), station["absolute_maximum"].to_list()
        )
    ]
    figure.add_trace(go.Scatter(
        x=dates, y=anchors, mode="lines", line=dict(width=0, color="rgba(0,0,0,0)"),
        showlegend=False, hoverinfo="all", customdata=metadata,
        hovertemplate=(
            "<span style='font-size:10px'><i>Selected hours: %{customdata[0]} · "
            "Historical years: %{customdata[1]}</i></span><extra></extra>"
        ),
    ), row=row, col=1)


def coverage_heatmap_figure(frame: pl.DataFrame, station_label: str, metric: str) -> go.Figure:
    if frame.is_empty():
        return empty_figure("No coverage information", 330)
    dates = frame["observation_date"].unique(maintain_order=True).to_list()
    lookup = {
        (row["observation_date"], row["observation_hour"]): row["observation_count"]
        for row in frame.iter_rows(named=True)
    }
    z = [[lookup.get((day, hour), 0) for day in dates] for hour in range(24)]
    figure = go.Figure(go.Heatmap(
        x=dates, y=list(range(24)), z=z, zmin=0, zmax=2,
        colorscale=[[0, "#f2f4f7"], [0.5, "#12b76a"], [1, "#7a5af8"]],
        colorbar=dict(title="Count", tickvals=[0, 1, 2]),
        hovertemplate="%{x}<br>Hour %{y}:00<br>%{z} observation(s)<extra></extra>",
    ))
    _base_layout(figure, f"{metric} hourly coverage — {station_label}", 330)
    figure.update_yaxes(title="Hour", dtick=3)
    return figure


def station_map_figure(frame: pl.DataFrame, selected: tuple[str, ...]) -> go.Figure:
    if frame.is_empty():
        return empty_figure("No station locations are available", 390)
    figure = go.Figure()
    for active, label in ((False, "Historical / outside period"), (True, "Active in period")):
        subset = frame.filter(pl.col("active_in_period") == active)
        if subset.is_empty():
            continue
        custom = [
            [row["NUM_POSTE"], row["NOM_USUEL"], row["ALTI"], row["first_observation_utc"],
             row["last_observation_utc"], row["coverage"]]
            for row in subset.iter_rows(named=True)
        ]
        sizes = [14 if station in selected else (10 if active else 7) for station in subset["NUM_POSTE"]]
        colors = subset["coverage"].to_list() if active else [0] * subset.height
        figure.add_trace(go.Scattermap(
            lat=subset["LAT"].to_list(), lon=subset["LON"].to_list(), mode="markers",
            name=label, customdata=custom,
            marker=dict(size=sizes, color=colors, colorscale="Blues", cmin=0, cmax=100,
                        opacity=0.9 if active else 0.35, showscale=active,
                        colorbar=dict(title="Coverage %")),
            hovertemplate=("%{customdata[1]} (%{customdata[0]})<br>Altitude: %{customdata[2]} m"
                           "<br>First: %{customdata[3]}<br>Last: %{customdata[4]}"
                           "<br>Coverage: %{customdata[5]:.1f}%<extra></extra>"),
        ))
    figure.update_layout(
        template="plotly_white", title="Station network", height=390,
        map=dict(style="carto-positron", center=dict(lat=47.28, lon=-1.68), zoom=7.2),
        margin=dict(l=0, r=0, t=50, b=0), legend=dict(orientation="h"),
    )
    return figure
