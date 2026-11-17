from test_openai_review import review
from test_agent_metrics import metrics


def test_review_records_remain_readable_by_metrics() -> None:
    built = review.build_review(
        {'summary': 'No findings.', 'findings': []}, {}, 'a' * 40,
        'gpt-6-luna', 5,
        {'input_tokens': 1000, 'output_tokens': 20,
         'input_tokens_details': {'cached_tokens': 600, 'cache_write_tokens': 400,
                                 'cache_write_tokens_details': {'ttl_30m_tokens': 400, 'ttl_12h_tokens': 0}}},
        effort='low',
    )
    rows = metrics.review_rows(
        {'number': 1, 'state': 'closed'},
        [{'id': 7, 'user': {'login': 'github-actions[bot]'}, 'body': built['body']}], [],
    )
    assert len(rows) == 1
    assert rows[0]['model'] == 'gpt-6-luna'
    assert rows[0]['effort'] == 'low'
    assert rows[0]['cached_tokens'] == 600
    assert rows[0]['cache_write_tokens'] == 400
