"""Host-owned user pause cards. Models display them; they do not invent options."""

from __future__ import annotations

from typing import Any

PROMPT_KINDS = (
    "setup_required",
    "choose_role_title",
    "select_jobs",
    "confirm_push",
    "confirm_intent",
    "confirm_base",
    "confirm_reset",
    "ask_preflight",
    "confirm_archive",
    "learning_proposal",
)


class UserPromptError(ValueError):
    """Raised when a pause card violates the interaction contract."""


def validate_user_prompt(prompt: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(prompt, dict) or not prompt:
        raise UserPromptError("user_prompt_required")
    kind = str(prompt.get("kind") or "").strip()
    if kind not in PROMPT_KINDS:
        raise UserPromptError(f"user_prompt_kind_invalid:{kind}")
    question = str(prompt.get("question") or "").strip()
    if not question:
        raise UserPromptError("user_prompt_question_required")
    options = list(prompt.get("options") or [])
    if not options:
        raise UserPromptError("user_prompt_options_empty")
    recommended = 0
    cleaned_options: list[dict[str, Any]] = []
    for item in options:
        if not isinstance(item, dict):
            raise UserPromptError("user_prompt_option_invalid")
        oid = str(item.get("id") or "").strip()
        label = str(item.get("label") or "").strip()
        if not oid or not label:
            raise UserPromptError("user_prompt_option_incomplete")
        flag = bool(item.get("recommended"))
        if flag:
            recommended += 1
        cleaned_options.append({"id": oid, "label": label, "recommended": flag})
    if recommended > 1:
        raise UserPromptError("user_prompt_recommended_overflow")
    contract = prompt.get("reply_contract")
    if contract is not None and not isinstance(contract, dict):
        raise UserPromptError("user_prompt_reply_contract_invalid")
    return {
        "kind": kind,
        "question": question,
        "options": cleaned_options,
        "reply_hint": str(prompt.get("reply_hint") or ""),
        "reply_contract": dict(contract or {}),
    }


def build_user_prompt(
    kind: str,
    *,
    question: str,
    options: list[dict[str, Any]],
    reply_hint: str = "",
    reply_contract: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return validate_user_prompt(
        {
            "kind": kind,
            "question": question,
            "options": options,
            "reply_hint": reply_hint,
            "reply_contract": reply_contract or {},
        }
    )
