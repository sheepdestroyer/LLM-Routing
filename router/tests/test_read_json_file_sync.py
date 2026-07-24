import json
from unittest.mock import patch, AsyncMock
import pytest

from router import main


@pytest.mark.asyncio
async def test_read_json_file_sync_success():
    mock_data = '{"key": "value"}'
    mock_file = AsyncMock()
    mock_file.read.return_value = mock_data
    mock_file.__aenter__.return_value = mock_file
    with patch("aiofiles.open", return_value=mock_file):
        result = await main._read_json_file_async("dummy_path.json")
        assert result == {"key": "value"}


@pytest.mark.parametrize(
    "mock_kwargs, expected_exc, match_msg",
    [
        (
            {"return_value": AsyncMock(__aenter__=AsyncMock(return_value=AsyncMock(read=AsyncMock(return_value='{"key": "value"'))))},
            json.JSONDecodeError,
            r"Unterminated string starting at|Expecting",
        ),
        (
            {"side_effect": FileNotFoundError("No such file or directory: 'dummy_path.json'")},
            FileNotFoundError,
            r"No such file or directory",
        ),
        (
            {"side_effect": PermissionError("Permission denied: 'dummy_path.json'")},
            PermissionError,
            r"Permission denied",
        ),
    ],
)
@pytest.mark.asyncio
async def test_read_json_file_sync_errors(mock_kwargs, expected_exc, match_msg):
    with patch("aiofiles.open", **mock_kwargs):
        with pytest.raises(expected_exc, match=match_msg):
            await main._read_json_file_async("dummy_path.json")
