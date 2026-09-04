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
