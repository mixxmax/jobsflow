from pathlib import Path

from tools.validate_command_scope import validate_command_scope, validate_command_scope_text


def test_command_scope_table_has_one_entry_for_each_command():
    assert validate_command_scope() == []


def test_command_scope_rejects_duplicate_or_missing_entry():
    text = """| 命令 | R | PW | TW | D | EXT | C | ENTRY |\n|---|---|---|---|---|---|---|---|\n| /scan | 读 | 无 | 无 | 无 | 无 | 无 | tools.workflow scan |\n| /scan | 读 | 无 | 无 | 无 | 无 | 无 | tools.workflow scan |\n| /push | 读 | 写 | 无 | 无 | 表格 | 是 | — |\n"""
    errors = validate_command_scope_text(text)
    assert any("duplicate command /scan" in error for error in errors)
    assert any("/push has no unique ENTRY" in error for error in errors)
