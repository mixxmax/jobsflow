import { describe, it, expect } from "bun:test";
import { runCLI, parseJSON, type CLIResult } from "./helpers.js";

const liveIt = process.env.LIVE_PORTAL_TESTS === "1" ? it : it.skip;

interface SearchOut {
  meta: { count: number; page: number; total: number | null };
  results: { id: string; title: string; company: string | null; location: string | null; date: string | null; url: string }[];
}

describe("jobsdb-search CLI", () => {
  it("prints help with --help", async () => {
    const r: CLIResult = await runCLI(["--help"]);
    expect(r.stdout).toContain("jobsdb-cli");
  });

  it("rejects unknown command with JSON error on stderr", async () => {
    const r = await runCLI(["frobnicate"]);
    expect(r.exitCode).toBe(1);
    expect(r.stderr).toContain("error");
  });

  it("rejects detail with no id", async () => {
    const r = await runCLI(["detail"]);
    expect(r.exitCode).toBe(1);
    expect(r.stderr).toContain("NO_ID");
  });

  it("rejects implicit detail lookups so full-JD work cannot bypass the gateway", async () => {
    const r = await runCLI(["detail", "93369834"]);
    expect(r.exitCode).toBe(2);
    expect(r.stderr).toContain("DETAIL_REQUIRES_GATEWAY");
  });

  liveIt("live search returns real results for a profession-neutral query", async () => {
    const r = await runCLI(["search", "-q", "operations", "--jobage", "30", "--limit", "5", "--format", "json"]);
    expect(r.exitCode).toBe(0);
    const out = parseJSON<SearchOut>(r);
    expect(out.meta.count).toBeGreaterThan(0);
    const first = out.results[0];
    expect(first.id).toMatch(/^\d+$/);
    expect(first.title.length).toBeGreaterThan(0);
    expect(first.url).toContain("hk.jobsdb.com/job/");
  });

  liveIt("live detail resolves a single job by id from search", async () => {
    const s = await runCLI(["search", "-q", "operations", "--limit", "1", "--format", "json"]);
    const out = parseJSON<SearchOut>(s);
    const id = out.results[0].id;
    const d = await runCLI(["detail", id, "--teaser-only", "--format", "json"]);
    expect(d.exitCode).toBe(0);
    const job = parseJSON<{ id: string; title: string }>(d);
    expect(job.id).toBe(id);
    expect(job.title.length).toBeGreaterThan(0);
  });
});
