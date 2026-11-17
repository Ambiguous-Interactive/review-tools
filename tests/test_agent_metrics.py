from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
from typing import Any

import pytest

HERE = pathlib.Path(__file__).resolve().parents[1] / "tools"


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


metrics = load("agent_metrics")
review = load("openai_review")
ARM = {"model": "claude-opus-5-5", "effort": "high", "max_turns": 120, "max_budget_usd": 5.0}


def issue(number: int, state: str, labels: list[str], cost: float, merged: bool = False, arm: dict[str, Any] = ARM) -> dict[str, Any]:
    record = {"v": 1, "job": "juice-play", "arm": arm, "outcome": "success", "cost_usd": cost, "turns": 30}
    item: dict[str, Any] = {
        "user": {"login": "github-actions[bot]", "type": "Bot"},
        "number": number,
        "state": state,
        "labels": [{"name": name} for name in labels],
        "body": f"report\n\n<!-- agent-run: {json.dumps(record)} -->",
    }
    if merged:
        item["pull_request"] = {"merged_at": "2026-11-10T00:00:00Z"}
    return item


def test_an_agent_report_is_approved_declined_or_pending() -> None:
    assert metrics.agent_outcome(issue(1, "open", ["needs-owner-review", "owner-approved"], 1.0)) == "approved"
    assert metrics.agent_outcome(issue(2, "closed", ["needs-owner-review"], 1.0)) == "declined"
    assert metrics.agent_outcome(issue(3, "open", ["needs-owner-review"], 1.0)) == "pending"
    assert metrics.agent_outcome(issue(4, "closed", [], 1.0, merged=True)) == "approved"


def test_agent_rows_group_by_job_and_settings() -> None:
    other = {**ARM, "model": "claude-sonnet-5-5"}
    items = [
        issue(1, "open", ["owner-approved"], 2.0),
        issue(2, "closed", [], 1.0),
        issue(3, "open", [], 3.0),
        issue(4, "closed", ["owner-approved"], 0.5, arm=other),
        {"number": 5, "state": "open", "labels": [], "body": "a person's issue"},
    ]
    rows = metrics.agent_rows(items)
    assert len(rows) == 4
    table = {row["arm"]: row for row in metrics.summarise_agents(rows)}
    opus = table[metrics.arm_key(ARM)]
    assert (opus["runs"], opus["approved"], opus["declined"], opus["pending"]) == (3, 1, 1, 1)
    assert opus["approval_rate"] == 0.5
    assert opus["cost_usd"] == 6.0 and opus["cost_per_approved"] == 6.0
    assert table[metrics.arm_key(other)]["approval_rate"] == 1.0


def comment(review_id: int, line: int | None, up: int = 0, down: int = 0) -> dict[str, Any]:
    return {"pull_request_review_id": review_id, "line": line, "original_line": 10, "reactions": {"+1": up, "-1": down}}


def test_a_review_comment_is_acted_on_dismissed_or_silent() -> None:
    assert metrics.comment_outcome(comment(1, None), closed=True) == "acted"
    assert metrics.comment_outcome(comment(1, 10, up=1), closed=True) == "acted"
    assert metrics.comment_outcome(comment(1, None, down=1), closed=True) == "dismissed"
    assert metrics.comment_outcome(comment(1, 10), closed=True) == "silent"
    assert metrics.comment_outcome(comment(1, 10), closed=False) == "pending"


def test_review_rows_count_comments_and_tokens_per_model() -> None:
    record = {"v": 1, "model": "gpt-5.6-terra", "effort": "default", "input_tokens": 1000, "output_tokens": 200,
              "findings": 3, "inline": 2}
    bot = {"login": "github-actions[bot]", "type": "Bot"}
    reviews = [{"id": 7, "user": bot, "body": f"x <!-- openai-review-run: {json.dumps(record)} --> <!-- openai-review:abc -->"},
               {"id": 8, "user": bot, "body": "a person's review"}]
    comments = [comment(7, None), comment(7, 12, down=1), comment(8, None)]
    rows = metrics.review_rows({"number": 4, "state": "closed"}, reviews, comments)
    assert len(rows) == 1
    assert rows[0]["acted"] == 1 and rows[0]["dismissed"] == 1 and rows[0]["findings"] == 3
    prices = {"gpt-5.6-terra": {"input_per_million": 2.0, "output_per_million": 10.0}}
    summary = metrics.summarise_reviews(rows, prices)[0]
    assert summary["acted_rate"] == 0.5
    assert abs(summary["cost_usd"] - (1000 * 2.0 + 200 * 10.0) / 1_000_000) < 1e-12
    assert metrics.summarise_reviews(rows, {})[0]["cost_usd"] is None


def test_the_report_names_no_secret_and_starts_with_the_disclosure() -> None:
    rows = metrics.agent_rows([issue(1, "open", ["owner-approved"], 2.0)])
    text = metrics.markdown(metrics.summarise_agents(rows), [], days=90)
    assert text.startswith("DISCLOSURE: LLM-GENERATED TEXT")
    assert "claude-opus-5-5" in text and "| 1 |" in text


def test_the_interval_narrows_with_more_runs() -> None:
    low_few, high_few = metrics.wilson(1, 2)
    low_many, high_many = metrics.wilson(50, 100)
    assert high_many - low_many < high_few - low_few
    assert metrics.wilson(0, 0) == (0.0, 1.0)


def test_collect_reads_issues_and_only_recent_reviewed_pulls() -> None:
    import datetime
    from urllib.parse import parse_qs, urlparse

    record = {"v": 1, "model": "m", "effort": "default", "findings": 1, "inline": 1}
    review_body = f"<!-- openai-review-run: {json.dumps(record)} -->"
    pulls = [
        {"number": 2, "state": "closed", "updated_at": "2026-11-15T00:00:00Z"},
        {"number": 3, "state": "open", "updated_at": "2026-11-14T00:00:00Z"},
        {"number": 1, "state": "closed", "updated_at": "2026-01-01T00:00:00Z"},
    ]
    asked: list[str] = []

    def fetch(url: str) -> Any:
        asked.append(url)
        parts = urlparse(url)
        page = int(parse_qs(parts.query).get("page", ["1"])[0])
        if 1 < page:
            return []
        if parts.path.endswith("/issues"):
            return [issue(9, "open", ["owner-approved"], 1.0)]
        if parts.path.endswith("/pulls"):
            return pulls
        if parts.path.endswith("/pulls/2/reviews"):
            return [{"id": 5, "user": {"login": "github-actions[bot]"}, "body": review_body}]
        if parts.path.endswith("/pulls/2/comments"):
            return [comment(5, None)]
        if parts.path.endswith("/reviews"):
            return [{"id": 6, "body": "a person"}]
        raise AssertionError(url)

    since = datetime.datetime(2026, 11, 1, tzinfo=datetime.timezone.utc)
    agents, reviews = metrics.collect(fetch, "o/r", since)
    assert [row["number"] for row in agents] == [9] and agents[0]["outcome"] == "approved"
    assert agents[0]["run_outcome"] == "success"
    assert [(row["pull"], row["acted"]) for row in reviews] == [(2, 1)]
    assert not any("/pulls/1/" in url for url in asked)
    assert not any("/pulls/3/comments" in url for url in asked)


def test_a_record_counts_only_when_the_workflow_bot_wrote_it() -> None:
    forged = {**issue(1, "open", ["owner-approved"], 0.01), "user": {"login": "someone", "type": "User"}}
    assert metrics.agent_rows([forged]) == []
    record = {"v": 1, "model": "m", "effort": "default"}
    review = {"id": 1, "user": {"login": "someone"}, "body": f"<!-- openai-review-run: {json.dumps(record)} -->"}
    assert metrics.review_rows({"number": 1, "state": "closed"}, [review], []) == []


def test_old_runs_and_reviews_stay_out_of_the_period() -> None:
    import datetime

    since = datetime.datetime(2026, 11, 1, tzinfo=datetime.timezone.utc)
    old = {**issue(1, "open", [], 1.0)}
    old["body"] = old["body"].replace('"turns": 30', '"turns": 30, "started": "2026-01-02T00:00:00+00:00"')
    new = {**issue(2, "open", [], 1.0)}
    new["body"] = new["body"].replace('"turns": 30', '"turns": 30, "started": "2026-11-10T00:00:00+00:00"')
    assert [row["number"] for row in metrics.agent_rows([old, new], since)] == [2]
    record = {"v": 1, "model": "m"}
    bot = {"login": "github-actions[bot]"}
    reviews = [
        {"id": 1, "user": bot, "submitted_at": "2026-01-05T00:00:00Z", "body": f"<!-- openai-review-run: {json.dumps(record)} -->"},
        {"id": 2, "user": bot, "submitted_at": "2026-11-05T00:00:00Z", "body": f"<!-- openai-review-run: {json.dumps(record)} -->"},
    ]
    assert [row["review"] for row in metrics.review_rows({"number": 1, "state": "closed"}, reviews, [], since)] == [2]


def test_the_review_table_shows_the_cached_share() -> None:
    rows = [{"model": "m", "effort": "default", "input_tokens": 1000, "output_tokens": 10, "cached_tokens": 600},
            {"model": "m", "effort": "default", "input_tokens": 1000, "output_tokens": 10}]
    summary = metrics.summarise_reviews(rows, {})[0]
    assert summary["cached_share"] == 0.3
    assert "Cached" in metrics.markdown([], [summary], 90)


def test_cached_input_is_priced_at_its_own_rate() -> None:
    prices = {"m": {"input_per_million": 1.0, "cached_input_per_million": 0.1, "output_per_million": 5.0}}
    row = {"model": "m", "input_tokens": 1_000_000, "cached_tokens": 600_000, "output_tokens": 100_000}
    assert metrics.token_cost(row, prices) == pytest.approx(0.4 + 0.06 + 0.5)
    assert metrics.token_cost({**row, "cached_tokens": 0}, prices) == pytest.approx(1.5)


def test_the_default_review_model_has_a_price() -> None:
    assert review.DEFAULT_MODEL in metrics.load_prices(HERE / "prices.toml")
