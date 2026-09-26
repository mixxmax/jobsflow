"""The README demo must tell the same story in every language and match the rules.

Running the whole demo takes about a minute and a half (every step measures the
real page capacity), so the suite only pins what the narration claims: each
scripted draft hits the verb tier the demo says it hits.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from tools.workflow.materials_vnext.semantic_lint import (
    FUNCTION_VERB_STEMS,
    INFLATION_VERB_STEMS,
    high_risk_verb_stems,
)

_DEMO = Path(__file__).resolve().parents[1] / "scripts" / "demo.py"


def _load_demo():
    spec = importlib.util.spec_from_file_location("jobsflow_demo", _DEMO)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_language_has_the_same_scenes():
    demo = _load_demo()
    keys = {lang: set(text) for lang, text in demo.TEXT.items()}
    assert set(keys) == {"zh", "zh-hant", "en"}
    assert len({frozenset(value) for value in keys.values()}) == 1


def test_scripted_drafts_hit_the_tiers_the_narration_names():
    demo = _load_demo()
    assert high_risk_verb_stems(demo.DRAFTS["led"][1]) & INFLATION_VERB_STEMS == {"lead"}
    assert high_risk_verb_stems(demo.DRAFTS["managed"][1]) & INFLATION_VERB_STEMS == {"manage"}
    drafted = high_risk_verb_stems(demo.DRAFTS["drafted"][1])
    assert drafted & FUNCTION_VERB_STEMS == {"draft"}
    assert not drafted & INFLATION_VERB_STEMS
