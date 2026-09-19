"""Gateway-owned private template registration and selection.

Templates are runtime data, not a second renderer.  Registration is a
preview-bound copy of a user-supplied DOCX; selection is a small atomic
runtime projection consumed by the existing lane renderer.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from tools.io_utils import atomic_write_json, atomic_write_text
from tools.workflow.confirmation import ConfirmationStore, build_proposal
from tools.workflow.contracts import result
from tools.workflow.interaction_shell import product_root, runtime_gate

_NAME_RE = re.compile(r"^[a-z0-9-]{1,64}$")
_TYPES = {"cv", "cover_letter"}
_MAX_BYTES = 20 * 1024 * 1024


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _root(workspace: Path) -> Path:
    return Path(workspace).expanduser().resolve() / "00_Profile" / "templates"


def _safe_source(source: Path) -> Path:
    raw = Path(source).expanduser()
    # Resolve only after rejecting a symlink at the supplied file itself.
    # System paths such as macOS ``/var`` legitimately contain symlinked
    # parents, so parent links are not treated as an unsafe template file.
    if raw.is_symlink():
        raise ValueError("template_source_symlink_rejected")
    path = raw.resolve()
    if not path.is_file() or path.suffix.casefold() != ".docx":
        raise ValueError("template_source_invalid")
    if path.stat().st_size > _MAX_BYTES:
        raise ValueError("template_source_too_large")
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            if "[Content_Types].xml" not in names or "word/document.xml" not in names:
                raise ValueError("template_docx_invalid")
            if any(name.casefold().endswith("vbaproject.bin") for name in names):
                raise ValueError("template_docx_macro_rejected")
    except (OSError, zipfile.BadZipFile) as exc:
        raise ValueError("template_docx_invalid") from exc
    return path


def _safe_name(name: str) -> str:
    name = str(name or "").strip()
    if not _NAME_RE.fullmatch(name):
        raise ValueError("template_name_invalid")
    return name


def _safe_type(value: str) -> str:
    value = str(value or "").strip().casefold()
    if value not in _TYPES:
        raise ValueError("template_type_invalid")
    return value


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _proposal_expired(proposal: dict[str, Any]) -> bool:
    try:
        expires = datetime.fromisoformat(str(proposal.get("expires_at") or "").replace("Z", "+00:00"))
    except ValueError:
        return True
    return datetime.now(timezone.utc) > expires


def preview_register(
    workspace: Path, *, source: str, name: str, template_type: str, notes: str = ""
) -> dict[str, Any]:
    try:
        source_path = _safe_source(Path(source))
        name = _safe_name(name)
        template_type = _safe_type(template_type)
    except (OSError, ValueError) as exc:
        return result(status="blocked", blockers=[str(exc)])
    target_root = _root(workspace) / name
    if target_root.exists():
        return result(status="blocked", blockers=["template_exists"])
    proposal = build_proposal(
        action="template_confirm",
        target=f"00_Profile/templates/{name}",
        target_digest=_sha(source_path),
        row_count=1,
        effects=["private_template_copy"],
        extra={
            "source": str(source_path),
            "source_sha256": _sha(source_path),
            "name": name,
            "template_type": template_type,
            "notes": str(notes or "").strip(),
        },
    )
    ConfirmationStore(Path(workspace)).save(proposal)
    return result(
        status="succeeded",
        proposal_id=proposal["proposal_id"],
        target=proposal["target"],
        source_sha256=proposal["target_digest"],
        expires_at=proposal["expires_at"],
        next_action="template_confirm",
    )


def confirm_register(workspace: Path, *, proposal_id: str) -> dict[str, Any]:
    store = ConfirmationStore(Path(workspace))
    proposal = store.load(proposal_id)
    if not proposal or proposal.get("action") != "template_confirm":
        return result(status="blocked", blockers=["template_proposal_unknown"])
    try:
        name = _safe_name(str(proposal.get("name") or ""))
    except ValueError as exc:
        return result(status="blocked", blockers=["template_proposal_corrupt"], error=str(exc))
    if proposal.get("status") == "applied":
        target = _root(workspace) / name
        artifact = target / "template.docx"
        if artifact.is_file() and _sha(artifact) == str(proposal.get("artifact_sha256") or ""):
            return result(status="succeeded", proposal_id=proposal_id, idempotent=True, path=str(target))
        return result(status="blocked", blockers=["template_confirm_diverged"])
    if proposal.get("status") != "pending_confirmation":
        return result(status="blocked", blockers=["template_proposal_mismatch"])
    if _proposal_expired(proposal):
        return result(status="blocked", blockers=["template_proposal_expired"])
    try:
        source = _safe_source(Path(str(proposal.get("source") or "")))
        template_type = _safe_type(str(proposal.get("template_type") or ""))
    except (OSError, ValueError) as exc:
        return result(status="blocked", blockers=["template_proposal_corrupt"], error=str(exc))
    if _sha(source) != str(proposal.get("source_sha256") or ""):
        return result(status="blocked", blockers=["template_proposal_stale"])
    target = _root(workspace) / name
    if target.exists():
        artifact = target / "template.docx"
        if artifact.is_file() and _sha(artifact) == str(proposal.get("source_sha256") or ""):
            completed = dict(proposal)
            completed["status"] = "applied"
            completed["applied_at"] = _utcnow()
            completed["artifact_sha256"] = _sha(artifact)
            store.save(completed)
            return result(status="succeeded", proposal_id=proposal_id, idempotent=True, path=str(target))
        return result(status="blocked", blockers=["template_exists"])
    templates_root = _root(workspace)
    templates_root.mkdir(parents=True, exist_ok=True)
    staging = templates_root / f".{name}.tmp-{uuid4().hex[:12]}"
    try:
        staging.mkdir(parents=False, exist_ok=False)
        artifact = staging / "template.docx"
        artifact.write_bytes(source.read_bytes())
        notes = str(proposal.get("notes") or "").strip()
        metadata = (
            f"# Template: {name}\n\n"
            f"- **Type:** {template_type}\n"
            "- **Format:** DOCX\n"
            "- **Engine:** fixed JobsFlow lane renderer + LibreOffice headless\n"
            "- **Status:** private runtime reference; not automatically substituted into vNext\n\n"
            "## Style rules\n\n"
            "- The selected lane renderer remains the only production renderer.\n"
            f"- User notes: {notes or 'none'}\n"
        )
        atomic_write_text(staging / "TEMPLATE.md", metadata)
        os.replace(staging, target)
    except Exception as exc:
        for path in (staging / "template.docx", staging / "TEMPLATE.md"):
            try:
                path.unlink()
            except OSError:
                pass
        try:
            staging.rmdir()
        except OSError:
            pass
        return result(status="failed", blockers=["template_register_failed"], error=str(exc))
    proposal = dict(proposal)
    proposal["status"] = "applied"
    proposal["applied_at"] = _utcnow()
    proposal["artifact_sha256"] = _sha(target / "template.docx")
    store.save(proposal)
    return result(status="succeeded", proposal_id=proposal_id, name=name, path=str(target))


def list_templates(workspace: Path) -> dict[str, Any]:
    root = _root(workspace)
    items = []
    if root.is_dir():
        for directory in sorted(path for path in root.iterdir() if path.is_dir()):
            docx = directory / "template.docx"
            if docx.is_file():
                items.append({"name": directory.name, "path": str(directory), "docx": str(docx)})
    active_path = root / "active.json"
    active = ""
    if active_path.is_file():
        try:
            active = str(json.loads(active_path.read_text(encoding="utf-8")).get("name") or "")
        except (OSError, ValueError, TypeError):
            return result(status="blocked", blockers=["template_selection_corrupt"])
    return result(status="succeeded", templates=items, active=active or "default")


def select_template(workspace: Path, *, name: str) -> dict[str, Any]:
    name = str(name or "default").strip()
    if name != "default":
        try:
            name = _safe_name(name)
        except ValueError as exc:
            return result(status="blocked", blockers=[str(exc)])
        if not (_root(workspace) / name / "template.docx").is_file():
            return result(status="blocked", blockers=["template_not_found"])
    path = _root(workspace) / "active.json"
    atomic_write_json(path, {"schema_version": 1, "name": name, "updated_at": _utcnow()})
    return result(status="succeeded", active=name or "default", path=str(path))


def handle(action: str, payload: dict[str, Any], *, workspace: Path) -> dict[str, Any]:
    resolved = Path(workspace).expanduser().resolve()
    if resolved == product_root().resolve():
        return result(status="blocked", blockers=["private_write_product_root_refused"])
    gated = runtime_gate(action, resolved)
    if gated is not None:
        return result(
            status="blocked",
            blockers=["runtime_workspace_invalid"],
            message=gated.get("message"),
            user_prompt=gated.get("user_prompt"),
            next_action=gated.get("next_action"),
        )
    workspace = resolved
    if action == "template_preview":
        return preview_register(
            workspace,
            source=str(payload.get("source") or ""),
            name=str(payload.get("name") or ""),
            template_type=str(payload.get("template_type") or ""),
            notes=str(payload.get("notes") or ""),
        )
    if action == "template_confirm":
        return confirm_register(workspace, proposal_id=str(payload.get("proposal_id") or ""))
    if action == "template_list":
        return list_templates(workspace)
    if action in {"template_select", "template_clear"}:
        return select_template(workspace, name="default" if action == "template_clear" else str(payload.get("name") or ""))
    return result(status="blocked", blockers=["unknown_action"])
