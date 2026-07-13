from __future__ import annotations

from pathlib import Path

from dash import Dash

from weather_analysis.queries.connection import validate_catalog

from .callbacks import register_callbacks
from .layout import build_layout


def create_app(analytics_dir: Path) -> Dash:
    analytics_dir = Path(analytics_dir).resolve()
    validate_catalog(analytics_dir)
    assets = Path(__file__).with_name("assets")
    app = Dash(
        __name__, assets_folder=str(assets), title="Loire-Atlantique Weather Explorer",
        update_title="Querying weather data…",
    )
    app.layout = build_layout(analytics_dir)
    register_callbacks(app, analytics_dir)
    return app


def run_dashboard(
    analytics_dir: Path,
    host: str = "127.0.0.1",
    port: int = 8050,
    debug: bool = False,
) -> None:
    app = create_app(analytics_dir)
    app.run(host=host, port=port, debug=debug)
