import os
import pytest
from unittest.mock import patch, AsyncMock

# Set CONFIG_PATH for import
os.environ["CONFIG_PATH"] = "router/config.yaml"

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from main import get_dashboard_stats

@pytest.mark.anyio
async def test_get_dashboard_stats():
    mock_data = {
        "valkey_status": "connected",
        "litellm_status": "healthy",
        "best_free_model": "test-model"
    }

    with patch("main.get_dashboard_data", new_callable=AsyncMock) as mock_get_dashboard_data:
        mock_get_dashboard_data.return_value = mock_data

        result = await get_dashboard_stats()

        mock_get_dashboard_data.assert_called_once()
        assert result == mock_data
