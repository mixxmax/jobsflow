"""Load the current materials context for one job. Callers do not roam the workspace."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from tools.job_materials.jd_store import jd_meta, package_id_from_path, read_jd
from tools.job_materials.manifest import derive_tier, load_job_manifest, parse_job_id
from tools.job_materials.paths import load_lanes
from tools.job_materials.packages import validate_package_binding
from tools.job_materials.publisher import classify_publisher
from tools.workflow.id_allocation import is_assigned_job_id
from tools.fresh_24h.job_assessment import jd_fingerprint, profile_fingerprint
from tools.workflow.profile_facts import load_profile_facts


def _sha(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _read_json(path: Path) -> Any:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _find_package(root: Path, job_id: str) -> Path | None:
    masters = root / "01_Masters"
    if not masters.is_dir():
        return None
    wanted = str(job_id or "").strip()
    parsed = parse_job_id(wanted)
    lane = str(parsed.get("lane") or "").upper()
    if lane:
        lane_folder = str((load_lanes(root).get(lane) or {}).get("folder") or f"{lane}_track")
        tier = derive_tier(wanted).get("label") or "待审"
        expected_root = masters / lane_folder / tier
        expected = sorted(
            path for path in expected_root.glob(f"{wanted}_*") if path.is_dir()
        )
        if expected:
            return expected[0]
    matches = sorted(
        path
        for path in masters.rglob(f"{wanted}_*")
        if path.is_dir()
    )
    # Never create a package while reading a materials context.  Package
    # creation is an explicit /push entry side effect; /materials can only
    # write inside the package that entry created.
    return matches[0] if matches else None


@dataclass
class MaterialsContext:
    job_id: str
    package: str | None
    lane: str = ""
    jd_text: str = ""
    jd_source: str = ""
    jd_depth: str = "missing"
    jd_hash: str = ""
    duties: list[str] = field(default_factory=list)
    requirements: list[str] = field(default_factory=list)
    anchors: list[str] = field(default_factory=list)
    evidence_nodes: list[dict[str, Any]] = field(default_factory=list)
    # Stable user-owned facts are kept separate from per-job evidence.  They
    # may be used without an external citation, but always through a stable ID.
    profile_facts: list[dict[str, Any]] = field(default_factory=list)
    profile_fact_ids: list[str] = field(default_factory=list)
    assessment: dict[str, Any] | None = None
    preflight: dict[str, Any] | None = None
    unanswered_hard: list[str] = field(default_factory=list)
    publisher_type: str = "unknown"
    publisher_name: str = ""
    employer_name: str = ""
    role_primary: str = ""
    company_research: dict[str, Any] = field(default_factory=dict)
    forbidden_claims: list[str] = field(default_factory=list)
    manifest: dict[str, Any] = field(default_factory=dict)
    artifacts: dict[str, Any] = field(default_factory=dict)
    input_hashes: dict[str, str] = field(default_factory=dict)
    stale_reasons: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    profile_digest: str = ""
    candidate_name: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class PackageContextLoader:
    def __init__(self, workspace: Path) -> None:
        self.workspace = Path(workspace)

    def load(self, job_id: str) -> MaterialsContext:
        ctx = MaterialsContext(job_id=str(job_id or "").strip(), package=None)
        if not ctx.job_id:
            ctx.blockers.append("package_missing")
            return ctx
        # A scan preview key (SCAN-xxx / preview-...) is only a review
        # identity.  Material generation starts after confirmed tracker entry,
        # when the durable A0-001-style ID has been allocated.
        if not is_assigned_job_id(ctx.job_id):
            ctx.blockers.append("persistent_job_id_required")
            return ctx
        package = _find_package(self.workspace, ctx.job_id)
        if package is None:
            ctx.blockers.append("package_missing")
            return ctx
        ctx.package = str(package)
        ctx.blockers.extend(validate_package_binding(self.workspace, package, ctx.job_id))
        ctx.lane = ctx.job_id[:1]
        personal = _read_json(self.workspace / "00_Profile" / "config.personal.json") or {}
        ctx.candidate_name = str(personal.get("candidate_name") or personal.get("name") or "").strip()
        if not ctx.candidate_name:
            # A generic fallback would leak into filenames and make a package
            # look complete while still lacking the user's confirmed identity.
            # Stop before any draft, DOCX or PDF is generated.
            ctx.blockers.append("candidate_name_missing")
        meta = jd_meta(package, self.workspace)
        ctx.jd_text = read_jd(package, self.workspace)
        ctx.jd_source = str(meta.get("source") or "")
        ctx.jd_depth = str(meta.get("depth") or "missing")
        ctx.jd_hash = jd_fingerprint(ctx.jd_text)
        ctx.duties, ctx.requirements, ctx.anchors = _extract_jd_signals(ctx.jd_text)
        if meta.get("is_shallow") or ctx.jd_depth not in {"deep", "ok"}:
            ctx.blockers.append("missing_full_jd")

        facts = _read_json(self.workspace / "00_Profile" / "fact_evidence.json") or {}
        ctx.profile_facts = load_profile_facts(self.workspace)
        ctx.profile_fact_ids = [
            str(item.get("fact_id"))
            for item in ctx.profile_facts
            if item.get("fact_id") and item.get("confirmed")
        ]
        nodes = facts.get("nodes") or facts.get("evidence") or []
        if not nodes and isinstance(facts.get("records"), list):
            nodes = [
                {
                    "id": item.get("evidence_id"),
                    "evidence_id": item.get("evidence_id"),
                    "text": item.get("claim"),
                    "claim": item.get("claim"),
                    "metrics": list(item.get("metrics") or []),
                    "source_refs": list(item.get("source_refs") or []),
                    "allowed_phrasing": list(item.get("allowed_phrasing") or []),
                    "forbidden_inference": list(item.get("forbidden_inference") or []),
                    "status": item.get("status") or "user_imported",
                }
                for item in facts.get("records") or []
                if isinstance(item, dict) and item.get("evidence_id") and item.get("claim")
            ]
        if isinstance(nodes, list) and nodes:
            ctx.evidence_nodes = [dict(item) for item in nodes if isinstance(item, dict)]
        local_ev = _read_json(package / "evidence.json") or {}
        if not ctx.evidence_nodes:
            ctx.evidence_nodes = list(local_ev.get("nodes") or [])
        if not ctx.evidence_nodes:
            ctx.blockers.append("missing_fact_evidence")
        ctx.forbidden_claims = list(facts.get("forbidden_claims") or local_ev.get("forbidden_claims") or [])

        assessment = (
            _read_json(package / "assessment.json")
            or _latest_assessment(self.workspace, ctx.job_id, ctx.jd_hash, legacy_jd_hash=_sha(ctx.jd_text))
        )
        ctx.assessment = assessment if isinstance(assessment, dict) else None
        if ctx.assessment is None:
            ctx.blockers.append("assessment_missing_or_stale")
        else:
            stored_jd = str(
                ctx.assessment.get("jd_hash")
                or (ctx.assessment.get("jd") or {}).get("sha256")
                or ctx.assessment.get("input_signature", {}).get("jd_sha256")
                or ctx.assessment.get("input_hashes", {}).get("jd")
                or ""
            )
            if not stored_jd or stored_jd not in {ctx.jd_hash, _sha(ctx.jd_text)}:
                ctx.stale_reasons.append("assessment_jd_hash")
                ctx.blockers.append("assessment_missing_or_stale")

        preflight = _read_json(package / "application_preflight.json")
        ctx.preflight = preflight if isinstance(preflight, dict) else None
        if ctx.preflight is None:
            ctx.blockers.append("preflight_missing")
        else:
            ctx.unanswered_hard = [
                str(item)
                for item in (ctx.preflight.get("unanswered_hard") or ctx.preflight.get("questions") or [])
                if item
            ]
            if ctx.unanswered_hard:
                ctx.blockers.append("unresolved_hard_requirement")

        research = _read_json(package / "company_research.json") or {}
        ctx.company_research = research if isinstance(research, dict) else {}
        manifest = load_job_manifest(package) or {}
        ctx.manifest = manifest
        job = manifest.get("job") if isinstance(manifest.get("job"), dict) else {}
        ctx.publisher_type = str(research.get("publisher_type") or job.get("publisher_type") or "unknown").casefold()
        ctx.publisher_name = str(research.get("publisher_name") or job.get("publisher_name") or "")
        ctx.employer_name = str(
            research.get("company_out")
            or research.get("employer_name")
            or research.get("company")
            or job.get("company_out")
            or job.get("employer_name")
            or ""
        )
        ctx.role_primary = str(job.get("role_material") or job.get("role_display") or "")
        if ctx.publisher_type == "unknown":
            classified = classify_publisher(
                publisher_name=ctx.publisher_name or str(job.get("company_source") or ""),
                publisher_type=ctx.publisher_type,
                employer_name=ctx.employer_name,
                source_url=str(job.get("url") or ""),
                jd_text=ctx.jd_text,
            )
            ctx.publisher_type = str(classified.get("publisher_type") or "unknown").casefold()
            ctx.publisher_name = str(classified.get("publisher_name") or ctx.publisher_name)
            ctx.employer_name = str(classified.get("application_target") or ctx.employer_name)
        # An undisclosed recruiter client is valid and must remain unnamed;
        # only the role and publisher boundary are mandatory here.
        if ctx.publisher_type == "unknown" or not ctx.role_primary:
            ctx.blockers.append("entity_contract_incomplete")
        ctx.artifacts = manifest.get("artifacts") if isinstance(manifest.get("artifacts"), dict) else {}

        queries = _read_json(self.workspace / "00_Profile" / "queries.json") or {}
        scoring_profile = queries.get("scoring_profile") if isinstance(queries.get("scoring_profile"), dict) else queries
        ctx.profile_digest = profile_fingerprint(scoring_profile)
        ctx.input_hashes = {
            "jd": ctx.jd_hash,
            "profile": ctx.profile_digest,
            "preflight": _sha(json.dumps(ctx.preflight or {}, sort_keys=True)),
            "assessment": _sha(json.dumps(ctx.assessment or {}, sort_keys=True)),
        }
        if not all(ctx.input_hashes.values()):
            ctx.blockers.append("input_hash_incomplete")
        return ctx


def _latest_assessment(root: Path, job_id: str, jd_hash: str, *, legacy_jd_hash: str = "") -> dict[str, Any] | None:
    folder = root / "02_Tracker" / "job_assessments"
    if not folder.is_dir():
        return None
    for path in sorted(folder.glob("*.json"), reverse=True):
        data = _read_json(path)
        if not isinstance(data, dict):
            continue
        stored_job = str(data.get("job_id") or (data.get("job") or {}).get("job_id") or "")
        stored_jd = str(
            data.get("jd_hash")
            or (data.get("jd") or {}).get("sha256")
            or (data.get("input_signature") or {}).get("jd_sha256")
            or ""
        )
        if stored_job == job_id and (not jd_hash or not stored_jd or stored_jd in {jd_hash, legacy_jd_hash}):
            return data
    return None


def _extract_jd_signals(text: str) -> tuple[list[str], list[str], list[str]]:
    """Extract bounded JD signals for the task packet.

    This is intentionally a deterministic, shallow parser.  It does not make
    semantic claims for the model; it prevents the task packet from carrying
    only ``full_jd=true`` while still giving a weaker model concrete duties,
    requirements and keyword anchors to work from.
    """
    import re

    lines = [re.sub(r"\s+", " ", line).strip(" -\t") for line in (text or "").splitlines()]
    lines = [line for line in lines if line]
    duty_heads = re.compile(r"^(?:key\s+)?responsibilit(?:y|ies)|duties|what you(?:'|’)ll do|主要职责", re.I)
    req_heads = re.compile(r"^(?:key\s+)?requirements?|qualifications?|experience|必备|要求", re.I)

    def section(head_re: re.Pattern[str]) -> list[str]:
        result: list[str] = []
        active = False
        for line in lines:
            if head_re.search(line):
                active = True
                continue
            if active and (duty_heads.search(line) or req_heads.search(line)) and not head_re.search(line):
                break
            if active:
                result.append(line)
        return _unique(result)

    duties = section(duty_heads)
    requirements = section(req_heads)
    if not duties:
        duties = _sentences(lines[:20])
    if not requirements:
        requirements = _sentences(lines[20:])
    anchors = _unique(
        [
            token.casefold()
            for token in re.findall(r"[A-Za-z][A-Za-z0-9+/-]{2,}|[\u3400-\u4dbf\u4e00-\u9fff]{2,}", " ".join(duties + requirements))
        ]
    )[:24]
    return duties[:12], requirements[:12], anchors


def _sentences(lines: list[str]) -> list[str]:
    import re

    text = " ".join(lines)
    return _unique([item.strip() for item in re.split(r"(?<=[.!?。！？])\s+", text) if item.strip()])[:12]


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = str(value or "").strip()
        if not normalized:
            continue
        key = normalized.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(normalized)
    return result
