"""Synthetic Fixture Loader and Case Manager."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

FIXTURES_DIR = Path(__file__).resolve().parent / "cases"


@dataclass
class TestCase:
    case_id: str
    scenario: Dict[str, Any]
    profile: Dict[str, Any]
    jd_text: str
    baseline_cv: Dict[str, Any]
    baseline_cl: Dict[str, Any]
    expected_events: List[Dict[str, Any]]
    expected_assertions: List[Dict[str, Any]]
    forbidden_side_effects: List[Dict[str, Any]]
    case_dir: Path


class FixtureLoader:
    """Loads and validates synthetic test fixtures."""

    def __init__(self, cases_dir: Optional[Path] = None):
        self.cases_dir = cases_dir or FIXTURES_DIR

    def list_case_ids(self) -> List[str]:
        if not self.cases_dir.exists():
            return []
        return sorted([
            d.name for d in self.cases_dir.iterdir()
            if d.is_dir() and (d / "scenario.json").exists()
        ])

    def load_case(self, case_id_or_path: str | Path) -> TestCase:
        path = Path(case_id_or_path)
        if not path.is_dir():
            path = self.cases_dir / str(case_id_or_path)

        if not path.is_dir():
            raise FileNotFoundError(f"Fixture case directory not found: {path}")

        scenario = self._read_json(path / "scenario.json")
        profile = self._read_json(path / "profile.json", default={})
        jd_text = self._read_text(path / "jd.md", default="")
        baseline_cv = self._read_json(path / "baseline_cv.json", default={})
        baseline_cl = self._read_json(path / "baseline_cl.json", default={})
        expected_events = self._read_json(path / "expected_events.json", default=[])
        expected_assertions = self._read_json(path / "expected_assertions.json", default=[])
        forbidden_side_effects = self._read_json(path / "forbidden_side_effects.json", default=[])

        return TestCase(
            case_id=scenario.get("case_id", path.name),
            scenario=scenario,
            profile=profile,
            jd_text=jd_text,
            baseline_cv=baseline_cv,
            baseline_cl=baseline_cl,
            expected_events=expected_events,
            expected_assertions=expected_assertions,
            forbidden_side_effects=forbidden_side_effects,
            case_dir=path,
        )

    def _read_json(self, file_path: Path, default: Any = None) -> Any:
        if not file_path.is_file():
            if default is not None:
                return default
            raise FileNotFoundError(f"Missing required fixture file: {file_path}")
        with open(file_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _read_text(self, file_path: Path, default: str = "") -> str:
        if not file_path.is_file():
            return default
        with open(file_path, "r", encoding="utf-8") as f:
            return f.read()
