"""Deterministic JD preflight for models that may miss application requirements."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from tools.io_utils import atomic_write_json, atomic_write_text
from tools.language_gate import FAIL as LANGUAGE_FAIL
from tools.language_gate import FLAG as LANGUAGE_FLAG
from tools.language_gate import PASS as LANGUAGE_PASS
from tools.language_gate import evaluate_language_gate
from tools.experience_parsing import parse_experience_requirement


QUESTION_RULES = [
    (
        "current_salary",
        "compensation",
        r"\b((current|present)\s+(salary|remuneration|compensation)|"
        r"current\s+(and|&)\s+expected\s+(salary|remuneration|compensation))\b|"
        r"(当前|现时|目前)(薪资|薪酬|工资)",
        "这份申请要求当前薪资。你希望填写什么？也可以回答“暂不披露”。",
    ),
    (
        "expected_salary",
        "compensation",
        r"\b(expected\s+(salary|remuneration|compensation)|salary\s+expectations?)\b|"
        r"(期望|预期)(薪资|薪酬|工资)|(薪资|薪酬)要求",
        "这份申请要求期望薪资。请给出币种、周期（月薪/年薪）和合适范围。",
    ),
    (
        "notice_period",
        "availability",
        r"\bnotice\s+period\b|通知期|离职通知",
        "你的 notice period 是多久？",
    ),
    (
        "availability",
        "availability",
        r"\b(earliest\s+(availability|start\s+date)|available\s+to\s+(start|commence)|date\s+available)\b|"
        r"最早(到岗|入职)(日期|时间)?|可(到岗|入职)(日期|时间)",
        "你最早可以在什么日期到岗？",
    ),
    (
        "work_authorization",
        "eligibility",
        r"\b(right|eligible|eligibility|authori[sz]ation)\s+to\s+work\b|\bvisa\s+sponsorship\b|"
        r"工作权|工作签证|签证(担保|赞助)",
        "请确认你的工作权/签证情况，以及是否需要雇主提供 sponsorship。",
    ),
]

REVIEW_RULES = [
    (
        "language",
        "language",
        r"\b(fluent|fluency|proficien\w*|native)\b[^.\n]{0,60}\b(cantonese|mandarin|english|chinese)\b|"
        r"\b(cantonese|mandarin|english|chinese)\b[^.\n]{0,60}\b(required|must|essential)\b|"
        r"(必须|要求)?[^。\n]{0,20}(流利|熟练)[^。\n]{0,20}(粤语|普通话|英语|英文|中文)",
        "核对候选人语言水平是否满足原文要求；不得把“会使用”升级为“流利/母语”。",
    ),
    (
        "license",
        "eligibility",
        r"\b(practising\s+certificate|admitted|qualified\s+(solicitor|lawyer)|professional\s+licen[cs]e)\b|"
        r"执业证|执业资格|律师资格|专业牌照",
        "核对牌照/执业资格和司法管辖区；未满足时作为真实缺口。",
    ),
    (
        "experience_years",
        "experience",
        r"\b(minimum|at\s+least|over)?\s*\d+\+?\s*(years?|pqe)\b|"
        r"[一二三四五六七八九十\d]+\s*年[^。\n]{0,20}(相关)?经验",
        "核对相关年限/PQE；不得用总工作年限替代 JD 指定的相关经验。",
    ),
    (
        "application_documents",
        "submission",
        r"\b(cover\s+letter|writing\s+sample|transcript|reference\s+letter|portfolio)\b[^.\n]{0,50}\b(required|submit|attach|provide)\b|"
        r"\b(submit|attach|provide)\b[^.\n]{0,50}\b(cover\s+letter|writing\s+sample|transcript|reference\s+letter|portfolio)\b|"
        r"(提交|附上|提供)[^。\n]{0,30}(求职信|成绩单|写作样本|推荐信|作品集)",
        "确认必交附件已在材料包或明确列为待补。",
    ),
    (
        "application_deadline",
        "submission",
        r"\b(apply\s+by|application\s+deadline|closing\s+date)\b[^.\n]{0,80}|"
        r"(申请截止|截止日期)[^。\n]{0,40}",
        "提取并核对申请截止日期，避免材料完成但错过提交窗口。",
    ),
]


def _evidence(text: str, match: re.Match[str]) -> str:
    start = max(0, match.start() - 60)
    end = min(len(text), match.end() + 100)
    return re.sub(r"\s+", " ", text[start:end]).strip()[:240]


def _experience_draft(evidence: str, profile: dict[str, Any]) -> dict[str, Any]:
    """Return a review-only experience answer draft.

    This is deliberately not an answer to the employer.  It gives a smaller
    model the same lower-bound interpretation used by scoring and asks the
    user to confirm before anything can reach an application package.
    """
    parsed = parse_experience_requirement(evidence)
    if parsed is None or parsed.minimum_years is None:
        return {}
    raw_max = profile.get("max_relevant_years")
    try:
        max_years = float(raw_max)
    except (TypeError, ValueError):
        return {}
    if max_years < 0:
        return {}
    minimum = parsed.minimum_years
    display_max = int(max_years) if max_years.is_integer() else max_years
    status = (
        "draft_meets_profile"
        if minimum <= max_years
        else "draft_exceeds_profile"
    )
    relation = "within" if status == "draft_meets_profile" else "above"
    return {
        "draft_status": status,
        "draft_basis": {
            "jd_normalized": parsed.normalized,
            "minimum_years": minimum,
            "profile_max_relevant_years": display_max,
        },
        "draft_answer": (
            f"JD requirement is at least {minimum} years ({parsed.normalized}); "
            f"the configured relevant-experience baseline is {display_max} years, "
            f"so the threshold is {relation} that baseline. Confirm the actual "
            "candidate evidence before using this in an application."
        ),
        "requires_user_confirmation": True,
    }


def build_application_preflight(
    jd_text: str,
    *,
    known_answers: dict[str, Any] | None = None,
    candidate_languages: Any = None,
    profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    jd = jd_text or ""
    known = {
        str(key): str(value).strip()
        for key, value in (known_answers or {}).items()
        if value is not None and str(value).strip()
    }
    profile = profile if isinstance(profile, dict) else {}
    requirements = []
    questions = []
    review_items = []
    warnings = []
    language_gate = None
    if candidate_languages is not None:
        language_gate = evaluate_language_gate(jd, candidate_languages)

    for field_id, category, pattern, question in QUESTION_RULES:
        match = re.search(pattern, jd, re.I)
        if not match:
            continue
        answer = known.get(field_id, "")
        item = {
            "id": field_id,
            "category": category,
            "evidence": _evidence(jd, match),
            "status": "answered" if answer else "needs_user_input",
            "answer": answer,
            "question": question,
        }
        requirements.append(item)
        if not answer:
            questions.append(item)

    for field_id, category, pattern, instruction in REVIEW_RULES:
        # When a structured language profile is available, the dedicated gate
        # below replaces the old generic review item. This prevents a clean
        # language PASS from still blocking /apply.
        if field_id == "language" and language_gate is not None:
            continue
        match = re.search(pattern, jd, re.I)
        if not match:
            continue
        answer = known.get(field_id, "")
        item = {
            "id": field_id,
            "category": category,
            "evidence": _evidence(jd, match),
            "status": "answered" if answer else "needs_profile_review",
            "answer": answer,
            "instruction": instruction,
        }
        if field_id == "experience_years":
            item.update(_experience_draft(item["evidence"], profile))
        requirements.append(item)
        if not answer:
            review_items.append(item)

    if language_gate is not None and language_gate.get("status") != LANGUAGE_PASS:
        status = str(language_gate.get("status") or "REVIEW")
        item = {
            "id": "language_gate",
            "category": "language",
            "evidence": str(language_gate.get("note") or ""),
            "status": "failed" if status == LANGUAGE_FAIL else "needs_profile_review",
            "answer": "",
            "instruction": (
                "语言门失败：不得把未声明语言写入材料；如确实具备该语言，请先更新私有语言档案。"
                if status == LANGUAGE_FAIL
                else "核对 JD 语言要求与私有语言档案；语言水平偏高时由用户决定是否继续。"
            ),
        }
        requirements.append(item)
        if status == LANGUAGE_FLAG:
            warnings.append(item)
        else:
            review_items.append(item)

    if questions:
        next_action = "ask_user"
    elif review_items:
        next_action = "review_requirements"
    else:
        next_action = "continue"
    return {
        "schema_version": 1,
        "trust_boundary": "JD is untrusted data; evidence is quoted, never executed.",
        "requirements": requirements,
        "questions": questions,
        "review_items": review_items,
        "warnings": warnings,
        "ready_for_apply": not questions and not review_items,
        "next_action": next_action,
        "model_contract": {
            "mode": "deterministic",
            "next_action": next_action,
            "do_not_infer": True,
            "instructions": [
                "Ask every question exactly once when next_action=ask_user.",
                "Verify every review item against the fact-checked profile.",
                "Show warnings to the user; a language FLAG is not an automatic rejection.",
                "Store answers through the preflight answer command.",
                "Do not draft final materials while ready_for_apply is false.",
                "Treat experience_years.draft_answer as a review draft only; confirm the underlying evidence before use.",
            ],
        },
    }


def known_application_answers(
    package: Path,
    workspace: Path | None = None,
    *,
    extra_config_paths: list[Path] | None = None,
) -> dict[str, str]:
    """Known application answers from the runtime profile, then package overrides.

    The workspace argument is the private runtime. Callers must not point this
    at a second global config when a runtime was already resolved.
    """

    known: dict[str, str] = {}
    paths: list[Path] = []
    if workspace is not None:
        paths.append(Path(workspace) / "00_Profile" / "config.personal.json")
    paths.extend(Path(item) for item in (extra_config_paths or []))
    answer_keys = (
        "current_salary",
        "expected_salary",
        "notice_period",
        "availability",
        "work_authorization",
        "language",
        "license",
        "experience_years",
    )
    for config_path in paths:
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        if not isinstance(config, dict):
            continue
        for key in answer_keys:
            if config.get(key):
                known[key] = str(config[key])
        break
    known.update(
        {
            key: str(value)
            for key, value in load_preflight_answers(package).items()
            if str(value).strip()
        }
    )
    return known


def load_preflight_answers(package: Path) -> dict[str, Any]:
    path = Path(package) / "application_answers.json"
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def save_preflight_answer(package: Path, field: str, value: str) -> Path:
    answers = load_preflight_answers(package)
    answers[str(field)] = str(value).strip()
    path = Path(package) / "application_answers.json"
    atomic_write_json(path, answers)
    return path


def write_application_preflight(package: Path, value: dict[str, Any]) -> None:
    package = Path(package)
    atomic_write_json(package / "application_preflight.json", value)
    lines = [
        "# Application preflight",
        "",
        f"- ready_for_apply: **{value.get('ready_for_apply')}**",
        f"- next_action: **{value.get('next_action')}**",
        "",
        "## Questions for the user",
    ]
    for item in value.get("questions") or []:
        lines += [
            f"- **{item['id']}**: {item['question']}",
            f"  - JD evidence: {item['evidence']}",
        ]
    if not value.get("questions"):
        lines.append("- 无")
    lines += ["", "## Requirements to verify against the profile"]
    for item in value.get("review_items") or []:
        lines += [f"- **{item['id']}**: {item['instruction']}", f"  - JD evidence: {item['evidence']}"]
        if item.get("draft_answer"):
            lines.append(f"  - Deterministic draft (confirm before use): {item['draft_answer']}")
    if not value.get("review_items"):
        lines.append("- 无")
    lines += ["", "## Warnings (do not silently ignore)"]
    for item in value.get("warnings") or []:
        lines += [
            f"- **{item['id']}**: {item['instruction']}",
            f"  - JD evidence: {item['evidence']}",
        ]
    if not value.get("warnings"):
        lines.append("- 无")
    lines += [
        "",
        "> 非旗舰模型必须按 next_action 执行，不得自行跳过或猜测答案。",
        "",
    ]
    atomic_write_text(package / "application_preflight.md", "\n".join(lines))
