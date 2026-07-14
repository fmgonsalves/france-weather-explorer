from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from typing import Literal


MAX_STATIONS = 5
MAX_CHART_POINTS = 100_000


class QueryLimitError(ValueError):
    """Raised when a dashboard query would return too many chart points."""


@dataclass(frozen=True)
class MetricSpec:
    mnemonic: str
    label: str
    unit: str
    quality: str
    daily_statistics: tuple[str, ...]
    default_daily_statistics: tuple[str, ...]


METRICS = {
    "T": MetricSpec("T", "Air temperature", "°C", "QT", ("average", "minimum", "maximum"), ("average",)),
    "TD": MetricSpec("TD", "Dew-point temperature", "°C", "QTD", ("average", "minimum", "maximum"), ("average",)),
    "U": MetricSpec("U", "Relative humidity", "%", "QU", ("average", "minimum", "maximum"), ("average",)),
    "RR1": MetricSpec(
        "RR1", "Hourly precipitation", "mm", "QRR1",
        ("total", "average", "minimum", "maximum"), ("total",),
    ),
    "FF": MetricSpec("FF", "Mean wind speed", "m/s", "QFF", ("average", "minimum", "maximum"), ("average",)),
    "PSTAT": MetricSpec("PSTAT", "Station pressure", "hPa", "QPSTAT", ("average", "minimum", "maximum"), ("average",)),
}


@dataclass(frozen=True)
class DashboardFilters:
    department: str
    metric: str
    stations: tuple[str, ...]
    start_date: date
    end_date: date
    time_basis: Literal["local", "utc"] = "local"
    resolution: Literal["hourly", "daily"] = "hourly"
    daily_statistics: tuple[str, ...] = ("average",)
    quality_mode: Literal["all", "exclude_doubtful"] = "all"
    exclude_selected_period_from_baseline: bool = True

    def __post_init__(self) -> None:
        if self.department != "44":
            raise ValueError("Dashboard v1 supports department 44 only")
        if self.metric not in METRICS:
            raise ValueError(f"Unsupported metric: {self.metric}")
        if not self.stations:
            raise ValueError("Select at least one station")
        if len(self.stations) > MAX_STATIONS:
            raise ValueError(f"Select at most {MAX_STATIONS} stations")
        if len(set(self.stations)) != len(self.stations):
            raise ValueError("Station selections must be unique")
        if self.start_date > self.end_date:
            raise ValueError("Start date cannot be later than end date")
        if self.time_basis not in {"local", "utc"}:
            raise ValueError("Time basis must be local or utc")
        if self.resolution not in {"hourly", "daily"}:
            raise ValueError("Resolution must be hourly or daily")
        if self.quality_mode not in {"all", "exclude_doubtful"}:
            raise ValueError("Unsupported quality mode")
        allowed = METRICS[self.metric].daily_statistics
        if not self.daily_statistics:
            raise ValueError("Select at least one daily statistic")
        invalid = [value for value in self.daily_statistics if value not in allowed]
        if invalid:
            raise ValueError(
                f"{', '.join(invalid)!r} is not valid for {self.metric}; choose {', '.join(allowed)}"
            )
        if len(set(self.daily_statistics)) != len(self.daily_statistics):
            raise ValueError("Daily statistics must be unique")

    @property
    def metric_spec(self) -> MetricSpec:
        return METRICS[self.metric]

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["stations"] = list(self.stations)
        payload["daily_statistics"] = list(self.daily_statistics)
        payload["start_date"] = self.start_date.isoformat()
        payload["end_date"] = self.end_date.isoformat()
        return payload

    @classmethod
    def from_dict(cls, payload: dict) -> "DashboardFilters":
        statistics = payload.get("daily_statistics", payload.get("daily_statistic", ["average"]))
        if isinstance(statistics, str):
            statistics = ["average" if statistics == "mean" else statistics]
        return cls(
            department=str(payload["department"]),
            metric=str(payload["metric"]),
            stations=tuple(str(value) for value in payload["stations"]),
            start_date=date.fromisoformat(payload["start_date"]),
            end_date=date.fromisoformat(payload["end_date"]),
            time_basis=payload.get("time_basis", "local"),
            resolution=payload.get("resolution", "hourly"),
            daily_statistics=tuple(statistics),
            quality_mode=payload.get("quality_mode", "all"),
            exclude_selected_period_from_baseline=bool(
                payload.get("exclude_selected_period_from_baseline", True)
            ),
        )
