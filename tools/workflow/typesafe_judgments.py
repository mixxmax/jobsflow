"""Advisory TypeSafe System One judgments for JobsFlow.

Deterministic code owns every rule, threshold and side effect in this product;
the System One model supplies only the narrow semantic judgments that ordinary
code cannot make.  Three primitives are used, each for one kind of question:

* ``Noul``   - does this material line evidence this job requirement?
* ``Noul``   - do these two lines carry substantially the same evidence?
* ``Score``  - how load-bearing is this line for the requirement set?

The model never writes, never selects a handler and never decides policy.  It
returns probabilities; this module turns them into a ranked advisory report
that a human (or a bounded, audited transform) acts on.

Auto-enable is a two-part, fail-open check: :func:`typesafe_status` reports
whether this machine *could* ask for a judgment (credential present) and
whether the SDK is installed.  Callers that want "on when possible, off
otherwise" read that status and skip silently when it is disabled; nothing in
the product imports this module on a required path, so a missing credential
can only disable an advisory feature - it can never change scan, push,
materials or apply behaviour.

Usage:
    python3 -m tools.workflow.typesafe_judgments --input judgments.json

The input is JSON with a ``requirements`` list and a ``lines`` list::

    {"requirements": [{"id": "JD-001", "text": "..."}],
     "lines": [{"id": "cv-foo-023", "text": "..."}]}
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator, Sequence

# Policy thresholds.  They are deliberately explicit and in code: a threshold
# is a cost decision, not a model output, so changing one never re-runs
# inference and never needs a new prompt.
SUPPORT_STRONG = 0.70  # line clearly evidences the requirement
SUPPORT_WEAK = 0.35  # below this the line evidences nothing measurable
REDUNDANCY_STRONG = 0.70  # two lines carry substantially the same evidence
CANDIDATE_TOKEN_OVERLAP = 0.45  # deterministic pre-filter before any model call
LOAD_BEARING_LEVELS = ("not load bearing", "weakly supporting", "supporting", "core evidence")
KEEP_LEVEL = "supporting"  # a line at or above this level is never proposed for trimming
MAX_QUESTIONS_PER_REQUEST = 40  # lines are batched so one request stays inside this budget
MAX_LINES_PER_RUN = 80  # hard cost ceiling; the report says so when it trims

_WORD = re.compile(r"[a-z0-9]+")


class TypeSafeJudgmentError(RuntimeError):
    """Raised when an advisory judgment cannot be obtained.

    The message always names the cause (missing credential, missing SDK, API
    failure) so a caller can distinguish "the service said no" from "this
    machine cannot ask".
    """


@dataclass(frozen=True)
class Primitives:
    """The two question types this module needs, resolved from the SDK.

    Injecting these keeps the deterministic policy testable without the SDK:
    the product path resolves them from ``typesafe_sdk`` and a test resolves
    them from a stand-in.  Nothing else about the call is injectable, so a
    stand-in can never be mistaken for a real judgment.
    """

    noul: Any
    score: Any


def _load_primitives() -> Primitives:
    try:
        from typesafe_sdk import Noul, Score
    except ImportError as exc:  # pragma: no cover - exercised via typesafe_status
        raise TypeSafeJudgmentError(
            "typesafe_unavailable: set TYPESAFE_API_KEY and install typesafe-sdk"
        ) from exc
    return Primitives(noul=Noul, score=Score)


def _sdk_installed() -> bool:
    try:
        import typesafe_sdk  # noqa: F401
    except ImportError:
        return False
    return True


def typesafe_status() -> dict[str, Any]:
    """Report the auto-enable decision and the reason behind it.

    ``enabled`` is the single switch a caller acts on.  ``reason`` is always
    populated so a skipped advisory can say *why* without guessing, and
    ``next_action`` names the one command that would turn it on.
    """

    key_present = bool(os.environ.get("TYPESAFE_API_KEY", "").strip())
    sdk_present = _sdk_installed()
    if key_present and sdk_present:
        return {
            "enabled": True,
            "reason": "api_key_and_sdk_present",
            "key_present": True,
            "sdk_present": True,
            "engine": "typesafe-system-one",
        }
    if not key_present and not sdk_present:
        reason = "api_key_missing_and_sdk_not_installed"
    elif not key_present:
        reason = "api_key_missing"
    else:
        reason = "sdk_not_installed"
    return {
        "enabled": False,
        "reason": reason,
        "key_present": key_present,
        "sdk_present": sdk_present,
        "engine": "typesafe-system-one",
        "next_action": "bash tools/setup_env.sh --advisory && export TYPESAFE_API_KEY=...",
    }


def typesafe_available() -> bool:
    """Report whether this machine can ask for a judgment at all."""

    return bool(typesafe_status().get("enabled"))


@dataclass(frozen=True)
class Requirement:
    id: str
    text: str


@dataclass(frozen=True)
class Line:
    id: str
    text: str


@dataclass(frozen=True)
class LineJudgment:
    line_id: str
    support: dict[str, float] = field(default_factory=dict)
    load_bearing: float = 0.0
    load_bearing_label: str = LOAD_BEARING_LEVELS[0]
    load_bearing_confidence: float = 0.0

    @property
    def strongest_support(self) -> float:
        return max(self.support.values(), default=0.0)

    @property
    def evidenced_requirements(self) -> list[str]:
        return sorted(key for key, value in self.support.items() if value >= SUPPORT_STRONG)


@dataclass(frozen=True)
class RedundancyPair:
    left_id: str
    right_id: str
    probability: float
    token_overlap: float


@dataclass(frozen=True)
class JudgmentReport:
    line_judgments: list[LineJudgment]
    redundant_pairs: list[RedundancyPair]
    trim_candidates: list[str]
    notes: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "line_judgments": [
                {
                    "line_id": item.line_id,
                    "support": dict(sorted(item.support.items())),
                    "strongest_support": round(item.strongest_support, 4),
                    "evidenced_requirements": item.evidenced_requirements,
                    "load_bearing": item.load_bearing,
                    "load_bearing_label": item.load_bearing_label,
                    "load_bearing_confidence": round(item.load_bearing_confidence, 4),
                }
                for item in self.line_judgments
            ],
            "redundant_pairs": [
                {
                    "left_id": item.left_id,
                    "right_id": item.right_id,
                    "probability": round(item.probability, 4),
                    "token_overlap": round(item.token_overlap, 4),
                }
                for item in self.redundant_pairs
            ],
            "trim_candidates": list(self.trim_candidates),
            "notes": list(self.notes),
            "policy": {
                "support_strong": SUPPORT_STRONG,
                "support_weak": SUPPORT_WEAK,
                "redundancy_strong": REDUNDANCY_STRONG,
                "candidate_token_overlap": CANDIDATE_TOKEN_OVERLAP,
                "keep_level": KEEP_LEVEL,
                "load_bearing_levels": list(LOAD_BEARING_LEVELS),
                "max_lines_per_run": MAX_LINES_PER_RUN,
            },
        }


def _tokens(text: str) -> set[str]:
    return set(_WORD.findall(str(text or "").casefold()))


def _overlap(left: str, right: str) -> float:
    a, b = _tokens(left), _tokens(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _question_key(line_id: str, suffix: str) -> str:
    return f"{line_id}__{suffix}"


@contextmanager
def _client(model: str | None = None) -> Iterator[Any]:
    """Open a TypeSafe client, failing closed with a named cause."""

    if not typesafe_available():
        raise TypeSafeJudgmentError(
            "typesafe_unavailable: set TYPESAFE_API_KEY and install typesafe-sdk"
        )
    from typesafe_sdk import TypeSafeClient

    try:
        # The documented form is a context manager; the client authenticates
        # from TYPESAFE_API_KEY itself.
        with TypeSafeClient() as client:
            yield client
    except (TypeError, AttributeError, OSError) as exc:
        raise TypeSafeJudgmentError(f"typesafe_client_open_failed: {type(exc).__name__}: {exc}") from exc


def _accepts_model(client: Any) -> bool:
    """Whether this SDK build takes ``model`` on ``system_one``.

    Probed from the signature instead of guessed, so a build that rejects the
    keyword never sees a failed request that was retried (and billed) twice.
    """

    try:
        import inspect

        parameters = inspect.signature(client.system_one).parameters
    except (AttributeError, TypeError, ValueError):
        return False
    if "model" in parameters:
        return True
    return any(item.kind is inspect.Parameter.VAR_KEYWORD for item in parameters.values())


def _ask(client: Any, state: dict[str, Any], questions: dict[str, Any], *, model: str | None = None) -> Any:
    try:
        if model and _accepts_model(client):
            return client.system_one(state=state, questions=questions, model=model)
        return client.system_one(state=state, questions=questions)
    except Exception as exc:  # the SDK raises a family of typed API errors
        raise TypeSafeJudgmentError(f"typesafe_request_failed: {type(exc).__name__}: {exc}") from exc


def _requirement_state(requirements: Sequence[Requirement]) -> list[dict[str, str]]:
    return [{"id": item.id, "requirement": item.text} for item in requirements]


def judge_line_support(
    lines: Sequence[Line],
    requirements: Sequence[Requirement],
    *,
    model: str | None = None,
    client: Any | None = None,
    primitives: Primitives | None = None,
) -> list[LineJudgment]:
    """Ask, per line, which requirements it evidences and how load-bearing it is.

    Lines are batched so one request stays inside ``MAX_QUESTIONS_PER_REQUEST``:
    every line in a batch contributes one Noul per requirement plus one Score.
    All questions in a batch see the same state and are evaluated independently,
    so batching costs no accuracy and one request can serve many lines.
    """

    if not lines:
        return []
    if not requirements:
        raise TypeSafeJudgmentError("no_requirements_supplied")

    resolved = primitives or _load_primitives()
    per_line = len(requirements) + 1
    batch_size = max(1, MAX_QUESTIONS_PER_REQUEST // max(1, per_line))
    judgments: list[LineJudgment] = []
    for start in range(0, len(lines), batch_size):
        batch = lines[start : start + batch_size]
        if client is not None:
            judgments.extend(_judge_batch(client, batch, requirements, resolved, model=model))
        else:
            with _client(model) as opened:
                judgments.extend(_judge_batch(opened, batch, requirements, resolved, model=model))
    return judgments


def _judge_batch(
    client: Any,
    batch: Sequence[Line],
    requirements: Sequence[Requirement],
    primitives: Primitives,
    *,
    model: str | None = None,
) -> list[LineJudgment]:
    state = {
        "job_requirements": _requirement_state(requirements),
        "lines": [{"id": item.id, "text": item.text} for item in batch],
    }
    questions: dict[str, Any] = {}
    for line in batch:
        for requirement in requirements:
            questions[_question_key(line.id, f"supports_{requirement.id}")] = primitives.noul(
                instructions=(
                    "Does the line with id `lines[].id == "
                    f"{line.id}` provide concrete evidence that the candidate meets the job "
                    f"requirement `job_requirements[].id == {requirement.id}`?"
                ),
                criteria={
                    "true": "The line states work the candidate actually did that satisfies the requirement.",
                    "false": "The line does not show this requirement, mentions it only as an aspiration, or is unrelated.",
                },
            )
        questions[_question_key(line.id, "load_bearing")] = primitives.score(
            instructions=(
                "How much unique, requirement-relevant evidence would be lost if this line were deleted "
                "from the material?"
            ),
            criteria=list(LOAD_BEARING_LEVELS),
        )

    response = _ask(client, state, questions, model=model)
    judgments: list[LineJudgment] = []
    for line in batch:
        support: dict[str, float] = {}
        for requirement in requirements:
            answer = response.nouls.get(_question_key(line.id, f"supports_{requirement.id}"))
            if answer is not None:
                support[requirement.id] = round(float(answer.noul), 4)
        scored = response.scores.get(_question_key(line.id, "load_bearing"))
        level = float(scored.score) if scored is not None else 0.0
        confidence = float(scored.confidence) if scored is not None else 0.0
        index = min(len(LOAD_BEARING_LEVELS) - 1, max(0, int(round(level))))
        judgments.append(
            LineJudgment(
                line_id=line.id,
                support=support,
                load_bearing=level,
                load_bearing_label=LOAD_BEARING_LEVELS[index],
                load_bearing_confidence=round(confidence, 4),
            )
        )
    return judgments


def find_redundant_pairs(
    lines: Sequence[Line],
    *,
    model: str | None = None,
    client: Any | None = None,
    primitives: Primitives | None = None,
) -> list[RedundancyPair]:
    """Find lines that carry substantially the same evidence.

    Code proposes the candidate pairs with a deterministic token-overlap filter,
    so the model is only ever asked to confirm a real candidate and never has to
    scan the whole document.  That keeps the question narrow and the cost
    proportional to the number of plausible duplicates.
    """

    candidates: list[tuple[Line, Line, float]] = []
    for index, left in enumerate(lines):
        for right in lines[index + 1 :]:
            overlap = _overlap(left.text, right.text)
            if overlap >= CANDIDATE_TOKEN_OVERLAP:
                candidates.append((left, right, overlap))
    if not candidates:
        return []

    resolved = primitives or _load_primitives()
    pairs: list[RedundancyPair] = []
    if client is not None:
        pairs.extend(_judge_pairs(client, candidates, resolved, model=model))
    else:
        with _client(model) as opened:
            pairs.extend(_judge_pairs(opened, candidates, resolved, model=model))
    return pairs


def _judge_pairs(
    client: Any,
    candidates: Sequence[tuple[Line, Line, float]],
    primitives: Primitives,
    *,
    model: str | None = None,
) -> list[RedundancyPair]:
    state = {
        "pairs": [
            {"left_id": left.id, "left_text": left.text, "right_id": right.id, "right_text": right.text}
            for left, right, _ in candidates
        ]
    }
    questions: dict[str, Any] = {}
    for position, (left, right, _) in enumerate(candidates):
        questions[f"pair_{position}__same_evidence"] = primitives.noul(
            instructions=(
                f"Do the two lines in `pairs[{position}]` carry substantially the same evidence about "
                "the candidate, so that keeping both adds no new information?"
            ),
            criteria={
                "true": "Both lines state the same fact, metric or achievement in different words.",
                "false": "The lines state different facts, metrics, scope or time periods.",
            },
        )
    response = _ask(client, state, questions, model=model)
    pairs: list[RedundancyPair] = []
    for position, (left, right, overlap) in enumerate(candidates):
        answer = response.nouls.get(f"pair_{position}__same_evidence")
        if answer is None:
            continue
        pairs.append(
            RedundancyPair(
                left_id=left.id,
                right_id=right.id,
                probability=round(float(answer.noul), 4),
                token_overlap=round(overlap, 4),
            )
        )
    return [pair for pair in pairs if pair.probability >= REDUNDANCY_STRONG]


def rank_trim_candidates(
    line_judgments: Sequence[LineJudgment],
    redundant_pairs: Sequence[RedundancyPair],
    lines: Sequence[Line],
) -> list[str]:
    """Rank lines that may be shortened to fit a one-page budget.

    Conservative by construction: a line is proposed only when it evidences no
    requirement strongly, is duplicated by another line, and sits below the
    keep level.  Core evidence is never offered, whatever the page budget.
    """

    duplicated = {pair.left_id for pair in redundant_pairs} | {pair.right_id for pair in redundant_pairs}
    candidates: list[tuple[float, str]] = []
    for judgment in line_judgments:
        if judgment.load_bearing_label in {KEEP_LEVEL, LOAD_BEARING_LEVELS[-1]}:
            continue
        if judgment.strongest_support >= SUPPORT_STRONG:
            continue
        if judgment.line_id not in duplicated:
            continue
        # Lower strongest support and lower load bearing come first.
        rank = judgment.strongest_support + judgment.load_bearing
        candidates.append((rank, judgment.line_id))
    candidates.sort()
    known = {line.id for line in lines}
    return [line_id for _, line_id in candidates if line_id in known]


def run_judgments(
    requirements: Sequence[Requirement],
    lines: Sequence[Line],
    *,
    model: str | None = None,
    client: Any | None = None,
    primitives: Primitives | None = None,
) -> JudgmentReport:
    """Run the support, redundancy and trim judgments and return one report."""

    notes: list[str] = []
    # A hard ceiling on judged lines: the feature is on by default whenever a
    # credential exists, so the worst-case request budget has to be bounded in
    # code rather than left to the size of the document.
    bounded = list(lines)
    if len(bounded) > MAX_LINES_PER_RUN:
        notes.append(
            f"lines_truncated: judged the first {MAX_LINES_PER_RUN} of {len(bounded)} lines "
            "(raise MAX_LINES_PER_RUN deliberately, not by accident)"
        )
        bounded = bounded[:MAX_LINES_PER_RUN]
    judgments = judge_line_support(bounded, requirements, model=model, client=client, primitives=primitives)
    pairs = find_redundant_pairs(bounded, model=model, client=client, primitives=primitives)
    trim = rank_trim_candidates(judgments, pairs, bounded)
    if not trim:
        notes.append(
            "no_trim_candidate: every line either evidences a requirement strongly or is not duplicated"
        )
    return JudgmentReport(
        line_judgments=judgments,
        redundant_pairs=pairs,
        trim_candidates=trim,
        notes=notes,
    )


def _load_input(path: str) -> tuple[list[Requirement], list[Line]]:
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.loads(handle.read())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TypeSafeJudgmentError(f"unreadable_input: {type(exc).__name__}: {exc}") from exc
    if not isinstance(payload, dict):
        raise TypeSafeJudgmentError("unreadable_input: expected a JSON object")
    requirements = [
        Requirement(id=str(item.get("id") or f"R{index}"), text=str(item.get("text") or ""))
        for index, item in enumerate(payload.get("requirements") or [])
        if str(item.get("text") or "").strip()
    ]
    lines = [
        Line(id=str(item.get("id") or f"L{index}"), text=str(item.get("text") or ""))
        for index, item in enumerate(payload.get("lines") or [])
        if str(item.get("text") or "").strip()
    ]
    return requirements, lines


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Advisory TypeSafe material judgments (read-only).")
    parser.add_argument("--input", required=False, help="JSON file with 'requirements' and 'lines'")
    parser.add_argument("--model", default=None, help="Optional TypeSafe model id")
    parser.add_argument("--output", default=None, help="Write the JSON report here instead of stdout")
    parser.add_argument(
        "--status",
        action="store_true",
        help="Report the auto-enable decision only; makes no request and needs no input",
    )
    args = parser.parse_args(argv)

    if args.status:
        print(json.dumps({"status": "succeeded", "advisory_only": True, "typesafe": typesafe_status()}, ensure_ascii=False))
        return 0
    if not args.input:
        parser.error("--input is required unless --status is given")
    try:
        requirements, lines = _load_input(args.input)
    except TypeSafeJudgmentError as exc:
        print(
            json.dumps(
                {"status": "blocked", "blockers": [str(exc)], "advisory_only": True, "typesafe": typesafe_status()},
                ensure_ascii=False,
            )
        )
        return 2
    if not requirements or not lines:
        print(
            json.dumps(
                {"status": "blocked", "blockers": ["empty_input"], "advisory_only": True, "typesafe": typesafe_status()},
                ensure_ascii=False,
            )
        )
        return 2
    try:
        report = run_judgments(requirements, lines, model=args.model)
    except TypeSafeJudgmentError as exc:
        print(
            json.dumps(
                {"status": "blocked", "blockers": [str(exc)], "advisory_only": True, "typesafe": typesafe_status()},
                ensure_ascii=False,
            )
        )
        return 2
    payload = {"status": "succeeded", "advisory_only": True, "typesafe": typesafe_status(), "report": report.to_dict()}
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
    else:
        print(text)
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    sys.exit(main())
