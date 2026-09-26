#!/usr/bin/env python3
"""60-second JobsFlow demo: the CV gate refuses an inflated verb.

Runs the real materials engine on the synthetic workspace the test suite uses
(a fictional candidate applying to a fictional firm).  No model call, no network
connection from this process and no browser; the SOP Control ticket exchange is
skipped the same way the test suite skips it.  Page capacity is measured with
headless LibreOffice, as in normal use.  Exits non-zero if any decision differs
from the expected one, so the recording cannot drift from real behavior.

    python3 scripts/demo.py [--lang zh|zh-hant|en] [--pause SCALE] [--cast FILE]

``--cast`` also writes an asciicast v2 file on a scripted timeline; render it
with ``agg --font-family "Menlo,PingFang SC" demo.cast demo.gif``.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import shutil
import socket
import sys
import tempfile
import textwrap
import time
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Same isolation as tests/conftest.py: never touch a browser, never mint tickets.
# Applied in main() only, so importing this module has no side effects.
ISOLATION_ENV = {
    "PORTAL_JD_BROWSER": "0",
    "JOBSFLOW_JOBSDB_CHROME_USER_DATA_DIR": "/nonexistent/jobsflow-demo-chrome",
    "JOBSFLOW_GATEWAY_ACTIVE": "1",
    "JOBSFLOW_SOPCONTROL_ALLOW_RELAX": "1",
    "JOBSFLOW_SOPCONTROL_TEST": "1",
    "JOBSFLOW_SOPCONTROL_MODE": "off",
    "JOBSFLOW_SOPCONTROL_TICKETS": "off",
}

JOB_ID = "C0-001"
TAIL = "vendor contracts for a payments team and translated findings into accurate operational checklists and reliable stakeholder follow-up."
DRAFTS = {
    "led": ("Led", f"Led the review of {TAIL}"),
    "managed": ("Managed", f"Managed the review of {TAIL}"),
    "drafted": ("Drafted", f"Drafted and reviewed {TAIL}"),
}

TEXT = {
    "zh": {
        "title": "JobsFlow · 帮你改简历的 AI，不许替你吹牛",
        "setup": [
            ("合成候选人", "Test Candidate → Acme 律所 Paralegal 岗位"),
            ("已核实事实", "Reviewed vendor contracts for a payments team."),
            ("JD 要求", "Draft and review vendor contracts ..."),
        ],
        "honest": "判断全部由确定性规则完成：不调用模型、不联网、不开浏览器。",
        "draft": "AI 草稿",
        "s1": "1. AI 照抄基础版用词",
        "s1_ok": "✔ 通过 → 交给独立审计",
        "s2": "2. AI 把「审阅」写成「主导」",
        "s2_no": "✖ 拦下：verb_escalation（lead 超出已核实事实）",
        "s2_hint": "建议：改回基础版用词；如果你确实主导过，可以只为这个岗位确认。",
        "s3": "3. 换成 Managed 也一样",
        "s3_no": "✖ 拦下：manage 属于夸大层",
        "s4": "4. 职能范围内的换词",
        "s4_ok": "✔ 放行：draft 出现在这个岗位的 JD 里",
        "s5": "5. 你确认「Led 这件事我确实做过」",
        "s5_ok": "✔ 只对 {job} 有效，JD 变更或草稿重置即失效",
        "s5_hash": "基础版与事实库未改动：{before} → {after}",
        "end": [
            "规则写在代码里，不靠提示词自觉。",
            "github.com/mixxmax/jobsflow",
        ],
        "fail": "演示结果与预期不符：",
    },
    "zh-hant": {
        "title": "JobsFlow · 幫你改簡歷的 AI，不許替你吹牛",
        "setup": [
            ("合成候選人", "Test Candidate → Acme 律所 Paralegal 崗位"),
            ("已核實事實", "Reviewed vendor contracts for a payments team."),
            ("JD 要求", "Draft and review vendor contracts ..."),
        ],
        "honest": "判斷全部由確定性規則完成：不調用模型、不聯網、不開瀏覽器。",
        "draft": "AI 草稿",
        "s1": "1. AI 照抄基礎版用詞",
        "s1_ok": "✔ 通過 → 交給獨立審計",
        "s2": "2. AI 把「審閱」寫成「主導」",
        "s2_no": "✖ 攔下：verb_escalation（lead 超出已核實事實）",
        "s2_hint": "建議：改回基礎版用詞；如果你確實主導過，可以只為這個崗位確認。",
        "s3": "3. 換成 Managed 也一樣",
        "s3_no": "✖ 攔下：manage 屬於誇大層",
        "s4": "4. 職能範圍內的換詞",
        "s4_ok": "✔ 放行：draft 出現在這個崗位的 JD 裡",
        "s5": "5. 你確認「Led 這件事我確實做過」",
        "s5_ok": "✔ 只對 {job} 有效，JD 變更或草稿重置即失效",
        "s5_hash": "基礎版與事實庫未改動：{before} → {after}",
        "end": [
            "規則寫在代碼裡，不靠提示詞自覺。",
            "github.com/mixxmax/jobsflow",
        ],
        "fail": "演示結果與預期不符：",
    },
    "en": {
        "title": "JobsFlow · the AI that tailors your CV is not allowed to inflate it",
        "setup": [
            ("Candidate", "Test Candidate (synthetic) → Paralegal at Acme"),
            ("Verified fact", "Reviewed vendor contracts for a payments team."),
            ("The JD asks", "Draft and review vendor contracts ..."),
        ],
        "honest": "Every decision is a deterministic rule: no model call, no network, no browser.",
        "draft": "AI draft",
        "s1": "1. The AI keeps the baseline wording",
        "s1_ok": "✔ Passes → handed to the independent audit",
        "s2": "2. The AI turns \"reviewed\" into \"led\"",
        "s2_no": "✖ Blocked: verb_escalation (lead goes beyond the verified fact)",
        "s2_hint": "Fix: return to the baseline wording, or confirm it for this job only.",
        "s3": "3. \"Managed\" is caught the same way",
        "s3_no": "✖ Blocked: manage is an inflation verb",
        "s4": "4. A change that stays within the role",
        "s4_ok": "✔ Passes: \"draft\" appears in this job's JD",
        "s5": "5. You confirm you really led it",
        "s5_ok": "✔ Valid for {job} only, until the JD changes or the draft is reset",
        "s5_hash": "Baseline CV and fact base untouched: {before} → {after}",
        "end": [
            "The rules live in code, not in a prompt the model may ignore.",
            "github.com/mixxmax/jobsflow",
        ],
        "fail": "Demo decision differs from the expected one:",
    },
}

BOLD, DIM, RESET = "\x1b[1m", "\x1b[2m", "\x1b[0m"
GREEN, RED, YELLOW, CYAN = "\x1b[1;32m", "\x1b[1;31m", "\x1b[1;33m", "\x1b[36m"


class Screen:
    """Print to the terminal and optionally record an asciicast timeline."""

    def __init__(self, pause: float, width: int = 92, height: int = 12) -> None:
        self.pause = pause
        self.width = width
        self.height = height
        self.clock = 0.0
        self.events: list[list] = []

    def out(self, text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()
        self.events.append([round(self.clock, 3), "o", text.replace("\n", "\r\n")])

    def line(self, text: str = "") -> None:
        self.out(text + "\n")

    def wait(self, seconds: float) -> None:
        self.clock += seconds
        if self.pause:
            time.sleep(seconds * self.pause)

    def clear(self) -> None:
        self.out("\x1b[2J\x1b[H")

    def type(self, command: str) -> None:
        self.out(f"{GREEN}${RESET} ")
        for char in command:
            self.out(char)
            self.wait(0.03)
        self.line()

    def save_cast(self, path: Path) -> None:
        header = {
            "version": 2,
            "width": self.width,
            "height": self.height,
            "title": "JobsFlow 60-second demo",
            "env": {"TERM": "xterm-256color", "SHELL": "/bin/zsh"},
        }
        lines = [json.dumps(header)] + [json.dumps(event, ensure_ascii=False) for event in self.events]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _deny_network() -> None:
    """Refuse outbound connections from this process (local sockets still work)."""

    original = socket.socket.connect

    def connect(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6) and address[0] not in ("127.0.0.1", "::1", "localhost"):
            raise OSError(f"demo is offline; refused connection to {address[0]}")
        return original(self, address)

    socket.socket.connect = connect


@contextmanager
def _quiet():
    """Silence engine and LibreOffice chatter at the file-descriptor level."""

    sys.stdout.flush()
    sys.stderr.flush()
    saved = os.dup(1), os.dup(2)
    devnull = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
        yield
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        os.dup2(saved[0], 1)
        os.dup2(saved[1], 2)
        for fd in (*saved, devnull):
            os.close(fd)


def _digest(paths: list[Path]) -> str:
    sha = hashlib.sha256()
    for path in paths:
        sha.update(path.read_bytes())
    return sha.hexdigest()[:12]


class DemoMismatch(RuntimeError):
    pass


def _expect(condition: bool, what: str) -> None:
    if not condition:
        raise DemoMismatch(what)


def _draft_block(screen: Screen, label: str, text: str, verb: str) -> None:
    lines = textwrap.wrap(text, width=screen.width - 6)
    screen.line(f"  {DIM}{label}{RESET}")
    for index, chunk in enumerate(lines):
        if index == 0:
            chunk = chunk.replace(verb, f"{YELLOW}{verb}{RESET}", 1)
        screen.line(f"  │ {chunk}")


def run(lang: str, screen: Screen, keep: bool) -> int:
    from tools.workflow.engine import dispatch
    from tools.workflow.testing_packages import baseline_transform_fixture, build_package, build_workspace

    t = TEXT[lang]
    root = Path(tempfile.mkdtemp(prefix="jobsflow-demo-"))
    try:
        with _quiet():
            ws = build_workspace(root)
            package = build_package(ws, with_outbound=False)
            plan = json.loads((package / "materials_plan.validated.json").read_text(encoding="utf-8"))
            planned = dispatch("materials", workspace=ws, payload={"job_id": JOB_ID, "model_plan": plan})
        _expect(planned.get("status") == "succeeded", f"plan: {planned.get('status')}")
        transform = baseline_transform_fixture(package)
        baseline_text = next(item for item in transform["changes"] if item["material"] == "cv")["text"]
        protected = [
            ws / "00_Profile" / "fact_evidence.json",
            ws / "01_Masters" / "C_track" / "master_C_test_v1.docx",
            ws / "01_Masters" / "C_track" / "cl_master_C_test_v1.docx",
        ]
        before = _digest(protected)

        def submit(text: str) -> dict:
            draft = copy.deepcopy(transform)
            next(item for item in draft["changes"] if item["material"] == "cv")["text"] = text
            with _quiet():
                return dispatch("materials", workspace=ws, payload={"job_id": JOB_ID, "canonical_draft": draft})

        def verbs(out: dict) -> list[str]:
            return [verb for option in out.get("claim_confirmation_options") or [] for verb in option["verbs"]]

        screen.clear()
        screen.type("python3 scripts/demo.py")
        screen.wait(0.4)
        screen.line()
        screen.line(f"{BOLD}{t['title']}{RESET}")
        screen.line()
        for key, value in t["setup"]:
            screen.line(f"  {CYAN}{key}{RESET}  {value}")
        screen.line()
        screen.line(f"  {DIM}{t['honest']}{RESET}")
        screen.wait(7.5)

        # 1. Baseline wording passes.
        out = submit(baseline_text)
        _expect(out.get("status") == "succeeded", f"baseline: {out.get('status')} {out.get('blockers')}")
        screen.clear()
        screen.line(f"{BOLD}{t['s1']}{RESET}")
        screen.line()
        _draft_block(screen, t["draft"], baseline_text, "reviewed")
        screen.wait(1.2)
        screen.line()
        screen.line(f"  {GREEN}{t['s1_ok']}{RESET}")
        screen.wait(6.5)

        # 2. "Led" is an inflation verb: blocked with a baseline-first repair.
        verb, text = DRAFTS["led"]
        out = submit(text)
        _expect(out.get("status") == "blocked" and "verb_escalation" in (out.get("blockers") or []), f"led: {out.get('status')}")
        _expect(verbs(out) == ["lead"], f"led verbs: {verbs(out)}")
        screen.clear()
        screen.line(f"{BOLD}{t['s2']}{RESET}")
        screen.line()
        _draft_block(screen, t["draft"], text, verb)
        screen.wait(1.2)
        screen.line()
        screen.line(f"  {RED}{t['s2_no']}{RESET}")
        screen.line(f"  {DIM}{t['s2_hint']}{RESET}")
        screen.wait(9.0)

        # 3. "Managed" sits in the same inflation tier.
        verb, text = DRAFTS["managed"]
        out = submit(text)
        _expect(out.get("status") == "blocked" and verbs(out) == ["manage"], f"managed: {out.get('status')} {verbs(out)}")
        screen.clear()
        screen.line(f"{BOLD}{t['s3']}{RESET}")
        screen.line()
        _draft_block(screen, t["draft"], text, verb)
        screen.wait(1.2)
        screen.line()
        screen.line(f"  {RED}{t['s3_no']}{RESET}")
        screen.wait(6.0)

        # 4. A function verb from this job's JD passes.
        verb, text = DRAFTS["drafted"]
        out = submit(text)
        _expect(out.get("status") == "succeeded", f"drafted: {out.get('status')} {out.get('blockers')}")
        screen.clear()
        screen.line(f"{BOLD}{t['s4']}{RESET}")
        screen.line()
        _draft_block(screen, t["draft"], text, verb)
        screen.wait(1.2)
        screen.line()
        screen.line(f"  {GREEN}{t['s4_ok']}{RESET}")
        screen.wait(7.0)

        # 5. The user confirms "led" for this job only; nothing shared changes.
        out = submit(DRAFTS["led"][1])
        options = out.get("claim_confirmation_options") or []
        _expect(len(options) == 1, f"confirm options: {len(options)}")
        with _quiet():
            confirmed = dispatch(
                "materials",
                workspace=ws,
                payload={"job_id": JOB_ID, "stage": "confirm_claim", "block_id": options[0]["block_id"]},
            )
        claim = confirmed.get("claim_confirmation") or {}
        _expect(claim.get("status") == "succeeded", f"confirm: {claim.get('status')}")
        _expect(claim.get("valid_for") == "this_job_until_draft_reset_or_jd_change", f"scope: {claim.get('valid_for')}")
        _expect("verb_escalation" not in (confirmed.get("blockers") or []), "confirm left the block in place")
        after = _digest(protected)
        _expect(after == before, "baseline or fact base changed")
        screen.clear()
        screen.line(f"{BOLD}{t['s5']}{RESET}")
        screen.line()
        screen.type(f"python3 -m tools.workflow materials confirm-claim --job-id {JOB_ID} \\\n      --block-id {options[0]['block_id']}")
        screen.wait(1.0)
        screen.line()
        screen.line(f"  {GREEN}{t['s5_ok'].format(job=JOB_ID)}{RESET}")
        screen.line(f"  {DIM}{t['s5_hash'].format(before=before, after=after)}{RESET}")
        screen.wait(8.0)

        screen.clear()
        screen.line()
        screen.line(f"  {BOLD}{t['end'][0]}{RESET}")
        screen.line()
        screen.line(f"  {CYAN}{t['end'][1]}{RESET}")
        screen.wait(5.0)
        return 0
    except DemoMismatch as exc:
        print(f"\n{RED}{t['fail']}{RESET} {exc}", file=sys.stderr)
        return 1
    finally:
        if keep:
            print(f"\nworkspace kept: {root}", file=sys.stderr)
        else:
            shutil.rmtree(root, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--lang", choices=sorted(TEXT), default="zh")
    parser.add_argument("--pause", type=float, default=None, help="scale for on-screen pauses (0 = none)")
    parser.add_argument("--cast", type=Path, help="also write an asciicast v2 recording here")
    parser.add_argument("--keep", action="store_true", help="keep the temporary workspace")
    args = parser.parse_args(argv)
    pause = args.pause if args.pause is not None else (0.0 if args.cast else 1.0)
    os.environ.update(ISOLATION_ENV)
    _deny_network()
    screen = Screen(pause)
    code = run(args.lang, screen, args.keep)
    if code == 0 and args.cast:
        screen.save_cast(args.cast)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
