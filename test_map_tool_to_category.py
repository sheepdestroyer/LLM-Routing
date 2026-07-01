import pytest
from router.main import map_tool_to_category

def test_map_tool_to_category_tree():
    assert map_tool_to_category("tree") == "tree"
    assert map_tool_to_category("list_dir") == "tree"
    assert map_tool_to_category("list-dir") == "tree"
    assert map_tool_to_category("run_tree") == "tree"

def test_map_tool_to_category_shell():
    assert map_tool_to_category("shell") == "shell"
    assert map_tool_to_category("command") == "shell"
    assert map_tool_to_category("execute") == "shell"
    assert map_tool_to_category("run") == "shell"

def test_map_tool_to_category_write():
    assert map_tool_to_category("write") == "write"
    assert map_tool_to_category("edit") == "write"
    assert map_tool_to_category("create") == "write"
    assert map_tool_to_category("patch") == "write"
    assert map_tool_to_category("replace") == "write"
    assert map_tool_to_category("save") == "write"

def test_map_tool_to_category_view():
    assert map_tool_to_category("view") == "view"
    assert map_tool_to_category("read") == "view"
    assert map_tool_to_category("cat") == "view"
    assert map_tool_to_category("grep") == "view"
    assert map_tool_to_category("search") == "view"
    assert map_tool_to_category("find") == "view"

def test_map_tool_to_category_other():
    assert map_tool_to_category("unknown") == "other"
    assert map_tool_to_category("random_tool") == "other"

def test_map_tool_to_category_strip_and_lower():
    assert map_tool_to_category(" TREE ") == "tree"
    assert map_tool_to_category("  WRITE_FILE  ") == "write"

def test_map_tool_to_category_handle_dunder():
    assert map_tool_to_category("namespace__tree") == "tree"
    assert map_tool_to_category("another__namespace__cat") == "view"
    assert map_tool_to_category("prefix__unknown") == "other"
