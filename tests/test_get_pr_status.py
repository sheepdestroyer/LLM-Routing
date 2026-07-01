import pytest
import subprocess
import json
from unittest.mock import patch, MagicMock
from scripts.get_pr_status import run_cmd, get_pr_status

def test_run_cmd_success():
    output = run_cmd(["echo", "hello"])
    assert output == "hello"

def test_run_cmd_strips_whitespace():
    output = run_cmd(["echo", "  hello  "])
    assert output == "hello"

def test_run_cmd_error():
    with pytest.raises(subprocess.CalledProcessError):
        run_cmd(["false"])

def test_run_cmd_timeout():
    # run_cmd has a 30s timeout.
    with pytest.raises(subprocess.TimeoutExpired):
        run_cmd(["sleep", "31"])

@patch("scripts.get_pr_status.run_cmd")
def test_get_pr_status_success(mock_run_cmd, capsys):
    mock_data = {
        "state": "OPEN",
        "reviewDecision": "APPROVED",
        "statusCheckRollup": [
            {"conclusion": "SUCCESS", "name": "test1"},
            {"state": "CLEAN", "name": "test2"},
            {"conclusion": "FAILURE", "name": "test3"}
        ]
    }
    mock_run_cmd.return_value = json.dumps(mock_data)

    get_pr_status("123")

    captured = capsys.readouterr()
    assert "PR Status: OPEN" in captured.out
    assert "Review Decision: APPROVED" in captured.out
    assert "Checks: 2/3 passed" in captured.out
    mock_run_cmd.assert_called_once_with(["gh", "pr", "view", "123", "--json", "state,reviewDecision,statusCheckRollup"])

@patch("scripts.get_pr_status.run_cmd")
def test_get_pr_status_no_id(mock_run_cmd, capsys):
    mock_data = {
        "state": "MERGED",
        "reviewDecision": None,
        "statusCheckRollup": []
    }
    mock_run_cmd.return_value = json.dumps(mock_data)

    get_pr_status()

    captured = capsys.readouterr()
    assert "PR Status: MERGED" in captured.out
    assert "Review Decision: NONE" in captured.out
    assert "Checks: 0/0 passed" in captured.out
    mock_run_cmd.assert_called_once_with(["gh", "pr", "view", "--json", "state,reviewDecision,statusCheckRollup"])

@patch("scripts.get_pr_status.run_cmd")
def test_get_pr_status_error(mock_run_cmd, capsys):
    mock_run_cmd.side_effect = subprocess.CalledProcessError(1, ["gh"], stderr="gh not found")

    with pytest.raises(SystemExit) as e:
        get_pr_status("123")

    assert e.value.code == 1
    captured = capsys.readouterr()
    assert "Error: Failed to fetch PR status: gh not found" in captured.err

@patch("scripts.get_pr_status.run_cmd")
def test_get_pr_status_invalid_json(mock_run_cmd, capsys):
    mock_run_cmd.return_value = "invalid json"

    with pytest.raises(SystemExit) as e:
        get_pr_status("123")

    assert e.value.code == 1
    captured = capsys.readouterr()
    assert "Error: Failed to parse gh CLI output" in captured.err
