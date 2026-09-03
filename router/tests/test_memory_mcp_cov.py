import io
from unittest.mock import patch
import pytest

from router.memory_mcp import main_loop


@pytest.mark.anyio
async def test_main_loop_result_none():
    """Verify main_loop skips output when handle_request returns None."""
    input_data = (
        "\n"  # empty line (tested if not line)
        + '{"jsonrpc": "2.0", "method": "notify_something"}\n'  # req_id is None
        + '{"jsonrpc": "2.0", "id": 1, "method": "unsupported_method"}\n'  # result is None
    )
    stdin_mock = io.StringIO(input_data)
    stdout_mock = io.StringIO()

    with patch("sys.stdin", stdin_mock), patch("sys.stdout", stdout_mock):
        await main_loop()

    # Nothing should be written to stdout because method returns None or has no id
    assert stdout_mock.getvalue() == ""


@pytest.mark.anyio
async def test_main_loop_result_valid_and_errors():
    """Verify main_loop processes valid responses, json errors, and exceptions."""
    input_data = (
        "not-valid-json\n"  # JSONDecodeError
        + '{"jsonrpc": "2.0", "id": 2, "method": "tools/list"}\n'  # valid response
    )
    stdin_mock = io.StringIO(input_data)
    stdout_mock = io.StringIO()

    with patch("sys.stdin", stdin_mock), patch("sys.stdout", stdout_mock):
        await main_loop()

    out = stdout_mock.getvalue()
    assert '"id":2' in out
    assert '"result"' in out


@pytest.mark.anyio
async def test_main_loop_unexpected_exception():
    """Verify main_loop handles unexpected exceptions inside the request processing."""
    input_data = '{"jsonrpc": "2.0", "id": 3, "method": "tools/call"}\n'
    stdin_mock = io.StringIO(input_data)
    stdout_mock = io.StringIO()

    with (
        patch("sys.stdin", stdin_mock),
        patch("sys.stdout", stdout_mock),
        patch("router.memory_mcp.handle_request", side_effect=RuntimeError("unexpected crash")),
    ):
        await main_loop()

    assert stdout_mock.getvalue() == ""
