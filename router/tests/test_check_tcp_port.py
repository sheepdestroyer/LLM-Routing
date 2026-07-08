import pytest
import asyncio
from unittest.mock import AsyncMock, patch, MagicMock
import os
import sys

# Ensure router directory is in sys.path
router_path = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if router_path not in sys.path:
    sys.path.insert(0, router_path)

import main

@pytest.mark.asyncio
async def test_check_tcp_port_success():
    mock_reader = MagicMock()
    mock_writer = AsyncMock()
    mock_writer.close = MagicMock()
    mock_writer.wait_closed = AsyncMock()

    with patch("asyncio.open_connection", new_callable=AsyncMock) as mock_open_connection:
        mock_open_connection.return_value = (mock_reader, mock_writer)

        result = await main.check_tcp_port("127.0.0.1", 8080)

        assert result is True
        mock_open_connection.assert_called_once_with("127.0.0.1", 8080)
        mock_writer.close.assert_called_once()
        mock_writer.wait_closed.assert_called_once()

@pytest.mark.asyncio
async def test_check_tcp_port_failure_timeout():
    with patch("asyncio.open_connection", new_callable=AsyncMock) as mock_open_connection:
        mock_open_connection.side_effect = asyncio.TimeoutError()

        result = await main.check_tcp_port("127.0.0.1", 8080)

        assert result is False
        mock_open_connection.assert_called_once_with("127.0.0.1", 8080)

@pytest.mark.asyncio
async def test_check_tcp_port_failure_connection_error():
    with patch("asyncio.open_connection", new_callable=AsyncMock) as mock_open_connection:
        mock_open_connection.side_effect = ConnectionRefusedError()

        result = await main.check_tcp_port("127.0.0.1", 8080)

        assert result is False
        mock_open_connection.assert_called_once_with("127.0.0.1", 8080)
