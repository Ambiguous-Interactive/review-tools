# Review Tools

Shared COMMENT-only OpenAI reviews and read-only Agent Metrics for Ambiguous Interactive.
Requires Python 3.13 and a funded OpenAI API project. Default model: `gpt-6-luna`.

Call `.github/workflows/openai-review.yml` from a `pull_request` workflow using
`Ambiguous-Interactive/review-tools/.github/workflows/openai-review.yml@<release commit SHA>`.
Pass the named `OPENAI_API_KEY` secret and grant `contents: read`, `pull-requests: write`.
Optional inputs: `model`, `effort`, `max-findings` (5), `timeout-minutes` (15), `disclosure`.
Do not make the review a required merge check. API errors fail visibly.
Forks, bots, drafts, and closed PRs are ineligible. Caller workflows must use the same guards.
Use caller-level concurrency keyed by workflow, event, and PR number to serialize reviews.
A completed head is reviewed once; re-running does not force another review.

The workflow executes its own source at `job.workflow_sha`, never caller code.
It fetches allowlisted guides from the captured base commit and reviews the complete eligible
PR diff. Generated/binary assets and sensitive paths are excluded. Eligible diffs above
180,000 characters fail before an API request, without a completion marker. No model tools
are enabled. Source and guide text are sent to OpenAI; `store` is false.
Exclusions reduce accidental disclosure; they cannot detect every embedded secret.

Call `.github/workflows/agent-metrics.yml` at the same release SHA with `contents: read`,
`issues: read`, and `pull-requests: read`. It needs no OpenAI secret. Inputs: `days` (90),
`timeout-minutes` (15). It writes a summary and a 90-day JSON artifact.
Keep schedule and dispatch events in caller repositories.

Metrics accept legacy run records. New records include cache reads and writes, coverage,
and workflow provenance. Costs use the versioned prices file. Historical records without
cache-write fields are estimates; unknown prices or incomplete write accounting show n/a.
“Acted” means an outdated line or a positive reaction; it is a proxy, not measured precision.
Compare settings within repository and time period; do not infer causation from these rates.

## Development

Run `uv sync --frozen`, `uv run pytest -q`, `uv run mypy --strict tools`,
`uv run yamllint .github`, and `actionlint`. Tests use mocked APIs and no paid key.

## Releases

Main requires PRs and Self Test; tooling changes require owner review. Tags `v*` are protected.
Release only a tested main commit. Publish an immutable version and pin callers to its SHA.
Never move a published tag. Roll back a caller by reverting its migration PR.
Keep fork tests secret-free. Review changes to workflows and code before sharing API secrets.
