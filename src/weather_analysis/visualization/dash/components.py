from __future__ import annotations

from dash import html


def summary_card(label: str, component_id: str, value: str = "—"):
    return html.Div(
        [html.Div(label, className="summary-label"), html.Div(value, id=component_id, className="summary-value")],
        className="summary-card",
    )


def field(label: str, component, help_text: str | None = None):
    children = [html.Label(label, className="field-label"), component]
    if help_text:
        children.append(html.Div(help_text, className="field-help"))
    return html.Div(children, className="field")


def analysis_options_panel(
    description: str,
    controls: list,
    pending_id: str,
    apply_id: str,
    download_id: str,
):
    return html.Div([
        html.P(description, className="analysis-note"),
        html.Div(controls, className="analysis-options-fields"),
        html.Div(
            "Unapplied changes — click Apply changes to update this analysis",
            id=pending_id,
            className="pending-message is-hidden",
        ),
        html.Div([
            html.Button(
                "Apply changes", id=apply_id,
                className="button button-primary",
            ),
            html.Button(
                "Download displayed data", id=download_id,
                className="button",
            ),
        ], className="analysis-action-row"),
    ], className="analysis-options-panel")
