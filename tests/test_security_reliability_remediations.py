import json
from pathlib import Path

from tools.fresh_24h import two_pass_score
from tools.fresh_24h.fresh_24h_scan import should_record_refresh
from tools.fresh_24h.push_to_gsheet import (
    _reject_pending_semantic,
    neutralize_spreadsheet_formula,
    replace_sheet_values_safely,
)
from tools.io_utils import atomic_write_text


def test_external_sheet_values_are_neutralized():
    for value in ("=IMPORTXML(\"x\")", "+1+1", "-2+3", "@SUM(A1:A2)", "\t=1", "\r=1"):
        safe = neutralize_spreadsheet_formula(value)
        assert safe.startswith("'")
    assert neutralize_spreadsheet_formula("ordinary company") == "ordinary company"
    assert neutralize_spreadsheet_formula(3.3) == 3.3


def test_sheet_replacement_uses_raw_without_clear():
    calls = []

    class Worksheet:
        def update(self, values, **kwargs):
            calls.append(("update", values, kwargs))

        def resize(self, **kwargs):
            calls.append(("resize", kwargs))

        def clear(self):
            raise AssertionError("destructive clear must not be called")

    replace_sheet_values_safely(Worksheet(), [["公司"], ["=bad"]], min_rows=5, min_cols=3)

    assert calls[0][0] == "update"
    assert calls[0][2]["value_input_option"] == "RAW"
    assert calls[0][1][1][0] == "'=bad"
    assert calls[1] == ("resize", {"rows": 5, "cols": 3})


def test_atomic_write_never_leaves_temp_file(tmp_path):
    target = tmp_path / "state.json"
    atomic_write_text(target, json.dumps({"ok": True}) + "\n")

    assert json.loads(target.read_text()) == {"ok": True}
    assert not list(tmp_path.glob(f".{target.name}.*.tmp"))


def test_failed_scan_does_not_advance_refresh_cursor():
    assert should_record_refresh([], new_count=0) is True
    # Partial results are useful for review but must not consume the refresh
    # watermark: the failed portal's window must be retried next time.
    assert should_record_refresh([{"portal": "jobsdb", "error": "timeout"}], new_count=2) is False
    assert should_record_refresh([{"portal": "jobsdb", "error": "timeout"}], new_count=0) is False


def test_formal_push_blocks_pending_semantic_rows_by_default():
    rows = [
        {
            "语义匹配来源": "pending_fallback",
            "语义待处理数": "1",
            "语义待处理任务": "semantic_resume_match:abc123",
        }
    ]

    assert _reject_pending_semantic(rows, allow=False, context="test push") is True
    assert _reject_pending_semantic(rows, allow=True, context="test push") is False


def test_two_pass_hard_drops_below_final_by_default(monkeypatch, tmp_path):
    class Score:
        score = 3.5
        grade = "C"
        resume_ver = "C"
        reason = "initial"
        confidence = "中"

    class LowScore(Score):
        score = 2.8
        reason = "deep low"

    scores = iter([Score(), LowScore()])
    monkeypatch.setattr(two_pass_score, "score_hit", lambda *args, **kwargs: next(scores))
    monkeypatch.setattr(
        two_pass_score,
        "deep_enrich_hit",
        lambda hit, repo: ("full description", "deep"),
    )

    rows, meta = two_pass_score.run_two_pass(
        [{"title": "Role", "company": "Co", "url": "https://example.com/1", "teaser": "x"}],
        repo=tmp_path,
        sleep_s=0,
    )

    assert rows == []
    assert len(meta["dropped_final"]) == 1
