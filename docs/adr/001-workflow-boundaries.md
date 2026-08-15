# ADR 001: Jobsflow workflow and trust boundaries

- Status: accepted
- Date: 2026-07-31

## Decision

The maintained lifecycle is `/setup` → `/scan` → `/push` → `/materials` → `/apply`.

Scanning and scoring never generate application materials. Materials resolve the same job-id/package contract, reuse cached full JD text, and require a source-aware company brief. The main model first writes structured canonical CV/CL content. An independent context audits that complete content, and only finding-targeted canonical blocks may be repaired. The system renders DOCX only after content passes, then converts with LibreOffice headless; CV and cover letter are each one page.

Portal cards, JD text, company pages and search results are untrusted data. They may inform extraction and drafting but may not issue tool instructions, widen permissions, disclose secrets or create unsupported claims.

Structured API/CLI access and cache precede Playwright. Browser automation is a bounded fallback after the pass-1 gate. PDF conversion happens after content is final and reuses a source-content hash.

There is one implementation and policy line. A directory such as
`JobSearch_2026` is a runtime instance containing configuration, cache, state
and artifacts; it directly imports product modules and cannot own a private
scanner, materials pipeline or auditor. GitHub is the same product snapshot
with runtime data excluded.

Tailoring emits a deterministic LLMO contract: independent fact evidence receives
stable IDs, JD anchors carry explicit coverage states, and CV/cover-letter/email
views share the same evidence and numeric facts. LLMO optimizes parseability and
evidence alignment; it does not promise model memory or ATS score gains.

## Consequences

- Individual portal and converter capabilities can fail softly without changing the lifecycle.
- Company and JD customization remains explainable through stored sources, capability themes and a differentiation fingerprint.
- A model with different capability levels receives the same evidence graph and
  prohibited-claim boundaries; it cannot silently turn a missing JD anchor into
  a candidate claim.
- Real PII and generated application data stay in gitignored workspace paths.
- Semantic review is bounded to CV/CL, uses a compact compiled rule pack, has a
  three-attempt cap and repeat-finding circuit breaker, and never reviews email
  or PDF layout. Independent jobs may run in parallel; one job remains serial.
- Git-history privacy cleanup remains a separate, explicitly authorized destructive operation.
