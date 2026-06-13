"""Helpers for MCP Apps (``ui://``) interactive widgets.

FastMCP 3.3.1 implements the MCP Apps extension (``io.modelcontextprotocol/ui``).
The pattern is three pieces (mirroring the existing guide resources):

1. A ``ui://`` resource returning self-contained HTML (the widget bundle).
2. A tool that shapes data via ``run_select_query()`` and declares
   ``app=AppConfig(resource_uri="ui://...")`` so the host renders the widget.
3. The widget JS reads the tool's structured result and renders it.

These helpers load the widget HTML (shipped under ``resources/widgets/`` via the
existing force-include in ``pyproject.toml``) and build the ``AppConfig``
consistently, so each UI tool wires its ``_meta`` the same way.
"""

from __future__ import annotations

import logging
from importlib import resources as importlib_resources
from importlib.resources.abc import Traversable
from pathlib import Path

from fastmcp.apps import AppConfig

logger = logging.getLogger(__name__)

# ui:// resource URIs for the interactive apps.
SURVIVAL_UI_URI = "ui://cbioportal/survival"
ONCOPRINT_UI_URI = "ui://cbioportal/oncoprint"
# Generic, model-driven chart widgets (data supplied by the tool caller, not a DB
# query). One ui:// resource + AppConfig per chart type.
PIE_UI_URI = "ui://cbioportal/pie"
BAR_UI_URI = "ui://cbioportal/bar"
LINE_UI_URI = "ui://cbioportal/line"


def _widgets_path() -> Traversable:
    """Resources/widgets directory, for both installed packages and dev mode."""
    try:
        return importlib_resources.files("cbioportal_mcp") / "resources" / "widgets"
    except (TypeError, AttributeError):
        return Path(__file__).parent / "resources" / "widgets"


def load_widget(filename: str) -> str:
    """Load a self-contained widget HTML bundle from resources/widgets/."""
    try:
        return (_widgets_path() / filename).read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.error("Widget asset not found: %s", filename)
        return (
            "<!doctype html><html><head><meta charset='utf-8'></head>"
            f"<body><p>Widget asset not found: {filename}</p></body></html>"
        )
    except Exception as e:  # pragma: no cover - defensive
        logger.error("Error loading widget %s: %s", filename, e)
        return (
            "<!doctype html><html><head><meta charset='utf-8'></head>"
            "<body><p>Error loading widget.</p></body></html>"
        )


def survival_app_config() -> AppConfig:
    """AppConfig for the Kaplan-Meier survival tool.

    The widget is fully self-contained (inline SVG, no external scripts or
    network calls), so no CSP connect/resource domains are required — only the
    host<->iframe postMessage bridge is used. ``visibility=["model"]`` means the
    model invokes this entry-point tool; the host then renders the ui:// widget.
    """
    return AppConfig(
        resource_uri=SURVIVAL_UI_URI,
        visibility=["model"],
        prefers_border=True,
    )


def oncoprint_app_config() -> AppConfig:
    """AppConfig for the OncoPrint tool.

    The widget is fully self-contained (inline SVG, no external scripts or
    network calls), so no CSP connect/resource domains are required — only the
    host<->iframe postMessage bridge is used. ``visibility=["model"]`` means the
    model invokes this entry-point tool; the host then renders the ui:// widget.
    """
    return AppConfig(
        resource_uri=ONCOPRINT_UI_URI,
        visibility=["model"],
        prefers_border=True,
    )


def _chart_app_config(resource_uri: str) -> AppConfig:
    """AppConfig for a generic chart widget.

    Same wiring as the survival/oncoprint apps: a self-contained inline-SVG
    widget (no external scripts or network calls), so only the host<->iframe
    postMessage bridge is used. ``visibility=["model"]`` means the model invokes
    the chart tool and the host renders the linked ui:// widget.
    """
    return AppConfig(
        resource_uri=resource_uri,
        visibility=["model"],
        prefers_border=True,
    )


def pie_chart_app_config() -> AppConfig:
    """AppConfig for the generic pie/donut chart tool."""
    return _chart_app_config(PIE_UI_URI)


def bar_chart_app_config() -> AppConfig:
    """AppConfig for the generic bar chart tool."""
    return _chart_app_config(BAR_UI_URI)


def line_chart_app_config() -> AppConfig:
    """AppConfig for the generic line chart tool."""
    return _chart_app_config(LINE_UI_URI)
