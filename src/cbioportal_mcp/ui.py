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
from pathlib import Path

try:  # importlib.resources became a package (gaining .abc) only in 3.11
    from importlib.resources.abc import Traversable
except ImportError:  # Python 3.10; deprecated from 3.12, hence the order
    from importlib.abc import Traversable


from fastmcp.apps import AppConfig
from fastmcp.apps.config import ResourceCSP

logger = logging.getLogger(__name__)

# ui:// resource URIs for the interactive apps.
SURVIVAL_UI_URI = "ui://cbioportal/survival"
ONCOPRINT_UI_URI = "ui://cbioportal/oncoprint"
LOLLIPOP_UI_URI = "ui://cbioportal/lollipop"
COOCCURRENCE_UI_URI = "ui://cbioportal/cooccurrence"
# Generic, model-driven chart widgets (data supplied by the tool caller, not a DB
# query). One ui:// resource + AppConfig per chart type.
PIE_UI_URI = "ui://cbioportal/pie"
BAR_UI_URI = "ui://cbioportal/bar"
LINE_UI_URI = "ui://cbioportal/line"

# The lollipop widget is the only app that talks to the network: it fetches the
# canonical transcript's protein length + Pfam domains live from Genome Nexus
# (react-mutation-mapper style). Its AppConfig declares this origin in the iframe
# CSP connect-src allowlist; every other widget is fully self-contained.
GENOME_NEXUS_ORIGIN = "https://www.genomenexus.org"


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


def app_config(resource_uri: str, connect_domains: list[str] | None = None) -> AppConfig:
    """AppConfig for a ui:// widget tool.

    Every widget uses the same host<->iframe postMessage bridge and
    ``visibility=["model"]`` (the model invokes the entry-point tool; the host
    renders the linked ui:// widget). ``connect_domains`` is only for a widget
    that reaches the network: currently just the lollipop, which fetches the
    gene's canonical-transcript protein length + Pfam domains live from Genome
    Nexus, so ``GENOME_NEXUS_ORIGIN`` must be in the iframe CSP ``connect-src``
    allowlist. Every other widget is fully self-contained (no CSP needed).
    """
    return AppConfig(
        resource_uri=resource_uri,
        visibility=["model"],
        prefers_border=True,
        csp=ResourceCSP(connect_domains=connect_domains) if connect_domains else None,
    )
