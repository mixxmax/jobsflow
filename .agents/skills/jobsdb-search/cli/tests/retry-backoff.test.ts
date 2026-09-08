import { afterEach, describe, expect, test } from "bun:test";
import { searchGet } from "../src/helpers";

const originalFetch = globalThis.fetch;
const originalSetTimeout = globalThis.setTimeout;
const originalJobsdbCookie = process.env.JOBSDB_COOKIE;

afterEach(() => {
  globalThis.fetch = originalFetch;
  globalThis.setTimeout = originalSetTimeout;
  if (originalJobsdbCookie === undefined) delete process.env.JOBSDB_COOKIE;
  else process.env.JOBSDB_COOKIE = originalJobsdbCookie;
});

function instantTimers() {
  globalThis.setTimeout = ((fn: () => void) =>
    originalSetTimeout(fn, 0)) as unknown as typeof setTimeout;
}

function stubFetch(responses: Array<() => Response>): { calls: number } {
  const state = { calls: 0 };
  globalThis.fetch = (async () => {
    const i = Math.min(state.calls, responses.length - 1);
    state.calls++;
    return responses[i]();
  }) as unknown as typeof fetch;
  return state;
}

describe("searchGet retry/backoff", () => {
  test("can disable the listing cookie bridge for structured detail lookups", async () => {
    let observedHeaders: HeadersInit | undefined;
    process.env.JOBSDB_COOKIE = "cf_clearance=must-not-cross-detail-boundary";
    globalThis.fetch = (async (_input, init) => {
      observedHeaders = init?.headers;
      return new Response('{"data":[]}', { status: 200 });
    }) as unknown as typeof fetch;

    await searchGet(
      { siteKey: "HK-Main", jobid: "123", page: "1", pageSize: "5" },
      { includeSearchCookie: false },
    );

    const headers = new Headers(observedHeaders);
    expect(headers.has("Cookie")).toBe(false);
    expect(JSON.stringify(observedHeaders)).not.toContain("must-not-cross-detail-boundary");
  });

  test("keeps the cookie bridge opt-in for listing compatibility", async () => {
    let observedHeaders: HeadersInit | undefined;
    process.env.JOBSDB_COOKIE = "listing_cookie=allowed-only-for-search";
    globalThis.fetch = (async (_input, init) => {
      observedHeaders = init?.headers;
      return new Response('{"data":[]}', { status: 200 });
    }) as unknown as typeof fetch;

    await searchGet({ keywords: "operations" }, { includeSearchCookie: true });

    const headers = new Headers(observedHeaders);
    expect(headers.get("Cookie")).toBe("listing_cookie=allowed-only-for-search");
  });

  test("does not attach the cookie bridge by default", async () => {
    let observedHeaders: HeadersInit | undefined;
    process.env.JOBSDB_COOKIE = "must-not-be-inherited-by-new-callers";
    globalThis.fetch = (async (_input, init) => {
      observedHeaders = init?.headers;
      return new Response('{"data":[]}', { status: 200 });
    }) as unknown as typeof fetch;

    await searchGet({ keywords: "operations" });

    const headers = new Headers(observedHeaders);
    expect(headers.has("Cookie")).toBe(false);
    expect(JSON.stringify(observedHeaders)).not.toContain(
      "must-not-be-inherited-by-new-callers",
    );
  });

  test("retries a 429 and succeeds on the next attempt", async () => {
    instantTimers();
    const state = stubFetch([
      () => new Response('{"data":[]}', { status: 429 }),
      () => new Response('{"data":[],"totalCount":0}', { status: 200 }),
    ]);

    const result = await searchGet({ keywords: "lawyer" });
    expect(result.data).toEqual([]);
    expect(state.calls).toBe(2);
  });

  test("throws on 404 without retrying", async () => {
    const state = stubFetch([() => new Response("", { status: 404 })]);

    await expect(searchGet({ keywords: "lawyer" })).rejects.toThrow(/404/);
    expect(state.calls).toBe(1);
  });

  test("gives up after the initial attempt plus six retries on persistent 5xx", async () => {
    instantTimers();
    const state = stubFetch([() => new Response("", { status: 500 })]);

    await expect(searchGet({ keywords: "lawyer" })).rejects.toThrow(/500/);
    expect(state.calls).toBe(7);
  });
});
