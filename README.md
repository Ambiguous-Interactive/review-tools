# Review Tools

Shared COMMENT-only OpenAI reviews and read-only Agent Metrics for Ambiguous Interactive.
Requires Python 3.13 and a funded OpenAI API project. Default model: `gpt-6-luna`.

Call `.github/workflows/openai-review.yml` from a `pull_request` workflow using
`Ambiguous-Interactive/review-tools/.github/workflows/openai-review.yml@<release commit SHA>`.
Pass the named `OPENAI_API_KEY` secret and grant `contents: read`, `pull-requests: write`.
Optional inputs: `model`, `effort`, `max-findings` (5), `max-diff-chars` (180,000 per request),
`max-chunks` (8 requests), `timeout-minutes` (15), `disclosure`.
The review is advisory. Model/transport failures remain visible as warnings and step outcomes,
but the review step cannot block merging. Keep the review out of required merge checks.
Forks, bots, drafts, and closed PRs are ineligible. Caller workflows must use the same guards.
Use caller-level concurrency keyed by workflow, event, and PR number to serialize reviews.
A completed head is reviewed once; re-running does not force another review.

The workflow executes its own source at `job.workflow_sha`, never caller code.
It fetches allowlisted guides from the captured base commit. Generated/binary assets and
sensitive paths (including either side of a rename) are excluded. Large diffs are split into
bounded requests, preserving hunk line numbers. Security, configuration and source files get
priority before tests and documentation when capacity is limited. Findings are combined into
one COMMENT review per attempt with a global finding limit; reported usage is summed.

The reviewer retains useful partial results when a file or line cannot fit, a chunk fails,
guides are unavailable, or the request/time budget is reached. It reserves time to publish
completed work before the workflow deadline (the script's default review budget is ten minutes).
If GitHub cannot provide the combined diff, it uses paginated per-file patches. These may be
truncated and are capped at 3,000 files, so fallback coverage is always labeled partial.
Partial reviews list reviewed/omitted files and part counts, have an explicit coverage record,
and never carry the completed-head marker. Incomplete attempts can be retried. Full reviews
are deduplicated per head; chunk boundaries can hide cross-file interactions from the model.
The workflow preserves the combined review as a 14-day artifact before publication, so a
GitHub comment failure does not erase completed work. Artifact access follows the caller repo.
No model tools
are enabled. Source and guide text are sent to OpenAI; `store` is false.
Exclusions reduce accidental disclosure; they cannot detect every embedded secret.

Call `.github/workflows/agent-metrics.yml` at the same release SHA with `contents: read`,
`issues: read`, and `pull-requests: read`. It needs no OpenAI secret. Inputs: `days` (90),
`timeout-minutes` (15). It writes a summary and requests 90-day JSON artifact retention. Organization and repository
limits still apply; the current organization maximum is 30 days.
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

Main requires PRs and Self Test; approvals are optional. Tags `v*` are protected.
Release only a tested main commit. Publish an immutable version and pin callers to its SHA.
Never move a published tag. Roll back a caller by reverting its migration PR.
Keep fork tests secret-free. Review changes to workflows and code before sharing API secrets.

Dependabot checks GitHub Actions and the Python development lockfile weekly on Mondays.
Updates are grouped by ecosystem and pass the same required checks before merge.
The standalone actionlint download and uv bootstrap version remain manual pins.
