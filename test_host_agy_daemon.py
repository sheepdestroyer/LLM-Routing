import os
import json
import pytest
from unittest.mock import patch, mock_open

import host_agy_daemon

def test_get_last_conversation_id_success(monkeypatch):
    test_data = {"/test/path": "conv_123"}
    monkeypatch.setattr(os, "getcwd", lambda: "/test/path")
    monkeypatch.setattr(os.path, "exists", lambda x: True)

    m_open = mock_open(read_data=json.dumps(test_data))
    with patch("builtins.open", m_open):
        assert host_agy_daemon.get_last_conversation_id() == "conv_123"

def test_get_last_conversation_id_not_found(monkeypatch):
    test_data = {"/other/path": "conv_123"}
    monkeypatch.setattr(os, "getcwd", lambda: "/test/path")
    monkeypatch.setattr(os.path, "exists", lambda x: True)

    m_open = mock_open(read_data=json.dumps(test_data))
    with patch("builtins.open", m_open):
        assert host_agy_daemon.get_last_conversation_id() is None

def test_get_last_conversation_id_file_missing(monkeypatch):
    monkeypatch.setattr(os.path, "exists", lambda x: False)
    assert host_agy_daemon.get_last_conversation_id() is None

def test_get_last_conversation_id_exception(monkeypatch):
    monkeypatch.setattr(os.path, "exists", lambda x: True)
    # Invalid JSON to trigger JSONDecodeError
    m_open = mock_open(read_data="{invalid json")
    with patch("builtins.open", m_open):
        assert host_agy_daemon.get_last_conversation_id() is None

def test_get_last_conversation_id_io_error(monkeypatch):
    monkeypatch.setattr(os.path, "exists", lambda x: True)

    def raise_error(*args, **kwargs):
        raise IOError("permission denied")

    with patch("builtins.open", side_effect=raise_error):
        assert host_agy_daemon.get_last_conversation_id() is None
