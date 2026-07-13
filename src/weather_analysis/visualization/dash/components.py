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
