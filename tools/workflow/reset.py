"""Gateway-owned reset for the legacy profile/documents surface.

``/reset`` used to instruct models to edit product files and ``rm`` user
files by hand.  Every destructive step now goes through this adapter:

preview (target list + digests, persisted proposal) -> user confirmation ->
confirm (re-derive state, require an exact match, execute atomically).

Tracked product templates (``.claude/skills/...``) are NEVER reset targets:
profile data lives in the private runtime workspace (``00_Profile``), and
only that data plus the gitignored ``documents/`` user files may be cleared.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tools.io_utils import atomic_write_json, atomic_write_text
from tools.workflow.contracts import result

RESET_DIR_NAME = ".jobsflow-reset"
PROPOSAL_TTL_SECONDS = 24 * 3600

PROFILE_FILES = (
    "00_Profile/queries.json",
    "00_Profile/config.personal.json",
    "00_Profile/fact_evidence.json",
)
PROFILE_SUBDIRS = ("00_Profile/resume_runtime",)
_PROFILE_FILE_NAMES = {path.split("/", 1)[1] for path in PROFILE_FILES}
_PROFILE_SUBDIR_NAMES = {path.split("/", 1)[1] for path in PROFILE_SUBDIRS}
DOCUMENT_SUBDIRS = (
    "documents/cv",
    "documents/linkedin",
    "documents/diplomas",
    "documents/references",
    "documents/applications",
)
KEEP_NAMES = {"README.md", ".gitkeep"}


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _proposal_expired(proposal: dict[str, Any], *, now: datetime | None = None) -> bool:
    try:
        created = datetime.strptime(str(proposal.get("created_at") or ""), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return True
    at = now or datetime.now(timezone.utc)
    return (at - created).total_seconds() > PROPOSAL_TTL_SECONDS


def _sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _scope_dirs(scope: str) -> tuple[str, ...]:
    if scope == "profile":
        return ("00_Profile",)
    if scope == "documents":
        return ("documents",)
    if scope == "all":
        return ("00_Profile", "documents")
    raise ValueError(f"reset_scope_invalid:{scope}")


def _resolve_within(root: Path, rel: str) -> Path:
    resolved_root = root.resolve()
    candidate = (resolved_root / rel).resolve()
    try:
        candidate.relative_to(resolved_root)
    except ValueError:
        raise ValueError(f"reset_target_outside_root:{rel}")
    return candidate


def _is_target(root: Path, rel: str) -> bool:
    """Allowlist check on the unresolved relative path (no traversal tricks)."""

    parts = Path(rel).parts
    if not parts or Path(rel).is_absolute() or ".." in parts:
        return False
    name = parts[-1]
    if name in KEEP_NAMES:
        return False
    if parts[0] == "00_Profile":
        rest = str(Path(*parts[1:])) if len(parts) > 1 else ""
        if rest in _PROFILE_FILE_NAMES:
            return True
        return any(
            rest == sub or rest.startswith(sub + "/") for sub in _PROFILE_SUBDIR_NAMES
        )
    if parts[0] == "documents":
        if len(parts) < 3:
            return False
        if parts[1] not in {sub.split("/", 1)[1] for sub in DOCUMENT_SUBDIRS}:
            return False
        return True
    return False


def _inventory(root: Path, scope: str) -> list[dict[str, Any]]:
    """Every file under the scope dirs: the confirm-time binding surface."""

    found: list[dict[str, Any]] = []
    for scope_dir in _scope_dirs(scope):
        base = root / scope_dir
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            rel = path.relative_to(root).as_posix()
            try:
                digest = _sha_bytes(path.read_bytes())
            except OSError:
                continue
            found.append({"rel": rel, "sha256": digest})
    return found


def _inventory_digest(inventory: list[dict[str, Any]]) -> str:
    raw = json.dumps(inventory, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _sha_bytes(raw.encode("utf-8"))


def _store_paths(root: Path) -> tuple[Path, Path, Path]:
    base = root / RESET_DIR_NAME
    return base / "proposals", base / "backups", base / "audit.jsonl"


def _require_scope_layout(root: Path, scope: str) -> str | None:
    if scope in {"profile", "all"} and not (root / "00_Profile").is_dir():
        return "reset_profile_scope_missing"
    if scope in {"documents", "all"} and not (root / "documents").is_dir():
        return "reset_documents_scope_missing"
    return None


def preview_reset(root: Path | str, *, scope: str) -> dict[str, Any]:
    """List reset targets with digests and persist a bound proposal."""

    root = Path(root).expanduser().resolve()
    try:
        _scope_dirs(scope)
    except ValueError:
        return result(status="blocked", after_state="idle", blockers=["reset_scope_invalid"])
    layout_error = _require_scope_layout(root, scope)
    if layout_error:
        return result(status="blocked", after_state="idle", blockers=[layout_error])
    inventory = _inventory(root, scope)
    targets = [
        {**item, "kind": "delete", "status": "has_content"}
        for item in inventory
        if _is_target(root, item["rel"])
    ]
    proposal_id = f"reset-{hashlib.sha256(f'{scope}:{_inventory_digest(inventory)}:{_utcnow()}'.encode()).hexdigest()[:12]}"
    proposals_dir, _, _ = _store_paths(root)
    proposals_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(
        proposals_dir / f"{proposal_id}.json",
        {
            "schema_version": 1,
            "proposal_id": proposal_id,
            "scope": scope,
            "root": str(root),
            "created_at": _utcnow(),
            "inventory_digest": _inventory_digest(inventory),
            "targets": targets,
        },
    )
    return result(
        status="succeeded",
        after_state="reset_previewed",
        proposal_id=proposal_id,
        scope=scope,
        targets=targets,
        target_digest=_inventory_digest(inventory),
        next_action="reset_confirm",
    )


def _load_proposal(root: Path, proposal_id: str) -> dict[str, Any] | None:
    proposals_dir, _, _ = _store_paths(root)
    try:
        data = json.loads((proposals_dir / f"{proposal_id}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _audit(root: Path, entry: dict[str, Any]) -> None:
    _, _, audit_path = _store_paths(root)
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    with audit_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"at": _utcnow(), **entry}, ensure_ascii=False) + "\n")


def confirm_reset(root: Path | str, *, scope: str, proposal_id: str | None) -> dict[str, Any]:
    """Execute a preview-bound reset atomically, or change nothing."""

    root = Path(root).expanduser().resolve()
    proposal = _load_proposal(root, proposal_id or "")
    if proposal is None or proposal.get("proposal_id") != (proposal_id or ""):
        return result(status="blocked", after_state="reset_previewed", blockers=["reset_proposal_unknown"])
    if proposal.get("scope") != scope or proposal.get("root") != str(root):
        return result(status="blocked", after_state="reset_previewed", blockers=["reset_proposal_mismatch"])
    if _proposal_expired(proposal):
        return result(status="blocked", after_state="reset_previewed", blockers=["reset_proposal_expired"])
    try:
        _scope_dirs(scope)
    except ValueError:
        return result(status="blocked", after_state="reset_previewed", blockers=["reset_scope_invalid"])
    targets = proposal.get("targets")
    if not isinstance(targets, list):
        return result(status="blocked", after_state="reset_previewed", blockers=["reset_proposal_corrupt"])
    # Reject tampered proposals before comparing the inventory binding.
    resolved: list[tuple[str, Path]] = []
    for item in targets:
        rel = str((item or {}).get("rel") or "")
        if not _is_target(root, rel):
            return result(status="blocked", after_state="reset_previewed", blockers=["reset_target_rejected"])
        try:
            resolved.append((rel, _resolve_within(root, rel)))
        except ValueError:
            return result(status="blocked", after_state="reset_previewed", blockers=["reset_target_rejected"])
    inventory = _inventory(root, scope)
    if _inventory_digest(inventory) != proposal.get("inventory_digest"):
        return result(status="blocked", after_state="reset_previewed", blockers=["reset_proposal_stale"])

    before = {item["rel"]: item["sha256"] for item in inventory}
    proposals_dir, backups_root, _ = _store_paths(root)
    backup_dir = backups_root / str(proposal_id)
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
        staged = []
        for rel, path in resolved:
            if not path.is_file():
                continue
            backup_path = backup_dir / rel
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            backup_path.write_bytes(path.read_bytes())
            staged.append((rel, path))
        for rel, path in staged:
            path.unlink()
        _prune_empty_dirs(root, scope)
        missing = [rel for rel, path in staged if path.exists()]
        if missing:
            raise OSError(f"reset_delete_incomplete:{sorted(missing)}")
    except Exception as exc:
        restored = _restore(backup_dir, root)
        receipt = {
            "proposal_id": proposal_id,
            "scope": scope,
            "status": "failed",
            "error": str(exc),
            "restored": restored,
        }
        _audit(root, receipt)
        out = result(
            status="failed",
            after_state="reset_previewed",
            blockers=["reset_execution_failed"],
            error=str(exc),
            restored=restored,
        )
        out["proposal_id"] = str(proposal_id)
        return out
    receipt_targets = [
        {"rel": rel, "before": before.get(rel, ""), "after": ""}
        for rel, _ in staged
    ]
    _audit(root, {"proposal_id": proposal_id, "scope": scope, "status": "executed", "targets": receipt_targets})
    try:
        (proposals_dir / f"{proposal_id}.json").unlink()
    except OSError:
        pass
    out = result(
        status="succeeded",
        after_state="reset_executed",
        proposal_id=proposal_id,
        scope=scope,
        targets=receipt_targets,
    )
    return out


def _prune_empty_dirs(root: Path, scope: str) -> None:
    keep = set()
    for scope_dir in _scope_dirs(scope):
        keep.add((root / scope_dir).resolve())
        if scope_dir == "documents":
            for sub in DOCUMENT_SUBDIRS:
                keep.add((root / sub).resolve())
    candidates: list[Path] = []
    for scope_dir in _scope_dirs(scope):
        base = root / scope_dir
        if base.is_dir():
            candidates.extend(sorted((p for p in base.rglob("*") if p.is_dir()), reverse=True))
    for path in candidates:
        resolved = path.resolve()
        if resolved in keep:
            continue
        try:
            if not any(resolved.iterdir()):
                resolved.rmdir()
        except OSError:
            continue


def _restore(backup_dir: Path, root: Path) -> bool:
    ok = True
    if not backup_dir.is_dir():
        return False
    for backup_path in sorted(backup_dir.rglob("*")):
        if not backup_path.is_file():
            continue
        rel = backup_path.relative_to(backup_dir).as_posix()
        try:
            target = _resolve_within(root, rel)
        except ValueError:
            ok = False
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(backup_path.read_bytes())
        except OSError:
            ok = False
    return ok


def handle(action: str, payload: dict[str, Any], *, workspace: Path) -> dict[str, Any]:
    """Gateway entrypoint: ``reset_preview`` / ``reset_confirm``."""

    scope = str(payload.get("scope") or "").strip()
    raw_root = payload.get("root") or payload.get("workspace") or workspace
    root = Path(str(raw_root)).expanduser()
    if action == "reset_preview":
        return preview_reset(root, scope=scope)
    if action == "reset_confirm":
        return confirm_reset(root, scope=scope, proposal_id=payload.get("proposal_id"))
    return result(status="blocked", blockers=["unknown_action"])
