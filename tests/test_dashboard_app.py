from __future__ import annotations

from weather_analysis.visualization.dash.app import create_app

from test_dashboard_queries import make_catalog


def test_dash_layout_and_callback_smoke(tmp_path):
    app = create_app(make_catalog(tmp_path))
    response = app.server.test_client().get("/")
    assert response.status_code == 200
    assert app.title == "Loire-Atlantique Weather Explorer"
    assert len(app.callback_map) >= 9
