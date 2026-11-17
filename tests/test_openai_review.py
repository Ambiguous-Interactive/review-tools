from __future__ import annotations

import importlib.util
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parents[1] / "tools"
spec = importlib.util.spec_from_file_location("openai_review", HERE / "openai_review.py")
review = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules["openai_review"] = review
spec.loader.exec_module(review)

DIFF = """diff --git a/game/player.gd b/game/player.gd
index 1..2 100644
--- a/game/player.gd
+++ b/game/player.gd
@@ -10,3 +10,4 @@ func _ready():
 var speed = 1
-var jump = 2
+var jump = 3
+var dash = 4
 var hp = 5
diff --git a/Assets/Thing.asset b/Assets/Thing.asset
--- a/Assets/Thing.asset
+++ b/Assets/Thing.asset
@@ -1 +1 @@
-a
+b
diff --git a/uv.lock b/uv.lock
--- a/uv.lock
+++ b/uv.lock
@@ -1 +1 @@
-x
+y
"""


def test_trim_drops_generated_files() -> None:
    kept, dropped = review.trim_diff(DIFF, 100000)
    assert "player.gd" in kept
    assert dropped == ["Assets/Thing.asset", "uv.lock"]


def test_trim_drops_files_past_the_budget() -> None:
    kept, dropped = review.trim_diff(DIFF, 10)
    assert kept == ""
    assert "game/player.gd" in dropped


def test_commentable_lines_follow_the_new_side() -> None:
    lines = review.commentable_lines(DIFF)
    assert lines["game/player.gd"] == {10, 11, 12, 13}


def test_review_puts_off_diff_findings_in_the_body() -> None:
    result = {
        "summary": "One bug.",
        "findings": [
            {"path": "game/player.gd", "line": 12, "severity": "minor", "title": "a", "failure": "f", "fix": "x"},
            {"path": "game/player.gd", "line": 99, "severity": "critical", "title": "b", "failure": "f", "fix": "x"},
        ],
    }
    built = review.build_review(result, review.commentable_lines(DIFF), "abc123", "m", 5, {})
    assert built["event"] == "COMMENT"
    assert [c["line"] for c in built["comments"]] == [12]
    assert "`game/player.gd:99`" in built["body"]
    assert built["body"].index("critical") < built["body"].index("<!-- openai-review:abc123 -->")
    bot = {"login": "github-actions[bot]"}
    assert review.already_reviewed([{"user": bot, "commit_id": "abc123", "body": built["body"]}], "abc123")
    assert not review.already_reviewed([{"body": built["body"]}], "def456")


def test_review_caps_findings() -> None:
    findings = [
        {"path": "game/player.gd", "line": 11, "severity": "minor", "title": str(i), "failure": "f", "fix": "x"}
        for i in range(9)
    ]
    built = review.build_review({"summary": "", "findings": findings}, review.commentable_lines(DIFF), "s", "m", 3, {})
    assert len(built["comments"]) == 3


def test_request_uses_strict_json_schema() -> None:
    request = review.build_request("m", 5, "guide", {"number": 1, "title": "t", "body": None}, "diff", ["a"])
    assert request["text"]["format"]["strict"] is True
    assert "Files left out" in request["input"]


def test_output_text_reads_both_shapes() -> None:
    assert review.output_text({"output_text": "{}"}) == "{}"
    shaped = {"output": [{"type": "message", "content": [{"type": "output_text", "text": "{\"a\":1}"}]}]}
    assert review.output_text(shaped) == "{\"a\":1}"


def test_missing_key_fails_fast(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert review.main(["--pr", "1"]) == 1


def test_guide_joins_every_guide_file(tmp_path) -> None:
    (tmp_path / "AGENTS.md").write_text("Read .llm/context.md", encoding="utf-8")
    (tmp_path / ".llm").mkdir()
    (tmp_path / ".llm" / "context.md").write_text("Real rules", encoding="utf-8")
    guide = review.read_guide(tmp_path)
    assert "Read .llm/context.md" in guide and "Real rules" in guide


def test_added_line_that_starts_with_plus_plus_counts() -> None:
    diff = "diff --git a/a.c b/a.c\n--- a/a.c\n+++ b/a.c\n@@ -1,1 +1,3 @@\n x\n+++counter;\n+y\n"
    assert review.commentable_lines(diff)["a.c"] == {1, 2, 3}


def test_refusal_or_incomplete_answers_are_not_a_clean_review() -> None:
    refused = {"status": "completed", "output": [{"type": "message", "content": [{"type": "refusal", "refusal": "no"}]}]}
    assert review.usable_answer(refused) is None
    incomplete = {"status": "incomplete", "output_text": "{\"summary\": \"cut"}
    assert review.usable_answer(incomplete) is None
    good = {"status": "completed", "output_text": "{\"summary\": \"ok\", \"findings\": []}"}
    assert review.usable_answer(good) == {"summary": "ok", "findings": []}


def test_a_review_records_its_settings_out_of_sight() -> None:
    result = {"summary": "s", "findings": [
        {"path": "game/player.gd", "line": 12, "severity": "minor", "title": "a", "failure": "f", "fix": "x"},
        {"path": "game/player.gd", "line": 99, "severity": "major", "title": "b", "failure": "f", "fix": "x"},
    ]}
    usage = {"input_tokens": 1200, "output_tokens": 300, "output_tokens_details": {"reasoning_tokens": 200}}
    built = review.build_review(
        result, review.commentable_lines(DIFF), "abc123", "gpt-5.6-sol", 5, usage, effort="high", diff_chars=4000, dropped=2
    )
    record = review.read_review_record(built["body"])
    assert record == {
        "v": 2, "workflow_sha": "", "coverage": "full-eligible-diff", "duration_seconds": None, "cache_write_tokens": None, "cache_write_tokens_details": None, "model": "gpt-5.6-sol", "effort": "high", "input_tokens": 1200, "output_tokens": 300,
        "reasoning_tokens": 200, "findings": 2, "inline": 1, "diff_chars": 4000, "dropped_files": 2,
        "cached_tokens": None, "unchanged_files": 0,
    }
    visible = built["body"].split(review.RECORD_PREFIX)[0]
    assert "gpt-5.6-sol" not in visible and "high" not in visible
    assert review.read_review_record("nothing") is None


def test_effort_goes_into_the_request_only_when_set() -> None:
    pull = {"number": 1, "title": "t", "body": None}
    assert "reasoning" not in review.build_request("m", 5, "g", pull, "d", [])
    assert review.build_request("m", 5, "g", pull, "d", [], effort="low")["reasoning"] == {"effort": "low"}


def test_quoted_and_spaced_paths_are_decoded() -> None:
    diff = (
        'diff --git "a/Assets/caf\\303\\251.asset" "b/Assets/caf\\303\\251.asset"\n'
        '--- "a/Assets/caf\\303\\251.asset"\n+++ "b/Assets/caf\\303\\251.asset"\n@@ -1 +1 @@\n-a\n+b\n'
        "diff --git a/game/a b/c.gd b/game/a b/c.gd\n--- a/game/a b/c.gd\n+++ b/game/a b/c.gd\t\n@@ -1 +1,2 @@\n x\n+y\n"
    )
    assert [path for path, _ in review.split_diff(diff)] == ["Assets/café.asset", "game/a b/c.gd"]
    assert review.commentable_lines(diff)["game/a b/c.gd"] == {1, 2}


def test_only_the_bots_review_of_this_commit_counts_as_done() -> None:
    marker = f"{review.MARKER_PREFIX}abc -->"
    bot = {"login": "github-actions[bot]"}
    assert review.already_reviewed([{"user": bot, "commit_id": "abc", "body": marker}], "abc")
    assert not review.already_reviewed([{"user": {"login": "someone"}, "commit_id": "abc", "body": marker}], "abc")
    assert not review.already_reviewed([{"user": bot, "commit_id": "old", "body": marker}], "abc")


def test_the_fallback_review_records_no_inline_comments() -> None:
    result = {"summary": "s", "findings": [
        {"path": "game/player.gd", "line": 12, "severity": "minor", "title": "a", "failure": "f", "fix": "x"}]}
    built = review.build_review(result, review.commentable_lines(DIFF), "abc", "m", 5, {})
    fallback = review.body_only(built, "m", "", {}, 1, 0, 0)
    assert fallback["comments"] == []
    record = review.read_review_record(fallback["body"])
    assert record is not None and record["inline"] == 0 and record["findings"] == 1
    assert "game/player.gd:12" in fallback["body"]


def test_a_re_review_sends_only_the_files_whose_change_differs() -> None:
    before = (
        "diff --git a/a.cs b/a.cs\nindex 1..2 100644\n--- a/a.cs\n+++ b/a.cs\n@@ -1,1 +1,2 @@\n x\n+one\n"
        "diff --git a/b.cs b/b.cs\nindex 3..4 100644\n--- a/b.cs\n+++ b/b.cs\n@@ -5,1 +5,2 @@\n y\n+two\n"
    )
    after = (
        "diff --git a/a.cs b/a.cs\nindex 7..8 100644\n--- a/a.cs\n+++ b/a.cs\n@@ -9,1 +9,2 @@\n x\n+one\n"
        "diff --git a/b.cs b/b.cs\nindex 3..9 100644\n--- a/b.cs\n+++ b/b.cs\n@@ -5,1 +5,2 @@\n y\n+changed\n"
        "diff --git a/c.cs b/c.cs\nnew file mode 100644\n--- /dev/null\n+++ b/c.cs\n@@ -0,0 +1 @@\n+new\n"
    )
    kept, unchanged = review.changed_since(after, before)
    assert [path for path, _ in review.split_diff(kept)] == ["b.cs", "c.cs"]
    assert unchanged == ["a.cs"]
    assert review.changed_since(after, "") == (after, [])


def test_the_last_reviewed_commit_is_the_bots_newest_marker() -> None:
    bot = {"login": "github-actions[bot]"}
    reviews = [
        {"user": bot, "commit_id": "a" * 40, "body": f"{review.MARKER_PREFIX}{'a' * 40} -->"},
        {"user": {"login": "x"}, "commit_id": "c" * 40, "body": f"{review.MARKER_PREFIX}{'c' * 40} -->"},
        {"user": bot, "commit_id": "b" * 40, "body": f"{review.MARKER_PREFIX}{'b' * 40} -->"},
    ]
    assert review.last_reviewed_sha(reviews, "d" * 40) == "b" * 40
    assert review.last_reviewed_sha([], "d" * 40) == ""


def test_the_request_names_unchanged_files_and_keys_the_cache_by_repository() -> None:
    pull = {"number": 1, "title": "t", "body": None}
    request = review.build_request("m", 5, "g", pull, "d", [], unchanged=["a.cs"], cache_key="openai-review:o/r")
    assert "a.cs" in request["input"] and request["prompt_cache_key"] == "openai-review:o/r"
    assert "prompt_cache_key" not in review.build_request("m", 5, "g", pull, "d", [])


def test_the_record_counts_cached_and_re_review_tokens() -> None:
    usage = {"input_tokens": 100, "output_tokens": 5, "input_tokens_details": {"cached_tokens": 60}}
    record = review.read_review_record(review.review_record("m", "", usage, 1, 1, 10, 0, unchanged=3))
    assert record is not None and record["cached_tokens"] == 60 and record["unchanged_files"] == 3
