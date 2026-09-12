"""规则确认书：手册 6.5 条件5（bypass 分析）与条件7（文档-实现版本一致）的机制化。

条件7 的字面意思是「文档与实现版本一致」。它天然是一个绑定问题，不是一次表态：
规则是从某份文档的某个版本编译出来的，那份文档后来改了，这条规则就不再确定
仍然忠于它的出处——哪怕代码一个字没动。所以确认书里记的是**当时源文档的 hash**，
每轮 audit 重算并比对。文档一改，enforced 自动撤销，不需要谁记得来撤。

条件5 无法自动推导：「这条规则能被怎么绕过」是分析结论，不是可观测事实。
所以它是一段必填的人写文字。写了不等于写对，但空着就一定没想过——
机制能保证的只有后者，声称更多就是治理幻觉（手册 16.4）。

两者共用一次动作（sopctl rule attest），因为它们的失效时机相同：
源文档一变，绕过分析的前提也跟着变，两条都该重新做。
"""
from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from typing import Optional

from .model import Evidence, Rule, content_hash, utcnow
from .registry import Registry, RegistryError

ATTEST_KIND = "rule.attestation"


class AttestError(Exception):
    pass


def _registry(root: Path) -> Registry:
    return Registry(Path(root) / ".sopcontrol" / "rules" / "registry.yaml")


def source_file(root: Path, rule: Rule) -> Optional[Path]:
    """规则出处对应的仓内常规文件；越界或符号链接 fail-closed。"""
    ref = (rule.source.ref or "").strip()
    if not ref:
        return None
    relative = Path(ref)
    if relative.is_absolute() or ".." in relative.parts:
        return None
    root_path = Path(root).resolve()
    candidate = root_path
    for part in relative.parts:
        candidate /= part
        if candidate.is_symlink():
            return None
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root_path)
    except (OSError, ValueError):
        return None
    return resolved if resolved.is_file() else None


def _source_hash(root: Path, rule: Rule) -> Optional[str]:
    """从仓根逐层无跟随打开源文件，并对同一个已打开 fd 计算摘要。"""
    ref = (rule.source.ref or "").strip()
    relative = Path(ref)
    parts = relative.parts
    if (
        not ref
        or relative.is_absolute()
        or not parts
        or any(part in {"", ".", ".."} for part in parts)
    ):
        return None

    root_fd = current_fd = source_fd = None
    try:
        root_fd = os.open(
            Path(root).resolve(),
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
        current_fd = os.dup(root_fd)
        for part in parts[:-1]:
            next_fd = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=current_fd,
            )
            os.close(current_fd)
            current_fd = next_fd
        source_fd = os.open(
            parts[-1],
            os.O_RDONLY | os.O_NOFOLLOW,
            dir_fd=current_fd,
        )
        if not stat.S_ISREG(os.fstat(source_fd).st_mode):
            return None
        digest = hashlib.sha256()
        while True:
            chunk = os.read(source_fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        return digest.hexdigest()[:16]
    except OSError:
        return None
    finally:
        for descriptor in (source_fd, current_fd, root_fd):
            if descriptor is not None:
                os.close(descriptor)


def record_attestation(root: Path, rule_id: str, *, bypass_note: str, by: str) -> Rule:
    """记录确认书：绑定当前源文档 hash + 绕过分析文字。"""
    note = (bypass_note or "").strip()
    if not note:
        raise AttestError("绕过分析不能为空（条件5）：说明这条规则能被怎么绕过、为什么可接受")

    reg = _registry(root)
    with reg.exclusive():
        rules = reg.load()
        target = next((r for r in rules if r.rule_id == rule_id), None)
        if target is None:
            known = ", ".join(r.rule_id for r in rules) or "（注册表为空）"
            raise RegistryError(f"未找到规则 {rule_id}；现有: {known}")

        source_hash = _source_hash(root, target)
        if source_hash is None:
            raise AttestError(
                f"规则 {rule_id} 的出处 {target.source.ref!r} 不是仓内文件，无法绑定版本（条件7）。"
                f"请把规则依据落成仓内文档后重新 attest"
            )

        return reg.record_attestation(
            rule_id,
            source_hash=source_hash,
            bypass_note=note,
            actor=by,
            at=utcnow(),
        )



def attestation_evidence(root: Path, rules: list[Rule]) -> list[Evidence]:
    """每条已确认规则一条证据：当时绑定的源文档 hash 与现在是否还一致。

    level=3：这是控制器读文件得出的事实（手册 4.3），不冒充运行时证据。
    条件7 要的是版本一致，不是运行时行为，E3 就是它的正确等级。

    未 attest 的规则不产证据——判定器看不到就不会颁发 enforced（fail-closed）。
    """
    out: list[Evidence] = []
    for rule in rules:
        if not rule.source_hash:
            continue
        current = _source_hash(root, rule)
        if current is None:
            # 出处文件被删/改名：曾经绑定过，现在无从比对 → 明确记成不一致，
            # 而不是安静地不产证据（后者与「从未 attest」无法区分）。
            observed = {
                "rule_id": rule.rule_id,
                "source_ref": rule.source.ref,
                "recorded": rule.source_hash,
                "current": None,
                "matches": False,
                "detail": "出处文件不存在（被删除或改名）",
                "bypass_note": bool(rule.bypass_note.strip()),
                "attested_by": rule.attested_by,
                "attested_revision": rule.attested_revision,
                "lifecycle_revision": rule.lifecycle_revision,
            }
            out.append(
                Evidence(
                    kind=ATTEST_KIND,
                    subject=".sopcontrol/rules/registry.yaml",
                    observed=observed,
                    observer="attestation",
                    level=3,
                    input_hash=content_hash(observed),
                )
            )
            continue
        observed = {
            "rule_id": rule.rule_id,
            "source_ref": rule.source.ref,
            "recorded": rule.source_hash,
            "current": current,
            "matches": current == rule.source_hash,
            "detail": "" if current == rule.source_hash else "源文档在确认之后被修改",
            "bypass_note": bool(rule.bypass_note.strip()),
            "attested_by": rule.attested_by,
            "attested_revision": rule.attested_revision,
            "lifecycle_revision": rule.lifecycle_revision,
        }
        out.append(
            Evidence(
                kind=ATTEST_KIND,
                subject=rule.source.ref,
                observed=observed,
                observer="attestation",
                level=3,
                input_hash=current,
            )
        )
    return out
