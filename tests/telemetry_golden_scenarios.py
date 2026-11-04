"""Telemetry scenarios whose output is pinned by tests/golden/.

Run against any checkout of the package, so the same scenarios can produce a
golden file from the base commit and be compared with the current code:

    PYTHONPATH=<checkout>/src python tests/telemetry_golden_scenarios.py > golden.json

Every scenario uses only default-provider (google) inputs: a FastMCP
AccessToken with Google's claim shape, LibreChat headers, or nothing. For
each it records the OTel span attributes of a tool call and a discovery
request, the arguments of the LLMObs tool span, and the DogStatsD packets for
tool and db_query metrics. Volatile values (durations) are dropped.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

from fastmcp.server.auth import AccessToken
from mcp.types import Implementation
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

GOOGLE_CLAIMS = {
    "sub": "109876543210",
    "email": "alice@example.org",
    "name": "Alice",
    "google_user_data": {
        "id": "109876543210",
        "email": "alice@example.org",
        "verified_email": True,
    },
}

SCENARIOS = {
    "google_oauth_token": {"claims": GOOGLE_CLAIMS, "headers": {}},
    "google_token_and_librechat_headers": {
        "claims": GOOGLE_CLAIMS,
        "headers": {"x-user-id": "librechat-1", "x-user-email": "other@example.org"},
    },
    "librechat_headers": {
        "claims": None,
        "headers": {"x-user-id": "librechat-1", "x-user-email": "bob@example.org"},
    },
    "librechat_b64_email": {
        "claims": None,
        "headers": {"x-user-id": "librechat-2", "x-user-email": "b64:Y2Fyb2xAZXhhbXBsZS5vcmc="},
    },
    "direct_anonymous": {"claims": None, "headers": {}},
    "forwarded_ip": {"claims": None, "headers": {"x-forwarded-for": "203.0.113.9, 10.0.0.1"}},
}

_VOLATILE = {"mcp.tool.duration_ms", "db.query.duration_ms"}


def _clear_env() -> None:
    for name in list(os.environ):
        if name.startswith(("CBIOPORTAL_MCP_", "DD_", "OTEL_")) or name == "REDIS_URL":
            del os.environ[name]
    os.environ["CBIOPORTAL_MCP_DD_METRICS_ENABLED"] = "true"
    import cbioportal_mcp.env as env

    env._MCP_CONFIG_INSTANCE = None


def run() -> dict:
    _clear_env()
    from cbioportal_mcp import telemetry

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    packets: list[str] = []
    statsd = telemetry._DogStatsDClient("127.0.0.1", 9, constant_tags={"service": "golden"})
    statsd._socket = SimpleNamespace(sendto=lambda data, _addr: packets.append(data.decode()))

    out: dict = {}
    for name, scenario in SCENARIOS.items():
        exporter.clear()
        packets.clear()
        llmobs_calls: list = []
        token = (
            AccessToken(token="t", client_id="c", scopes=[], claims=dict(scenario["claims"]))
            if scenario["claims"]
            else None
        )
        context = SimpleNamespace(
            message=SimpleNamespace(name="ping", arguments={"q": 1}),
            fastmcp_context=SimpleNamespace(
                session=SimpleNamespace(
                    client_params=SimpleNamespace(
                        clientInfo=Implementation(name="claude-code", version="1.2.3")
                    )
                ),
                session_id="session-golden",
            ),
        )

        async def ok(_context):
            return "pong"

        async def fail(_context):
            raise RuntimeError("boom")

        with (
            patch("fastmcp.server.dependencies.get_access_token", return_value=token),
            patch(
                "fastmcp.server.dependencies.get_http_headers",
                return_value=dict(scenario["headers"]),
            ),
            patch.object(telemetry, "_get_dogstatsd_client", return_value=statsd),
            patch.dict(sys.modules, _fake_llmobs_modules(llmobs_calls)),
        ):
            middleware = telemetry.TelemetryMiddleware()
            asyncio.run(middleware.on_call_tool(context, ok))
            try:
                asyncio.run(middleware.on_call_tool(context, fail))
            except RuntimeError:
                pass
            asyncio.run(middleware.on_list_tools(context, ok))
            with telemetry.traced_db_query("golden.label"):
                pass

        out[name] = {
            "spans": [
                {
                    "name": span.name,
                    "attributes": {
                        k: v for k, v in sorted(span.attributes.items()) if k not in _VOLATILE
                    },
                }
                for span in exporter.get_finished_spans()
            ],
            "llmobs": llmobs_calls,
            "metrics": [_strip_value(p) for p in packets],
        }
    return out


class _FakeLLMObsSpan:
    def __init__(self, calls: list, name: str, session_id) -> None:
        self._calls = calls
        calls.append(["tool", name, session_id])

    def set_tag(self, key, value):
        self._calls.append(["set_tag", key, value])

    def set_exc_info(self, exc_type, exc, _tb):
        self._calls.append(["set_exc_info", exc_type.__name__, str(exc)])

    def finish(self):
        self._calls.append(["finish"])


def _fake_llmobs_modules(calls: list) -> dict:
    """A stand-in ddtrace.llmobs recording exactly what the real tool span is given."""

    class LLMObs:
        enabled = True

        @staticmethod
        def tool(name, session_id=None):
            return _FakeLLMObsSpan(calls, name, session_id)

        @staticmethod
        def annotate(span=None, **kwargs):
            calls.append(["annotate", {k: v for k, v in sorted(kwargs.items())}])

    llmobs_module = SimpleNamespace(LLMObs=LLMObs)
    ddtrace_module = sys.modules.get("ddtrace") or SimpleNamespace(llmobs=llmobs_module)
    return {"ddtrace": ddtrace_module, "ddtrace.llmobs": llmobs_module}


def _strip_value(packet: str) -> str:
    """'cbioportal_mcp.tool.duration_ms:1.23|d|#...' -> drop the volatile value."""
    metric, rest = packet.split(":", 1)
    _value, remainder = rest.split("|", 1)
    if metric.endswith("duration_ms"):
        return f"{metric}:<v>|{remainder}"
    return packet


if __name__ == "__main__":
    json.dump(run(), sys.stdout, indent=1, sort_keys=True)
    sys.stdout.write("\n")
