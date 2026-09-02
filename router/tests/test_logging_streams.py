import io
import logging
import pytest
from router.main import MaxLevelFilter


def test_max_level_filter():
    """Verify that MaxLevelFilter only allows records with level <= max_level."""
    filter_obj = MaxLevelFilter(logging.WARNING)

    debug_record = logging.LogRecord("test", logging.DEBUG, "test.py", 1, "debug", (), None)
    info_record = logging.LogRecord("test", logging.INFO, "test.py", 1, "info", (), None)
    warn_record = logging.LogRecord("test", logging.WARNING, "test.py", 1, "warn", (), None)
    err_record = logging.LogRecord("test", logging.ERROR, "test.py", 1, "err", (), None)
    crit_record = logging.LogRecord("test", logging.CRITICAL, "test.py", 1, "crit", (), None)

    assert filter_obj.filter(debug_record) is True
    assert filter_obj.filter(info_record) is True
    assert filter_obj.filter(warn_record) is True
    assert filter_obj.filter(err_record) is False
    assert filter_obj.filter(crit_record) is False


def test_stream_separation():
    """Verify that INFO/WARN logs write to stdout stream and ERROR logs write to stderr stream."""
    stdout_stream = io.StringIO()
    stderr_stream = io.StringIO()

    stdout_handler = logging.StreamHandler(stdout_stream)
    stdout_handler.setLevel(logging.DEBUG)
    stdout_handler.addFilter(MaxLevelFilter(logging.WARNING))
    stdout_handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))

    stderr_handler = logging.StreamHandler(stderr_stream)
    stderr_handler.setLevel(logging.ERROR)
    stderr_handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))

    test_logger = logging.getLogger("test_stream_separation")
    test_logger.setLevel(logging.DEBUG)
    test_logger.handlers = [stdout_handler, stderr_handler]
    test_logger.propagate = False

    test_logger.debug("debug message")
    test_logger.info("info message")
    test_logger.warning("warning message")
    test_logger.error("error message")
    test_logger.critical("critical message")

    stdout_output = stdout_stream.getvalue()
    stderr_output = stderr_stream.getvalue()

    assert "DEBUG: debug message" in stdout_output
    assert "INFO: info message" in stdout_output
    assert "WARNING: warning message" in stdout_output
    assert "error message" not in stdout_output
    assert "critical message" not in stdout_output

    assert "ERROR: error message" in stderr_output
    assert "CRITICAL: critical message" in stderr_output
    assert "info message" not in stderr_output
    assert "warning message" not in stderr_output
    assert "debug message" not in stderr_output
