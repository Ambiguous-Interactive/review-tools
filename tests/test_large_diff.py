import json
import urllib.error
import pytest

from test_safety import setup_review
from test_openai_review import review
from test_agent_metrics import metrics


def patch(path, count=4):
    return (f'diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n'
            f'@@ -0,0 +1,{count} @@\n'
            + ''.join(f'+value_{i} = "{"x" * 24}"\n' for i in range(count)))


def posted(calls):
    return [body for method, url, body in calls if method == 'POST' and url.endswith('/reviews')]


def test_large_diff_is_reviewed_in_bounded_parts(monkeypatch):
    diff = patch('src/a.py') + patch('src/b.py')
    calls = setup_review(monkeypatch, diff)
    assert review.main(['--pr', '1', '--max-diff-chars', '260']) == 0
    requests = [body for method, url, body in calls if url == review.OPENAI_API]
    assert len(requests) > 1
    assert all(len(body['input'].split('\n\nDiff:\n')[1]) <= 260 for body in requests)
    assert len(posted(calls)) == 1
    assert review.already_reviewed([dict(posted(calls)[0], user={'login': review.REVIEWER})], 'a'*40)


def test_single_large_hunk_preserves_every_added_line_number():
    diff = patch('src/large.py', 30)
    chunks, excluded, omitted = review.plan_review(diff, 260, 30)
    assert not excluded and not omitted
    assert len(chunks) > 1 and all(len(chunk) <= 260 for chunk in chunks)
    lines = set().union(*(review.commentable_lines(chunk)['src/large.py'] for chunk in chunks))
    assert lines == set(range(1, 31))
    assert all(sum(chunk.count(f'+value_{i} =') for chunk in chunks) == 1 for i in range(30))


def test_partial_review_prioritizes_key_files_and_is_not_a_completed_head(monkeypatch):
    diff = patch('docs/readme.md', 30) + patch('src/security.py', 2)
    calls = setup_review(monkeypatch, diff)
    assert review.main(['--pr', '1', '--max-diff-chars', '260', '--max-chunks', '1']) == 0
    request = next(body for _, url, body in calls if url == review.OPENAI_API)
    assert '+value_1 =' in request['input'] and 'diff --git a/src/security.py' in request['input']
    assert 'diff --git a/docs/readme.md' not in request['input']
    result = posted(calls)[0]
    assert 'Partial' in result['body']
    assert review.read_review_record(result['body'])['coverage'] == 'partial'
    assert not review.already_reviewed([dict(result, user={'login': review.REVIEWER})], 'a'*40)


def test_unsplittable_line_does_not_discard_other_files(monkeypatch):
    diff = patch('a.py', 1) + patch('b.py', 1).replace('x'*24, 'x'*1000)
    calls = setup_review(monkeypatch, diff)
    assert review.main(['--pr', '1', '--max-diff-chars', '260']) == 0
    assert any(url == review.OPENAI_API for _, url, _ in calls)
    assert review.read_review_record(posted(calls)[0]['body'])['coverage'] == 'partial'


def test_failed_part_keeps_successful_findings_and_usage(monkeypatch):
    calls = setup_review(monkeypatch, patch('a.py', 2) + patch('b.py', 2))
    original = review.http_json
    count = 0

    def fetch(method, url, headers, body=None, raw=False):
        nonlocal count
        result = original(method, url, headers, body, raw)
        if url == review.OPENAI_API:
            count += 1
            if count == 2:
                raise urllib.error.HTTPError(url, 503, 'unavailable', None, None)
            return {'output_text': json.dumps({'summary': 'Bug.', 'findings': [
                {'path': 'a.py', 'line': 1, 'severity': 'major', 'title': 'Bug', 'failure': 'Bad result.', 'fix': 'Fix it.'}]}),
                'usage': {'input_tokens': 200, 'output_tokens': 30}}
        return result

    monkeypatch.setattr(review, 'http_json', fetch)
    assert review.main(['--pr', '1', '--max-diff-chars', '260']) == 0
    result = posted(calls)[0]
    assert len(result['comments']) == 1
    record = review.read_review_record(result['body'])
    assert record['coverage'] == 'partial' and record['input_tokens'] == 200
    assert record['failed_chunks'] == 1


def test_sensitive_files_never_reach_any_part():
    chunks, excluded, omitted = review.plan_review(patch('.env', 50) + patch('a.py', 20), 260, 30)
    assert excluded == ['.env'] and not omitted
    assert all('diff --git a/.env' not in chunk for chunk in chunks)


def test_partial_inline_fallback_preserves_findings():
    usage = {'coverage': 'partial', 'coverage_note': 'Partial coverage.'}
    result = {'summary': 'Partial.', 'findings': [
        {'path': 'a.py', 'line': 1, 'severity': 'major', 'title': 'Bug', 'failure': 'Bad result.', 'fix': 'Fix it.'}]}
    built = review.build_review(result, {'a.py': {1}}, 'a'*40, 'm', 5, usage)
    fallback = review.body_only(built, 'm', '', usage, 1, 100, 0)
    assert '`a.py:1`' in fallback['body'] and not fallback['comments']
    assert review.MARKER_PREFIX + 'a'*40 not in fallback['body']


def test_unavailable_combined_diff_uses_available_file_patches(monkeypatch):
    calls = setup_review(monkeypatch, '')
    original = review.http_json

    def fetch(method, url, headers, body=None, raw=False):
        if '/compare/' in url:
            raise urllib.error.HTTPError(url, 502, 'large diff', None, None)
        if '/files?' in url:
            return [{'filename': 'src/a.py', 'status': 'modified', 'patch': '@@ -1 +1 @@\n-old\n+new'},
                    {'filename': 'src/b.py', 'status': 'modified'}]
        return original(method, url, headers, body, raw)

    monkeypatch.setattr(review, 'http_json', fetch)
    assert review.main(['--pr', '1']) == 0
    request = next(body for _, url, body in calls if url == review.OPENAI_API)
    assert '+new' in request['input']
    result = posted(calls)[0]
    assert review.read_review_record(result['body'])['coverage'] == 'partial'
    assert 'file patches' in result['body']


def test_rename_from_sensitive_path_never_reaches_model():
    diff = patch('src/settings.py').replace('--- a/src/settings.py', '--- a/.env')
    chunks, excluded, _ = review.plan_review(diff, 1000, 8)
    assert not chunks and excluded == ['src/settings.py']


def test_mixed_hunk_offsets_and_unicode_paths_survive_splitting():
    path = 'caf\u00e9 folder/thing.py'
    diff = (f'diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -10,5 +20,5 @@ section\n'
            ' context\n-old\n+new\n context2\n-old2\n+new2\n context3\n')
    chunks, _, omitted = review.plan_review(diff, 170, 20)
    actual = set().union(*(review.commentable_lines(c).get(path, set()) for c in chunks))
    assert not omitted and actual == review.commentable_lines(diff)[path]


def test_usage_totals_include_cache_accounting_without_inventing_unknowns():
    usages = [{'input_tokens': 100, 'output_tokens': 20, 'input_tokens_details': {
        'cached_tokens': 30, 'cache_write_tokens': 70,
        'cache_write_tokens_details': {'ttl_30m_tokens': 70, 'ttl_12h_tokens': 0}}}] * 2
    total = review.sum_usage(usages)
    assert total['input_tokens'] == 200 and total['output_tokens'] == 40
    assert total['input_tokens_details']['cache_write_tokens_details']['ttl_30m_tokens'] == 140
    assert review.sum_usage([usages[0], {}])['input_tokens'] is None
    assert metrics.token_cost({'model': 'm', 'usage_complete': False, 'input_tokens': 100},
                              {'m': {'input_per_million': 1.0}}) is None


def test_time_budget_retains_completed_parts(monkeypatch):
    calls = setup_review(monkeypatch, patch('a.py', 2) + patch('b.py', 2))
    original = review.http_json
    elapsed = [0.0]
    monkeypatch.setattr(review.time, 'monotonic', lambda: elapsed[0])

    def fetch(method, url, headers, body=None, raw=False):
        result = original(method, url, headers, body, raw)
        if url == review.OPENAI_API:
            elapsed[0] = 350.0
        return result

    monkeypatch.setattr(review, 'http_json', fetch)
    assert review.main(['--pr', '1', '--max-diff-chars', '260']) == 0
    assert sum(url == review.OPENAI_API for _, url, _ in calls) == 1
    record = review.read_review_record(posted(calls)[0]['body'])
    assert record['coverage'] == 'partial' and record['unreviewed_chunks'] == 1


def test_unavailable_guide_keeps_reviewing_with_available_guides(monkeypatch):
    calls = setup_review(monkeypatch, patch('a.py', 1))
    original = review.http_json

    def fetch(method, url, headers, body=None, raw=False):
        if '/contents/AGENTS.md?' in url:
            raise urllib.error.HTTPError(url, 503, 'unavailable', None, None)
        return original(method, url, headers, body, raw)

    monkeypatch.setattr(review, 'http_json', fetch)
    assert review.main(['--pr', '1']) == 0
    assert review.read_review_record(posted(calls)[0]['body'])['coverage'] == 'partial'
    assert 'repository guides were unavailable' in posted(calls)[0]['body']


def test_file_patch_pagination_failure_keeps_earlier_pages(monkeypatch):
    def fetch(method, url, headers, body=None, raw=False):
        if url.endswith('page=1'):
            return [{'filename': f'{i}.py', 'patch': '@@ -1 +1 @@\n-old\n+new'} for i in range(100)]
        raise urllib.error.HTTPError(url, 503, 'unavailable', None, None)

    monkeypatch.setattr(review, 'http_json', fetch)
    diff, _ = review.file_patch_diff('https://api.github.com/repos/o/r/pulls/1', 'fake', float('inf'))
    assert len(review.split_diff(diff)) == 100


def test_quoted_fallback_paths_round_trip():
    path = 'caf\u00e9 folder/new\nfile\\name.py'
    assert review.side_path(review.quoted_path('b/' + path)) == path


def test_publication_failure_preserves_completed_review(monkeypatch, tmp_path):
    setup_review(monkeypatch, patch('a.py', 1))
    original = review.http_json

    def fetch(method, url, headers, body=None, raw=False):
        if method == 'POST' and url.endswith('/reviews'):
            raise urllib.error.HTTPError(url, 503, 'unavailable', None, None)
        return original(method, url, headers, body, raw)

    monkeypatch.setattr(review, 'http_json', fetch)
    output = tmp_path / 'result.json'
    with pytest.raises(urllib.error.HTTPError):
        review.main(['--pr', '1', '--output', str(output)])
    payload = json.loads(output.read_text())
    assert payload['commit_id'] == 'a'*40 and payload['event'] == 'COMMENT'
