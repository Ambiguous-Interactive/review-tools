from test_openai_review import review
from test_agent_metrics import metrics
import pytest


def test_sensitive_paths_are_excluded():
    for path in ('.env', '.env.local', 'nested/.env.production', 'private.pem', 'credentials.json', 'Assets/Resources/holdout.json'):
        assert review.is_skipped(path)


def test_cache_writes_are_not_ordinary_input():
    prices = {'m': {'input_per_million': 1., 'cached_input_per_million': .1, 'output_per_million': 5.}}
    row = {'model': 'm', 'input_tokens': 12000, 'cached_tokens': 4000, 'cache_write_tokens': 7000,
           'cache_write_tokens_details': {'ttl_30m_tokens': 3000, 'ttl_12h_tokens': 4000}}
    assert metrics.token_cost(row, prices) == pytest.approx(.01515)


def test_schema_is_validated_locally():
    assert review.usable_answer({'output_text': '{"findings": "bad"}'}) is None


def test_limits_must_be_positive(monkeypatch):
    with pytest.raises(SystemExit):
        review.main(['--pr', '1', '--max-findings', '-1'])


def setup_review(monkeypatch, diff, response=None):
    calls = []
    pull = {'number': 1, 'title': 'test', 'body': '', 'state': 'open', 'draft': False,
            'head': {'sha': 'a'*40, 'repo': {'full_name': 'o/r'}},
            'base': {'sha': 'b'*40}, 'user': {'type': 'User'}}
    def fetch(method, url, headers, body=None, raw=False):
        calls.append((method, url, body))
        if method == 'POST' and url == review.OPENAI_API:
            return response or {'status': 'completed', 'output_text': '{"summary":"ok","findings":[]}'}
        if method == 'POST':
            return {}
        if '/compare/' in url:
            return diff
        if '/contents/' in url:
            return {'type': 'file', 'encoding': 'base64', 'content': ''}
        if '/reviews' in url:
            return []
        return pull
    for key, value in [('GITHUB_REPOSITORY', 'o/r'), ('GITHUB_TOKEN', 'fake'), ('OPENAI_API_KEY', 'fake')]:
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(review, 'http_json', fetch)
    return calls


DIFF = 'diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+new\n'


def test_unreviewable_budget_posts_explicit_notice_without_completion_marker(monkeypatch):
    calls = setup_review(monkeypatch, DIFF)
    assert review.main(['--pr', '1', '--max-diff-chars', '1']) == 0
    assert all(url != review.OPENAI_API for _, url, _ in calls)
    posted = next(body for method, url, body in calls if method == 'POST' and url.endswith('/reviews'))
    assert review.read_review_record(posted['body'])['coverage'] == 'not-reviewed'
    assert review.MARKER_PREFIX not in posted['body']


def test_full_review_posts_one_comment(monkeypatch):
    calls = setup_review(monkeypatch, DIFF)
    assert review.main(['--pr', '1']) == 0
    posted = [body for method, url, body in calls if method == 'POST' and url.endswith('/reviews')]
    assert len(posted) == 1 and posted[0]['event'] == 'COMMENT'
    request = next(body for method, url, body in calls if url == review.OPENAI_API)
    assert DIFF in request['input'] and request['store'] is False


def test_unusable_response_posts_incomplete_notice_without_completion_marker(monkeypatch):
    calls = setup_review(monkeypatch, DIFF, {'status': 'incomplete'})
    assert review.main(['--pr', '1']) == 0
    posted = next(body for method, url, body in calls if method == 'POST' and url.endswith('/reviews'))
    assert review.read_review_record(posted['body'])['coverage'] == 'not-reviewed'
    assert review.MARKER_PREFIX not in posted['body']
