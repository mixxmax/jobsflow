#!/usr/bin/env python3
"""JobsFlow setup wizard.

Guides a new user through setup:
  1. Check prerequisites (Python, Bun, LibreOffice, Playwright)
  2. Create JobSearch_2026/ directory structure
  3. Ask: local CSV or Google Sheets tracking?
  4. Read resume folder -> extract name/phone/email/education/skills
  5. Ask: job search intent
  6. Generate personal config under the gitignored workspace
  7. Optionally install portal CLI tools

Usage:
  python3 setup.py
  python3 setup.py --resume-folder ~/Documents/my-cv
  python3 setup.py --install-portals
"""

from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from tools.io_utils import atomic_write_json
from tools.workflow.runtime_instructions import ensure_runtime_instruction_delegates
from tools.language_gate import parse_candidate_languages
from tools.salary_parsing import AMBIGUOUS, INVALID, PARSED, parse_salary_range
from tools.fresh_24h.policy import (
    normalize_retention_preference,
    normalize_scan_depth,
)

REPO = Path(__file__).resolve().parent

JOBSEARCH_DIRS = [
    "00_Profile",
    "00_Profile/bases_runtime",
    "01_Masters",
    "02_Tracker",
    "02_Tracker/jds",
    "02_Tracker/jds/cache",
    "03_Applications",
    "03_Applications/_TEMPLATE",
    "04_Outbox_Batch",
    "05_Archive",
]

PORTAL_SKILLS = [
    "linkedin-search",
    "jobsdb-search",
    "ctgoodjobs-search",
    "freehire-search",
]

PERSONAL_QUERY_BUCKETS = [
    "core_target_roles",
    "adjacent_target_roles",
    "exploration_roles",
]

# The semantic matcher may reason about transferable capability, but the user
# decides how far the private profile may extend beyond directly evidenced
# experience.  These are product-level policies, not an industry preset.
SEMANTIC_PROFILE_LEVELS: dict[str, dict[str, Any]] = {
    "low": {
        "label": "低（保守）",
        "transfer_scope": "仅直接事实或非常接近的可迁移能力",
        "transfer_score_cap": 4.0,
        "upper_only_score_cap": 3.5,
    },
    "medium": {
        "label": "中（平衡）",
        "transfer_scope": "允许相邻职责和明确可迁移能力，但不等同于已有实操经历",
        "transfer_score_cap": 4.5,
        "upper_only_score_cap": 4.0,
    },
    "high": {
        "label": "高（扩展）",
        "transfer_scope": "允许较宽的能力迁移和潜在适配判断，但仍禁止虚构经历",
        "transfer_score_cap": 5.0,
        "upper_only_score_cap": 4.5,
    },
}


def normalize_semantic_profile_level(value: str | None) -> str:
    """Normalize user-facing calibration choices to low/medium/high."""
    raw = str(value or "").strip().casefold()
    aliases = {
        "1": "low",
        "低": "low",
        "保守": "low",
        "谨慎": "low",
        "conservative": "low",
        "low": "low",
        "2": "medium",
        "中": "medium",
        "平衡": "medium",
        "适中": "medium",
        "medium": "medium",
        "3": "high",
        "高": "high",
        "扩展": "high",
        "开放": "high",
        "high": "high",
    }
    return aliases.get(raw, "medium")


def semantic_profile_for_level(value: str | None) -> dict[str, Any]:
    level = normalize_semantic_profile_level(value)
    return {
        "schema_version": 1,
        "upper_bound_level": level,
        **SEMANTIC_PROFILE_LEVELS[level],
        "direct_facts_score_cap": 5.0,
        "forbid_invented_experience": True,
    }

INITIAL_TRACKER_HEADERS = [
    "岗位编号", "本轮新增", "层级", "批次", "入表时间", "匹配分", "职位",
    "公司", "赛道", "来源", "地点", "薪资", "链接", "简述", "语言要求",
    "领域背景", "资格要求", "经验要求", "匹配要点", "主要缺口",
    "发布日期", "简历版本", "版本说明", "材料状态", "工作时间风险",
    "公司简介", "CareerOps分数", "CareerOps等级", "CareerOps理由", "置信度",
]


def personal_queries_path(repo: Path = REPO) -> Path:
    """Per-user search intent; never part of the product source tree."""
    return Path(repo) / "JobSearch_2026" / "00_Profile" / "queries.json"


def personal_config_path(repo: Path = REPO) -> Path:
    return Path(repo) / "JobSearch_2026" / "00_Profile" / "config.personal.json"


def search_queries_path(repo: Path = REPO) -> Path:
    """Prefer private setup output, with the tracked preset as a clean-clone fallback."""
    private = personal_queries_path(repo)
    if private.exists():
        return private
    return Path(repo) / "tools" / "fresh_24h" / "queries.json"


def info(msg: str) -> None:
    print(f"  {msg}")


def ok(msg: str) -> None:
    print(f"  ✓ {msg}")


def warn(msg: str) -> None:
    print(f"  ! {msg}")


def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    val = input(f"  {prompt}{suffix}: ").strip()
    return val or default


def ask_yes_no(prompt: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    val = input(f"  {prompt} ({hint}): ").strip().lower()
    if not val:
        return default
    return val in {"y", "yes", "是"}


def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ModuleNotFoundError, AttributeError):
        return False


# ── Prerequisites ─────────────────────────────────────────────────────

def check_prerequisites() -> dict[str, bool]:
    print("\n── Step 1: Prerequisites ──\n")
    results = {"python": sys.version_info >= (3, 10)}
    if results["python"]:
        ok(f"Python {sys.version_info.major}.{sys.version_info.minor}")
    else:
        warn("Python 3.10+ required")

    results["bun"] = bool(shutil.which("bun"))
    if results["bun"]:
        ok("Bun found")
    else:
        warn("Bun not found. Install: curl -fsSL https://bun.sh/install | bash")

    results["libreoffice"] = bool(
        shutil.which("soffice")
        or Path("/Applications/LibreOffice.app/Contents/MacOS/soffice").exists()
    )
    if results["libreoffice"]:
        ok("LibreOffice found (PDF export ready)")
    else:
        warn("LibreOffice not found. It runs headless (no windows). Install: brew install --cask libreoffice")

    results["playwright"] = False
    try:
        import playwright  # noqa: F401
        browsers = Path.home() / ".cache" / "ms-playwright"
        if browsers.exists() and any(browsers.iterdir()):
            results["playwright"] = True
            ok("Playwright found (JD deep fetch ready)")
        else:
            warn("Playwright installed but chromium not. Run: playwright install chromium")
    except ImportError:
        warn("Playwright not installed. Install: pip install playwright && playwright install chromium")

    return results


def run_doctor() -> int:
    """Read-only readiness check with concrete remediation commands."""
    print("JobsFlow doctor")
    checks = check_prerequisites()
    modules = {
        "docx": "python-docx",
        "fitz": "PyMuPDF",
        "pypdf": "pypdf",
        "openpyxl": "openpyxl",
        "gspread": "gspread",
        "google.oauth2": "google-auth",
    }
    for module, package in modules.items():
        ready = _module_available(module)
        checks[f"python:{package}"] = ready
        (ok if ready else warn)(f"{package}: {'ready' if ready else 'missing'}")
    for skill in PORTAL_SKILLS:
        cli_dir = REPO / ".agents" / "skills" / skill / "cli"
        ready = (cli_dir / "package.json").exists() and (cli_dir / "bun.lock").exists()
        checks[f"portal:{skill}"] = ready
        (ok if ready else warn)(
            f"{skill}: {'ready' if ready else 'missing package.json or tracked bun.lock'}"
        )
    tracker_ready = bool(
        list((REPO / "JobSearch_2026" / "02_Tracker").glob("hk_apply_list_*.csv"))
    )
    checks["tracker"] = tracker_ready
    (ok if tracker_ready else warn)(
        "tracker: ready" if tracker_ready else "tracker missing; run python3 setup.py"
    )
    try:
        from tools.workflow.base_onboarding import status as base_status

        base_snapshot = base_status(REPO / "JobSearch_2026")
        if base_snapshot.get("ready"):
            ok("materials base CV/CL: all configured lanes ready")
        else:
            warn("materials base CV/CL: pending; run python3 -m tools.workflow base status")
    except Exception as exc:
        warn(f"materials base CV/CL: unavailable ({exc})")
    failed = [name for name, ready in checks.items() if not ready]
    if failed:
        print("\nFix core Python packages with: python3 -m pip install -r requirements.lock")
        print("Fix portal packages with: python3 setup.py --install-portals")
        print(f"Doctor: {len(failed)} check(s) need attention")
        return 1
    print("\nDoctor: all checks ready")
    return 0


def doctor_snapshot() -> dict[str, Any]:
    """Machine-readable readiness contract for lower-capability models."""
    modules = {
        "docx": "python-docx",
        "fitz": "PyMuPDF",
        "pypdf": "pypdf",
        "openpyxl": "openpyxl",
        "gspread": "gspread",
        "google.oauth2": "google-auth",
        "playwright": "playwright",
    }
    checks = {
        "python_3_10_plus": sys.version_info >= (3, 10),
        "bun": bool(shutil.which("bun")),
        "libreoffice": bool(
            shutil.which("soffice")
            or Path("/Applications/LibreOffice.app/Contents/MacOS/soffice").exists()
        ),
    }
    checks.update(
        {
            f"python_package:{package}": _module_available(module)
            for module, package in modules.items()
        }
    )
    browser_roots = [
        Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")).expanduser()
        if os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
        else None,
        Path.home() / ".cache" / "ms-playwright",
        Path.home() / "Library" / "Caches" / "ms-playwright",
    ]
    checks["playwright_browser"] = bool(
        Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome").exists()
        or any(
            root is not None
            and root.exists()
            and any(root.iterdir())
            for root in browser_roots
        )
    )
    for skill in PORTAL_SKILLS:
        cli_dir = REPO / ".agents" / "skills" / skill / "cli"
        checks[f"portal:{skill}"] = (
            (cli_dir / "package.json").exists()
            and (cli_dir / "bun.lock").exists()
        )
    checks["tracker"] = bool(
        list((REPO / "JobSearch_2026" / "02_Tracker").glob("hk_apply_list_*.csv"))
    )
    try:
        from tools.workflow.base_onboarding import status as base_status

        base_snapshot = base_status(REPO / "JobSearch_2026")
        materials_base_ready = bool(base_snapshot.get("ready"))
    except Exception:
        base_snapshot = {"ready": False, "lanes": [], "next_action": "base_init_or_generate_and_confirm"}
        materials_base_ready = False
    # Keep the historical ``ready`` field focused on environment/setup so a
    # user can still scan before making every lane base.  Expose the stricter
    # materials gate separately; /materials itself remains fail-closed when a
    # selected lane has no activated master.
    failed = [name for name, ready in checks.items() if not ready]
    repair_commands = []
    for name in failed:
        if name == "python_3_10_plus":
            repair_commands.append("Install Python 3.10+ and recreate the virtual environment")
        elif name == "bun":
            repair_commands.append("Install Bun from https://bun.sh")
        elif name == "libreoffice":
            repair_commands.append("Install LibreOffice (headless soffice is required)")
        elif name == "playwright_browser":
            repair_commands.append("playwright install chromium")
        elif name.startswith("python_package:"):
            if "python3 -m pip install -r requirements.lock" not in repair_commands:
                repair_commands.append("python3 -m pip install -r requirements.lock")
        elif name.startswith("portal:"):
            if "python3 setup.py --install-portals" not in repair_commands:
                repair_commands.append("python3 setup.py --install-portals")
        elif name == "tracker":
            repair_commands.append("python3 setup.py --resume-folder /path/to/cv-folder")
    return {
        "schema_version": 1,
        "ready": not failed,
        "workflow_ready": not failed,
        "materials_ready": materials_base_ready,
        "materials_base": base_snapshot,
        "checks": checks,
        "failed": failed,
        "next_action": (
            "continue"
            if not failed and materials_base_ready
            else "prepare_base_masters"
            if not failed
            else "repair_failed_checks"
        ),
        "repair_commands": repair_commands,
    }


# ── Directories ────────────────────────────────────────────────────────

def create_directories() -> Path:
    print("\n── Step 2: Creating workspace ──\n")
    js_root = REPO / "JobSearch_2026"
    for d in JOBSEARCH_DIRS:
        (js_root / d).mkdir(parents=True, exist_ok=True)
    instruction_status = ensure_runtime_instruction_delegates(js_root)
    if instruction_status.get("status") != "ready":
        warn(
            "Runtime AGENTS.md/CLAUDE.md contains an unmanaged instruction override; "
            "resolve it before running workflow commands."
        )
    ok(f"Workspace at {js_root}")
    return js_root


def ensure_initial_tracker(
    js_root: Path,
    headers: list[str] | None = None,
) -> Path:
    """Create or safely update the empty tracker required by the first `/scan`."""
    desired_headers = headers or INITIAL_TRACKER_HEADERS
    tracker_dir = Path(js_root) / "02_Tracker"
    tracker_dir.mkdir(parents=True, exist_ok=True)
    existing = sorted(tracker_dir.glob("hk_apply_list_*.csv"), reverse=True)
    if existing:
        path = existing[0]
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.reader(handle))
        # A customized schema may arrive immediately after deterministic setup.
        # Only an empty tracker is safe to update without an explicit migration.
        if len(rows) <= 1 and rows[:1] != [desired_headers]:
            with path.open("w", encoding="utf-8-sig", newline="") as handle:
                csv.writer(handle).writerow(desired_headers)
        return path
    from datetime import date

    path = tracker_dir / f"hk_apply_list_{date.today().isoformat()}.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        csv.writer(handle).writerow(desired_headers)
    return path


# ── Tracking method ───────────────────────────────────────────────────

def ask_tracking() -> dict[str, Any]:
    print("\n── Step 3: Tracking ──\n")
    print("  1. Google Sheets (cloud, needs service account JSON)")
    print("  2. Local CSV only (zero config)")
    choice = ask("Choose 1 or 2", "2")
    if choice == "1":
        return {"method": "google_sheets"}
    return {"method": "local_csv"}


def ask_semantic_profile_level() -> str:
    """Ask how broad semantic capability transfer may be during matching."""
    print("\n── Step 5: Resume-match calibration ──\n")
    print("  低 / low    保守：主要接受直接事实和非常接近的能力迁移")
    print("  中 / medium 平衡：接受相邻职责，但不把潜力当作已有经历")
    print("  高 / high   扩展：允许较宽的能力迁移，仍禁止虚构经历")
    choice = ask("选择简历匹配画像上沿幅度（low/medium/high）", "medium")
    level = normalize_semantic_profile_level(choice)
    if str(choice or "").strip() and str(choice).strip().casefold() not in {
        "1", "低", "保守", "谨慎", "conservative", "low",
        "2", "中", "平衡", "适中", "medium",
        "3", "高", "扩展", "开放", "high",
    }:
        warn("未识别该选项，使用 medium（平衡）")
    print(f"  已选择：{SEMANTIC_PROFILE_LEVELS[level]['label']}")
    return level


def ask_workflow_preferences() -> dict[str, str]:
    """Ask separately about retrieval cost and final-list selectivity."""
    print("\n── Step 6: Scan and shortlist preferences ──\n")
    print("  扫描深度：节能（约 10 个网络深取）/ 平衡（约 20 个）/ 广覆盖（约 40 个）")
    scan_depth = normalize_scan_depth(
        ask("选择扫描深度（节能/平衡/广覆盖）", "平衡")
    )
    print("  保留偏好：宽松 3.0 / 标准 3.3 / 精选 3.5；只影响完整 JD 后的清单")
    retention = normalize_retention_preference(
        ask("选择最终清单偏好（宽松/标准/精选）", "标准")
    )
    return {
        "scan_depth": scan_depth,
        "retention_preference": retention,
    }


# ── Resume ────────────────────────────────────────────────────────────

def read_resume(folder: Path) -> str:
    print(f"\n── Step 4: Reading resume from {folder} ──\n")
    if not folder.exists():
        warn("Folder not found")
        return ""

    texts: list[str] = []
    for f in sorted(folder.rglob("*")):
        if not f.is_file():
            continue
        try:
            text = ""
            if f.suffix.lower() in {".txt", ".md", ".json"}:
                text = f.read_text("utf-8", errors="replace")[:10000]
            elif f.suffix.lower() == ".pdf":
                text = extract_pdf_text(f)
            elif f.suffix.lower() == ".docx":
                text = extract_docx_text(f)
            if text and text.strip():
                texts.append(f"### {f.name}\n\n{text[:5000]}")
                info(f"Read {f.name} ({len(text)} chars)")
        except OSError:
            pass

    if not texts:
        warn("No readable files found")
        return ""
    combined = "\n\n---\n\n".join(texts)
    ok(f"{len(texts)} files, {len(combined)} chars")
    return combined


def extract_pdf_text(path: Path) -> str:
    try:
        import fitz
        doc = fitz.open(str(path))
        text = "\n".join(page.get_text() for page in doc)
        doc.close()
        return text.strip()
    except Exception:
        pass
    if shutil.which("pdftotext"):
        try:
            r = subprocess.run(["pdftotext", str(path), "-"], capture_output=True, text=True, timeout=15)
            return r.stdout.strip()
        except Exception:
            pass
    return ""


def extract_docx_text(path: Path) -> str:
    try:
        from docx import Document
        return "\n".join(p.text for p in Document(str(path)).paragraphs if p.text.strip())
    except Exception:
        return ""


# ── Resume parsing (regex-based, no LLM needed) ──────────────────────

def extract_name(text: str) -> str:
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    for l in lines[:20]:
        if re.match(r"^[A-Z][a-z]+ [A-Z][a-z]+$", l) and len(l) < 40:
            return l
    # Chinese name pattern
    for l in lines[:20]:
        if re.match(r"^[\u4e00-\u9fff]{2,4}$", l):
            return l
    return ""


def extract_phone(text: str) -> str:
    m = re.search(r"(\+?\d{1,3}[-.\s]?)?\(?\d{3,4}\)?[-.\s]?\d{3,4}[-.\s]?\d{3,4}", text)
    return m.group(0) if m else ""


def extract_email(text: str) -> str:
    m = re.search(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", text)
    return m.group(0) if m else ""


def extract_education(text: str) -> str:
    keywords = ["bachelor", "master", "phd", "doctor", "jd", "llm", "mba", "本科", "硕士", "博士"]
    for line in text.split("\n"):
        ll = line.lower()
        if any(k in ll for k in keywords):
            return line.strip()[:120]
    return ""


def extract_languages(text: str) -> str:
    langs = []
    for word in ["mandarin", "cantonese", "english", "普通话", "粤语", "英语", "Chinese", "Japanese", "Korean", "French", "German"]:
        if word.lower() in text.lower():
            langs.append(word.title())
    return ", ".join(langs[:5]) if langs else ""


def extract_profile_keywords(text: str, *, limit: int = 40) -> list[str]:
    """Small deterministic résumé vocabulary for private relevance scoring."""
    stop = {
        "and", "the", "for", "with", "from", "that", "this", "your", "our",
        "job", "work", "role", "years", "year", "experience", "education",
        "email", "phone", "address", "summary", "professional",
    }
    counts: dict[str, int] = {}
    for raw in re.findall(
        r"[A-Za-z\u4e00-\u9fff][A-Za-z0-9\u4e00-\u9fff+#./-]{2,30}",
        text or "",
    ):
        word = raw.casefold()
        if word in stop or word.isdigit():
            continue
        counts[word] = counts.get(word, 0) + 1
    return [
        word
        for word, _ in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ][:limit]


def classify_profession(intent: str, resume_text: str) -> dict[str, Any]:
    """Classify from explicit intent first; résumé history is only a fallback."""
    category_terms = {
        "legal": [
            "law", "legal", "lawyer", "paralegal", "compliance", "solicitor",
            "counsel", "诉讼", "律师", "合规", "法务",
        ],
        "technology": [
            "engineer", "developer", "software", "backend", "frontend",
            "fullstack", "devops", "data", "python", "javascript", "java", "go",
            "工程师", "开发", "软件", "数据",
        ],
        "finance": [
            "finance", "banking", "investment", "accounting", "audit",
            "financial analyst", "金融", "财务", "审计", "投资",
        ],
        "marketing": [
            "marketing", "brand", "content", "social media", "growth", "pr",
            "communication", "市场", "品牌", "内容", "增长", "传播",
        ],
    }

    def category_scores(text: str) -> dict[str, int]:
        lowered = text.casefold()

        def hit(term: str) -> bool:
            # Short abbreviations such as ``PR`` must be whole tokens.  A
            # substring check turns ordinary words like ``prepared`` into a
            # marketing signal and can route a new user's entire setup into
            # the wrong lane family.  Longer bilingual terms remain
            # intentionally tolerant of normal resume prose.
            value = str(term).casefold()
            if len(value) <= 3 or re.fullmatch(r"[a-z0-9]+", value):
                return bool(re.search(rf"(?<![a-z0-9]){re.escape(value)}(?![a-z0-9])", lowered))
            return value in lowered

        return {
            category: sum(1 for term in terms if hit(term))
            for category, terms in category_terms.items()
        }

    scores = category_scores(intent)
    if not any(scores.values()):
        scores = category_scores(resume_text)
    primary = max(scores, key=scores.get) if any(scores.values()) else "general"
    is_law = primary == "legal"
    is_tech = primary == "technology"
    is_finance = primary == "finance"
    is_marketing = primary == "marketing"

    if is_law:
        return {
            "domain": "legal",
            "relevance_keywords": [
                "legal", "lawyer", "counsel", "solicitor", "paralegal",
                "litigation", "compliance", "regulatory", "aml", "kyc",
                "法务", "律师", "合规", "诉讼",
            ],
            "adjacent_keywords": ["risk", "governance", "company secretary", "policy"],
            "track_rules": [
                {"letter": "A", "patterns": ["litigation", "dispute", "paralegal"]},
                {"letter": "B", "patterns": ["counsel", "contract", "commercial"]},
                {"letter": "C", "patterns": ["compliance", "aml", "kyc", "regulatory"]},
                {"letter": "D", "patterns": ["cross-border", "prc", "china"]},
                {"letter": "E", "patterns": ["restructuring", "insolvency", "bankruptcy"]},
            ],
            "track_mapping": {
                "A": "诉讼/所内支持", "B": "合同商事/Counsel", "C": "合规/AML",
                "D": "跨境/中国法", "E": "重组/破产顾问", "F": "通用法律",
            },
            "extra_columns": [
                {"name": "粤语", "type": "text", "description": "语言要求"},
                {"name": "港牌要求", "type": "text", "description": "HK solicitor license"},
                {"name": "PQE提示", "type": "text", "description": "Post-qualification experience"},
                {"name": "内地法/PRC背景", "type": "text", "description": "PRC law background needed"},
            ],
            "queries": [
                {"id": "paralegal", "bucket": "legal", "terms": {"linkedin": "paralegal", "jobsdb": "paralegal", "ctgoodjobs": "paralegal"}, "track_hint": "F"},
                {"id": "legal_assistant", "bucket": "legal", "terms": {"linkedin": "legal assistant", "jobsdb": "legal assistant", "ctgoodjobs": "legal assistant"}, "track_hint": "F"},
                {"id": "legal_counsel", "bucket": "legal", "terms": {"linkedin": "legal counsel", "jobsdb": "legal counsel", "ctgoodjobs": "legal counsel"}, "track_hint": "B"},
                {"id": "compliance", "bucket": "legal", "terms": {"linkedin": "compliance officer junior OR compliance assistant", "jobsdb": "compliance officer", "ctgoodjobs": "compliance officer"}, "track_hint": "C"},
                {"id": "litigation_paralegal", "bucket": "legal", "terms": {"linkedin": "litigation paralegal", "jobsdb": "litigation paralegal", "ctgoodjobs": "litigation paralegal"}, "track_hint": "A"},
                {"id": "company_secretary", "bucket": "legal", "terms": {"linkedin": "company secretary junior", "jobsdb": "company secretary", "ctgoodjobs": "company secretary"}, "track_hint": "B"},
                {"id": "prc_china_legal", "bucket": "legal", "terms": {"linkedin": "PRC lawyer OR China counsel Hong Kong", "jobsdb": "PRC legal", "ctgoodjobs": "PRC"}, "track_hint": "D"},
                {"id": "startup_fintech_legal", "bucket": "legal", "terms": {"linkedin": "legal counsel fintech OR legal counsel startup", "jobsdb": "legal counsel fintech", "ctgoodjobs": "legal counsel fintech"}, "track_hint": "B"},
            ],
            "scoring_weights": {"resume": 0.35, "eligibility": 0.25, "direction": 0.15, "industry": 0.10, "work": 0.10, "pay": 0.05},
            "hard_reject": [],
            "soft_flags": {"cantonese": "(?i)cantonese|粤语", "senior_hint": "(?i)\\b(senior|lead|director)\\b", "pqe_high": "(?i)\\b[5-9]\\+\\s*(pqe|years)\\b"},
        }
    if is_tech:
        return {
            "domain": "technology",
            "relevance_keywords": [
                "software", "engineer", "developer", "backend", "frontend",
                "fullstack", "devops", "sre", "data", "machine learning",
                "security", "platform", "软件", "开发", "工程师", "数据",
            ],
            "adjacent_keywords": ["product", "cloud", "qa", "automation", "technical"],
            "track_rules": [
                {"letter": "A", "patterns": ["backend", "api", "platform"]},
                {"letter": "B", "patterns": ["frontend", "fullstack", "web"]},
                {"letter": "C", "patterns": ["devops", "sre", "cloud", "infrastructure"]},
                {"letter": "D", "patterns": ["data", "machine learning", "ai", "ml"]},
                {"letter": "E", "patterns": ["security", "cyber"]},
            ],
            "track_mapping": {"A": "后端", "B": "前端/全栈", "C": "DevOps/SRE", "D": "数据/ML", "E": "安全", "F": "通用技术"},
            "extra_columns": [
                {"name": "技术栈", "type": "text", "description": "Key tech skills mentioned"},
                {"name": "远程OK", "type": "text", "description": "Remote-friendly"},
                {"name": "签证赞助", "type": "text", "description": "Visa sponsorship"},
            ],
            "queries": [
                {"id": "backend", "bucket": "tech", "terms": {"linkedin": "backend engineer", "jobsdb": "backend developer", "ctgoodjobs": "backend"}, "track_hint": "A"},
                {"id": "fullstack", "bucket": "tech", "terms": {"linkedin": "fullstack developer", "jobsdb": "full stack", "ctgoodjobs": "full stack"}, "track_hint": "B"},
                {"id": "devops", "bucket": "tech", "terms": {"linkedin": "devops engineer OR SRE", "jobsdb": "devops", "ctgoodjobs": "DevOps"}, "track_hint": "C"},
                {"id": "data_engineer", "bucket": "tech", "terms": {"linkedin": "data engineer OR data scientist", "jobsdb": "data engineer", "ctgoodjobs": "data"}, "track_hint": "D"},
            ],
            "scoring_weights": {"resume": 0.40, "eligibility": 0.15, "direction": 0.15, "industry": 0.10, "work": 0.10, "pay": 0.10},
            "hard_reject": [],
            "soft_flags": {"senior_hint": "(?i)\\b(senior|lead|principal)\\b", "remote": "(?i)\\bremote\\b"},
        }
    if is_finance:
        return {
            "domain": "finance",
            "relevance_keywords": [
                "finance", "financial", "banking", "investment", "accounting",
                "audit", "risk", "treasury", "portfolio", "财务", "金融", "审计",
            ],
            "adjacent_keywords": ["operations", "analytics", "compliance", "strategy"],
            "track_rules": [
                {"letter": "A", "patterns": ["investment banking", "ibd", "capital markets"]},
                {"letter": "B", "patterns": ["asset management", "portfolio", "fund"]},
                {"letter": "C", "patterns": ["risk", "control"]},
                {"letter": "D", "patterns": ["audit", "assurance"]},
                {"letter": "E", "patterns": ["financial analyst", "fp&a", "finance analyst"]},
            ],
            "track_mapping": {"A": "投行/IBD", "B": "资产管理", "C": "风控", "D": "审计", "E": "财务分析", "F": "通用金融"},
            "extra_columns": [
                {"name": "证书要求", "type": "text", "description": "CFA/CPA/FRM"},
                {"name": "行业", "type": "text", "description": "Sector focus"},
            ],
            "queries": [
                {"id": "financial_analyst", "bucket": "finance", "terms": {"linkedin": "financial analyst", "jobsdb": "financial analyst", "ctgoodjobs": "financial analyst"}, "track_hint": "E"},
                {"id": "risk_management", "bucket": "finance", "terms": {"linkedin": "risk management officer", "jobsdb": "risk management", "ctgoodjobs": "risk"}, "track_hint": "C"},
            ],
            "scoring_weights": {"resume": 0.35, "eligibility": 0.20, "direction": 0.15, "industry": 0.15, "work": 0.10, "pay": 0.05},
            "hard_reject": [],
            "soft_flags": {"senior_hint": "(?i)\\b(senior|lead)\\b"},
        }
    if is_marketing:
        return {
            "domain": "marketing",
            "relevance_keywords": [
                "marketing", "brand", "content", "growth", "campaign",
                "communications", "social media", "seo", "市场", "品牌", "内容",
            ],
            "adjacent_keywords": ["sales", "partnership", "community", "product"],
            "track_rules": [
                {"letter": "A", "patterns": ["growth", "performance", "acquisition"]},
                {"letter": "B", "patterns": ["brand", "campaign"]},
                {"letter": "C", "patterns": ["content", "editorial", "seo"]},
                {"letter": "D", "patterns": ["social media", "community"]},
                {"letter": "E", "patterns": ["communications", "public relations", "pr"]},
            ],
            "track_mapping": {
                "A": "增长/投放", "B": "品牌", "C": "内容/SEO",
                "D": "社交/社区", "E": "传播/公关", "F": "通用市场",
            },
            "extra_columns": [
                {"name": "渠道", "type": "text", "description": "Marketing channels"},
                {"name": "作品集要求", "type": "text", "description": "Portfolio requirement"},
            ],
            "queries": [
                {"id": "growth", "bucket": "marketing", "terms": {"linkedin": "growth marketing", "jobsdb": "growth marketing", "ctgoodjobs": "growth marketing"}, "track_hint": "A"},
                {"id": "brand", "bucket": "marketing", "terms": {"linkedin": "brand marketing", "jobsdb": "brand marketing", "ctgoodjobs": "brand"}, "track_hint": "B"},
                {"id": "content", "bucket": "marketing", "terms": {"linkedin": "content marketing", "jobsdb": "content marketing", "ctgoodjobs": "content"}, "track_hint": "C"},
            ],
            "scoring_weights": {"resume": 0.35, "eligibility": 0.15, "direction": 0.20, "industry": 0.10, "work": 0.10, "pay": 0.10},
            "hard_reject": [],
            "soft_flags": {"senior_hint": "(?i)\\b(senior|lead|director)\\b"},
        }
    # Generic
    intent_words = [
        word
        for word in re.findall(r"[A-Za-z\u4e00-\u9fff][A-Za-z0-9\u4e00-\u9fff+#./-]{1,30}", intent)
        if word.lower() not in {
            "job", "jobs", "role", "roles", "work", "looking", "for", "in",
            "hong", "kong", "singapore", "junior", "senior",
        }
    ][:12]
    target = " ".join(intent_words[:5]).strip() or "general professional"
    return {
        "domain": "general",
        "relevance_keywords": intent_words or ["professional", "specialist", "coordinator"],
        "adjacent_keywords": ["operations", "project", "analyst", "associate"],
        "track_rules": [],
        "track_mapping": {
            "A": "核心目标",
            "B": "相邻目标",
            "C": "技能相近",
            "D": "行业相近",
            "E": "探索机会",
            "F": "其他",
        },
        "extra_columns": [],
        "queries": [
            {"id": "core_target", "bucket": "general", "terms": {"linkedin": target, "jobsdb": target, "ctgoodjobs": target}, "track_hint": "F"},
            {"id": "junior_target", "bucket": "general", "terms": {"linkedin": f"junior {target}", "jobsdb": f"junior {target}", "ctgoodjobs": target}, "track_hint": "A"},
            {"id": "specialist_target", "bucket": "general", "terms": {"linkedin": f"{target} specialist", "jobsdb": f"{target} specialist", "ctgoodjobs": target}, "track_hint": "E"},
        ],
        "scoring_weights": {"resume": 0.35, "eligibility": 0.20, "direction": 0.15, "industry": 0.10, "work": 0.10, "pay": 0.10},
        "hard_reject": [],
        "soft_flags": {},
    }


# ── Generate config files ────────────────────────────────────────────

def build_queries_config(
    *,
    profession: dict[str, Any],
    location: str,
) -> dict[str, Any]:
    """Build per-user search policy for the gitignored personal workspace."""
    source_queries = profession.get("queries") or []
    mandatory_buckets = list(
        profession.get("mandatory_buckets") or PERSONAL_QUERY_BUCKETS
    )
    queries = []
    for index, raw in enumerate(source_queries):
        item = dict(raw)
        item["bucket"] = mandatory_buckets[index % len(mandatory_buckets)]
        terms = dict(item.get("terms") or {})
        terms["freehire"] = (
            terms.get("freehire")
            or terms.get("linkedin")
            or terms.get("jobsdb")
            or item.get("id")
            or ""
        )
        item["terms"] = terms
        queries.append(item)

    if queries:
        seed = queries[0]
        present = {q["bucket"] for q in queries}
        for bucket in mandatory_buckets:
            if bucket in present:
                continue
            clone = dict(seed)
            clone["id"] = f"{seed.get('id', 'general')}_{bucket}"
            clone["bucket"] = bucket
            clone["terms"] = dict(seed.get("terms") or {})
            queries.append(clone)

    return {
        "description": "Jobsflow search queries generated from local setup preferences",
        "location_linkedin": location,
        "workflow_preferences": {
            "scan_depth": normalize_scan_depth(
                (profession.get("workflow_preferences") or {}).get("scan_depth")
            ),
            "retention_preference": normalize_retention_preference(
                (profession.get("workflow_preferences") or {}).get(
                    "retention_preference"
                )
            ),
        },
        "query_policy": {
            "mandatory_buckets": mandatory_buckets,
            "notes": "Private setup output; buckets reflect this user's target domain.",
        },
        "relevance_keywords": list(profession.get("relevance_keywords") or []),
        "adjacent_keywords": list(profession.get("adjacent_keywords") or []),
        "noise_title_patterns": list(profession.get("noise_title_patterns") or []),
        "scoring_profile": {
            "domain": profession.get("domain") or "general",
            "core_keywords": list(profession.get("relevance_keywords") or []),
            "adjacent_keywords": list(profession.get("adjacent_keywords") or []),
            "evidence_keywords": list(profession.get("evidence_keywords") or []),
            "preferred_industry_keywords": list(
                profession.get("preferred_industry_keywords") or []
            ),
            "track_mapping": dict(profession.get("track_mapping") or {}),
            "track_rules": list(profession.get("track_rules") or []),
            "weights": dict(profession.get("scoring_weights") or {}),
            # These fields are intentionally explicit so the scorer can remain
            # neutral when the user has not supplied a constraint, while still
            # changing eligibility/work/pay dimensions when setup captured one.
            "minimum_salary": profession.get("minimum_salary"),
            "minimum_salary_currency": profession.get("minimum_salary_currency"),
            "minimum_salary_period": profession.get("minimum_salary_period"),
            "minimum_salary_parse_status": profession.get("minimum_salary_parse_status"),
            "minimum_salary_parse_warning": profession.get("minimum_salary_parse_warning"),
            "candidate_languages": list(profession.get("candidate_languages") or []),
            "max_relevant_years": profession.get("max_relevant_years"),
            "schedule_risk_keywords": list(profession.get("schedule_risk_keywords") or []),
            "qualification_keywords": list(profession.get("qualification_keywords") or []),
            "semantic_profile": dict(
                profession.get("semantic_profile")
                or semantic_profile_for_level("medium")
            ),
            "neutral_scores": dict(
                profession.get("neutral_scores")
                or {"eligibility": 3.5, "work": 3.5, "pay": 3.0}
            ),
            "industry_context": dict(profession.get("industry_context") or {}),
        },
        "queries": queries,
        "portals": {
            "linkedin": {
                "enabled": True,
                "cli": ".agents/skills/linkedin-search/cli/src/cli.ts",
                "jobage_days": 1,
            },
            "jobsdb": {
                "enabled": True,
                "cli": ".agents/skills/jobsdb-search/cli/src/cli.ts",
                "jobage_days": 7,
                "client_max_hours": 24,
            },
            "ctgoodjobs": {
                "enabled": True,
                "cli": ".agents/skills/ctgoodjobs-search/cli/src/cli.ts",
                "jobage_days": 1,
            },
            "freehire": {
                "enabled": True,
                "cli": ".agents/skills/freehire-search/cli/src/cli.ts",
                "jobage_days": 1,
            },
        },
        "hard_reject_title_patterns": profession.get("hard_reject") or [],
        "soft_flag_patterns": profession.get("soft_flags") or {},
    }


def _setup_design_fallback(profession: dict[str, Any]) -> dict[str, Any]:
    return {
        "track_mapping": dict(profession.get("track_mapping") or {}),
        "extra_columns": list(profession.get("extra_columns") or []),
        "relevance_keywords": list(profession.get("relevance_keywords") or []),
        "adjacent_keywords": list(profession.get("adjacent_keywords") or []),
        "track_rules": list(profession.get("track_rules") or []),
        "scoring_weights": dict(profession.get("scoring_weights") or {}),
        "industry_context": dict(
            profession.get("industry_context")
            or {
                "target_industry": profession.get("domain") or "general",
                "common_requirements": [],
                "source_urls": [],
                "uncertainties": [
                    "Deterministic fallback has not performed external industry research."
                ],
            }
        ),
    }


def build_tracker_schema(profession: dict[str, Any]) -> dict[str, Any]:
    base_types = {
        "匹配分": "number",
        "薪资": "text",
        "链接": "url",
        "发布日期": "date",
        "CareerOps分数": "number",
    }
    columns = [
        {
            "name": name,
            "type": base_types.get(name, "text"),
            "description": (
                "Job ID: {track}{tier}-{seq}" if name == "岗位编号" else ""
            ),
        }
        for name in INITIAL_TRACKER_HEADERS
    ]
    insert_at = next(
        (index for index, item in enumerate(columns) if item["name"] == "发布日期"),
        len(columns),
    )
    for column in reversed(list(profession.get("extra_columns") or [])):
        columns.insert(insert_at, dict(column))
    return {
        "columns": columns,
        "track_mapping": dict(profession.get("track_mapping") or {}),
        "scoring_weights": dict(profession.get("scoring_weights") or {}),
        "industry_context": dict(profession.get("industry_context") or {}),
    }


def _apply_design_to_profession(
    profession: dict[str, Any],
    design: dict[str, Any],
) -> dict[str, Any]:
    updated = dict(profession)
    for key in (
        "track_mapping",
        "extra_columns",
        "relevance_keywords",
        "adjacent_keywords",
        "track_rules",
        "scoring_weights",
        "industry_context",
    ):
        if key in design:
            updated[key] = design[key]
    return updated


def generate_config(
    resume_text: str,
    intent: str,
    tracking: dict,
    prereqs: dict,
    semantic_upper_level: str = "medium",
    workflow_preferences: dict[str, str] | None = None,
) -> int:
    print("\n── Step 7: Generating config ──\n")

    name = extract_name(resume_text) or ask("Your name", "Your Name")
    phone = extract_phone(resume_text) or ask("Your phone", "+852 XXXX XXXX")
    email = extract_email(resume_text) or ask("Your email", "your.email@example.com")
    education = extract_education(resume_text)
    languages = extract_languages(resume_text)
    if sys.stdin.isatty():
        languages = ask(
            "Languages you can work in and honest levels (e.g. English C1; Cantonese native)",
            languages,
        )
    prof_languages = parse_candidate_languages(languages)

    prof = classify_profession(intent, resume_text)
    prof["candidate_languages"] = prof_languages
    prof["semantic_profile"] = semantic_profile_for_level(semantic_upper_level)
    prof["workflow_preferences"] = dict(workflow_preferences or {})
    # Parse only explicit, machine-checkable constraints from the user's intent;
    # missing values stay unknown rather than being guessed from résumé history.
    intent_lower = str(intent or "").casefold()
    salary_marker = re.search(r"(?:minimum|min|at\s+least|最低|不少于|至少)", intent_lower)
    if salary_marker:
        salary = parse_salary_range(intent_lower[salary_marker.end() : salary_marker.end() + 80])
        if salary.status == PARSED and salary.low is not None:
            prof["minimum_salary"] = int(salary.low) if float(salary.low).is_integer() else salary.low
            if salary.currency:
                prof["minimum_salary_currency"] = salary.currency
            if salary.period:
                prof["minimum_salary_period"] = salary.period
        elif salary.status in {AMBIGUOUS, INVALID}:
            prof["minimum_salary"] = None
            prof["minimum_salary_parse_status"] = salary.status
            prof["minimum_salary_parse_warning"] = salary.reason or "请补充币种或明确千位/小数分隔方式"
            print(
                "[warning] Minimum salary was not recorded because the number format is "
                f"{salary.status}; add a currency/period or enter an unambiguous value."
            )
    max_years_match = re.search(
        r"(?:up\s+to|no\s+more\s+than|<=|不超过)\s*(\d+)\s*(?:years?|年)",
        intent_lower,
    )
    if max_years_match:
        prof["max_relevant_years"] = int(max_years_match.group(1))
    if re.search(r"(?:no|without|avoid|不要|不含).{0,12}(?:evening|night|weekend|shift|晚班|夜班|周末|轮班)", intent_lower):
        prof["schedule_risk_keywords"] = [
            "evening",
            "night",
            "weekend",
            "shift",
            "晚班",
            "夜班",
            "周末",
            "轮班",
        ]
    prof["evidence_keywords"] = extract_profile_keywords(resume_text)
    prof["preferred_industry_keywords"] = extract_profile_keywords(intent, limit=16)
    from tools.setup_contract import build_setup_design_request, resolve_setup_design

    profile_dir = REPO / "JobSearch_2026" / "00_Profile"
    profile_dir.mkdir(parents=True, exist_ok=True)
    # Keep the imported résumé in the ignored runtime workspace so materials
    # fact-checking has an evidence source immediately after /setup.  This is
    # intentionally separate from tracked product templates and master DOCX.
    resume_runtime = profile_dir / "resume_runtime"
    resume_runtime.mkdir(parents=True, exist_ok=True)
    (resume_runtime / "resume.txt").write_text(
        str(resume_text or "").strip() + "\n", encoding="utf-8"
    )
    # Build a private structured fact index from the user-provided résumé. It
    # is a source boundary for fact-checking, not a public product preset.
    records = []
    seen_claims = set()
    for line in re.split(r"\n\s*\n|\n", str(resume_text or "")):
        claim = re.sub(r"^\s*[-*•]\s*", "", line).strip()
        claim = re.sub(r"\s+", " ", claim)
        # Do not drop short but meaningful facts (GPA, degree, language,
        # certification, employer/title or a concise skill).  The previous
        # 40-character cutoff silently erased exactly the fields that later
        # base CV/CL generation needs.  Contact-only lines are already stored
        # in config.personal.json and are not evidence claims.
        if (
            len(claim) < 8
            or claim.casefold() in seen_claims
            or re.fullmatch(r"(?:https?://\S+|[^\s@]+@[^\s@]+\.[^\s@]+|[+()\d\- .]{7,})", claim)
        ):
            continue
        seen_claims.add(claim.casefold())
        digest = hashlib.sha256(claim.casefold().encode("utf-8")).hexdigest()[:10].upper()
        lower_claim = claim.casefold()
        if re.search(r"\b(gpa|grade|bachelor|master|phd|jd|llm|mba|degree|本科|硕士|博士)\b", lower_claim):
            claim_type = "education"
        elif re.search(r"\b(certificate|certification|language|english|cantonese|mandarin|ielts|toefl|证书|语言|英语|粤语|普通话)\b", lower_claim):
            claim_type = "qualification"
        elif re.search(r"\b(19|20)\d{2}\b|\b(present|current|至今)\b", lower_claim):
            claim_type = "experience_or_date"
        else:
            claim_type = "experience_or_skill"
        records.append(
            {
                "evidence_id": f"EVID-{digest}",
                "claim": claim,
                "section": claim_type,
                "entities": [],
                "metrics": re.findall(r"\d+(?:[.,]\d+)*\+?%?", claim),
                "contexts": [],
                "allowed_phrasing": [claim],
                "forbidden_inference": [
                    "Do not infer duties, tools, scope, seniority, clients or outcomes not stated in the imported résumé."
                ],
                "source_refs": ["00_Profile/resume_runtime/resume.txt"],
                "status": "user_imported",
            }
        )
    (profile_dir / "fact_evidence.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source": "user_imported_resume",
                "records": records,
                "policy": "Only records in this private file may ground generated material; users remain responsible for final accuracy.",
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    fallback = _setup_design_fallback(prof)
    request = build_setup_design_request(
        intent=intent,
        resume_keywords=prof["evidence_keywords"],
        fallback=fallback,
        semantic_profile=prof["semantic_profile"],
    )
    request_path = profile_dir / "setup_design_request.json"
    request_path.write_text(
        json.dumps(request, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    proposal_path = profile_dir / "setup_schema_proposal.json"
    if proposal_path.exists():
        try:
            proposal = json.loads(proposal_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            proposal = None
        result = resolve_setup_design(proposal, fallback=fallback)
        if result["ready"]:
            prof = _apply_design_to_profession(prof, result["design"])
    else:
        result = {
            "schema_version": 1,
            "ready": True,
            "source": "deterministic_fallback",
            "validation_errors": [],
            "design": fallback,
            "next_action": "model_may_propose_private_setup_design",
        }
    (profile_dir / "setup_design_result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    # config.personal.json
    config = {
        "candidate_name": name,
        "phone": phone,
        "email": email,
        "education": education,
        "languages": languages,
        "language_profile": prof_languages,
        "location": intent,
    }
    config_path = personal_config_path(REPO)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    ok(f"{config_path.relative_to(REPO)} -> personal profile saved privately")

    # Per-user queries belong to the private workspace, never the tracked preset.
    location = "Hong Kong"
    m = re.search(r"(hong kong|香港|singapore|上海|北京|shenzhen|深圳|tokyo|london|new york)", intent.lower())
    if m:
        location = m.group(1).title()

    queries = build_queries_config(profession=prof, location=location)
    queries_path = personal_queries_path(REPO)
    queries_path.parent.mkdir(parents=True, exist_ok=True)
    queries_path.write_text(
        json.dumps(queries, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    intent_state_path = profile_dir / "intent_state.json"
    try:
        previous_state = json.loads(intent_state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        previous_state = {}
    history = previous_state.get("history") if isinstance(previous_state, dict) else []
    if not isinstance(history, list):
        history = []
    history.append({"operation": "setup", "input": intent, "confirmed_at": datetime.now().isoformat()})
    atomic_write_json(
        intent_state_path,
        {
            "schema_version": 1,
            "current_intent": intent,
            "updated_at": datetime.now().isoformat(),
            "history": history[-20:],
        },
    )
    ok("intent_state.json -> current intent saved privately")
    ok(
        f"{queries_path.relative_to(REPO)} -> {len(prof['queries'])} queries "
        f"across {len(set(q['bucket'] for q in prof['queries']))} buckets"
    )

    # tracker_schema.json
    schema = build_tracker_schema(prof)
    schema_path = REPO / "JobSearch_2026" / "02_Tracker" / "tracker_schema.json"
    schema_path.parent.mkdir(parents=True, exist_ok=True)
    schema_path.write_text(json.dumps(schema, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    ok(f"tracker_schema.json -> {len(schema['columns'])} columns, {len(prof['track_mapping'])} tracks")
    tracker_path = ensure_initial_tracker(
        REPO / "JobSearch_2026",
        [item["name"] for item in schema["columns"]],
    )
    ok(f"{tracker_path.name} -> ready for first /scan")

    # Prepare one bounded base-CV/CL request per lane.  This is a private
    # onboarding artifact: it contains the user's evidence IDs/claims and is
    # written only under the ignored runtime workspace.  It does not create
    # an active master automatically; activation remains preview + explicit
    # confirmation so an incomplete or weak model response cannot become the
    # source for /materials.
    try:
        from tools.workflow.base_onboarding import prepare_requests

        request_paths = prepare_requests(REPO / "JobSearch_2026")
        ok(f"base onboarding -> {len(request_paths)} lane request(s) ready; fill and confirm before /materials")
    except Exception as exc:
        warn(f"base onboarding request preparation deferred: {exc}")

    # .env
    env_path = REPO / ".env"
    if not env_path.exists():
        lines = ["# JobsFlow - Personal Configuration", f'CANDIDATE_NAME="{name}"', ""]
        if tracking["method"] == "google_sheets":
            lines.append("GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json")
            lines.append("GSHEET_ID=your-sheet-id")
        else:
            lines.append("# Using local CSV tracking")
        env_path.write_text("\n".join(lines), encoding="utf-8")
        ok(".env created")
    else:
        info(".env already exists")

    print(f"\n  Track mapping (A-F):")
    for k, v in prof["track_mapping"].items():
        print(f"    {k} -> {v}")

    return 0


# ── Install portals ──────────────────────────────────────────────────

def install_portals() -> int:
    print("\n── Installing portal CLI tools ──\n")
    failed = 0
    for skill in PORTAL_SKILLS:
        cli_dir = REPO / ".agents" / "skills" / skill / "cli"
        if not (cli_dir / "package.json").exists():
            warn(f"{skill}: no package.json, skipping")
            continue
        print(f"  Installing {skill}...")
        try:
            subprocess.run(
                ["bun", "install", "--frozen-lockfile"],
                cwd=str(cli_dir),
                check=True,
                capture_output=True,
            )
            ok(f"{skill} installed")
        except subprocess.CalledProcessError as e:
            warn(f"{skill} install failed: {e.stderr.decode()[:200]}")
            failed += 1
    if failed:
        warn(f"{failed} portal(s) failed to install")
    return failed


def apply_schema_proposal(proposal_path: Path) -> int:
    """Validate a model proposal and apply it only to the private workspace."""
    from tools.setup_contract import resolve_setup_design

    profile_dir = REPO / "JobSearch_2026" / "00_Profile"
    request_path = profile_dir / "setup_design_request.json"
    queries_path = personal_queries_path(REPO)
    try:
        proposal = json.loads(Path(proposal_path).expanduser().read_text(encoding="utf-8"))
        request = json.loads(request_path.read_text(encoding="utf-8"))
        queries = json.loads(queries_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        warn(f"Cannot apply setup schema proposal: {exc}")
        return 2
    fallback = (
        (request.get("inputs") or {}).get("deterministic_fallback")
        if isinstance(request, dict)
        else None
    )
    if not isinstance(fallback, dict):
        warn("Setup design request has no deterministic fallback")
        return 2
    result = resolve_setup_design(proposal, fallback=fallback)
    (profile_dir / "setup_design_result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if not result["ready"]:
        warn("Schema proposal rejected; deterministic fallback remains active")
        for error in result["validation_errors"]:
            warn(error)
        return 3

    design = result["design"]
    queries["relevance_keywords"] = design["relevance_keywords"]
    queries["adjacent_keywords"] = design["adjacent_keywords"]
    scoring = queries.get("scoring_profile") or {}
    scoring.update(
        {
            "core_keywords": design["relevance_keywords"],
            "adjacent_keywords": design["adjacent_keywords"],
            "track_mapping": design["track_mapping"],
            "track_rules": design["track_rules"],
            "weights": design["scoring_weights"],
            "industry_context": design["industry_context"],
        }
    )
    queries["scoring_profile"] = scoring
    queries_path.write_text(
        json.dumps(queries, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    profession = {
        "track_mapping": design["track_mapping"],
        "extra_columns": design["extra_columns"],
        "scoring_weights": design["scoring_weights"],
        "industry_context": design["industry_context"],
    }
    schema_path = REPO / "JobSearch_2026" / "02_Tracker" / "tracker_schema.json"
    schema_path.write_text(
        json.dumps(build_tracker_schema(profession), ensure_ascii=False, indent=2)
        + "\n",
        encoding="utf-8",
    )
    schema = build_tracker_schema(profession)
    tracker_path = ensure_initial_tracker(
        REPO / "JobSearch_2026",
        [item["name"] for item in schema["columns"]],
    )
    ok("Validated model proposal applied to private queries and tracker schema")
    ok(f"{tracker_path.name} -> customized header applied when tracker is empty")
    return 0


# ── Main ──────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="JobsFlow - First-time setup")
    ap.add_argument("--resume-folder", type=Path, default=None)
    ap.add_argument("--install-portals", action="store_true", help="Install portal CLI dependencies")
    ap.add_argument(
        "--schema-proposal",
        type=Path,
        default=None,
        help="Validate and apply a model-proposed tracker/scoring schema to the private workspace",
    )
    ap.add_argument("--doctor", action="store_true", help="Read-only readiness check")
    ap.add_argument(
        "--doctor-json",
        action="store_true",
        help="Machine-readable readiness check for agent workflows",
    )
    args = ap.parse_args(argv)

    if args.doctor_json:
        snapshot = doctor_snapshot()
        print(json.dumps(snapshot, ensure_ascii=False, indent=2))
        return 0 if snapshot["ready"] else 1
    if args.doctor:
        return run_doctor()
    if args.install_portals:
        return install_portals()
    if args.schema_proposal:
        return apply_schema_proposal(args.schema_proposal)

    print("=" * 56)
    print("  JobsFlow Setup")
    print("  Answer a few questions, I'll generate everything.")
    print("=" * 56)

    prereqs = check_prerequisites()
    js_root = create_directories()
    tracking = ask_tracking()

    if args.resume_folder:
        folder = args.resume_folder.resolve()
    else:
        print("\n  Usage: python3 setup.py --resume-folder ~/Documents/my-cv")
        print("  Or in AI chat: /setup ~/Documents/my-cv\n")
        folder = Path(ask("Resume folder path", str(Path.home() / "Documents"))).expanduser().resolve()
    resume_text = read_resume(folder)
    intent = ask("\nWhat jobs are you looking for?\n  (e.g. 'Junior paralegal in Hong Kong, PRC background')")
    semantic_upper_level = ask_semantic_profile_level()
    workflow_preferences = ask_workflow_preferences()
    generate_config(
        resume_text,
        intent,
        tracking,
        prereqs,
        semantic_upper_level=semantic_upper_level,
        workflow_preferences=workflow_preferences,
    )

    print(f"\n  Done! Config files generated.")

    path = ask_yes_no("你想先做什么？\n  先做基础版简历（按刚才说的方向分 A-F 版本）\n  还是先开始检索新职位？\n  输入 y 先做基础版，n 先检索", default=False)
    if path:
        print(f"\n  好，先做基础版。方向字母映射已经生成了，运行：")
        print(f"    python3 -m tools.workflow base init --lane <字母>")
        print(f"    # 按 00_Profile/base_requests/<字母>/request.json 填写 response.json")
        print(f"    python3 -m tools.workflow base generate --lane <字母> --content JobSearch_2026/00_Profile/base_requests/<字母>/response.json")
        print(f"    python3 -m tools.workflow base confirm --lane <字母>  # 先预览，再加 --confirm 激活")
    else:
        print(f"\n  好，先检索。运行：")
        print(f"    /scan             扫新职位")
        print(f"    /push             推到 Google Sheets")

    print(f"\n  First time? Also run setup.py --install-portals")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
