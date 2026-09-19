"""可信确认通道（WP-C3）：预览→批准一次性绑定，防重放、防跨项目、防伪造。

原语统一：底层一次性凭据复用 learning.py 的签发/核销（全仓唯一确认原语，
secret 只落 0600 handoff、记录只存 digest）；本模块在其上补齐通用权威变更
所需的绑定层——有效期、项目、变更摘要——三者任一不符即拒。

权限模型（诚实边界）：
- 确认凭据只能由 Agent 无法访问的用户侧签发才算“已验证的人类确认”。
  本地 CLI 没有宿主 UI 通道，因此所有经 CLI 到达的批准一律记为
  claimed（自称），人类在场为 UNPROVEN——如实标注，不伪装。
- secret 解析顺序：显式传入 > --secret-file（0600）> 拒绝；
  永不自动读 handoff（与 learning.resolve_confirmation_secret 同约）。
- 非交互 CI/迁移走维护者凭据（SOPCTL_MAINTAINER_TOKEN），记录
  via=maintainer-token，可审计；单靠 TTY 或把 secret 打到模型可见
  stdout 不构成人类证明。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

PENDING_TTL_SECONDS = 3600


class ConfirmationError(ValueError):
    pass


def change_digest(rule_id: str, statement: str, extra: dict[str, Any] | None = None) -> str:
    """变更内容摘要：rule_id + 规范化陈述 + 附加绑定字段。"""
    import hashlib as _hashlib

    payload = {"rule_id": rule_id,
               "statement": " ".join(str(statement or "").split()),
               "extra": extra or {}}
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return "chg-" + _hashlib.sha256(raw).hexdigest()[:32]


def _binding_path(root: Path, confirmation_id: str) -> Path:
    from .scope import validate_identifier

    validate_identifier(confirmation_id, kind="confirmation id")
    return Path(root) / ".sopcontrol-local" / "confirmations" / f"{confirmation_id}.json"


def request_confirmation(
    root: Path | str, *, kind: str, subject_id: str, digest: str,
    purpose: str, project_id: str = "", ttl_seconds: int = PENDING_TTL_SECONDS,
    actor_claim: str = "",
) -> dict[str, Any]:
    """签发一次确认请求（预览）。底层凭据走统一原语；本层记录绑定。"""
    from .learning import issue_learning_confirmation

    root = Path(root)
    if not kind.strip() or not subject_id.strip():
        raise ConfirmationError("确认请求需要 kind 与 subject_id")
    if not digest.strip():
        raise ConfirmationError("确认请求需要变更内容摘要（不得批空白变更）")
    proposal_id = f"{kind}:{subject_id}"
    issued = issue_learning_confirmation(root, proposal_id)
    confirmation_id = issued["confirmation_id"]
    expires_at = (datetime.now(timezone.utc)
                  + timedelta(seconds=int(ttl_seconds or PENDING_TTL_SECONDS))).isoformat()
    binding = {"confirmation_id": confirmation_id, "kind": kind,
               "subject_id": subject_id, "change_digest": digest,
               "purpose": purpose, "project_id": project_id,
               "actor_claim": actor_claim, "expires_at": expires_at,
               "consumed": False}
    path = _binding_path(root, confirmation_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(binding, ensure_ascii=False, indent=2), encoding="utf-8")
    return binding


def _load_binding(root: Path, confirmation_id: str) -> dict[str, Any]:
    path = _binding_path(root, confirmation_id)
    if not path.is_file():
        raise ConfirmationError(f"未知确认请求: {confirmation_id}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ConfirmationError(f"确认记录损坏: {exc}") from exc


def _resolve_secret(*, secret: str = "", secret_file: str = "") -> str:
    """secret 解析：显式传入 > 0600 文件 > 拒绝。永不自动读 handoff。"""
    if str(secret or "").strip():
        return str(secret).strip()
    if not secret_file:
        raise ConfirmationError("批准需要显式 secret（--confirmation-secret）或 "
                                "--secret-file（0600 文件），不自动读 handoff")
    try:
        mode = os.stat(secret_file).st_mode & 0o777
    except OSError as exc:
        raise ConfirmationError(f"secret 文件不可读: {exc}") from exc
    if mode & 0o077:
        raise ConfirmationError(f"secret 文件权限过宽 ({oct(mode)})：必须仅所有者可读写")
    try:
        return Path(secret_file).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ConfirmationError(f"secret 文件不可读: {exc}") from exc


def approve_confirmation(
    root: Path | str, confirmation_id: str, *, secret: str = "",
    secret_file: str = "", expected_digest: str = "",
    expected_project_id: str = "", via: str = "user-channel",
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """批准（兑换，一次性）。成功返回批准记录（claimed，非已验证人类）。"""
    from .learning import redeem_learning_confirmation

    root = Path(root)
    binding = _load_binding(root, confirmation_id)
    if binding.get("consumed"):
        raise ConfirmationError(f"确认 {confirmation_id} 已被使用：重放拒绝")
    if expected_digest and binding["change_digest"] != expected_digest:
        raise ConfirmationError("变更摘要不一致：批准绑定的是另一份变更")
    if expected_project_id and binding.get("project_id") \
            and binding["project_id"] != expected_project_id:
        raise ConfirmationError("项目不一致：跨项目凭据拒绝")
    exp = binding.get("expires_at") or ""
    try:
        exp_dt = datetime.fromisoformat(str(exp).replace("Z", "+00:00"))
    except ValueError:
        raise ConfirmationError("确认记录过期时间非法：拒绝") from None
    if exp_dt.tzinfo is None:
        raise ConfirmationError("确认记录过期时间无时区：拒绝") from None
    if (now or datetime.now(timezone.utc)) >= exp_dt:
        raise ConfirmationError("确认请求已过期：重发请求后批准") from None
    raw_secret = _resolve_secret(secret=secret, secret_file=secret_file)
    maintainer = os.environ.get("SOPCTL_MAINTAINER_TOKEN", "").strip()
    via_label = via
    if maintainer and raw_secret == maintainer:
        # 维护者凭据路径（CI/迁移）：凭据对上后，由本进程经统一原语的
        # handoff 取实际 secret 完成兑换——绑定检查与单次消费照常，不跳过。
        from .learning import read_learning_confirmation_secret

        via_label = "maintainer-token"
        try:
            raw_secret = read_learning_confirmation_secret(root, confirmation_id)
        except ValueError as exc:
            raise ConfirmationError(f"批准失败: {exc}") from exc
    try:
        redeem_learning_confirmation(
            root, proposal_id=f"{binding['kind']}:{binding['subject_id']}",
            confirmation_id=confirmation_id, secret=raw_secret)
    except ValueError as exc:
        raise ConfirmationError(f"批准失败: {exc}") from exc
    binding["consumed"] = True
    _binding_path(root, confirmation_id).write_text(
        json.dumps(binding, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"confirmation_id": confirmation_id, "kind": binding["kind"],
            "subject_id": binding["subject_id"],
            "change_digest": binding["change_digest"],
            "approved_via": via_label,
            "authority": "claimed",
            "human_presence": "UNPROVEN",
            "note": "本地 CLI 无法证明人类在场；无宿主可信通道时视为自称，需用户侧复核"}


def show_confirmation(root: Path | str, confirmation_id: str) -> dict[str, Any]:
    """只读查看确认请求状态（不消费）。"""
    return _load_binding(Path(root), confirmation_id)
