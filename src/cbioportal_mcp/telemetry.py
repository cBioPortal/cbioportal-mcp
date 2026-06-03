"""OpenTelemetry configuration and FastMCP middleware for Datadog monitoring."""

from __future__ import annotations

import atexit
import logging
import os
from typing import Any

import mcp.types as mt
from fastmcp.server.middleware import Middleware, MiddlewareContext, CallNext
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource, SERVICE_NAME
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

logger = logging.getLogger(__name__)

_tracer_provider: TracerProvider | None = None


def configure_telemetry() -> TracerProvider | None:
    """Configure OpenTelemetry with OTLP gRPC exporter toward the Datadog node agent.

    Environment variables (in priority order):
      OTEL_EXPORTER_OTLP_ENDPOINT  Full gRPC endpoint URL, e.g. "http://1.2.3.4:4317"
      DD_AGENT_HOST                Node IP injected via Kubernetes Downward API
                                   (falls back to "localhost")
      OTEL_SERVICE_NAME            Service name reported to Datadog (default: "cbioportal-mcp")

    Returns the configured TracerProvider, or None if setup failed (server keeps running).
    """
    global _tracer_provider

    service_name = os.getenv("OTEL_SERVICE_NAME", "cbioportal-mcp")

    # OTEL_EXPORTER_OTLP_ENDPOINT takes precedence; otherwise derive from DD_AGENT_HOST.
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        agent_host = os.getenv("DD_AGENT_HOST", "localhost")
        endpoint = f"http://{agent_host}:4317"

    try:
        resource = Resource.create({SERVICE_NAME: service_name})
        exporter = OTLPSpanExporter(endpoint=endpoint)
        provider = TracerProvider(resource=resource)
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
        _tracer_provider = provider
        atexit.register(shutdown_telemetry)
        logger.info(
            "✅ OpenTelemetry configured: service=%s endpoint=%s", service_name, endpoint
        )
        return provider
    except Exception as exc:
        logger.warning("⚠️  Failed to configure OpenTelemetry (%s). Telemetry disabled.", exc)
        return None


def shutdown_telemetry() -> None:
    """Flush pending spans and shut down the tracer provider gracefully.

    Registered via atexit so it runs on SIGTERM→SystemExit during pod termination.
    """
    global _tracer_provider
    if _tracer_provider is not None:
        try:
            _tracer_provider.force_flush(timeout_millis=5000)
            _tracer_provider.shutdown()
            logger.info("OpenTelemetry tracer provider shut down.")
        except Exception as exc:
            logger.warning("Error during telemetry shutdown: %s", exc)
        finally:
            _tracer_provider = None


def _extract_client_ip() -> str | None:
    """Extract the original client IP from the active HTTP request.

    Reads X-Forwarded-For (populated by Traefik from the ELB/NLB) and returns
    the leftmost entry, which is the true client IP when trusted-proxy mode is
    configured on Traefik.  Falls back to the direct connection host.

    Returns None when no HTTP request context is active (e.g. stdio transport).
    """
    try:
        from fastmcp.server.dependencies import get_http_headers
        from fastmcp.server.http import _current_http_request

        headers = get_http_headers(include_all=True)
        forwarded_for = headers.get("x-forwarded-for", "").strip()
        if forwarded_for:
            # "client, proxy1, proxy2" — take the leftmost (original client)
            return forwarded_for.split(",")[0].strip()

        # Fallback: direct connection host
        request = _current_http_request.get()
        if request and request.client:
            return request.client.host
    except Exception:
        pass
    return None


class TelemetryMiddleware(Middleware):
    """FastMCP middleware that emits an OTel span for every MCP tool call.

    Span name : ``mcp.tool/<tool_name>``
    Attributes:
      mcp.tool.name       Tool name
      network.client.ip   Original client IP from X-Forwarded-For (HTTP only)
      mcp.tool.success    True on success, False when an exception propagates
      error.type          Exception class name on failure
    """

    def __init__(self) -> None:
        self._tracer = trace.get_tracer(__name__)

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, mt.CallToolResult],
    ) -> mt.CallToolResult:
        tool_name = getattr(context.message, "name", None) or "unknown"

        with self._tracer.start_as_current_span(f"mcp.tool/{tool_name}") as span:
            span.set_attribute("mcp.tool.name", tool_name)

            client_ip = _extract_client_ip()
            if client_ip:
                span.set_attribute("network.client.ip", client_ip)

            try:
                result = await call_next(context)
                span.set_attribute("mcp.tool.success", True)
                return result
            except Exception as exc:
                span.set_attribute("mcp.tool.success", False)
                span.set_attribute("error.type", type(exc).__name__)
                span.record_exception(exc)
                raise
