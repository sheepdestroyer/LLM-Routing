import pytest
from unittest.mock import MagicMock, patch
from router.main import _end_parent_obs, _end_child_span

FUNCTIONS_TO_TEST = [
    (_end_parent_obs, "_end_parent_obs failed (non-fatal)"),
    (_end_child_span, "_end_child_span failed (non-fatal)"),
]


@pytest.mark.parametrize("func, log_msg", FUNCTIONS_TO_TEST)
def test_end_span_none(func, log_msg):
    # Should not raise, should just return
    func(None)


@pytest.mark.parametrize("func, log_msg", FUNCTIONS_TO_TEST)
def test_end_span_no_kwargs(func, log_msg):
    mock_span = MagicMock()
    func(mock_span)
    mock_span.update.assert_not_called()
    mock_span.end.assert_called_once()


@pytest.mark.parametrize("func, log_msg", FUNCTIONS_TO_TEST)
def test_end_span_with_output(func, log_msg):
    mock_span = MagicMock()
    func(mock_span, output="test_out")
    mock_span.update.assert_called_once_with(output="test_out")
    mock_span.end.assert_called_once()


@pytest.mark.parametrize("func, log_msg", FUNCTIONS_TO_TEST)
def test_end_span_with_metadata(func, log_msg):
    mock_span = MagicMock()
    func(mock_span, metadata={"key": "val"})
    mock_span.update.assert_called_once_with(metadata={"key": "val"})
    mock_span.end.assert_called_once()


@pytest.mark.parametrize("func, log_msg", FUNCTIONS_TO_TEST)
def test_end_span_with_both(func, log_msg):
    mock_span = MagicMock()
    func(mock_span, output="test_out", metadata={"key": "val"})
    mock_span.update.assert_called_once_with(output="test_out", metadata={"key": "val"})
    mock_span.end.assert_called_once()


@pytest.mark.parametrize("func, log_msg", FUNCTIONS_TO_TEST)
@patch("router.main.logger")
def test_end_span_exception(mock_logger, func, log_msg):
    mock_span = MagicMock()
    mock_span.end.side_effect = Exception("Test error")

    # Should swallow exception
    func(mock_span)

    mock_logger.debug.assert_called_once_with(log_msg, exc_info=True)
