# Datadog dashboard & monitors: request origin / user activity

These definitions consume the span tags emitted by `TelemetryMiddleware`
(`src/cbioportal_mcp/telemetry.py`): `mcp.client.name`, `mcp.client.version`,
`mcp.client`, `mcp.session.id`, `enduser.id`, `network.client.ip`,
`mcp.tool.name`, `mcp.tool.success`.

They were written and JSON-validated locally but **not applied against a live
Datadog account** — this environment has no `DD_API_KEY`/`DD_APP_KEY`. Apply
and sanity-check the widget/monitor results once you have access.

## Apply

Dashboard:

```bash
curl -X POST "https://api.datadoghq.com/api/v1/dashboard" \
  -H "DD-API-KEY: ${DD_API_KEY}" \
  -H "DD-APPLICATION-KEY: ${DD_APP_KEY}" \
  -H "Content-Type: application/json" \
  -d @observability/datadog/dashboard.json
```

Monitors (one `POST` per object in the `monitors` array — strip the
`_comment` key and post each entry individually, or import them by hand via
Monitors > New Monitor > the JSON editor in the Datadog UI):

```bash
uv run python -c "
import json, urllib.request, os
data = json.load(open('observability/datadog/monitors.json'))
for m in data['monitors']:
    req = urllib.request.Request(
        'https://api.datadoghq.com/api/v1/monitor',
        data=json.dumps(m).encode(),
        headers={
            'DD-API-KEY': os.environ['DD_API_KEY'],
            'DD-APPLICATION-KEY': os.environ['DD_APP_KEY'],
            'Content-Type': 'application/json',
        },
    )
    print(urllib.request.urlopen(req).read())
"
```

## Before enabling

- **`env` template variable / search fragment**: the dashboard filters on a
  `$env` template variable and monitors search `service:cbioportal-mcp` with
  no env filter. If your Datadog Agent doesn't tag these OTLP spans with a
  unified-service-tagging `env` (check via Trace Explorer first), either set
  `deployment.environment` in `configure_telemetry()`'s `Resource.create(...)`
  or drop the `$env` variable from the dashboard queries.
- **Monitor thresholds** (20 errors/10m, 50 unattributed/30m) are starting
  guesses, not tuned against real traffic volume — adjust after a week of
  live data.
- **Notification targets** (`@pagerduty-...`, `@slack-...`) are placeholders
  — swap for real Datadog integration handles before enabling, or the
  monitors will fire silently.
- **"No requests" heartbeat** monitor will also fire during genuinely quiet
  periods (e.g. overnight with no LibreChat/connector traffic) — consider
  scoping its schedule or threshold if that's expected for this deployment.

## What this can't show you

Token usage per user is not obtainable from this server — it never calls an
LLM itself (pure ClickHouse tool server), so token accounting lives entirely
client-side (LibreChat's/Claude Code's/Codex's own usage, invisible here).
And for a fully anonymous direct connector (no auth proxy in front of it),
`mcp.session.id` counts distinct *sessions*, not distinct *people* — the same
person reconnecting looks like a new session. Closing that gap requires an
identity-injecting proxy in front of the deployment, not more span tags.
