import pytest
from unittest.mock import patch, MagicMock
import sys
import os
import logging
import importlib.util

spec = importlib.util.spec_from_file_location("entrypoint", "litellm/entrypoint.py")
entrypoint = importlib.util.module_from_spec(spec)

mock_litellm = MagicMock()
mock_litellm.__file__ = "/mock/litellm/__init__.py"
mock_litellm.__path__ = []  # Ensure litellm is treated as a package for sub-module imports

mock_proxy_cli = MagicMock()

# Mock socket instance for import-time check_tcp_port execution
mock_socket_instance = MagicMock()
mock_socket_instance.connect_ex.return_value = 0

import threading

# Save original modules and excepthooks to avoid leaking state globally
orig_modules = {
    "litellm": sys.modules.get("litellm"),
    "litellm.proxy": sys.modules.get("litellm.proxy"),
    "litellm.proxy.proxy_cli": sys.modules.get("litellm.proxy.proxy_cli"),
}
orig_excepthook = sys.excepthook
orig_threading_excepthook = getattr(threading, "excepthook", None)

try:
    with (
        patch("os.path.exists", return_value=False),
        patch("builtins.print"),
        patch("time.sleep"),
        patch("os.execvp"),
        patch("sys.stdout.flush"),
        patch("glob.glob", return_value=[]),
        patch("socket.socket", return_value=mock_socket_instance),
        patch("builtins.open"),
    ):
        sys.modules["litellm"] = mock_litellm
        sys.modules["litellm.proxy"] = MagicMock()
        sys.modules["litellm.proxy.proxy_cli"] = mock_proxy_cli
        spec.loader.exec_module(entrypoint)
finally:
    # Restore original modules and excepthooks state
    for k, v in orig_modules.items():
        if v is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = v
    sys.excepthook = orig_excepthook
    if orig_threading_excepthook is not None:
        threading.excepthook = orig_threading_excepthook


def test_check_tcp_port_success():
    with patch("socket.socket") as mock_socket_class:
        mock_sock_instance = MagicMock()
        mock_sock_instance.connect_ex.return_value = 0
        mock_socket_class.return_value = mock_sock_instance

        result = entrypoint.check_tcp_port("127.0.0.1", 5432)

        assert result is True
        mock_sock_instance.connect_ex.assert_called_once_with(("127.0.0.1", 5432))
        mock_sock_instance.close.assert_called_once()
        mock_sock_instance.settimeout.assert_called_once_with(2.0)


def test_check_tcp_port_failure_connection_refused():
    with patch("socket.socket") as mock_socket_class:
        mock_sock_instance = MagicMock()
        mock_sock_instance.connect_ex.return_value = 111  # Connection refused
        mock_socket_class.return_value = mock_sock_instance

        result = entrypoint.check_tcp_port("127.0.0.1", 5432)

        assert result is False
        mock_sock_instance.connect_ex.assert_called_once_with(("127.0.0.1", 5432))
        mock_sock_instance.close.assert_called_once()


def test_check_tcp_port_failure_exception():
    with patch("socket.socket") as mock_socket_class:
        mock_socket_class.side_effect = Exception("Network error")

        result = entrypoint.check_tcp_port("127.0.0.1", 5432)

        assert result is False


def test_max_level_filter():
    filter_obj = entrypoint.MaxLevelFilter(logging.WARNING)
    rec_debug = logging.LogRecord("test", logging.DEBUG, "", 0, "debug msg", (), None)
    rec_info = logging.LogRecord("test", logging.INFO, "", 0, "info msg", (), None)
    rec_warn = logging.LogRecord("test", logging.WARNING, "", 0, "warn msg", (), None)
    rec_err = logging.LogRecord("test", logging.ERROR, "", 0, "err msg", (), None)
    rec_crit = logging.LogRecord("test", logging.CRITICAL, "", 0, "crit msg", (), None)

    assert filter_obj.filter(rec_debug) is True
    assert filter_obj.filter(rec_info) is True
    assert filter_obj.filter(rec_warn) is True
    assert filter_obj.filter(rec_err) is False
    assert filter_obj.filter(rec_crit) is False


def test_patch_langfuse_media_manager_disabled():
    class FakeMediaManager:
        called = False

        def process_media_in_event(self, event):
            FakeMediaManager.called = True

    fake_module = MagicMock()
    fake_module.MediaManager = FakeMediaManager

    with patch.dict(os.environ, {"LANGFUSE_MEDIA_UPLOAD_ENABLED": "false"}):
        with patch.dict(sys.modules, {"langfuse._task_manager.media_manager": fake_module}):
            res = entrypoint.patch_langfuse_media_manager()
            assert res is True
            instance = FakeMediaManager()
            instance.process_media_in_event({"test": 1})
            assert FakeMediaManager.called is False


def test_patch_langfuse_media_manager_enabled():
    class FakeMediaManager:
        called = False

        def process_media_in_event(self, event):
            FakeMediaManager.called = True

    fake_module = MagicMock()
    fake_module.MediaManager = FakeMediaManager

    with patch.dict(os.environ, {"LANGFUSE_MEDIA_UPLOAD_ENABLED": "true"}):
        with patch.dict(sys.modules, {"langfuse._task_manager.media_manager": fake_module}):
            res = entrypoint.patch_langfuse_media_manager()
            assert res is False
            instance = FakeMediaManager()
            instance.process_media_in_event({"test": 1})
            assert FakeMediaManager.called is True


def test_patch_langfuse_media_manager_import_error():
    with patch.dict(os.environ, {"LANGFUSE_MEDIA_UPLOAD_ENABLED": "false"}):
        with patch.dict(sys.modules, {"langfuse._task_manager.media_manager": None}):
            res = entrypoint.patch_langfuse_media_manager()
            assert res is False


def test_single_line_formatter_strips_ansi():
    formatter = entrypoint.SingleLineFormatter()
    rec = logging.LogRecord(
        "LiteLLM Router",
        logging.ERROR,
        "router.py",
        7799,
        "\x1b[92m23:15:05 - LiteLLM Router:ERROR\x1b[0m: Error creating deployment",
        (),
        None,
    )
    result = formatter.format(rec)
    assert "\x1b[" not in result
    assert "[ERROR]" in result
    assert "Error creating deployment" in result
    assert "\n" not in result


def test_single_line_formatter_collapses_newlines():
    formatter = entrypoint.SingleLineFormatter()
    rec = logging.LogRecord(
        "test_logger",
        logging.WARNING,
        "test.py",
        10,
        "Line 1\nLine 2\nLine 3",
        (),
        None,
    )
    result = formatter.format(rec)
    assert "\n" not in result
    assert "Line 1 | Line 2 | Line 3" in result
    assert "[WARNING]" in result


def test_single_line_formatter_exception():
    formatter = entrypoint.SingleLineFormatter()
    try:
        raise ValueError("Something went wrong")
    except ValueError:
        exc = sys.exc_info()
        rec = logging.LogRecord(
            "LiteLLM Router",
            logging.ERROR,
            "router.py",
            7799,
            "Deployment failure: %s",
            ("bad model",),
            exc,
        )
        result = formatter.format(rec)
        assert "\n" not in result
        assert "[ERROR]" in result
        assert "[Traceback:" in result
        assert "ValueError: Something went wrong" in result


def test_single_line_formatter_correlation_context():
    formatter = entrypoint.SingleLineFormatter()
    rec = logging.LogRecord(
        "LiteLLM",
        logging.INFO,
        "server.py",
        20,
        "Request processed",
        (),
        None,
    )
    rec.trace_id = "trace-123"
    rec.session_id = "sess-456"
    result = formatter.format(rec)
    assert "[trace_id=trace-123 session_id=sess-456]" in result
    assert "\n" not in result


def test_single_line_excepthook(capsys):
    try:
        raise RuntimeError("Fatal crash")
    except RuntimeError:
        exc_type, exc_val, exc_tb = sys.exc_info()
        entrypoint.single_line_excepthook(exc_type, exc_val, exc_tb)

    captured = capsys.readouterr()
    lines = [line for line in captured.err.splitlines() if line.strip()]
    assert len(lines) == 1
    assert "[CRITICAL]" in lines[0]
    assert "[UncaughtException]" in lines[0]
    assert "RuntimeError: Fatal crash" in lines[0]


def test_single_line_excepthook_delegates_signals(capsys):
    with patch("sys.__excepthook__") as mock_orig_hook:
        entrypoint.single_line_excepthook(KeyboardInterrupt, KeyboardInterrupt(), None)
        assert mock_orig_hook.called is True

    with patch("sys.__excepthook__") as mock_orig_hook:
        entrypoint.single_line_excepthook(SystemExit, SystemExit(0), None)
        assert mock_orig_hook.called is True

    captured = capsys.readouterr()
    assert captured.err == ""


def test_threading_excepthook():
    mock_args = MagicMock()
    mock_args.exc_type = ValueError
    mock_args.exc_value = ValueError("Thread error")
    mock_args.exc_tb = None

    with patch.object(entrypoint, "single_line_excepthook") as mock_hook:
        entrypoint._threading_excepthook(mock_args)
        mock_hook.assert_called_once_with(ValueError, mock_args.exc_value, None)


def test_single_line_formatter_non_string_msg():
    formatter = entrypoint.SingleLineFormatter()
    rec = logging.LogRecord(
        "test_logger",
        logging.INFO,
        "test.py",
        15,
        12345,
        (),
        None,
    )
    result = formatter.format(rec)
    assert "12345" in result
    assert "[INFO]" in result


def test_single_line_formatter_bare_carriage_return():
    formatter = entrypoint.SingleLineFormatter()
    rec = logging.LogRecord(
        "test_logger",
        logging.INFO,
        "test.py",
        25,
        "Progress 50%\rProgress 100%",
        (),
        None,
    )
    result = formatter.format(rec)
    assert "\r" not in result
    assert "Progress 50% | Progress 100%" in result


def test_single_line_formatter_partial_correlation_context():
    formatter = entrypoint.SingleLineFormatter()

    rec_trace = logging.LogRecord("test", logging.INFO, "test.py", 1, "msg", (), None)
    rec_trace.trace_id = "trace-only"
    result_trace = formatter.format(rec_trace)
    assert "[trace_id=trace-only]" in result_trace
    assert "session_id" not in result_trace

    rec_sess = logging.LogRecord("test", logging.INFO, "test.py", 2, "msg", (), None)
    rec_sess.session_id = "sess-only"
    result_sess = formatter.format(rec_sess)
    assert "[session_id=sess-only]" in result_sess
    assert "trace_id" not in result_sess


def test_single_line_formatter_correlation_context_before_traceback():
    formatter = entrypoint.SingleLineFormatter()
    try:
        raise RuntimeError("Crash with context")
    except RuntimeError:
        exc = sys.exc_info()
        rec = logging.LogRecord("test", logging.ERROR, "test.py", 1, "failed", (), exc)
        rec.trace_id = "trace-corr"
        rec.session_id = "sess-corr"
        result = formatter.format(rec)
        assert "[trace_id=trace-corr session_id=sess-corr] [Traceback:" in result
        assert "\n" not in result


def test_min_level_filter():
    filter_obj = entrypoint.MinLevelFilter(logging.ERROR)
    rec_debug = logging.LogRecord("test", logging.DEBUG, "", 0, "debug msg", (), None)
    rec_info = logging.LogRecord("test", logging.INFO, "", 0, "info msg", (), None)
    rec_warn = logging.LogRecord("test", logging.WARNING, "", 0, "warn msg", (), None)
    rec_err = logging.LogRecord("test", logging.ERROR, "", 0, "err msg", (), None)
    rec_crit = logging.LogRecord("test", logging.CRITICAL, "", 0, "crit msg", (), None)

    assert filter_obj.filter(rec_debug) is False
    assert filter_obj.filter(rec_info) is False
    assert filter_obj.filter(rec_warn) is False
    assert filter_obj.filter(rec_err) is True
    assert filter_obj.filter(rec_crit) is True


@pytest.mark.parametrize(
    "msg,exc_msg",
    [
        ("Exception: Key not found in database", None),
        ("Authentication Error, Invalid proxy server token passed. key=abc123", None),
        (
            "litellm.proxy.proxy_server.user_api_key_auth(): Exception occured - Authentication Error",
            None,
        ),
        ("litellm.proxy.proxy_server.user_api_key_auth(): Exception occured", "KeyNotFoundError: not found"),
        ("Request failed: LiteLLM Virtual Key expected", None),
        ("ProxyException: Key not found in database", None),
        ("Key not found.", None),
        ("Key not found: hashed_token_123", None),
        ("Key not found in team team-456", None),
    ],
)
def test_client_auth_log_filter_downgrades_to_warning(msg, exc_msg):
    auth_filter = entrypoint.ClientAuthLogFilter()
    exc_info = None
    if exc_msg:
        try:
            raise RuntimeError(exc_msg)
        except RuntimeError:
            exc_info = sys.exc_info()

    rec = logging.LogRecord(
        name="LiteLLM Proxy",
        level=logging.ERROR,
        pathname="utils.py",
        lineno=100,
        msg=msg,
        args=(),
        exc_info=exc_info,
    )
    assert rec.levelno == logging.ERROR
    assert rec.levelname == "ERROR"

    res = auth_filter.filter(rec)
    assert res is True
    assert rec.levelno == logging.WARNING
    assert rec.levelname == "WARNING"
    assert rec.exc_info is None
    assert rec.exc_text is None
    assert rec.stack_info is None


def test_client_auth_log_filter_handles_boolean_or_invalid_exc_info():
    """Verify that non-tuple exc_info (like exc_info=True or True) does not crash is_client_auth_error."""
    auth_filter = entrypoint.ClientAuthLogFilter()
    rec = logging.LogRecord(
        name="LiteLLM Proxy",
        level=logging.ERROR,
        pathname="auth_exception_handler.py",
        lineno=112,
        msg="Invalid proxy server token passed",
        args=(),
        exc_info=True,
    )
    res = auth_filter.filter(rec)
    assert res is True
    assert rec.levelno == logging.WARNING
    assert rec.exc_info is None


def test_client_auth_log_filter_preserves_upstream_provider_auth_errors():
    """Upstream LLM provider auth errors (e.g. invalid OpenAI/Anthropic keys) must NOT be downgraded."""
    auth_filter = entrypoint.ClientAuthLogFilter()
    formatter = entrypoint.SingleLineFormatter()

    try:
        raise ValueError("openai.AuthenticationError: Incorrect API key provided or token expired")
    except ValueError:
        exc_info = sys.exc_info()

    rec = logging.LogRecord(
        name="LiteLLM Proxy",
        level=logging.ERROR,
        pathname="llm_http_handler.py",
        lineno=200,
        msg="Authentication Error: Provider rejected upstream credentials for model gpt-4o",
        args=(),
        exc_info=exc_info,
    )

    res = auth_filter.filter(rec)
    assert res is True
    # Must stay ERROR
    assert rec.levelno == logging.ERROR
    assert rec.levelname == "ERROR"
    assert rec.exc_info is not None

    formatted = formatter.format(rec)
    assert "[ERROR]" in formatted
    assert "[Traceback:" in formatted
    assert "openai.AuthenticationError: Incorrect API key provided" in formatted


def test_client_auth_log_filter_strips_traceback_and_formats_cleanly():
    auth_filter = entrypoint.ClientAuthLogFilter()
    formatter = entrypoint.SingleLineFormatter()

    try:
        raise ValueError("Invalid proxy server token passed. valid_token=None.")
    except ValueError:
        exc_info = sys.exc_info()

    rec = logging.LogRecord(
        name="LiteLLM Proxy",
        level=logging.ERROR,
        pathname="auth_exception_handler.py",
        lineno=112,
        msg="litellm.proxy.proxy_server.user_api_key_auth(): Exception occured - Authentication Error\nRequester IP Address:127.0.0.1",
        args=(),
        exc_info=exc_info,
    )
    rec.exc_text = "Traceback (most recent call last):\n  File 'test.py', line 1, in <module>"

    auth_filter.filter(rec)

    assert rec.levelno == logging.WARNING
    assert rec.levelname == "WARNING"
    assert rec.exc_info is None
    assert rec.exc_text is None
    assert rec.stack_info is None

    formatted = formatter.format(rec)
    assert "[WARNING]" in formatted
    assert "[Traceback:" not in formatted
    assert "Traceback (most recent call last)" not in formatted
    assert "Requester IP Address:127.0.0.1" in formatted
    assert "\n" not in formatted


def test_client_auth_log_filter_strips_embedded_traceback_string():
    auth_filter = entrypoint.ClientAuthLogFilter()
    rec = logging.LogRecord(
        name="LiteLLM Proxy",
        level=logging.ERROR,
        pathname="utils.py",
        lineno=6825,
        msg="Exception: Key not found in database: %s | Traceback (most recent call last): | File 'foo.py', line 10",
        args=("some_key",),
        exc_info=None,
    )

    auth_filter.filter(rec)

    assert rec.levelno == logging.WARNING
    assert rec.levelname == "WARNING"
    assert "Traceback (most recent call last)" not in rec.msg
    assert "Exception: Key not found in database: some_key" in rec.msg
    assert rec.args is None


def test_client_auth_log_filter_preserves_non_auth_server_errors():
    auth_filter = entrypoint.ClientAuthLogFilter()
    formatter = entrypoint.SingleLineFormatter()

    try:
        raise ConnectionRefusedError("Could not connect to PostgreSQL on :5432")
    except ConnectionRefusedError:
        exc_info = sys.exc_info()

    rec = logging.LogRecord(
        name="LiteLLM Proxy",
        level=logging.ERROR,
        pathname="prisma.py",
        lineno=50,
        msg="Database query execution failed: %s",
        args=("Connection refused",),
        exc_info=exc_info,
    )

    res = auth_filter.filter(rec)
    assert res is True
    # Server errors must NOT be downgraded
    assert rec.levelno == logging.ERROR
    assert rec.levelname == "ERROR"
    # Traceback must NOT be stripped
    assert rec.exc_info is not None

    formatted = formatter.format(rec)
    assert "[ERROR]" in formatted
    assert "[Traceback:" in formatted
    assert "ConnectionRefusedError: Could not connect to PostgreSQL on :5432" in formatted


def test_client_auth_log_routing_stdout_vs_stderr():
    import io

    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()

    formatter = entrypoint.SingleLineFormatter()
    auth_filter = entrypoint.ClientAuthLogFilter()

    stdout_h = logging.StreamHandler(stdout_buf)
    stdout_h.setLevel(logging.INFO)
    stdout_h.addFilter(auth_filter)
    stdout_h.addFilter(entrypoint.MaxLevelFilter(logging.WARNING))
    stdout_h.setFormatter(formatter)

    stderr_h = logging.StreamHandler(stderr_buf)
    stderr_h.setLevel(logging.ERROR)
    stderr_h.addFilter(auth_filter)
    stderr_h.addFilter(entrypoint.MinLevelFilter(logging.ERROR))
    stderr_h.setFormatter(formatter)

    test_logger = logging.getLogger("test_router_logger")
    test_logger.setLevel(logging.INFO)
    test_logger.handlers = [stdout_h, stderr_h]
    test_logger.addFilter(auth_filter)

    # 1. Log client auth error -> should route to stdout as WARNING, not stderr
    try:
        raise ValueError("Invalid proxy server token passed")
    except ValueError:
        test_logger.exception("Auth failed: Invalid proxy server token passed")

    stdout_output = stdout_buf.getvalue()
    stderr_output = stderr_buf.getvalue()

    assert "[WARNING]" in stdout_output
    assert "Invalid proxy server token passed" in stdout_output
    assert "[Traceback:" not in stdout_output
    assert stderr_output == ""

    # Clear buffers
    stdout_buf.seek(0)
    stdout_buf.truncate(0)
    stderr_buf.seek(0)
    stderr_buf.truncate(0)

    # 2. Log real server error -> should route to stderr as ERROR, not stdout
    try:
        raise RuntimeError("Prisma engine query failed fatally")
    except RuntimeError:
        test_logger.exception("Prisma failure")

    stdout_output = stdout_buf.getvalue()
    stderr_output = stderr_buf.getvalue()

    assert stdout_output == ""
    assert "[ERROR]" in stderr_output
    assert "Prisma failure" in stderr_output
    assert "[Traceback:" in stderr_output
    assert "Prisma engine query failed fatally" in stderr_output
