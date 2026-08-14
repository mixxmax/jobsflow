#!/usr/bin/env python3
"""Auto-compress a package CV/CL docx to the target page count.

Strategy (deterministic, no semantic invention):
  1. Export the current PDF and count pages (LibreOffice is fast: ~2-4s warm).
  2. While over the target, trim the longest non-protected paragraphs at
     sentence boundaries, largest first, until pages fit or the budget is
     exhausted.
  3. Protected paragraphs: openings (para 0-1), bold-headed bullets' header
     runs, signature/final lines, and any paragraph below the min length.

Usage:
  python3 tools/job_materials/auto_compress.py <docx> [--max-pages 1] [--dry-run]
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from docx import Document  # noqa: E402
from pypdf import PdfReader  # noqa: E402

MIN_PARAGRAPH_CHARS = 60          # shorter paragraphs are never trimmed
MAX_TRIM_PER_PARAGRAPH = 0.45     # never remove more than 45% of one paragraph
MAX_ITERATIONS = 8
SENTENCE_SPLIT = re.compile(r"(?<=[.!?。！？])\s+")


def _pdf_pages(pdf: Path) -> int:
    try:
        return len(PdfReader(str(pdf)).pages)
    except Exception:
        return 0


def _export_pdf(docx: Path, outdir: Path) -> Path | None:
    soffice = "/Applications/LibreOffice.app/Contents/MacOS/soffice"
    profile = Path("/tmp/lo_pdf_profile")
    profile.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            soffice, "--headless", "--nologo", "--nofirststartwizard", "--norestore",
            f"-env:UserInstallation=file://{profile}",
            "--convert-to", "pdf:writer_pdf_Export",
            "--outdir", str(outdir), str(docx),
        ],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120,
    )
    produced = outdir / (docx.stem + ".pdf")
    return produced if produced.exists() else None


def _split_sentences(text: str) -> list[str]:
    parts = [p.strip() for p in SENTENCE_SPLIT.split(text) if p.strip()]
    return parts or [text]


def _trim_paragraph(paragraph, target_chars: int, dry_run: bool) -> tuple[bool, int]:
    """Trim paragraph text at sentence boundaries toward target_chars."""
    text = "".join(r.text for r in paragraph.runs)
    if len(text) <= target_chars:
        return False, len(text)
    sentences = _split_sentences(text)
    if len(sentences) <= 1:
        return False, len(text)
    # Drop trailing sentences, keeping the cut whose length is CLOSEST to the
    # target (sentence granularity can otherwise overshoot badly).
    kept = sentences[:]
    best = kept[:]
    while len(kept) > 1:
        kept = kept[:-1]
        if abs(sum(len(s) for s in kept) - target_chars) < abs(
            sum(len(s) for s in best) - target_chars
        ):
            best = kept[:]
    new_text = " ".join(best)
    if new_text == text:
        return False, len(text)
    if dry_run:
        return True, len(new_text)
    if paragraph.runs:
        paragraph.runs[0].text = new_text
        for r in paragraph.runs[1:]:
            r.text = ""
    return True, len(new_text)


# Opening/closing paragraphs that must stay intact (they carry the application
# pitch).  Also covers bold-headed bullet headers (trim body only).
_OPENING_PREFIXES = (
    "I am writing",
    "I would welcome",
    "I would bring",
    "I would be delighted",
    "Please accept",
    "Thank you for",
    "Dear ",
    "Across these matters",
    "This combination",
)


def _protected(paragraph, index: int, total: int) -> bool:
    """Paragraphs we must never trim."""
    text = "".join(r.text for r in paragraph.runs)
    if index in (0, 1) or index >= total - 2:      # header / signature blocks
        return True
    if len(text) < MIN_PARAGRAPH_CHARS:
        return True
    # Bold-headed bullets: trim body only, never the bold header run
    if paragraph.runs and paragraph.runs[0].bold:
        return True
    # Application pitch / closing lines: keep intact
    stripped = text.strip()
    if stripped.startswith(_OPENING_PREFIXES):
        return True
    return False


def auto_compress(docx_path: Path, max_pages: int = 1, dry_run: bool = False) -> int:
    docx_path = Path(docx_path).resolve()
    doc = Document(str(docx_path))
    with tempfile.TemporaryDirectory() as tmp:
        pdf = _export_pdf(docx_path, Path(tmp))
        if pdf is None:
            print("ERROR: PDF export failed", file=sys.stderr)
            return 2
        pages = _pdf_pages(pdf)
        if pages <= max_pages:
            print(f"already {pages} page(s) <= {max_pages}; nothing to do")
            return 0
        print(f"pages={pages} (target {max_pages})")

        paragraphs = doc.paragraphs
        total = len(paragraphs)
        for iteration in range(1, MAX_ITERATIONS + 1):
            # Candidate paragraphs: longest unprotected first
            candidates = []
            for idx, p in enumerate(paragraphs):
                if _protected(p, idx, total):
                    continue
                text = "".join(r.text for r in p.runs)
                if len(text) > MIN_PARAGRAPH_CHARS:
                    candidates.append((len(text), idx, p, text))
            candidates.sort(key=lambda c: c[0], reverse=True)

            trimmed_any = False
            for _len, idx, p, _text in candidates:
                cur = len("".join(r.text for r in p.runs))
                # Trim the largest paragraphs toward the per-paragraph cap in
                # one pass; the closest-to-target sentence cut stops near it.
                target = max(MIN_PARAGRAPH_CHARS, int(cur * (1 - MAX_TRIM_PER_PARAGRAPH)))
                trimmed, new_len = _trim_paragraph(p, target, dry_run)
                if trimmed:
                    trimmed_any = True
                    print(f"  iter{iteration} para[{idx}] {cur} -> {new_len} chars")

            if not trimmed_any:
                print("no further trim possible; pages still over target")
                return 1
            if dry_run:
                continue
            doc.save(str(docx_path))
            pdf = _export_pdf(docx_path, Path(tmp))
            if pdf is None:
                return 2
            pages = _pdf_pages(pdf)
            print(f"  -> pages={pages}")
            if pages <= max_pages:
                print("compressed to target")
                return 0
        print(f"exhausted {MAX_ITERATIONS} iterations; final pages={pages}")
        return 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("docx", type=Path)
    ap.add_argument("--max-pages", type=int, default=1)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    return auto_compress(args.docx, args.max_pages, args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
