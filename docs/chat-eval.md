# Chat eval (behavioral regression CI)

`.github/workflows/chat-eval.yml` checks whether a change to this server makes the chat's answers worse. The chat
(chat.cbioportal.org) is a LibreChat agent that calls this server with `serverInstructions` on. That means
`src/cbioportal_mcp/resources/system-prompt.md`, which the server returns as its MCP `instructions`, is part of
the chat's system prompt, next to the tool descriptions and tool results. A change there can break no tool call
and raise no error and still degrade the answers, for example a more compact result format or new tools. The
tool-call regression test (`mcp-regression.yml`) can't see that. This eval can.

## What it does

The reusable workflow in [cbioportal-mcp-qa](https://github.com/cBioPortal/cbioportal-mcp-qa/blob/main/.github/workflows/chat-eval.yml):

1. Builds this PR's image (`docker/Dockerfile`) and pulls `cbioportal/mcp:latest` (the baseline). It starts both
   with the same ClickHouse credentials, HTTP transport, at `/db/mcp`.
2. Asks the 26 subset questions (`input/questions-subset.yaml`, all four tracks) through each server, 2 repeats
   by default. Baseline and candidate run in parallel in the same job, on the same data. The agent is Claude
   through the Anthropic SDK (Haiku 4.5 by default, the model prod's chat runs, thinking off). Its system prompt
   is the `CHAT_SYSTEM_PROMPT` secret, if set, followed by each server's served instructions, in LibreChat's
   order. The public navigator MCP is shared by both runs.
3. Grades every answer with an LLM judge (Sonnet 4.6, temperature 0) against the reference answers. It also runs
   objective checks: a number present in the answer, and whether each study id in its links exists.
4. Fails the check when the candidate is worse than the baseline:
   - its pass rate is more than **10 points** lower (failed requests and ungraded answers count as not passed);
   - it has more than **8** more tool errors;
   - it links more than **1** more distinct nonexistent study id.

   The job summary lists pass rates per run, per track and per repeat, and every question that regressed, with
   the judge's reason. Both runs (run.json, HTML report, transcripts) are uploaded as the `chat-eval-*` artifact.

The gate also refuses to pass runs that aren't comparable. Both runs must match in model, provider, judge (for
every grade), repeats, agent prompt hash and navigator, and in every question's text, history and references.
Only this server may differ. The gate likewise refuses runs where most turns failed.

**Cost caps and secrets.** Inputs are validated before anything runs: repeats 1–5, concurrency 1–6, at most
`max-questions` (default 30, ≤60) questions and `max-answers` (default 130, ≤300) answers per server, checked
before any model call. Paths must be repo-relative. `CHAT_SYSTEM_PROMPT` is never written to the results, only
its hash. Logs, results and the summary are scrubbed of the prompt, ClickHouse credentials, API key and AWS
credentials before they are printed, shown or uploaded, and nothing is uploaded if scrubbing fails. The scrubber also catches escaped, base64 and line-wrapped forms of each value. JSON results are scrubbed by structure: any string holding a secret under stacked encodings is replaced whole, and numbers and booleans are left alone. The servers' logs are scrubbed too. Every `CLICKHOUSE_*` value it is given must be at least 4 characters, or the upload is refused.

It runs on PRs to `main` that touch `src/`, `resources/`, `sql/`, `docker/`, `pyproject.toml` or `uv.lock`. It
also runs on any PR when it gets the `mcp-behavior-change` label, nightly (07:17 UTC), and from **Actions → Chat
eval → Run workflow** (repeats, model, threshold and baseline image are inputs). A new push to a PR cancels the
eval of the commit it supersedes.

## Repository secrets

| Secret | Required | What |
|---|---|---|
| `CLICKHOUSE_HOST`, `CLICKHOUSE_PORT`, `CLICKHOUSE_USER`, `CLICKHOUSE_PASSWORD`, `CLICKHOUSE_DATABASE` | yes | The read-only ClickHouse user the regression test already uses. `PORT` defaults to 8443. |
| `ANTHROPIC_API_KEY` | one of these two | Claude API key for the agent and the judge. Used when set. |
| `AWS_ROLE_ARN` | one of these two | An AWS IAM role with Bedrock access to Haiku 4.5 and Sonnet 4.6 (`bedrock:InvokeModel*` on the `us.anthropic.*` inference profiles) in us-east-1. It needs a trust policy for GitHub's OIDC provider (`token.actions.githubusercontent.com`) that allows `repo:cBioPortal/cbioportal-mcp:*`. The workflow assumes it with `aws-actions/configure-aws-credentials`. |
| `CHAT_SYSTEM_PROMPT` | no | Prod's agent instructions, from the cBioAgent MongoDB (`agents.instructions` of the prod agent). Without it the agent runs on the MCP servers' instructions alone, which still tests this repo's prompt but less like prod. Secrets are limited to 48 KB. |

When secrets are missing, a PR run is skipped with a notice, for example a PR from a fork, which gets no
secrets. A nightly or manual run fails with an `::error::` instead.

## Cost and time

One run makes about 104 answers (26 questions × 2 repeats × 2 servers) and as many judge calls. Estimated at
list prices: roughly **$10–25** per run on the Claude API, mostly the agent's Haiku 4.5 input tokens; the judge is
a few dollars. Bedrock bills differently. It takes about **15–25 minutes**. `agent-model: haiku-5.5`
(Claude API only) is about 10× cheaper per token, but it is a different model from prod's.

## Reading a failure

LLM answers vary between runs, so check the regressed questions in the summary before believing a small drop.
Re-run the job, or raise `repeats` on a manual run, to separate noise from a real regression. If the behavior
change is intended and the answers are still right, the reference may need updating in cbioportal-mcp-qa. The
thresholds are inputs of the reusable workflow.

The workflow is pinned to a commit of cbioportal-mcp-qa: update `uses:` and `qa-ref` together.
