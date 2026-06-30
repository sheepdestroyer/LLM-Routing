import pytest
from unittest.mock import patch, MagicMock
import sys
import os
import importlib.util

spec = importlib.util.spec_from_file_location("entrypoint", "litellm/entrypoint.py")
entrypoint = importlib.util.module_from_spec(spec)

mock_litellm = MagicMock()
mock_litellm.__file__ = "/mock/litellm/__init__.py"
mock_litellm.__path__ = []  # Ensure litellm is treated as a package for sub-module imports

mock_proxy_cli = MagicMock()

with patch('os.path.exists', return_value=False), \
     patch('builtins.print'), \
     patch('time.sleep'), \
     patch('os.execvp'), \
     patch('sys.stdout.flush'), \
     patch('glob.glob', return_value=[]), \
     patch('builtins.open'):

    sys.modules['litellm'] = mock_litellm
    sys.modules['litellm.proxy'] = MagicMock()
    sys.modules['litellm.proxy.proxy_cli'] = mock_proxy_cli
    spec.loader.exec_module(entrypoint)

def test_check_tcp_port_success():
    with patch('socket.socket') as mock_socket_class:
        mock_sock_instance = MagicMock()
        mock_sock_instance.connect_ex.return_value = 0
        mock_socket_class.return_value = mock_sock_instance

        result = entrypoint.check_tcp_port("127.0.0.1", 5432)

        assert result is True
        mock_sock_instance.connect_ex.assert_called_once_with(("127.0.0.1", 5432))
        mock_sock_instance.close.assert_called_once()
        mock_sock_instance.settimeout.assert_called_once_with(2.0)

def test_check_tcp_port_failure_connection_refused():
    with patch('socket.socket') as mock_socket_class:
        mock_sock_instance = MagicMock()
        mock_sock_instance.connect_ex.return_value = 111  # Connection refused
        mock_socket_class.return_value = mock_sock_instance

        result = entrypoint.check_tcp_port("127.0.0.1", 5432)

        assert result is False
        mock_sock_instance.connect_ex.assert_called_once_with(("127.0.0.1", 5432))
        mock_sock_instance.close.assert_called_once()

def test_check_tcp_port_failure_exception():
    with patch('socket.socket') as mock_socket_class:
        mock_socket_class.side_effect = Exception("Network error")

        result = entrypoint.check_tcp_port("127.0.0.1", 5432)

        assert result is False
