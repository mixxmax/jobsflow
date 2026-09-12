"""Worktree-local evidence cache for incremental discovery.

Keys: rel_path + file_hash + sensor_id/version + scan_config + project_id.
Stored under .sopcontrol-local/ (gitignored); never writes private body text
into the committed ledger.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .context import ProjectScope, content_hash_safe, file_hash
from .model import Evidence


CACHE_VERSION = "1"
SENSOR_VERSIONS = {
    "doc_scan": "1",
    "code_scan": "1",
    "ast_scan": "1",
    "js_scan": "1",
    "ts_ast_scan": "1",
    "go_scan": "1",
    "go_ast_scan": "1",
    "rust_scan": "1",
    "rust_ast_scan": "1",
    "import_graph": "1",
    "trace_scan": "1",
}


def cache_root(scope: ProjectScope) -> Path:
    base = Path(scope.root) / ".sopcontrol-local" / "worktrees" / (scope.worktree_id or "default")
    return base / "evidence-cache"


def _scan_config_fingerprint(scope: ProjectScope) -> str:
    return content_hash_safe(
        json.dumps(
            {
                "excludes": [list(x) for x in scope.scan_excludes],
                "mode": scope.mode,
            },
            sort_keys=True,
        )
    )


def cache_key(
    *,
    project_id: str,
    rel_path: str,
    digest: str,
    sensor_id: str,
    scope: ProjectScope,
) -> str:
    sensor_ver = SENSOR_VERSIONS.get(sensor_id, "0")
    return content_hash_safe(
        "|".join(
            [
                CACHE_VERSION,
                project_id,
                rel_path,
                digest,
                sensor_id,
                sensor_ver,
                _scan_config_fingerprint(scope),
            ]
        )
    )


class EvidenceCache:
    def __init__(self, scope: ProjectScope, project_id: str = ""):
        self.scope = scope
        self.project_id = project_id or "unknown"
        self.root = cache_root(scope)
        self.root.mkdir(parents=True, exist_ok=True)
        self.hits = 0
        self.misses = 0

    def _path_for(self, key: str) -> Path:
        return self.root / f"{key}.json"

    def get(self, *, rel_path: str, path: Path, sensor_id: str) -> list[Evidence] | None:
        try:
            digest = file_hash(path)
        except OSError:
            self.misses += 1
            return None
        key = cache_key(
            project_id=self.project_id,
            rel_path=rel_path,
            digest=digest,
            sensor_id=sensor_id,
            scope=self.scope,
        )
        blob = self._path_for(key)
        if not blob.exists():
            self.misses += 1
            return None
        try:
            data = json.loads(blob.read_text(encoding="utf-8"))
            rows = [Evidence.model_validate(item) for item in data.get("evidence") or []]
        except (OSError, json.JSONDecodeError, ValueError, TypeError):
            self.misses += 1
            return None
        self.hits += 1
        return rows

    def put(self, *, rel_path: str, path: Path, sensor_id: str, evidence: list[Evidence]) -> None:
        try:
            digest = file_hash(path)
        except OSError:
            return
        key = cache_key(
            project_id=self.project_id,
            rel_path=rel_path,
            digest=digest,
            sensor_id=sensor_id,
            scope=self.scope,
        )
        payload = {
            "key": key,
            "rel_path": rel_path,
            "sensor_id": sensor_id,
            "evidence": [ev.model_dump(mode="json") for ev in evidence],
        }
        self.root.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=".cache.", suffix=".tmp", dir=str(self.root))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, self._path_for(key))
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass

    def stats(self) -> dict[str, Any]:
        return {"cache_hits": self.hits, "cache_misses": self.misses}
