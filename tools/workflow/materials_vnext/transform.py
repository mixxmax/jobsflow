"""Bounded baseline-to-canonical transformation compiler."""

from __future__ import annotations

import copy
import json
import re
from typing import Any

from tools.workflow.materials_vnext.contracts import BLOCK_TYPES, MATERIALS, digest, text


ALLOWED_ACTIONS = {"replace", "append_after", "reorder"}
# A bounded transform may touch a broad portion of a genuinely different JD,
# but a near-total rewrite is still rejected.  Broad (35%+) deltas are routed
# to a stronger independent auditor rather than being rejected as if they were
# a full replacement; the hard ceiling remains 60% per material.
MAX_CHANGED_RATIO = 0.60
MAX_ADDED_BLOCKS = 6
MAX_ADDED_CHARS_RATIO = 0.55
_NUMBER_WORDS = {
    "zero", "one", "two", "three", "four", "five", "six", "seven",
    "eight", "nine", "ten", "eleven", "twelve", "thirteen", "fourteen",
    "fifteen", "sixteen", "seventeen", "eighteen", "nineteen", "twenty",
}


def _blocks(baseline: dict[str, Any], material: str) -> list[dict[str, Any]]:
    value = baseline.get(material) if isinstance(baseline, dict) else None
    # The initial baseline stores each material as {blocks: [...]}, while a
    # compiled in-memory state stores the material directly as [...].  Repair
    # patches operate on the latter; accepting both shapes keeps the bounded
    # compiler pure and prevents a list.get crash at the audit->repair seam.
    if isinstance(value, list):
        blocks = value
    elif isinstance(value, dict):
        blocks = value.get("blocks") or []
    else:
        blocks = []
    return [dict(item) for item in blocks if isinstance(item, dict)]


def _index(blocks: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {text(item.get("id")): item for item in blocks if text(item.get("id"))}


def _ops(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw = payload.get("operations")
    if raw is None:
        raw = payload.get("changes")
    if not isinstance(raw, list):
        return []
    normalized: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        operation = dict(item)
        action = text(operation.get("action")).casefold()
        # The first bounded-transform response schema called a replacement
        # a ``rewrite`` and named its target ``baseline_id``.  Accept that
        # narrow legacy spelling at the compiler boundary, but expose only
        # the canonical replace/append_after/reorder vocabulary in new task
        # packets.  This keeps old staged responses resumable without
        # creating a second authoring path.
        if action == "rewrite":
            operation["action"] = "replace"
            operation.setdefault("target_id", operation.get("baseline_id"))
            operation.setdefault("after_text", operation.get("text"))
            operation["_compat_rewrite"] = True
        elif action == "add":
            operation["action"] = "append_after"
            operation.setdefault("after_id", operation.get("target_id"))
            operation.setdefault("block", dict(item))
        normalized.append(operation)
    return normalized


def normalize_transform_operations(
    transform: dict[str, Any],
    baseline: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Complete host-owned transform fields from the frozen baseline.

    A model-facing response may legitimately omit ``material`` and
    ``before_text``: the baseline block ID is globally unique per material and
    the exact current text is host-owned.  This normalizer fills those fields
    deterministically and migrates the historical ``changes``/``additions``
    envelopes into the single ``operations`` contract.  An explicit but wrong
    ``before_text`` is never overwritten; it fails closed with a precise
    mismatch error so a stale model cannot silently replace the wrong text.
    """

    raw = transform.get("operations")
    if raw is None:
        raw = transform.get("changes")
    additions = transform.get("additions")
    if not isinstance(raw, list):
        raw = []
    if not isinstance(additions, list):
        additions = []
    blocks: list[dict[str, Any]] = []
    for material in MATERIALS:
        blocks.extend((baseline.get(material) or {}).get("blocks") or [])
    by_id = {text(item.get("id")): item for item in blocks if text(item.get("id"))}

    operations: list[dict[str, Any]] = []
    errors: list[str] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            errors.append(f"operation_not_object:{index}")
            continue
        operation = dict(item)
        target_id = text(operation.get("target_id") or operation.get("block_id") or operation.get("baseline_id"))
        block = by_id.get(target_id)
        if block is None:
            errors.append(f"operation_target_unresolvable:{index}:{target_id or ''}")
            continue
        action = text(operation.get("action")).casefold()
        if not operation.get("material"):
            operation["material"] = "cv" if target_id.startswith("cv-") else "cover_letter"
        if action == "replace":
            before = text(operation.get("before_text"))
            if not before:
                operation["before_text"] = text(block.get("text"))
            elif before != text(block.get("text")):
                errors.append(f"operation_before_text_mismatch:{index}:{target_id}")
                continue
        operations.append(operation)

    for index, item in enumerate(additions):
        if not isinstance(item, dict):
            errors.append(f"addition_not_object:{index}")
            continue
        block = item.get("block")
        if not isinstance(block, dict) or not text(block.get("text")):
            errors.append(f"addition_block_missing:{index}")
            continue
        anchor = text(item.get("after_id") or item.get("target_id"))
        if anchor and anchor in by_id:
            operations.append(
                {
                    "action": "append_after",
                    "material": "cv" if anchor.startswith("cv-") else "cover_letter",
                    "target_id": anchor,
                    "block": block,
                }
            )
        elif anchor:
            errors.append(f"operation_append_anchor_missing:{index}:{anchor}")
        else:
            errors.append(f"operation_append_anchor_missing:{index}")
    return operations, sorted(set(errors))


def _protected_evidence_tokens(value: Any) -> set[str]:
    """Return small, deterministic evidence markers that must not vanish.

    This is intentionally not a fact checker.  It only catches silent loss of
    high-signal baseline evidence (numbers, dates, percentages, money and
    explicit acronyms) when a bounded rewrite is submitted.  The independent
    CV/CL auditor handles the semantic before/after quality review.
    """

    raw = str(value or "")
    tokens: set[str] = set()
    for match in re.findall(r"\b\d[\d,.%+/-]*\b|\b[A-Z]{2,}(?:[-/][A-Z0-9]{2,})?\b", raw):
        tokens.add(match.casefold())
    tokens.update(word for word in re.findall(r"\b[a-z]+\b", raw.casefold()) if word in _NUMBER_WORDS)
    return tokens


def baseline_preservation_errors(baseline: dict[str, Any], current: dict[str, Any]) -> list[str]:
    """Check block identity and protected evidence after all transforms.

    Every truthful baseline block remains represented through ``baseline_refs``
    (or its original id), and a rewrite cannot silently drop a baseline number
    or explicit numeric word.  The rule is host-enforced before an audit task
    is created, so a weak model cannot trade away content to make a document
    shorter and ask the child auditor to discover it later.
    """

    errors: list[str] = []
    for material in MATERIALS:
        base_blocks = _blocks(baseline, material)
        current_blocks = _blocks(current, material)
        for base in base_blocks:
            base_id = text(base.get("id"))
            if not base_id:
                continue
            is_floor = bool(base.get("content_floor", not base.get("host_managed")))
            if bool(base.get("host_managed_optional")) and not is_floor:
                continue
            matching = [block for block in current_blocks if base_id in refs_for_block(block)]
            if not matching:
                errors.append(f"baseline_block_lost:{material}:{base_id}")
                continue
            if not is_floor:
                continue
            baseline_tokens = _protected_evidence_tokens(base.get("text"))
            current_text = " ".join(text(block.get("text")) for block in matching)
            missing = sorted(token for token in baseline_tokens if token not in current_text.casefold())
            for token in missing:
                errors.append(f"baseline_protected_evidence_removed:{material}:{base_id}:{token}")
    return sorted(set(errors))


def refs_for_block(block: dict[str, Any], fallback: list[str] | None = None) -> set[str]:
    refs = block.get("baseline_refs")
    if isinstance(refs, list):
        return {text(item) for item in refs if text(item)}
    return {text(block.get("id"))} if text(block.get("id")) else set(fallback or [])


def validate_transform(
    transform: Any,
    baseline: dict[str, Any],
    *,
    current: dict[str, Any] | None = None,
    repair: bool = False,
) -> list[str]:
    if not isinstance(transform, dict):
        return ["transform_not_object"]
    if int(transform.get("schema_version") or 0) != 1:
        return ["transform_schema_version_invalid"]
    operations = _ops(transform)
    if not operations:
        return ["transform_operations_missing"]
    errors: list[str] = []
    changed: dict[str, int] = {material: 0 for material in MATERIALS}
    additions = 0
    added_chars = 0
    for index, operation in enumerate(operations):
        material = text(operation.get("material")).casefold()
        action = text(operation.get("action")).casefold()
        target_id = text(operation.get("target_id") or operation.get("block_id"))
        if material not in MATERIALS:
            errors.append(f"operation_material_invalid:{index}")
            continue
        if action not in ALLOWED_ACTIONS:
            errors.append(f"operation_action_invalid:{index}")
            continue
        source = _blocks(current or baseline, material)
        lookup = _index(source)
        target = lookup.get(target_id)
        if action != "append_after" and target is None:
            errors.append(f"operation_target_missing:{index}:{target_id}")
            continue
        if target is not None and bool(target.get("host_managed")):
            errors.append(f"operation_host_managed:{index}:{target_id}")
        if action == "replace":
            before = text(operation.get("before_text"))
            after = text(operation.get("after_text") or operation.get("text"))
            if not before and operation.get("_compat_rewrite"):
                before = text(target.get("text"))
            if not before or not after:
                errors.append(f"operation_replace_text_missing:{index}")
            elif before != text(target.get("text")):
                errors.append(f"operation_before_text_mismatch:{index}:{target_id}")
            elif after == before:
                errors.append(f"operation_noop:{index}:{target_id}")
            else:
                changed[material] += 1
        elif action == "append_after":
            after_id = text(operation.get("after_id") or operation.get("target_id"))
            if after_id not in lookup:
                errors.append(f"operation_after_target_missing:{index}:{after_id}")
            block = operation.get("block") if isinstance(operation.get("block"), dict) else operation
            new_id = text(block.get("new_id") or block.get("id"))
            new_text = text(block.get("text") or block.get("after_text"))
            if not new_id or not new_text:
                errors.append(f"operation_append_block_missing:{index}")
            elif new_id in lookup:
                errors.append(f"operation_append_duplicate_id:{index}:{new_id}")
            else:
                if text(block.get("type") or "bullet") not in BLOCK_TYPES:
                    errors.append(f"operation_append_type_invalid:{index}")
                additions += 1
                added_chars += len(new_text)
                changed[material] += 1
        elif action == "reorder":
            after_id = text(operation.get("after_id") or operation.get("before_id"))
            if after_id and after_id not in lookup:
                errors.append(f"operation_reorder_target_missing:{index}:{after_id}")
            changed[material] += 1
        anchors = operation.get("jd_anchor_ids")
        if anchors is not None and not isinstance(anchors, list):
            errors.append(f"operation_jd_anchor_ids_not_list:{index}")
    if not repair:
        for material in MATERIALS:
            base_count = max(1, len(_blocks(baseline, material)))
            if changed[material] / base_count > MAX_CHANGED_RATIO:
                errors.append(f"transform_too_many_changes:{material}")
        baseline_chars = sum(len(text(block.get("text"))) for material in MATERIALS for block in _blocks(baseline, material))
        if additions > MAX_ADDED_BLOCKS:
            errors.append("transform_too_many_added_blocks")
        if baseline_chars and added_chars / baseline_chars > MAX_ADDED_CHARS_RATIO:
            errors.append("transform_added_text_too_large")
    else:
        # A repair is even narrower: it must name one existing target per
        # operation and cannot introduce a new section or delete content.
        if additions:
            errors.append("repair_cannot_add_unbounded_block")
    return sorted(set(errors))


def _apply_operations(base: dict[str, Any], transform: dict[str, Any], *, repair: bool = False) -> dict[str, Any]:
    result = {
        material: [
            block for block in _blocks(base, material)
            if not (bool(block.get("host_managed_optional")) and not bool(block.get("content_floor", False)))
        ]
        for material in MATERIALS
    }
    for material in MATERIALS:
        for block in result[material]:
            block.setdefault("baseline_refs", [text(block.get("id"))] if text(block.get("id")) else [])
            block.setdefault("baseline_before_text", text(block.get("text")))
            block.setdefault("baseline_content_floor", bool(block.get("content_floor", not block.get("host_managed"))))
    for operation in _ops(transform):
        material = text(operation.get("material")).casefold()
        action = text(operation.get("action")).casefold()
        blocks = result[material]
        lookup = _index(blocks)
        target_id = text(operation.get("target_id") or operation.get("block_id"))
        if action == "replace":
            target = lookup[target_id]
            target["text"] = text(operation.get("after_text") or operation.get("text"))
            if isinstance(operation.get("jd_anchor_ids"), list):
                target["jd_anchor_ids"] = list(operation["jd_anchor_ids"])
            target["customized"] = True
            target["change_action"] = "replace"
        elif action == "append_after":
            block = operation.get("block") if isinstance(operation.get("block"), dict) else operation
            section = text(block.get("section")) or ("summary" if material == "cv" else "body")
            block_type = text(block.get("type") or "bullet")
            if material == "cv" and section == "core":
                source_style, presentation_role = "Compact Line", "core_line"
            elif material == "cv" and section in {"education", "qualifications", "compact"}:
                source_style, presentation_role = "Compact Line", "compact_line"
            elif material == "cv" and section == "experience" and block_type == "bullet":
                source_style, presentation_role = "Resume Bullet", "experience_bullet"
            else:
                source_style, presentation_role = "Normal", "added_block"
            new_block = {
                "id": text(block.get("new_id") or block.get("id")),
                "type": text(block.get("type") or "bullet"),
                "text": text(block.get("text") or block.get("after_text")),
                "section": section,
                "experience_id": text(block.get("experience_id")),
                "priority": block.get("priority", 999),
                "jd_anchor_ids": list(block.get("jd_anchor_ids") or operation.get("jd_anchor_ids") or []),
                "host_managed": False,
                "customized": True,
                "baseline_refs": [],
                "baseline_before_text": "",
                "baseline_content_floor": False,
                "change_action": "append_after",
                "source_style": source_style,
                "presentation_role": presentation_role,
            }
            after_id = text(operation.get("after_id") or operation.get("target_id"))
            position = next(index for index, item in enumerate(blocks) if text(item.get("id")) == after_id)
            blocks.insert(position + 1, new_block)
        elif action == "reorder":
            target = next(item for item in blocks if text(item.get("id")) == target_id)
            blocks.remove(target)
            after_id = text(operation.get("after_id") or operation.get("before_id"))
            if not after_id:
                blocks.insert(0, target)
            else:
                position = next(index for index, item in enumerate(blocks) if text(item.get("id")) == after_id)
                blocks.insert(position + 1, target)
    return result


def compile_canonical(
    *,
    baseline: dict[str, Any],
    original_transform: dict[str, Any],
    patches: list[dict[str, Any]] | None,
    job_id: str,
    generation_id: str,
    bundle_sha256: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    errors = validate_transform(original_transform, baseline)
    if errors:
        raise ValueError("invalid_original_transform: " + ", ".join(errors))
    state = _apply_operations(baseline, original_transform)
    patch_rows: list[dict[str, Any]] = []
    for patch in patches or []:
        errors = validate_transform(patch, state, current=state, repair=True)
        if errors:
            raise ValueError("invalid_repair_patch: " + ", ".join(errors))
        state = _apply_operations(state, patch, repair=True)
        patch_rows.append(copy.deepcopy(patch))
    preservation_errors = baseline_preservation_errors(baseline, state)
    effective = {
        "schema_version": 1,
        "artifact_type": "jobsflow_effective_materials_transform",
        "job_id": job_id,
        "generation_id": generation_id,
        "bundle_sha256": bundle_sha256,
        "baseline_sha256": str(baseline.get("baseline_sha256") or digest(baseline)),
        "original": copy.deepcopy(original_transform),
        "repair_patches": patch_rows,
        # Keep the compiled candidate available for the deterministic
        # preflight to report the most actionable blocker (for example a
        # negative self-disclosure alongside a dropped baseline metric).
        # The engine never creates an audit task while this list is non-empty.
        "baseline_preservation_errors": preservation_errors,
    }
    effective["effective_transform_sha256"] = digest(effective)
    dispositions: dict[str, dict[str, Any]] = {}
    for material in MATERIALS:
        for source in _blocks(baseline, material):
            source_id = text(source.get("id"))
            if not source_id or not bool(source.get("content_floor", not source.get("host_managed"))):
                continue
            target = next(
                (
                    item for item in state[material]
                    if source_id in refs_for_block(item)
                ),
                None,
            )
            if target is None:
                dispositions[source_id] = {"material": material, "action": "omit", "target_id": ""}
            else:
                dispositions[source_id] = {
                    "material": material,
                    "action": text(target.get("change_action")) or "retain",
                    "target_id": text(target.get("id")),
                }
    canonical = {
        "schema_version": 1,
        "artifact_type": "jobsflow_canonical_cv_cl",
        "job_id": job_id,
        "generation_id": generation_id,
        "bundle_sha256": bundle_sha256,
        "baseline_sha256": effective["baseline_sha256"],
        "effective_transform_sha256": effective["effective_transform_sha256"],
        "cv": {"blocks": state["cv"]},
        "cover_letter": {"blocks": state["cover_letter"]},
        "baseline_dispositions": dispositions,
    }
    canonical["canonical_sha256"] = digest(canonical)
    return canonical, effective
