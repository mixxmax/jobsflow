"""Offline tests for the advisory TypeSafe judgment layer.

The tests never touch the network: a fake client reproduces the SDK answer
shapes, so the deterministic policy around the model - thresholds, batching,
fail-closed behaviour and trim ranking - is what is actually under test.
"""

from __future__ import annotations

import json

import pytest

from tools.workflow.typesafe_judgments import (
    CANDIDATE_TOKEN_OVERLAP,
    LOAD_BEARING_LEVELS,
    MAX_LINES_PER_RUN,
    Primitives,
    RedundancyPair,
    Requirement,
    TypeSafeJudgmentError,
    Line,
    LineJudgment,
    _accepts_model,
    _overlap,
    find_redundant_pairs,
    judge_line_support,
    rank_trim_candidates,
    run_judgments,
    typesafe_available,
    typesafe_status,
)


class _Noul:
    def __init__(self, noul: float) -> None:
        self.type = "noul"
        self.noul = noul


class _Score:
    def __init__(self, score: float, confidence: float = 0.8) -> None:
        self.type = "score"
        self.score = score
        self.confidence = confidence
        self.legend = {index: label for index, label in enumerate(LOAD_BEARING_LEVELS)}
        self.probabilities = {}


class _Response:
    def __init__(self, nouls: dict, scores: dict) -> None:
        self.nouls = nouls
        self.scores = scores
        self.choices = {}


class _QuestionNoul:
    """Stand-in for the SDK's Noul, recording what was actually asked."""

    def __init__(self, instructions: str, criteria: dict | None = None) -> None:
        self.instructions = instructions
        self.criteria = criteria or {}


class _QuestionScore:
    """Stand-in for the SDK's Score, recording levels and instructions."""

    def __init__(self, instructions: str, criteria: list | None = None) -> None:
        self.instructions = instructions
        self.criteria = list(criteria or [])


# The product path resolves these from ``typesafe_sdk``; a test resolves them
# from stand-ins so the deterministic policy is what is under test.
FAKE_PRIMITIVES = Primitives(noul=_QuestionNoul, score=_QuestionScore)


class _FakeClient:
    """Answers support/load-bearing from a table and redundancy from a set."""

    def __init__(
        self,
        support: dict[str, dict[str, float]],
        load_bearing: dict[str, float],
        redundant: set[tuple[str, str]] | None = None,
    ) -> None:
        self._support = support
        self._load = load_bearing
        self._redundant = redundant or set()
        self.calls: list[dict] = []

    def system_one(self, state, questions, **kwargs):  # noqa: ANN001 - SDK signature
        self.calls.append({"state": state, "questions": sorted(questions)})
        nouls: dict[str, _Noul] = {}
        scores: dict[str, _Score] = {}
        for key in questions:
            line_id, _, suffix = key.partition("__")
            if suffix.startswith("supports_"):
                requirement_id = suffix[len("supports_") :]
                value = self._support.get(line_id, {}).get(requirement_id, 0.0)
                nouls[key] = _Noul(value)
            elif suffix == "load_bearing":
                scores[key] = _Score(self._load.get(line_id, 0.0))
            elif suffix == "same_evidence":
                # Only the redundancy request carries a ``pairs`` state.
                position = int(key.split("__")[0].split("_")[1])
                pair = state["pairs"][position]
                same = (pair["left_id"], pair["right_id"]) in self._redundant
                nouls[key] = _Noul(0.95 if same else 0.05)
        return _Response(nouls, scores)


REQUIREMENTS = [
    Requirement(id="JD-001", text="Proficient in KYC/Compliance/AML procedures"),
    Requirement(id="JD-002", text="Strong verbal and written communication skills"),
]

LINES = [
    Line(id="cv-a-001", text="Redesigned procedures for five critical assets, cutting related costs by 30%."),
    Line(id="cv-a-002", text="Cut related costs and risks by approximately 30% by redesigning procedures for five critical assets."),
    Line(id="cv-a-003", text="Led 10+ commercial trials, including revocation-right and unpaid-capital disputes."),
]


def test_support_and_load_bearing_are_read_per_line():
    client = _FakeClient(
        support={"cv-a-001": {"JD-001": 0.82}, "cv-a-002": {"JD-001": 0.80}, "cv-a-003": {"JD-002": 0.10}},
        load_bearing={"cv-a-001": 2.0, "cv-a-002": 0.0, "cv-a-003": 1.0},
    )
    judgments = judge_line_support(LINES, REQUIREMENTS, client=client, primitives=FAKE_PRIMITIVES)

    assert [item.line_id for item in judgments] == ["cv-a-001", "cv-a-002", "cv-a-003"]
    assert judgments[0].evidenced_requirements == ["JD-001"]
    assert judgments[2].evidenced_requirements == []
    assert judgments[1].load_bearing_label == LOAD_BEARING_LEVELS[0]
    assert judgments[0].load_bearing_label == LOAD_BEARING_LEVELS[2]
    # Two requirements plus the load-bearing score is three questions per line,
    # so all three lines fit in one batch and share a single request.
    assert len(client.calls) == 1
    assert len(client.calls[0]["questions"]) == 9


def test_missing_requirements_fails_closed():
    with pytest.raises(TypeSafeJudgmentError):
        judge_line_support(LINES, [], client=_FakeClient({}, {}))


def test_unavailable_environment_reports_the_cause(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    assert typesafe_available() is False
    with pytest.raises(TypeSafeJudgmentError) as excinfo:
        judge_line_support(LINES, REQUIREMENTS)
    assert "TYPESAFE_API_KEY" in str(excinfo.value)


def test_token_overlap_prefilter_selects_the_duplicate_pair():
    overlap = _overlap(LINES[0].text, LINES[1].text)
    assert overlap >= CANDIDATE_TOKEN_OVERLAP
    assert _overlap(LINES[0].text, LINES[2].text) < CANDIDATE_TOKEN_OVERLAP


def test_redundancy_keeps_only_confirmed_pairs():
    client = _FakeClient({}, {}, redundant={("cv-a-001", "cv-a-002")})
    pairs = find_redundant_pairs(LINES, client=client, primitives=FAKE_PRIMITIVES)

    assert [(pair.left_id, pair.right_id) for pair in pairs] == [("cv-a-001", "cv-a-002")]
    assert pairs[0].probability == 0.95
    assert pairs[0].token_overlap >= CANDIDATE_TOKEN_OVERLAP


def test_redundancy_with_no_candidate_pair_makes_no_request():
    unique = [
        Line(id="cv-x-001", text="Reviewed 100+ commercial contracts for 10+ clients."),
        Line(id="cv-x-002", text="Mandarin Chinese: Native."),
    ]
    client = _FakeClient({}, {})
    assert find_redundant_pairs(unique, client=client) == []
    assert client.calls == []


def test_trim_candidates_require_weak_support_and_a_duplicate():
    judgments = [
        LineJudgment(line_id="cv-a-001", support={"JD-001": 0.82}, load_bearing=2.0, load_bearing_label=LOAD_BEARING_LEVELS[2]),
        LineJudgment(line_id="cv-a-002", support={"JD-001": 0.80}, load_bearing=0.0, load_bearing_label=LOAD_BEARING_LEVELS[0]),
        LineJudgment(line_id="cv-a-003", support={"JD-002": 0.10}, load_bearing=1.0, load_bearing_label=LOAD_BEARING_LEVELS[1]),
    ]
    pairs = [RedundancyPair(left_id="cv-a-001", right_id="cv-a-002", probability=0.95, token_overlap=0.6)]

    # cv-a-001 is strongly supported, cv-a-002 duplicates a strong line, and
    # cv-a-003 is weak but unique: nothing may be trimmed.
    assert rank_trim_candidates(judgments, pairs, LINES) == []


def test_weak_and_duplicated_line_is_offered_for_trimming():
    judgments = [
        LineJudgment(line_id="cv-a-001", support={"JD-001": 0.20}, load_bearing=0.0, load_bearing_label=LOAD_BEARING_LEVELS[0]),
        LineJudgment(line_id="cv-a-002", support={"JD-001": 0.18}, load_bearing=0.0, load_bearing_label=LOAD_BEARING_LEVELS[0]),
    ]
    pairs = [RedundancyPair(left_id="cv-a-001", right_id="cv-a-002", probability=0.93, token_overlap=0.6)]

    # The weaker line comes first: ranking is weakest support plus load bearing.
    assert rank_trim_candidates(judgments, pairs, LINES) == ["cv-a-002", "cv-a-001"]


def test_core_evidence_is_never_offered_for_trimming():
    judgments = [
        LineJudgment(line_id="cv-a-001", support={"JD-001": 0.05}, load_bearing=3.0, load_bearing_label=LOAD_BEARING_LEVELS[3]),
    ]
    pairs = [RedundancyPair(left_id="cv-a-001", right_id="cv-a-003", probability=0.9, token_overlap=0.6)]

    assert rank_trim_candidates(judgments, pairs, LINES) == []


def test_run_judgments_returns_one_advisory_report():
    client = _FakeClient(
        support={"cv-a-001": {"JD-001": 0.20}, "cv-a-002": {"JD-001": 0.18}, "cv-a-003": {"JD-002": 0.12}},
        load_bearing={"cv-a-001": 0.0, "cv-a-002": 0.0, "cv-a-003": 1.0},
        redundant={("cv-a-001", "cv-a-002")},
    )
    report = run_judgments(REQUIREMENTS, LINES, client=client, primitives=FAKE_PRIMITIVES)
    payload = report.to_dict()

    assert report.trim_candidates == ["cv-a-002", "cv-a-001"]
    assert payload["policy"]["support_strong"] == 0.70
    assert payload["policy"]["keep_level"] == "supporting"
    assert payload["line_judgments"][0]["evidenced_requirements"] == []
    assert json.dumps(payload)  # the report stays JSON-serializable for the CLI


def test_status_is_disabled_without_a_credential_and_names_the_fix(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    status = typesafe_status()

    assert status["enabled"] is False
    assert status["key_present"] is False
    assert status["reason"] in {"api_key_missing", "api_key_missing_and_sdk_not_installed"}
    assert "TYPESAFE_API_KEY" in status["next_action"]
    assert typesafe_available() is False


def test_status_requires_both_the_key_and_the_sdk(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    from tools.workflow import typesafe_judgments

    monkeypatch.setattr(typesafe_judgments, "_sdk_installed", lambda: True)
    assert typesafe_status() == {
        "enabled": True,
        "reason": "api_key_and_sdk_present",
        "key_present": True,
        "sdk_present": True,
        "engine": "typesafe-system-one",
    }
    assert typesafe_available() is True


def test_status_blames_the_missing_sdk_when_only_the_key_is_present(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    from tools.workflow import typesafe_judgments

    monkeypatch.setattr(typesafe_judgments, "_sdk_installed", lambda: False)
    status = typesafe_status()

    assert status["enabled"] is False
    assert status["reason"] == "sdk_not_installed"
    assert status["key_present"] is True


def test_status_flag_reports_the_decision_without_any_request(monkeypatch, capsys):
    from tools.workflow import typesafe_judgments

    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    exit_code = typesafe_judgments.main(["--status"])
    envelope = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert envelope["status"] == "succeeded"
    assert envelope["advisory_only"] is True
    assert envelope["typesafe"]["enabled"] is False


def test_model_is_passed_only_when_the_sdk_signature_accepts_it():
    class _TakesModel:
        def system_one(self, state, questions, model=None):  # noqa: ANN001
            return None

    class _TakesNoModel:
        def system_one(self, state, questions):  # noqa: ANN001
            return None

    class _VarKeyword:
        def system_one(self, state, questions, **kwargs):  # noqa: ANN001
            return None

    assert _accepts_model(_TakesModel()) is True
    assert _accepts_model(_TakesNoModel()) is False
    assert _accepts_model(_VarKeyword()) is True


def test_line_ceiling_is_bounded_in_code_and_reported():
    """The feature is on by default, so its worst-case budget is a constant."""

    many = [
        Line(id=f"cv-x-{index:03d}", text=f"Reviewed contract number {index}.")
        for index in range(MAX_LINES_PER_RUN + 5)
    ]
    client = _FakeClient({}, {})
    report = run_judgments(REQUIREMENTS, many, client=client, primitives=FAKE_PRIMITIVES)

    assert len(report.line_judgments) == MAX_LINES_PER_RUN
    assert any("lines_truncated" in note for note in report.notes)


def test_questions_carry_the_state_paths_the_model_needs():
    client = _FakeClient({}, {})
    judge_line_support(LINES[:1], REQUIREMENTS[:1], client=client, primitives=FAKE_PRIMITIVES)

    state = client.calls[0]["state"]
    assert state["job_requirements"][0]["id"] == "JD-001"
    assert state["lines"][0]["id"] == "cv-a-001"
    assert len(client.calls[0]["questions"]) == 2  # one Noul per requirement plus one Score


def test_cli_reports_a_blocked_envelope_when_the_service_is_unavailable(tmp_path, monkeypatch, capsys):
    from tools.workflow import typesafe_judgments

    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    source = tmp_path / "input.json"
    source.write_text(
        json.dumps({"requirements": [{"id": "JD-001", "text": "AML procedures"}], "lines": [{"id": "l1", "text": "Did AML work."}]}),
        encoding="utf-8",
    )
    exit_code = typesafe_judgments.main(["--input", str(source)])
    envelope = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert envelope["status"] == "blocked"
    assert "TYPESAFE_API_KEY" in envelope["blockers"][0]


@pytest.mark.parametrize("body", ["{}", "", "not json", "[]"])
def test_cli_never_leaks_a_traceback_on_bad_input(tmp_path, capsys, body):
    from tools.workflow import typesafe_judgments

    source = tmp_path / "input.json"
    source.write_text(body, encoding="utf-8")
    exit_code = typesafe_judgments.main(["--input", str(source)])
    envelope = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert envelope["status"] == "blocked"
    assert envelope["blockers"]
