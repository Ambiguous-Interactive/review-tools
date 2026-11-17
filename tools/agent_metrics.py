#!/usr/bin/env python3
"""Compare agent and review settings by cost and outcome, from the records the runs leave.

Each agent issue or draft PR carries a hidden `agent-run` record (publish_agent_report.py): the
settings it tried (model, effort, turn and budget caps), its cost and its tokens. Each OpenAI review
carries a hidden `openai-review-run` record (openai_review.py). This script reads them back and
groups them by settings.

Outcomes:
- Agent report: approved (the owner added owner-approved, or the draft PR merged), declined (closed
  without approval), or pending.
- Review comment: acted (a later commit changed its line, or a thumbs up), dismissed (a thumbs
  down), silent (the pull request closed with neither), or pending (the pull request is open).
  "Acted" is a proxy: other work can change a line too.

It counts only records the workflow bot wrote (AGENT_METRICS_WRITER, default github-actions[bot]).
It reads only, with GITHUB_TOKEN. The JSON output keeps every row, for a later settings bandit.

Usage:
    agent_metrics.py [--days 90] [--json FILE] [--prices FILE.toml]
"""

from __future__ import annotations

import argparse
import datetime
import json
import math
import os
import pathlib
import sys
import tomllib
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

GITHUB_API = "https://api.github.com"
DISCLOSURE = os.environ.get("AGENT_DISCLOSURE", "DISCLOSURE: LLM-GENERATED TEXT")
AGENT_PREFIX = "<!-- agent-run: "
REVIEW_PREFIX = "<!-- openai-review-run: "
SUFFIX = " -->"
APPROVED_LABEL = "owner-approved"
WRITER = os.environ.get("AGENT_METRICS_WRITER", "github-actions[bot]")
Z_90 = 1.6449
Fetch = Callable[[str], Any]


def read_record(body: str | None, prefix: str) -> dict[str, Any] | None:
    text = body or ""
    start = text.rfind(prefix)
    end = text.find(SUFFIX, start + len(prefix)) if 0 <= start else -1
    if end < 0:
        return None
    try:
        record = json.loads(text[start + len(prefix) : end])
    except json.JSONDecodeError:
        return None
    return record if isinstance(record, dict) else None


def arm_key(arm: Mapping[str, Any] | None) -> str:
    values = arm or {}
    parts = (values.get("model"), values.get("effort"), values.get("max_turns"), values.get("max_budget_usd"))
    return " / ".join(str(part) for part in parts)


def wilson(successes: int, trials: int) -> tuple[float, float]:
    """A 90% interval for a rate: wide for a few runs, narrow for many."""
    if trials <= 0:
        return 0.0, 1.0
    rate = successes / trials
    spread = Z_90 * math.sqrt(rate * (1 - rate) / trials + Z_90**2 / (4 * trials**2))
    centre = rate + Z_90**2 / (2 * trials)
    scale = 1 + Z_90**2 / trials
    return max(0.0, (centre - spread) / scale), min(1.0, (centre + spread) / scale)


def written_by_workflow(item: Mapping[str, Any]) -> bool:
    """Only the workflow bot writes run records; a record anyone else wrote is not counted."""
    return (item.get("user") or {}).get("login") == WRITER


def agent_outcome(item: Mapping[str, Any]) -> str:
    labels = {label.get("name") for label in item.get("labels", []) if isinstance(label, Mapping)}
    merged = (item.get("pull_request") or {}).get("merged_at")
    if APPROVED_LABEL in labels or merged:
        return "approved"
    return "declined" if item.get("state") == "closed" else "pending"


def on_or_after(stamp: Any, since: datetime.datetime | None) -> bool:
    if since is None or stamp is None:
        return True
    try:
        moment = datetime.datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return False
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.timezone.utc)
    return since <= moment


def agent_rows(items: Iterable[Mapping[str, Any]], since: datetime.datetime | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in items:
        if not written_by_workflow(item):
            continue
        record = read_record(item.get("body"), AGENT_PREFIX)
        if record is None or not on_or_after(record.get("started", item.get("created_at")), since):
            continue
        rows.append({**record, "number": item.get("number"), "run_outcome": record.get("outcome"),
                     "outcome": agent_outcome(item)})
    return rows


def number(value: Any) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def summarise_agents(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for row in rows:
        groups.setdefault((str(row.get("job")), arm_key(row.get("arm"))), []).append(row)
    table: list[dict[str, Any]] = []
    for (job, arm), members in sorted(groups.items()):
        approved = sum(1 for row in members if row["outcome"] == "approved")
        declined = sum(1 for row in members if row["outcome"] == "declined")
        cost = sum(number(row.get("cost_usd")) for row in members)
        decided = approved + declined
        low, high = wilson(approved, decided)
        table.append({
            "job": job,
            "arm": arm,
            "runs": len(members),
            "approved": approved,
            "declined": declined,
            "pending": len(members) - decided,
            "approval_rate": approved / decided if decided else None,
            "approval_low": low,
            "approval_high": high,
            "cost_usd": round(cost, 4),
            "cost_per_approved": round(cost / approved, 4) if approved else None,
            "mean_turns": round(sum(number(row.get("turns")) for row in members) / len(members), 1),
        })
    return table


def comment_outcome(comment: Mapping[str, Any], closed: bool) -> str:
    reactions = comment.get("reactions") or {}
    if 0 < number(reactions.get("-1")):
        return "dismissed"
    changed = comment.get("line") is None and comment.get("original_line") is not None
    if changed or 0 < number(reactions.get("+1")):
        return "acted"
    return "silent" if closed else "pending"


def review_rows(
    pull: Mapping[str, Any],
    reviews: Iterable[Mapping[str, Any]],
    comments: Sequence[Mapping[str, Any]],
    since: datetime.datetime | None = None,
) -> list[dict[str, Any]]:
    closed = pull.get("state") == "closed"
    rows: list[dict[str, Any]] = []
    for review in reviews:
        if not written_by_workflow(review) or not on_or_after(review.get("submitted_at"), since):
            continue
        record = read_record(review.get("body"), REVIEW_PREFIX)
        if record is None:
            continue
        outcomes = [
            comment_outcome(comment, closed)
            for comment in comments
            if comment.get("pull_request_review_id") == review.get("id")
        ]
        counts = {name: outcomes.count(name) for name in ("acted", "dismissed", "silent", "pending")}
        rows.append({"pull": pull.get("number"), "review": review.get("id"), **record, **counts})
    return rows


def token_cost(row: Mapping[str, Any], prices: Mapping[str, Mapping[str, float]]) -> float | None:
    price = prices.get(str(row.get("model")))
    if not price:
        return None
    input_price = price.get("input_per_million", 0.0)
    cached = min(number(row.get("cached_tokens")), number(row.get("input_tokens")))
    writes = min(max(0.0, number(row.get("cache_write_tokens"))), number(row.get("input_tokens")) - cached)
    details = row.get("cache_write_tokens_details") or {}
    short = number(details.get("ttl_30m_tokens")) if isinstance(details, Mapping) else 0.0
    long = number(details.get("ttl_12h_tokens")) if isinstance(details, Mapping) else 0.0
    if writes and short + long != writes:
        return None
    return (
        (number(row.get("input_tokens")) - cached - writes) * input_price
        + cached * price.get("cached_input_per_million", input_price)
        + short * input_price * 1.25 + long * input_price * 2.5
        + number(row.get("output_tokens")) * price.get("output_per_million", 0.0)
    ) / 1_000_000


def summarise_reviews(
    rows: Sequence[Mapping[str, Any]], prices: Mapping[str, Mapping[str, float]]
) -> list[dict[str, Any]]:
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        groups.setdefault(f"{row.get('model')} / {row.get('effort')}", []).append(row)
    table: list[dict[str, Any]] = []
    for arm, members in sorted(groups.items()):
        acted = sum(int(number(row.get("acted"))) for row in members)
        dismissed = sum(int(number(row.get("dismissed"))) for row in members)
        silent = sum(int(number(row.get("silent"))) for row in members)
        judged = acted + dismissed + silent
        low, high = wilson(acted, judged)
        costs = [token_cost(row, prices) for row in members]
        known = [cost for cost in costs if cost is not None]
        table.append({
            "arm": arm,
            "reviews": len(members),
            "findings": sum(int(number(row.get("findings"))) for row in members),
            "inline": sum(int(number(row.get("inline"))) for row in members),
            "acted": acted,
            "dismissed": dismissed,
            "silent": silent,
            "pending": sum(int(number(row.get("pending"))) for row in members),
            "acted_rate": acted / judged if judged else None,
            "acted_low": low,
            "acted_high": high,
            "mean_input_tokens": round(sum(number(row.get("input_tokens")) for row in members) / len(members)),
            "mean_output_tokens": round(sum(number(row.get("output_tokens")) for row in members) / len(members)),
            "cached_share": round(
                sum(number(row.get("cached_tokens")) for row in members)
                / max(1.0, sum(number(row.get("input_tokens")) for row in members)),
                3,
            ),
            "cost_usd": round(sum(known), 4) if len(known) == len(costs) else None,
        })
    return table


def percent(value: Any) -> str:
    return f"{value:.0%}" if isinstance(value, float) else "n/a"


def money(value: Any) -> str:
    return f"${value:.2f}" if isinstance(value, (int, float)) else "n/a"


def markdown(agents: Sequence[Mapping[str, Any]], reviews: Sequence[Mapping[str, Any]], days: int) -> str:
    lines = [DISCLOSURE, "", f"## Agent and review settings, last {days} days", ""] if DISCLOSURE else []
    lines += [
        "Settings are model / effort / turn cap / budget cap. The range is a 90% interval: when two",
        "ranges overlap, the data cannot tell the settings apart yet.",
        "",
        "### Agent reports",
        "",
        "| Job | Settings | Runs | Approved | Declined | Pending | Approval (90% range) | Cost | Cost per approved |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for row in agents:
        rate = f"{percent(row['approval_rate'])} ({percent(row['approval_low'])}-{percent(row['approval_high'])})"
        lines.append(
            f"| {row['job']} | {row['arm']} | {row['runs']} | {row['approved']} | {row['declined']} | "
            f"{row['pending']} | {rate} | {money(row['cost_usd'])} | {money(row['cost_per_approved'])} |"
        )
    if not agents:
        lines.append("| (no agent reports with a run record yet) | | | | | | | | |")
    lines += [
        "",
        "### OpenAI review comments",
        "",
        "| Settings | Reviews | Findings | Inline | Acted | Dismissed | Silent | Pending | Acted (90% range) | Tokens in / out | Cached | Cost |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in reviews:
        rate = f"{percent(row['acted_rate'])} ({percent(row['acted_low'])}-{percent(row['acted_high'])})"
        lines.append(
            f"| {row['arm']} | {row['reviews']} | {row['findings']} | {row['inline']} | {row['acted']} | "
            f"{row['dismissed']} | {row['silent']} | {row['pending']} | {rate} | "
            f"{row['mean_input_tokens']} / {row['mean_output_tokens']} | {percent(row['cached_share'])} | "
            f"{money(row['cost_usd'])} |"
        )
    if not reviews:
        lines.append("| (no reviews with a run record yet) | | | | | | | | | | | |")
    lines += [
        "",
        "Review cost shows only for known prices. Historical records without cache-write usage are estimates.",
    ]
    return "\n".join(lines) + "\n"


def github_fetch(token: str) -> Fetch:
    def fetch(url: str) -> Any:
        request = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))

    return fetch


def pages(fetch: Fetch, url: str, limit: int = 50) -> list[Any]:
    items: list[Any] = []
    joiner = "&" if "?" in url else "?"
    for page in range(1, limit + 1):
        batch = fetch(f"{url}{joiner}per_page=100&page={page}")
        if not batch:
            break
        items.extend(batch)
    return items


def collect(fetch: Fetch, repository: str, since: datetime.datetime) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    base = f"{GITHUB_API}/repos/{repository}"
    stamp = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    issues = pages(fetch, f"{base}/issues?state=all&since={stamp}")
    agents = agent_rows(issues, since)
    reviews: list[dict[str, Any]] = []
    for page in range(1, 51):
        pulls = fetch(f"{base}/pulls?state=all&sort=updated&direction=desc&per_page=100&page={page}")
        if not pulls:
            break
        recent = [pull for pull in pulls if stamp <= str(pull.get("updated_at"))]
        for pull in recent:
            pull_reviews = pages(fetch, f"{base}/pulls/{pull['number']}/reviews")
            if not any(REVIEW_PREFIX in (review.get("body") or "") for review in pull_reviews):
                continue
            comments = pages(fetch, f"{base}/pulls/{pull['number']}/comments")
            reviews.extend(review_rows(pull, pull_reviews, comments, since))
        if len(recent) < len(pulls):
            break
    return agents, reviews


def load_prices(path: pathlib.Path | None) -> dict[str, dict[str, float]]:
    if path is None or not path.exists():
        return {}
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    models = data.get("models", {})
    return {str(name): {key: float(value) for key, value in entry.items()} for name, entry in models.items()}


def main(arguments: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--json", type=pathlib.Path)
    parser.add_argument("--prices", type=pathlib.Path, default=pathlib.Path(__file__).resolve().parent / "prices.toml")
    parsed = parser.parse_args(arguments)
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    token = os.environ.get("GITHUB_TOKEN", "")
    if not repository or not token:
        print("::error::GITHUB_REPOSITORY and GITHUB_TOKEN are required.", file=sys.stderr)
        return 1
    if parsed.days <= 0:
        parser.error("days must be positive")
    since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=parsed.days)
    agents, reviews = collect(github_fetch(token), repository, since)
    prices = load_prices(parsed.prices)
    text = markdown(summarise_agents(agents), summarise_reviews(reviews, prices), parsed.days)
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as stream:
            stream.write(text)
    if parsed.json is not None:
        parsed.json.write_text(json.dumps({"agents": agents, "reviews": reviews}, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
