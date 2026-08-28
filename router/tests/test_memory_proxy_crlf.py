import pytest
from unittest.mock import MagicMock
from fastapi import HTTPException, Request
from router.main import proxy_memory, proxy_audio

@pytest.mark.asyncio
async def test_proxy_memory_crlf():
    mock_request = MagicMock(spec=Request)
    with pytest.raises(HTTPException) as exc:
        await proxy_memory(mock_request, path="/test\r\nHost: evil.com")
    assert exc.value.status_code == 400

@pytest.mark.asyncio
async def test_proxy_audio_crlf():
    mock_request = MagicMock(spec=Request)
    with pytest.raises(HTTPException) as exc:
        await proxy_audio(mock_request, path="/test\r\nHost: evil.com")
    assert exc.value.status_code == 400
