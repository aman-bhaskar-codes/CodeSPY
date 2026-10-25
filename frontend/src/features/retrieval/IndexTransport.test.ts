import { afterEach, expect, it, vi } from "vitest";
import { api } from "../../api/client";
afterEach(() => vi.unstubAllGlobals());
it("sends only the command key and CSRF protection when starting or retrying an index", async () => {
  const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) });
  vi.stubGlobal("fetch", fetch);
  await api.createIndex("repo", "snapshot", "csrf", "stable-key");
  await api.retryIndex("repo", "snapshot", "job", "csrf", "retry-key");
  expect(fetch.mock.calls[0][0]).toBe(
    "/api/v1/repositories/repo/snapshots/snapshot/indexes",
  );
  expect(fetch.mock.calls[1][0]).toBe(
    "/api/v1/repositories/repo/snapshots/snapshot/index-jobs/job/retry",
  );
  for (const [position, key] of [
    [0, "stable-key"],
    [1, "retry-key"],
  ] as const) {
    const init = fetch.mock.calls[position][1];
    expect(JSON.parse(init.body)).toEqual({ request_key: key });
    expect(init.headers).toMatchObject({
      "X-CSRF-Token": "csrf",
      "Idempotency-Key": key,
    });
    expect(init.credentials).toBe("same-origin");
  }
});
