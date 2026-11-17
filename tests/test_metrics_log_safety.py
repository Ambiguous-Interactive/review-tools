from test_agent_metrics import metrics


def test_workflow_error_keeps_untrusted_text_in_one_message():
    message = 'failed 100%\r\n::add-mask::hidden'
    assert metrics.workflow_command('error', message) == (
        '::error::failed 100%25%0D%0A::add-mask::hidden'
    )
