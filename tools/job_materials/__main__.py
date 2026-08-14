#!/usr/bin/env python3
"""
JobSearch_2026 materials pipeline CLI.

Honest scope:
  - Scan two-pass (fresh_24h) is SEPARATE; this module never auto-runs on /scan.
  - Deep full JD is reliable mainly for LinkedIn; CT/JobsDB need paste (`jd set`).
  - tailor reorders an independently fact-checked A–F evidence set; it does NOT
    reopen the source or invent claims; the plan is emphasis (skills/bullets order).

Stages:
  A) Read CV / masters → A–F bases with FACT-CHECK
  B) See full JD (URL normalize + LinkedIn deep + paste)
  C) Tailor from passed independent evidence set toward JD (no freestyle claims)

Usage examples:
  python3 -m tools.job_materials base sync
  python3 -m tools.job_materials base factcheck --lane C
  python3 -m tools.job_materials base list

  python3 -m tools.job_materials url normalize --url 'https://jobs.example/job/123'

  python3 -m tools.job_materials jd set --package 'JobSearch_2026/01_Masters/A_track/核心/A0-005_未投_Example' --file ./jd.txt
  python3 -m tools.job_materials enrich --package '...'

  python3 -m tools.job_materials tailor --package '...' --lane C
  python3 -m tools.job_materials pipeline --package '...' --lane C
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

# allow `python3 -m tools.job_materials` from repo root
REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools.job_materials.bases import (  # noqa: E402
    factcheck_base,
    list_bases,
    load_base,
    pick_lane_from_text,
    save_base,
    sync_base_from_masters,
)
from tools.job_materials.enrich import enrich_package, normalize_url_in_snapshot  # noqa: E402
from tools.job_materials.company_research import (  # noqa: E402
    load_company_research,
    save_company_research,
    write_company_research_request,
)
from tools.job_materials.jd_store import (  # noqa: E402
    extract_url_from_snapshot,
    jd_meta,
    package_id_from_path,
    read_jd,
    write_jd,
)
from tools.job_materials.build_jobs_json import build_jobs_json  # noqa: E402
from tools.job_materials.llmo import audit_plain_text  # noqa: E402
from tools.job_materials.paths import LANES, jobsearch_root  # noqa: E402
from tools.job_materials.packages import resolve_package  # noqa: E402
from tools.job_materials.manifest import (  # noqa: E402
    build_job_manifest,
    load_job_manifest,
    refresh_job_manifest,
    role_filename_component,
    update_manifest_from_payload,
    write_job_manifest,
)
from tools.job_materials.role_titles import build_role_title_contract  # noqa: E402
from tools.job_materials.publisher import snapshot_context  # noqa: E402
from tools.workflow.materials_state import compute_apply_ready  # noqa: E402
from tools.job_materials.tailor import (  # noqa: E402
    build_tailored_payload,
    package_quality_exit_code,
    write_base_master_ref,
    write_materials_status,
    write_tailor_outputs,
)
from tools.job_materials.url_normalize import normalize_job_url  # noqa: E402
from tools.job_materials.resume_parse import (  # noqa: E402
    load_resume_meta,
    load_resume_text,
    save_parsed_resume,
)
from tools.job_materials.requirements_engine import (  # noqa: E402
    build_application_preflight,
    load_preflight_answers,
    save_preflight_answer,
    write_application_preflight,
)
from tools.fresh_24h.careerops_quickscore import load_scoring_profile  # noqa: E402
from tools.fresh_24h.job_assessment import (  # noqa: E402
    assessment_context,
    load_job_assessment,
)
from tools.audit_log import append_audit_event  # noqa: E402
from tools.core_applications.validate_package import validate_package  # noqa: E402
from tools.io_utils import atomic_write_json, atomic_write_text  # noqa: E402


def _pkg(path: str | None, *, job_id: str | None = None) -> Path | None:
    """Resolve --package path or create a package from a local tracker row."""
    if path:
        p = Path(path).expanduser()
        if not p.is_absolute():
            p = (Path.cwd() / p).resolve()
        return p
    if job_id:
        masters_root = (jobsearch_root() / "01_Masters").resolve()
        try:
            package = resolve_package(jobsearch_root(), job_id)
        except LookupError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            print(
                "Run /push --local-only (or --also-local) and append the selected "
                "row to a local tracker before /materials.",
                file=sys.stderr,
            )
            return None
        try:
            package.resolve().relative_to(masters_root)
        except ValueError:
            print(
                f"ERROR: package resolution escaped 01_Masters: {package}",
                file=sys.stderr,
            )
            return None
        return package.resolve()
    return None


def _parse_title_company(package: Path) -> tuple[str, str]:
    snap = package / "job_snapshot.md"
    title, company = package.name, ""
    if snap.exists():
        text = snap.read_text(encoding="utf-8", errors="replace")
        m = re.search(r"Role:\s*(.+)", text)
        if m:
            title = m.group(1).strip()
        m = re.search(r"Company:\s*(.+)", text)
        if m:
            company = m.group(1).strip()
        # fallback header: # C0-005 — Compliance … @ Gate
        m = re.search(r"^#\s+.+?—\s+(.+?)\s+@\s+(.+)$", text, re.M)
        if m:
            title = title if title != package.name else m.group(1).strip()
            company = company or m.group(2).strip()
    return title, company


def _ensure_material_manifest(
    *,
    root: Path,
    package: Path,
    title: str,
    company: str,
    lane: str,
    jd: str,
    profile: dict[str, Any],
    snapshot: dict[str, str],
) -> dict[str, Any]:
    """Refresh the generated manifest while keeping user-owned overrides."""
    previous = load_job_manifest(package)
    previous_job = previous.get("job") if isinstance(previous.get("job"), dict) else {}
    def _meaningful(*values: Any) -> str:
        for value in values:
            text = str(value or "").strip()
            if text and text.casefold() not in {"—", "–", "-", "unknown", "n/a", "na"}:
                return text
        return ""

    publisher_name = (
        _meaningful(
            snapshot.get("publisher_name"),
            snapshot.get("publisher"),
            previous_job.get("publisher_name"),
            company,
        )
    )
    publisher_type = (
        _meaningful(snapshot.get("publisher_type"), previous_job.get("publisher_type"), "unknown")
    )
    employer_name = _meaningful(
        snapshot.get("employer_name"),
        snapshot.get("employer"),
        previous_job.get("employer_name"),
    )
    row = {
        "岗位编号": package_id_from_path(package),
        "职位": title,
        "公司": publisher_name,
        "发布者": publisher_name,
        "发布者类型": publisher_type,
        "用人公司": employer_name,
        "简历版本": lane,
        "层级": (previous.get("tier") or {}).get("label") or "",
        "链接": extract_url_from_snapshot(package),
        "来源": snapshot.get("source") or "unknown",
        "材料语言": snapshot.get("material_language") or "en",
    }
    if previous:
        return refresh_job_manifest(
            root=root,
            package=package,
            row=row,
            tracker_path=Path(previous.get("provenance", {}).get("tracker_path") or "")
            if previous.get("provenance", {}).get("tracker_path")
            else None,
            jd_text=jd,
            profile=profile,
        )
    manifest = build_job_manifest(
        root=root,
        package=package,
        row=row,
        jd_text=jd,
        profile=profile,
    )
    write_job_manifest(package, manifest)
    return manifest


def _known_application_answers(package: Path) -> dict[str, str]:
    known = {}
    config_paths = [
        jobsearch_root() / "00_Profile" / "config.personal.json",
        REPO / "config.personal.json",
    ]
    for config_path in config_paths:
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        for key in (
            "current_salary",
            "expected_salary",
            "notice_period",
            "availability",
            "work_authorization",
            "language",
            "license",
            "experience_years",
        ):
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


def _print_pdf_next_steps(package: Path) -> None:
    print("")
    print("== next (PDF export — after you edit DOCX from master) ==")
    print(f"  python3 tools/fresh_24h/docx_to_pdf.py '{package}/<CV>.docx' --engine libreoffice")
    print(f"  python3 tools/fresh_24h/docx_to_pdf.py '{package}/<Cover Letter>.docx' --engine libreoffice")
    print("  Handbook: JobSearch_2026/03_Applications/二级及部分一级岗位定制材料技术手册_2026-07-28.md")
    print("  Rules:    JobSearch_2026/03_Applications/系统规则_PDF与检索_强制遵守.md")


def cmd_base(args: argparse.Namespace) -> int:
    root = jobsearch_root()
    if args.action == "list":
        for b in list_bases(root):
            fc = (b.get("factcheck") or {}).get("status") or "?"
            print(f"  {b.get('base_id')}  factcheck={fc:8}  {b.get('label')}  emp={','.join(b.get('emphasis') or [])}")
        return 0

    if args.action == "sync":
        lanes = [args.lane.upper()] if args.lane else list(LANES.keys())
        for lane in lanes:
            base = sync_base_from_masters(root, lane)
            base = factcheck_base(root, base)
            p = save_base(root, base)
            print(f"{lane}: factcheck={base['factcheck']['status']} bullets={len(base.get('bullets') or [])} → {p}")
            if base["factcheck"]["status"] != "passed":
                for c in base["factcheck"].get("claims") or []:
                    if not c.get("supported"):
                        print(f"  ✗ {str(c.get('text'))[:100]}")
        return 0

    if args.action == "factcheck":
        if not args.lane:
            print("need --lane A-F", file=sys.stderr)
            return 2
        base = load_base(root, args.lane) or sync_base_from_masters(root, args.lane)
        base = factcheck_base(root, base)
        p = save_base(root, base)
        print(f"factcheck={base['factcheck']['status']} → {p}")
        for c in base["factcheck"].get("claims") or []:
            mark = "✓" if c.get("supported") else "✗"
            print(f"  {mark} [{c.get('kind')}] {str(c.get('text'))[:100]}")
        return 0 if base["factcheck"]["status"] == "passed" else 1

    if args.action == "show":
        if not args.lane:
            print("need --lane", file=sys.stderr)
            return 2
        base = load_base(root, args.lane)
        if not base:
            print("missing — run base sync", file=sys.stderr)
            return 1
        print(json.dumps(base, ensure_ascii=False, indent=2))
        return 0
    return 0


def cmd_url(args: argparse.Namespace) -> int:
    if args.action == "normalize":
        print(normalize_job_url(args.url or "", source=args.source or ""))
        return 0
    return 0


def cmd_jd(args: argparse.Namespace) -> int:
    root = jobsearch_root()
    package = _pkg(args.package, job_id=getattr(args, "job_id", None))
    if package is None or not package.is_dir():
        print(f"not a package dir: {package}", file=sys.stderr)
        return 2

    if args.action == "set":
        if args.file:
            text = Path(args.file).expanduser().read_text(encoding="utf-8")
        else:
            print("Paste full JD, end with Ctrl-D:")
            text = sys.stdin.read()
        if len(text.strip()) < 40:
            print("JD too short", file=sys.stderr)
            return 1
        url = extract_url_from_snapshot(package)
        path = write_jd(root, package, text, url=url, source="user_paste")
        # also normalize URLs in snapshot
        for c in normalize_url_in_snapshot(package):
            print(f"  · {c}")
        print(f"Wrote {path} ({len(text.strip())} chars) id={package_id_from_path(package)}")
        return 0

    if args.action == "show":
        print(read_jd(package, root) or "(empty)")
        return 0
    return 0


def cmd_enrich(args: argparse.Namespace) -> int:
    root = jobsearch_root()
    package = _pkg(args.package)
    if package is None or not package.is_dir():
        print(f"not a package dir: {package}", file=sys.stderr)
        return 2
    notes = enrich_package(package, root)
    for n in notes:
        print(f"  · {n}")
    meta = jd_meta(package, root)
    print(f"jd depth={meta.get('depth')} chars={meta.get('chars')} source={meta.get('source')}")
    if meta.get("is_shallow"):
        print(
            "Note: JD still shallow/stub — for CT/JobsDB paste is required for materials quality.",
            file=sys.stderr,
        )
        return 2
    return 0


def cmd_tailor(args: argparse.Namespace) -> int:
    root = jobsearch_root()
    package = _pkg(args.package)
    if package is None or not package.is_dir():
        print(f"not a package dir: {package}", file=sys.stderr)
        return 2
    from tools.workflow.plan_gate import PlanGateError, packet_started, require_validated_plan

    if packet_started(package):
        try:
            require_validated_plan(package)
        except PlanGateError:
            print("validated materials plan missing — refuse drafting (MAT-001)", file=sys.stderr)
            return 2
    title, company = _parse_title_company(package)
    publisher_context = snapshot_context(package)
    lane = (args.lane or "").upper()
    if not lane:
        lane = pick_lane_from_text(title, read_jd(package, root)[:500])
        print(f"auto lane={lane}")

    base = load_base(root, lane)
    if not base:
        print(f"base {lane} missing — running sync+factcheck…")
        base = sync_base_from_masters(root, lane)
        base = factcheck_base(root, base)
        save_base(root, base)

    fc = (base.get("factcheck") or {}).get("status")
    if fc not in {"passed", "capability_profile"} and not args.allow_unchecked:
        print(
            f"BASE {lane} factcheck={fc}. Fix evidence or: base factcheck --lane {lane}\n"
            f"Or pass --allow-unchecked (not recommended).",
            file=sys.stderr,
        )
        return 1

    jd = read_jd(package, root)
    if len(jd) < 80 and not args.allow_shallow_jd:
        print(
            "JD too short. Run: enrich --package …  OR  jd set --package … --file jd.txt\n"
            "(Deep full JD reliable mainly for LinkedIn; CT/JobsDB → paste.)",
            file=sys.stderr,
        )
        return 2

    scoring_profile = load_scoring_profile(root)
    manifest = _ensure_material_manifest(
        root=root,
        package=package,
        title=title,
        company=company,
        lane=lane,
        jd=jd,
        profile=scoring_profile,
        snapshot=publisher_context,
    )
    manifest_job = manifest.get("job") if isinstance(manifest.get("job"), dict) else {}
    # Material-facing role names are normalized once in the manifest and reused
    # on every rerun: obvious metadata parentheses may be removed, while
    # substantive specialisms stay in their original parentheses. A slash-
    # separated title keeps its source/display form and uses one selected
    # primary role for outbound material.
    title = str(manifest_job.get("role_material") or title).strip()
    company = str(manifest_job.get("company_source") or company).strip()
    role_selection = manifest_job.get("role_selection") if isinstance(manifest_job.get("role_selection"), dict) else {}
    if role_selection.get("confirmation_needed"):
        alternatives = ", ".join(str(item) for item in manifest_job.get("role_alternates") or [])
        print(
            "Role confirmation recommended: using primary "
            f"{title!r}; alternatives={alternatives or '—'}. "
            "Use `python3 -m tools.job_materials role choose --package … --title …` "
            "to select another detected title before finalizing materials."
        )
    preflight = build_application_preflight(
        jd,
        known_answers=_known_application_answers(package),
        candidate_languages=scoring_profile.get("candidate_languages"),
        profile=scoring_profile,
    )
    write_application_preflight(package, preflight)
    research = load_company_research(
        package,
        root=root,
        company=company or "",
    )
    # Reuse the scan's structured strengths/gaps only when both the JD and the
    # confirmed scoring profile still match. A pasted/updated JD naturally
    # invalidates the record and forces this materials run to reassess it.
    assessment = load_job_assessment(
        root,
        url=extract_url_from_snapshot(package),
        title=title,
        company=company,
        source=str(
            publisher_context.get("source")
            or jd_meta(package, root).get("source")
            or ""
        ),
        jd_text=jd,
        profile=scoring_profile,
    )
    if assessment:
        print(
            "Reusing current private job assessment: "
            f"status={assessment.get('status')} revision={assessment.get('revision', 1)}"
        )
    else:
        print(
            "No current private job assessment found (missing or stale); "
            "tailor_plan will mark this explicitly and must not present a fresh JD read as stored scoring."
        )
    if not (research.get("quality") or {}).get("ready_for_tailoring"):
        request_path = write_company_research_request(
            package,
            company=research.get("employer_name") or company or "",
            role=title,
            jd_text=jd,
            publisher_name=research.get("publisher_name")
            or publisher_context.get("publisher_name")
            or company
            or "",
            source_url=research.get("source_url")
            or publisher_context.get("source_url")
            or "",
            publisher_type=research.get("publisher_type") or "unknown",
            employer_name=research.get("employer_name") or "",
        )
        print(f"Wrote {request_path} -> complete sourced company quick research")
    payload = build_tailored_payload(
        base=base,
        job_title=title,
        company=company or "Company",
        jd_text=jd,
        company_research=research,
        use_llm=bool(args.llm),
        publisher_context=publisher_context,
        job_assessment=assessment,
        manifest=manifest,
    )
    payload["application_preflight"] = {
        "ready_for_apply": preflight["ready_for_apply"],
        "next_action": preflight["next_action"],
        "questions": preflight.get("questions") or [],
        "review_items": preflight.get("review_items") or [],
        "warnings": preflight.get("warnings") or [],
        "question_ids": [item["id"] for item in preflight["questions"]],
        "review_ids": [item["id"] for item in preflight["review_items"]],
        "warning_ids": [item["id"] for item in preflight.get("warnings") or []],
    }
    write_tailor_outputs(package, payload)
    manifest = update_manifest_from_payload(package, payload)
    if manifest:
        print(
            f"Updated {package / 'job_manifest.json'} "
            f"(artifacts={','.join(sorted(manifest.get('artifacts') or {}))})"
        )
    ref = write_base_master_ref(package, lane, root)
    if ref:
        print(f"Wrote {ref}")
    cov = payload.get("jd_coverage") or {}
    print(f"base={payload.get('base_id')} factcheck={fc} mode={payload.get('mode')}")
    print(f"coverage hit_rate={cov.get('hit_rate')} hits={cov.get('hits')[:6]}")
    print(f"Wrote {package / 'tailor_plan.md'}")
    print(f"Wrote {package / 'tailor_plan.json'}")
    print(
        "Tailor = emphasis reorder from independently fact-checked evidence (no source reopen; no freestyle invent)."
    )
    print("Next: apply summary/bullets into CV/CL DOCX per 二级手册, then PDF export.")
    _print_pdf_next_steps(package)

    # When plan exists, surface quality issues for agents (unless pure tailor strict path already returned)
    code = package_quality_exit_code(payload, package, root)
    quality_gate = payload.get("quality_gate") or {}
    if (
        quality_gate
        and not quality_gate.get("ready_for_drafting", True)
        and not quality_gate.get("ready_for_generic_drafting", False)
    ):
        code = code or 4
    elif quality_gate and quality_gate.get("ready_for_generic_drafting"):
        print("Company-specific sources are incomplete; safe JD-only/generic fallback remains available.")
    if code and args.allow_shallow_jd:
        # still wrote plan; non-zero so agents notice
        meta = jd_meta(package, root)
        print(
            f"WARN exit={code}: factcheck={fc} jd_depth={meta.get('depth')} "
            f"(tailor_plan written; fix blockers before sending materials)",
            file=sys.stderr,
        )
    return code if args.allow_shallow_jd or args.allow_unchecked or code == 4 else 0


def cmd_preflight(args: argparse.Namespace) -> int:
    root = jobsearch_root()
    package = _pkg(args.package, job_id=getattr(args, "job_id", None))
    if package is None or not package.is_dir():
        print(f"not a package dir: {package}", file=sys.stderr)
        return 2
    if args.action == "answer":
        if not args.field or not args.value:
            print("preflight answer needs --field and --value", file=sys.stderr)
            return 2
        path = save_preflight_answer(package, args.field, args.value)
        print(f"Wrote {path}")
    jd = read_jd(package, root)
    scoring_profile = load_scoring_profile(root)
    value = build_application_preflight(
        jd,
        known_answers=_known_application_answers(package),
        candidate_languages=scoring_profile.get("candidate_languages"),
        profile=scoring_profile,
    )
    write_application_preflight(package, value)
    print(json.dumps(value, ensure_ascii=False, indent=2))
    return 0 if value["ready_for_apply"] else 4


def cmd_assessment(args: argparse.Namespace) -> int:
    """Read the current private assessment for downstream workflows.

    This is intentionally deterministic and model-independent.  Interview and
    other consumers can call it instead of re-scoring the JD from memory.
    """
    root = jobsearch_root()
    package = _pkg(args.package, job_id=getattr(args, "job_id", None))
    if package is None or not package.is_dir():
        print(f"not a package dir: {package}", file=sys.stderr)
        return 2
    title, company = _parse_title_company(package)
    snapshot = snapshot_context(package)
    jd = read_jd(package, root)
    profile = load_scoring_profile(root)
    record = load_job_assessment(
        root,
        url=extract_url_from_snapshot(package) or snapshot.get("source_url", ""),
        title=title,
        company=company,
        source=str(snapshot.get("source") or ""),
        jd_text=jd,
        profile=profile,
    )
    context = assessment_context(record)
    context["consumer"] = "interview_or_materials"
    context["job"] = {"title": title, "company": company}
    if record is not None:
        context["record"] = record
    print(json.dumps(context, ensure_ascii=False, indent=2))
    return 0 if record is not None else 3


def cmd_company(args: argparse.Namespace) -> int:
    package = _pkg(args.package, job_id=getattr(args, "job_id", None))
    if package is None or not package.is_dir():
        print(f"not a package dir: {package}", file=sys.stderr)
        return 2
    if args.action == "show":
        _, company = _parse_title_company(package)
        print(
            json.dumps(
                load_company_research(
                    package,
                    root=jobsearch_root(),
                    company=company,
                ),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if not args.file:
        print("need --file company_research.json", file=sys.stderr)
        return 2
    try:
        value = json.loads(Path(args.file).expanduser().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"invalid company research JSON: {exc}", file=sys.stderr)
        return 2
    saved = save_company_research(package, value, root=jobsearch_root())
    print(
        f"Wrote {package / 'company_research.json'} "
        f"({len(saved.get('verified_signals') or [])} sourced signals)"
    )
    print(f"Wrote {package / 'company_research.md'}")
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    """PDF → plain text (apply-bot style graft)."""
    root = jobsearch_root()
    if args.action == "parse":
        if not args.pdf:
            print("need --pdf path/to/CV.pdf", file=sys.stderr)
            return 2
        meta = save_parsed_resume(Path(args.pdf), root=root, also_copy_bullets=True)
        print(
            f"OK source={meta.get('sourceName')} chars={meta.get('textLength')} "
            f"→ {root / '00_Profile' / 'resume_runtime' / 'resume.txt'}"
        )
        return 0
    if args.action == "show":
        meta = load_resume_meta(root)
        if not meta:
            print("(no parsed resume — run: resume parse --pdf …)")
            return 1
        print(json.dumps(meta, ensure_ascii=False, indent=2))
        text = load_resume_text(root)
        print("--- text preview ---")
        print(text[:1500] + ("…" if len(text) > 1500 else ""))
        return 0
    return 0


def cmd_llmo(args: argparse.Namespace) -> int:
    """Audit extracted material text without pretending to calculate an ATS score."""
    if args.action != "audit":
        return 0
    try:
        text = Path(args.file).expanduser().read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"cannot read material text: {exc}", file=sys.stderr)
        return 2
    contacts = [item.strip() for item in (args.contact or []) if item.strip()]
    result = audit_plain_text(text, kind=args.kind, expected_contact_tokens=contacts)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not result.get("human_review_required") else 4


def cmd_role(args: argparse.Namespace) -> int:
    """Show or confirm the primary title used by one material package."""
    package = _pkg(args.package, job_id=getattr(args, "job_id", None))
    if package is None or not package.is_dir():
        print(f"not a package dir: {package}", file=sys.stderr)
        return 2
    manifest = load_job_manifest(package)
    if not manifest:
        print("job_manifest.json missing — run /materials or package creation first", file=sys.stderr)
        return 2
    job = manifest.get("job") if isinstance(manifest.get("job"), dict) else {}
    display = str(job.get("role_display") or job.get("role_material") or "").strip()
    contract = job.get("role_title_contract")
    if not isinstance(contract, dict):
        contract = build_role_title_contract(
            display,
            selected_primary=str((manifest.get("overrides") or {}).get("role_primary") or ""),
        )
    if args.action == "show":
        print(json.dumps(contract, ensure_ascii=False, indent=2))
        return 0
    requested = str(args.title or "").strip()
    if not requested:
        print("role choose requires --title", file=sys.stderr)
        return 2
    selected = build_role_title_contract(display, selected_primary=requested)
    if selected.get("selection_mode") != "user_override":
        choices = [str(item.get("material") or item.get("display") or "") for item in selected.get("variants") or []]
        print(
            "title is not one of the detected role variants; choose one of: "
            + ", ".join(choice for choice in choices if choice),
            file=sys.stderr,
        )
        return 2
    overrides = manifest.get("overrides") if isinstance(manifest.get("overrides"), dict) else {}
    overrides = dict(overrides)
    overrides["role_primary"] = selected.get("primary")
    manifest["overrides"] = overrides
    # Rebuild the generated role fields while retaining the existing JD/profile
    # fingerprints and user-owned wording.  The manifest is the sole writer for
    # this selection, so later tailor runs use exactly the confirmed primary.
    refreshed = build_role_title_contract(display, selected_primary=str(selected.get("primary") or ""))
    job = dict(job)
    job.update(
        {
            "role_title_contract": refreshed,
            "role_material": refreshed.get("primary"),
            "role_primary": refreshed.get("primary"),
            "role_alternates": list(refreshed.get("alternates") or []),
            "role_specialisms": list(refreshed.get("specialisms") or []),
            "role_parentheticals": list(refreshed.get("primary_parentheticals") or []),
            "role_selection": {
                "mode": "user_override",
                "confirmation_needed": bool(refreshed.get("confirmation_needed")),
                "policy": refreshed.get("policy"),
            },
        }
    )
    manifest["job"] = job
    generated = manifest.get("generated") if isinstance(manifest.get("generated"), dict) else {}
    generated = dict(generated)
    generated["role_fn"] = role_filename_component(str(refreshed.get("primary") or "Application"))
    manifest["generated"] = generated
    artifacts = manifest.get("artifacts") if isinstance(manifest.get("artifacts"), dict) else {}
    for name in ("resume", "cover_letter", "application_email", "validation"):
        item = artifacts.get(name)
        if isinstance(item, dict) and item.get("status") not in {None, "", "not_generated"}:
            item.update(
                {
                    "status": "stale",
                    "stale_reason": "role_selection_changed",
                    "changed_inputs": ["job_context_sha256"],
                }
            )
    manifest["artifacts"] = artifacts
    write_job_manifest(package, manifest)
    print(f"Confirmed primary role: {refreshed.get('primary')}")
    if refreshed.get("alternates"):
        print(f"Alternatives retained for traceability: {', '.join(refreshed['alternates'])}")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    """Run the manifest-aware release gate for one selected package."""
    package = _pkg(args.package, job_id=getattr(args, "job_id", None))
    if package is None or not package.is_dir():
        print(f"not a package dir: {package}", file=sys.stderr)
        return 2
    manifest = load_job_manifest(package)
    if not manifest:
        print(
            "job_manifest.json missing — run package creation or tailor first",
            file=sys.stderr,
        )
        return 2
    job = manifest.get("job") if isinstance(manifest.get("job"), dict) else {}
    role = str(job.get("role_material") or job.get("role_display") or "").strip()
    company = str(job.get("company_out") or "").strip()
    errors = validate_package(
        package,
        company,
        role,
        job_manifest=manifest,
    )
    report = {
        "schema_version": 1,
        "job_id": manifest.get("job_id"),
        "package": str(package.resolve()),
        "status": "passed" if not errors else "failed",
        "errors": errors,
        "apply_ready": compute_apply_ready(
            p0_count=len(errors),
            p1_count=0,
            files_ok=not errors,
        ),
        "contract": {
            "role": role,
            "verified_employer": company,
            "publisher_type": job.get("publisher_type") or "unknown",
        },
    }
    manifest.setdefault("artifacts", {})["validation"] = {
        "status": report["status"],
        "error_count": len(errors),
        "checked_manifest_schema": manifest.get("schema_version"),
    }
    write_job_manifest(package, manifest)
    atomic_write_json(package / "materials_validation.json", report)
    lines = [
        "# Materials validation",
        "",
        f"- status: **{report['status']}**",
        f"- job_id: `{manifest.get('job_id')}`",
        f"- role: `{role}`",
        f"- verified employer: `{company or 'not named'}`",
        "",
        "## Findings",
    ]
    if errors:
        lines.extend(f"- {error}" for error in errors)
    else:
        lines.append("- No contract violations found.")
    atomic_write_text(package / "materials_validation.md", "\n".join(lines) + "\n")
    print(f"{report['status'].upper()} {manifest.get('job_id')} → {package}")
    for error in errors:
        print(f"  - {error}")
    return 0 if not errors else 1


def cmd_build_jobs(args: argparse.Namespace) -> int:
    """Generate the private tracker-backed batch manifest."""
    if not args.all_jobs and not args.job_id:
        print("build-jobs requires --job-id (repeatable) or --all", file=sys.stderr)
        return 2
    result = build_jobs_json(
        args.root,
        job_ids=None if args.all_jobs else args.job_id,
        output=args.output,
        create_packages=not args.no_create_packages,
    )
    output = args.output or (args.root / "02_Tracker" / "jobs.generated.json")
    print(f"Wrote {len(result.get('jobs') or [])} job manifest(s)")
    print(f"Output: {output}")
    return 0


def cmd_pipeline(args: argparse.Namespace) -> int:
    """
    Package step: enrich → tailor → materials_status + master ref.
    Does NOT invent facts. Exit non-zero if base factcheck failed or JD stub/shallow
    (plan is still written so humans can see blockers).
    """
    root = jobsearch_root()
    package = _pkg(args.package, job_id=args.job_id)
    if package is None or not package.is_dir():
        print(f"not a package dir: {package}", file=sys.stderr)
        return 2
    args.package = str(package)

    print("== normalize + enrich ==")
    print("  (LinkedIn deep OK; CT/JobsDB → URL fix / structured only — paste for full body)")
    enrich_notes = enrich_package(package, root)
    for n in enrich_notes:
        print(f"  · {n}")
    meta = jd_meta(package, root)
    print(f"  jd depth={meta.get('depth')} chars={meta.get('chars')} source={meta.get('source')}")

    # Always produce plan for agent visibility; quality reflected in exit code
    args.allow_shallow_jd = True
    print("== tailor (emphasis from independently fact-checked A–F evidence) ==")
    title, company = _parse_title_company(package)
    lane = (args.lane or "").upper()
    if not lane:
        lane = pick_lane_from_text(title, read_jd(package, root)[:500])
        print(f"auto lane={lane}")
        args.lane = lane

    # Run tailor body (may return early if factcheck hard-fail without allow_unchecked)
    rc_tailor = cmd_tailor(args)

    # materials_status after plan exists (if tailor wrote it)
    plan = package / "tailor_plan.json"
    if plan.exists():
        try:
            payload = json.loads(plan.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            payload = {
                "role": title,
                "company": company,
                "base_id": lane,
                "base_factcheck": "?",
                "mode": "unknown",
                "jd_shallow": True,
                "jd_coverage": {},
            }
        write_base_master_ref(package, lane or str(payload.get("base_id") or "F"), root)
        status = write_materials_status(
            package,
            root=root,
            payload=payload,
            lane=lane or str(payload.get("base_id") or "F"),
            enrich_notes=enrich_notes,
        )
        print(f"Wrote {status}")
        append_audit_event(
            root,
            "materials_pipeline",
            {
                "job_id": package_id_from_path(package),
                "base_id": payload.get("base_id"),
                "company_research_sources": len(
                    (payload.get("company_profile") or {}).get("verified_signals") or []
                ),
                "differentiation_fingerprint": payload.get(
                    "differentiation_fingerprint"
                ),
            },
        )
        code = package_quality_exit_code(payload, package, root)
        quality_gate = payload.get("quality_gate") or {}
        if (
            quality_gate
            and not quality_gate.get("ready_for_drafting", True)
            and not quality_gate.get("ready_for_generic_drafting", False)
        ):
            code = code or 4
        preflight = payload.get("application_preflight") or {}
        if not preflight.get("ready_for_apply", True):
            code = code or 4
        # Prefer quality code over tailor early-exit if plan exists
        if code:
            print(
                f"PIPELINE WARN exit={code}: check materials_status.md "
                f"(factcheck and/or JD depth). Plan written for review.",
                file=sys.stderr,
            )
            return code
        print("pipeline ok — review tailor_plan.md + materials_status.md")
        return 0

    # no plan (e.g. hard factcheck fail without --allow-unchecked)
    print("pipeline incomplete — no tailor_plan.json", file=sys.stderr)
    return int(rc_tailor or 1)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="job_materials",
        description=(
            "JobSearch_2026 materials (on-demand only). "
            "Separate from scan two-pass. "
            "JD body: LinkedIn CLI + Playwright browser (JobsDB/CT); paste fallback. "
            "tailor = emphasis from independently fact-checked A–F evidence (no source reopen)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Notes:\n"
            "  • Never auto-runs on /scan / push_to_gsheet / temp_two_pass.\n"
            "  • pipeline writes tailor_plan + materials_status + base_master_ref;\n"
            "    exit ≠ 0 if base factcheck failed or JD stub/shallow.\n"
            "  • PDF export is manual: docx_to_pdf (LibreOffice headless).\n"
            "  • resume parse grafts apply-bot PDF→text into 00_Profile/resume_runtime/.\n"
        ),
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    pr = sub.add_parser("resume", help="Parse CV PDF → plain text (apply-bot style)")
    pr_sub = pr.add_subparsers(dest="action", required=True)
    pr_parse = pr_sub.add_parser("parse", help="Parse a PDF resume")
    pr_parse.add_argument("--pdf", required=True, help="Path to CV PDF")
    pr_parse.set_defaults(func=cmd_resume)
    pr_show = pr_sub.add_parser("show", help="Show parsed resume meta + preview")
    pr_show.set_defaults(func=cmd_resume)

    p = sub.add_parser(
        "llmo",
        help="Audit extracted CV/cover-letter text using the deterministic LLMO contract",
    )
    p_sub = p.add_subparsers(dest="action", required=True)
    p_audit = p_sub.add_parser("audit", help="Audit plain text extracted from a material")
    p_audit.add_argument("--file", required=True, help="UTF-8 plain-text extraction")
    p_audit.add_argument("--kind", choices=["cv", "cover_letter", "application_email"], default="cv")
    p_audit.add_argument("--contact", action="append", default=[], help="Expected contact token; repeatable")
    p_audit.set_defaults(func=cmd_llmo)

    p = sub.add_parser(
        "role",
        help="Inspect or confirm the one primary role selected from a slash-separated title",
    )
    p.add_argument("action", choices=["show", "choose"])
    p.add_argument("--package", default=None, help="Package path (or use --job-id)")
    p.add_argument("--job-id", default=None)
    p.add_argument("--title", default="", help="One detected role variant for `role choose`")
    p.set_defaults(func=cmd_role)

    p = sub.add_parser(
        "validate",
        help="Validate one package against its generated job_manifest.json",
    )
    p.add_argument("--package", default=None, help="Package path (or use --job-id)")
    p.add_argument("--job-id", default=None, help="Job ID resolved from the local tracker")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser(
        "build-jobs",
        help="Generate private tracker-backed job manifests for a materials batch",
    )
    p.add_argument("--root", type=Path, default=jobsearch_root())
    p.add_argument("--job-id", action="append", default=[])
    p.add_argument("--all", action="store_true", dest="all_jobs")
    p.add_argument("--output", type=Path, default=None)
    p.add_argument("--no-create-packages", action="store_true")
    p.set_defaults(func=cmd_build_jobs)

    p = sub.add_parser("base", help="A–F bases sync/factcheck (required before trustworthy tailor)")
    p.add_argument("action", choices=["list", "sync", "factcheck", "show"])
    p.add_argument("--lane", default="", help="A-F")
    p.set_defaults(func=cmd_base)

    p = sub.add_parser("url", help="URL helpers (CT / JobsDB / LinkedIn canonicalize)")
    p.add_argument("action", choices=["normalize"])
    p.add_argument("--url", default="")
    p.add_argument("--source", default="")
    p.set_defaults(func=cmd_url)

    p = sub.add_parser(
        "jd",
        help="Full JD paste/store (required for CT/JobsDB; LinkedIn often via enrich)",
    )
    p.add_argument("action", choices=["set", "show"])
    p.add_argument("--package", default=None, help="Path to package folder (or use --job-id)")
    p.add_argument("--job-id", default=None, help="Job ID resolved from the local tracker")
    p.add_argument("--file", default="")
    p.set_defaults(func=cmd_jd)

    p = sub.add_parser(
        "enrich",
        help="Normalize URLs + LinkedIn deep JD; CT/JobsDB = URL fix / structured only",
    )
    p.add_argument("--package", default=None, help="Package path (or use --job-id)")
    p.set_defaults(func=cmd_enrich)

    p = sub.add_parser(
        "company",
        help="Store/show source-aware company research used by CV/cover-letter tailoring",
    )
    p.add_argument("action", choices=["set", "show"])
    p.add_argument("--package", default=None, help="Package path (or use --job-id)")
    p.add_argument("--job-id", default=None)
    p.add_argument("--file", default="", help="Research JSON for `company set`")
    p.set_defaults(func=cmd_company)

    p = sub.add_parser(
        "preflight",
        help="Deterministically surface JD questions and hard requirements",
    )
    p.add_argument("action", choices=["show", "refresh", "answer"])
    p.add_argument("--package", default=None, help="Package path (or use --job-id)")
    p.add_argument("--job-id", default=None)
    p.add_argument("--field", default="")
    p.add_argument("--value", default="")
    p.set_defaults(func=cmd_preflight)

    p = sub.add_parser(
        "assessment",
        help="Read the current private per-job assessment for materials/interview",
    )
    p.add_argument("action", choices=["show"])
    p.add_argument("--package", default=None, help="Package path (or use --job-id)")
    p.add_argument("--job-id", default=None)
    p.set_defaults(func=cmd_assessment)

    p = sub.add_parser(
        "tailor",
        help="Reorder emphasis from independently fact-checked A–F evidence toward JD (no freestyle claims)",
    )
    p.add_argument("--package", required=True)
    p.add_argument("--lane", default="", help="A-F (auto if empty)")
    p.add_argument("--llm", action="store_true", help="Optional rephrase of base lines only")
    p.add_argument("--allow-unchecked", action="store_true", help="Allow non-passed base (not recommended)")
    p.add_argument(
        "--allow-shallow-jd",
        action="store_true",
        help="Write plan even if JD short; exit non-zero so agents notice",
    )
    p.set_defaults(func=cmd_tailor)

    p = sub.add_parser(
        "pipeline",
        help=(
            "On-demand package step: enrich → tailor → materials_status "
            "(not part of scan; exit ≠0 if factcheck/JD weak)"
        ),
    )
    p.add_argument("--package", default=None, help="Package path (or use --job-id)")
    p.add_argument("--job-id", default=None, help="Job ID like C0-005 (resolves to package path)")
    p.add_argument("--lane", default="")
    p.add_argument("--llm", action="store_true")
    p.add_argument("--allow-unchecked", action="store_true")
    p.set_defaults(func=cmd_pipeline)

    args = ap.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
