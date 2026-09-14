"""One-shot capability tickets — portable, non-forgeable via env alone.

Tickets bind project/worktree/task/action/input/side-effects and expire.
Stored under .sopcontrol-local (gitignored). Adapters verify without trusting
environment variables as proof of authorization.
"""
from __future__ import annotations

import fcntl
import json
import os
import secrets
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

from .context import ProjectScope, content_hash_safe
from .identity import load_identity
from .model import utcnow

SCHEMA_VERSION = "1"


class TicketError(Exception):
    pass


class CapabilityTicket(BaseModel):
    schema_version: str = SCHEMA_VERSION
    ticket_id: str
    secret: str  # high-entropy; must be presented to redeem
    project_id: str
    worktree_id: str
    task_id: str = ""
    run_id: str = ""
    action: str
    input_fingerprint: str
    allowed_side_effects: list[str] = Field(default_factory=list)
    expires_at: datetime
    created_at: datetime = Field(default_factory=utcnow)
    consumed_at: Optional[datetime] = None
    issued_by: str = "sopctl"
    effective_plan_digest: str = ""  # §9.4：能力授权绑定的冻结计划（空=未绑定，不改变旧行为）
    phase: str = ""  # §10.4：阶段级授权归属阶段（空=单动作票；阶段票绑定 allowed_side_effects 集合）
    operation: str = ""  # §12.1：绑定的稳定 operation（空=未绑定）
    allowed_actions: list[str] = Field(default_factory=list)  # §12.1：允许的动作名集合（空=不限动作名，只验其他绑定）
    capability_binding: str = ""
    # MSE（§17.1）：计划/步骤/输入集合绑定——计划或输入变化后旧 ticket 失效
    goal_digest: str = ""
    execution_plan_digest: str = ""
    plan_step_id: str = ""
    operator_id: str = ""
    input_set_digest: str = ""
    input_cardinality: int = 0
    correction_revision: int = 0
    strategy_fingerprint: str = ""
    gate_scope_digest: str = ""

    @property
    def operation_id(self) -> str:
        return self.operation


def _ticket_dir(root: Path, worktree_id: str) -> Path:
    return (
        Path(root) / ".sopcontrol-local" / "worktrees" / (worktree_id or "default") / "tickets"
    )


def issue_ticket(
    root: Path,
    *,
    action: str,
    input_fingerprint: str,
    allowed_side_effects: list[str] | None = None,
    task_id: str = "",
    run_id: str = "",
    ttl_seconds: int = 900,
    issued_by: str = "sopctl",
    effective_plan_digest: str = "",
    phase: str = "",
    operation: str = "",
    operation_id: str = "",
    allowed_actions: list[str] | None = None,
    capability_binding: str = "",
    goal_digest: str = "",
    execution_plan_digest: str = "",
    plan_step_id: str = "",
    operator_id: str = "",
    input_set_digest: str = "",
    input_cardinality: int = 0,
    correction_revision: int = 0,
    strategy_fingerprint: str = "",
    gate_scope_digest: str = "",
) -> CapabilityTicket:
    root = Path(root)
    scope = ProjectScope(root, mode="discovery")
    ident = load_identity(root)
    project_id = ident.project_id if ident else "unknown"
    op = operation_id or operation
    ticket = CapabilityTicket(
        ticket_id="tkt-" + secrets.token_hex(8),
        secret=secrets.token_urlsafe(32),
        project_id=project_id,
        worktree_id=scope.worktree_id,
        task_id=task_id,
        run_id=run_id,
        action=action,
        input_fingerprint=input_fingerprint,
        allowed_side_effects=list(allowed_side_effects or []),
        expires_at=utcnow() + timedelta(seconds=int(ttl_seconds)),
        issued_by=issued_by,
        effective_plan_digest=effective_plan_digest,
        phase=phase,
        operation=op,
        allowed_actions=list(allowed_actions) if allowed_actions is not None else [],
        capability_binding=capability_binding,
        goal_digest=goal_digest,
        execution_plan_digest=execution_plan_digest,
        plan_step_id=plan_step_id,
        operator_id=operator_id,
        input_set_digest=input_set_digest,
        input_cardinality=input_cardinality,
        correction_revision=correction_revision,
        strategy_fingerprint=strategy_fingerprint,
        gate_scope_digest=gate_scope_digest,
    )
    directory = _ticket_dir(root, ticket.worktree_id)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{ticket.ticket_id}.json"
    fd, tmp = tempfile.mkstemp(prefix=".tkt.", suffix=".tmp", dir=str(directory))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(ticket.model_dump_json())
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return ticket


def _load_ticket(root: Path, ticket_id: str, worktree_id: str = "") -> CapabilityTicket:
    if not worktree_id:
        worktree_id = ProjectScope(Path(root), mode="discovery").worktree_id
    path = _ticket_dir(root, worktree_id) / f"{ticket_id}.json"
    if not path.is_file():
        raise TicketError(f"ticket not found: {ticket_id}")
    try:
        return CapabilityTicket.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise TicketError(f"ticket unreadable: {exc}") from exc


def _save_ticket(root: Path, ticket: CapabilityTicket) -> None:
    directory = _ticket_dir(root, ticket.worktree_id)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{ticket.ticket_id}.json"
    fd, tmp = tempfile.mkstemp(prefix=".tkt.", suffix=".tmp", dir=str(directory))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(ticket.model_dump_json())
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _validate_ticket(
    ticket: CapabilityTicket,
    *,
    root_path: Path,
    scope_wt: str,
    secret: str,
    action: str,
    input_fingerprint: str,
    side_effect: str = "",
    task_id: str = "",
    expected_project_id: str = "",
    expected_task_id: str = "",
    expected_phase: str = "",
    expected_action: str = "",
    expected_operation_id: str = "",
    expected_operation: str = "",
    expected_run_id: str = "",
    expected_plan_digest: str = "",
    expected_capability_binding: str = "",
    expected_input_fingerprint: str = "",
    expected_side_effect: str = "",
    expected_goal_digest: str = "",
    expected_execution_plan_digest: str = "",
    expected_plan_step_id: str = "",
    expected_operator_id: str = "",
    expected_input_set_digest: str = "",
    when: datetime,
) -> None:
    # §18：消费状态不可经任何参数绕过——已消费票据无条件拒绝。
    if ticket.consumed_at is not None:
        raise TicketError("ticket already consumed")
    if when >= ticket.expires_at:
        raise TicketError("ticket expired")
    if not secrets.compare_digest(ticket.secret, secret):
        raise TicketError("ticket secret mismatch")
    if ticket.worktree_id and ticket.worktree_id != scope_wt:
        raise TicketError("ticket worktree mismatch")
    ident = load_identity(root_path)
    if ident and ticket.project_id not in ("", "unknown") and ticket.project_id != ident.project_id:
        raise TicketError("ticket project_id mismatch")
    if expected_project_id:
        if not ticket.project_id or ticket.project_id != expected_project_id:
            raise TicketError(f"ticket project_id mismatch: expected {expected_project_id}, got {ticket.project_id}")

    # allowed_actions 与 action 检查
    if ticket.allowed_actions:
        if action not in ticket.allowed_actions:
            raise TicketError(f"action not allowed: {action}")
    elif ticket.action != action:
        raise TicketError(f"ticket action mismatch: expected {ticket.action}, got {action}")
    if expected_action and action != expected_action:
        raise TicketError(f"action mismatch: expected {expected_action}, got {action}")

    # input_fingerprint 检查
    if ticket.input_fingerprint != input_fingerprint:
        raise TicketError("ticket input fingerprint mismatch")
    if expected_input_fingerprint and input_fingerprint != expected_input_fingerprint:
        raise TicketError("ticket input fingerprint mismatch")

    # task 检查：期待非空时，ticket 必须非空且相等（空不是通配符）
    exp_task = expected_task_id or task_id
    if exp_task:
        if not ticket.task_id or ticket.task_id != exp_task:
            raise TicketError(f"ticket task_id mismatch: expected {exp_task}, got {ticket.task_id or '<empty>'}")

    # side_effect 检查
    eff = expected_side_effect or side_effect
    if eff and eff not in ticket.allowed_side_effects:
        raise TicketError(f"side effect not allowed: {eff}")

    # 计划绑定检查：期待非空时，ticket 必须非空且相等（空不是通配符）
    if expected_plan_digest:
        if not ticket.effective_plan_digest or ticket.effective_plan_digest != expected_plan_digest:
            raise TicketError("ticket plan digest mismatch: 授权不属于当前冻结计划")

    # phase 检查：期待非空时，ticket 必须非空且相等（空不是通配符）
    if expected_phase:
        if not ticket.phase or ticket.phase != expected_phase:
            raise TicketError(f"ticket phase mismatch: 阶段授权不可跨阶段使用 (expected {expected_phase}, got {ticket.phase or '<empty>'})")

    # operation 检查：期待非空时，ticket 必须非空且相等（空不是通配符）
    exp_op = expected_operation_id or expected_operation
    if exp_op:
        ticket_op = ticket.operation
        if not ticket_op or ticket_op != exp_op:
            raise TicketError(f"ticket operation mismatch: 运行不一致 (expected {exp_op}, got {ticket_op or '<empty>'})")

    # run_id 检查：期待非空时，ticket 必须非空且相等（空不是通配符）
    if expected_run_id:
        if not ticket.run_id or ticket.run_id != expected_run_id:
            raise TicketError(f"ticket run_id mismatch: expected {expected_run_id}, got {ticket.run_id or '<empty>'}")

    # capability_binding 检查：期待非空时，ticket 必须非空且相等（空不是通配符）
    if expected_capability_binding:
        if not ticket.capability_binding or ticket.capability_binding != expected_capability_binding:
            raise TicketError(f"ticket capability binding mismatch: expected {expected_capability_binding}, got {ticket.capability_binding or '<empty>'}")

    # MSE 绑定检查（§17.1）：期待非空时 ticket 必须非空且相等；计划/输入/步骤
    # 变化后旧 ticket 不得继续使用（空不通配）
    for exp_key, got in (
        ("goal_digest", (expected_goal_digest, ticket.goal_digest)),
        ("execution_plan_digest", (expected_execution_plan_digest, ticket.execution_plan_digest)),
        ("plan_step_id", (expected_plan_step_id, ticket.plan_step_id)),
        ("operator_id", (expected_operator_id, ticket.operator_id)),
        ("input_set_digest", (expected_input_set_digest, ticket.input_set_digest)),
    ):
        exp, actual = got
        if exp and (not actual or actual != exp):
            raise TicketError(f"ticket mse {exp_key} mismatch: expected {exp}, got {actual or '<empty>'}")


def redeem_ticket(
    root: Path,
    *,
    ticket_id: str,
    secret: str,
    action: str,
    input_fingerprint: str,
    side_effect: str = "",
    task_id: str = "",
    worktree_id: str = "",
    expected_project_id: str = "",
    expected_task_id: str = "",
    expected_phase: str = "",
    expected_action: str = "",
    expected_operation: str = "",
    expected_operation_id: str = "",
    expected_run_id: str = "",
    expected_plan_digest: str = "",
    expected_capability_binding: str = "",
    expected_input_fingerprint: str = "",
    expected_side_effect: str = "",
    expected_goal_digest: str = "",
    expected_execution_plan_digest: str = "",
    expected_plan_step_id: str = "",
    expected_operator_id: str = "",
    expected_input_set_digest: str = "",
    now: datetime | None = None,
) -> CapabilityTicket:
    """Verify and consume a ticket. Env vars alone cannot forge a valid ticket."""
    root_path = Path(root)
    scope_wt = worktree_id or ProjectScope(root_path, mode="discovery").worktree_id
    directory = _ticket_dir(root_path, scope_wt)
    directory.mkdir(parents=True, exist_ok=True)
    lock_path = directory / f".{ticket_id}.lock"
    when = now or utcnow()
    # §9 P1：兑换的读-验-写全程持锁，防止并发双消费（flock 对进程与线程均互斥）
    with open(lock_path, "a+") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        ticket = _load_ticket(root_path, ticket_id, worktree_id=scope_wt)
        _validate_ticket(
            ticket,
            root_path=root_path,
            scope_wt=scope_wt,
            secret=secret,
            action=action,
            input_fingerprint=input_fingerprint,
            side_effect=side_effect,
            task_id=task_id,
            expected_project_id=expected_project_id,
            expected_task_id=expected_task_id,
            expected_phase=expected_phase,
            expected_action=expected_action,
            expected_operation_id=expected_operation_id,
            expected_operation=expected_operation,
            expected_run_id=expected_run_id,
            expected_plan_digest=expected_plan_digest,
            expected_capability_binding=expected_capability_binding,
            expected_input_fingerprint=expected_input_fingerprint,
            expected_side_effect=expected_side_effect,
            expected_goal_digest=expected_goal_digest,
            expected_execution_plan_digest=expected_execution_plan_digest,
            expected_plan_step_id=expected_plan_step_id,
            expected_operator_id=expected_operator_id,
            expected_input_set_digest=expected_input_set_digest,
            when=when,
        )
        ticket.consumed_at = when
        _save_ticket(root_path, ticket)
        return ticket


def phase_grant_fingerprint(*, phase: str, allowed_side_effects: list[str],
                            task_id: str = "", operation: str = "") -> str:
    """阶段授权指纹（确定性；兑换方用同一参数重算）。"""
    raw = json.dumps({"phase": phase, "side_effects": sorted(allowed_side_effects),
                      "task_id": task_id, "operation": operation},
                     ensure_ascii=False, sort_keys=True)
    return "phase-" + content_hash_safe(raw)


def issue_phase_grant(
    root: Path, *, phase: str, allowed_side_effects: list[str],
    task_id: str = "", effective_plan_digest: str = "",
    operation: str = "", ttl_seconds: int = 300, issued_by: str = "sopctl",
    allowed_actions: list[str] | None = None,
    capability_binding: str = "",
    goal_digest: str = "",
    execution_plan_digest: str = "",
    plan_step_id: str = "",
    operator_id: str = "",
    input_set_digest: str = "",
    input_cardinality: int = 0,
    correction_revision: int = 0,
    strategy_fingerprint: str = "",
    gate_scope_digest: str = "",
) -> CapabilityTicket:
    """§10.4/§12.2：同一 task/phase/plan/operation 签发阶段授权（短时，副作用集合限定）。

    不得跨项目/跨任务/跨阶段/跨 operation 使用（兑换时逐项比对）。
    """
    if not phase.strip():
        raise TicketError("phase grant 需要命名阶段")
    if not allowed_side_effects:
        raise TicketError("phase grant 需要非空副作用集合")
    return issue_ticket(
        root, action=f"phase:{phase}",
        input_fingerprint=phase_grant_fingerprint(
            phase=phase, allowed_side_effects=list(allowed_side_effects),
            task_id=task_id, operation=operation),
        allowed_side_effects=list(allowed_side_effects), task_id=task_id,
        ttl_seconds=ttl_seconds, issued_by=issued_by,
        effective_plan_digest=effective_plan_digest, phase=phase,
        operation=operation,
        allowed_actions=allowed_actions,
        capability_binding=capability_binding,
    )


def verify_ticket_for_admission(
    root: Path,
    *,
    ticket_id: str,
    secret: str,
    action: str,
    input_fingerprint: str,
    side_effect: str = "",
    task_id: str = "",
    worktree_id: str = "",
    expected_project_id: str = "",
    expected_task_id: str = "",
    expected_phase: str = "",
    expected_action: str = "",
    expected_operation_id: str = "",
    expected_operation: str = "",
    expected_run_id: str = "",
    expected_plan_digest: str = "",
    expected_capability_binding: str = "",
    expected_input_fingerprint: str = "",
    expected_side_effect: str = "",
    expected_goal_digest: str = "",
    expected_execution_plan_digest: str = "",
    expected_plan_step_id: str = "",
    expected_operator_id: str = "",
    expected_input_set_digest: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Adapter 在真实 admission point 核验授权：只读，不消费票据。

    供经 SOPCTL_TICKET_FILE handoff 拿到票据的子进程 adapter 调用；
     opaque 命令忽略该文件（由 bridge 代兑并在 receipt 明示）。
    §18 禁止项：本函数没有 allow_consumed 参数——已消费票据一律拒绝；
    admission 的唯一消费入口是 redeem_ticket（一次性），verify 只作只读核验。
    """
    root_path = Path(root)
    scope_wt = worktree_id or ProjectScope(root_path, mode="discovery").worktree_id
    when = now or utcnow()
    ticket = _load_ticket(root_path, ticket_id, worktree_id=scope_wt)
    _validate_ticket(
        ticket,
        root_path=root_path,
        scope_wt=scope_wt,
        secret=secret,
        action=action,
        input_fingerprint=input_fingerprint,
        side_effect=side_effect,
        task_id=task_id,
        expected_project_id=expected_project_id,
        expected_task_id=expected_task_id,
        expected_phase=expected_phase,
        expected_action=expected_action,
        expected_operation_id=expected_operation_id,
        expected_operation=expected_operation,
        expected_run_id=expected_run_id,
        expected_plan_digest=expected_plan_digest,
        expected_capability_binding=expected_capability_binding,
        expected_input_fingerprint=expected_input_fingerprint,
        expected_side_effect=expected_side_effect,
        expected_goal_digest=expected_goal_digest,
        expected_execution_plan_digest=expected_execution_plan_digest,
        expected_plan_step_id=expected_plan_step_id,
        expected_operator_id=expected_operator_id,
        expected_input_set_digest=expected_input_set_digest,
        when=when,
    )
    return {
        "ticket_id": ticket.ticket_id,
        "verified": True,
        "consumed": ticket.consumed_at is not None,
        "effective_plan_digest": ticket.effective_plan_digest,
        "phase": ticket.phase,
        "operation": ticket.operation,
        "run_id": ticket.run_id,
        "plan_step_id": ticket.plan_step_id,
        "operator_id": ticket.operator_id,
        "input_set_digest": ticket.input_set_digest,
    }


def is_ticket_consumed(root: Path, ticket_id: str, worktree_id: str = "") -> bool:
    """只读：票据是否已被兑换（消费后验用；缺失视为未消费）。"""
    try:
        ticket = _load_ticket(root, ticket_id, worktree_id=worktree_id)
    except TicketError:
        return False
    return ticket.consumed_at is not None


def ticket_public_view(ticket: CapabilityTicket) -> dict[str, Any]:
    """Safe view for logs (excludes secret)."""
    data = ticket.model_dump(mode="json")
    data.pop("secret", None)
    return data
