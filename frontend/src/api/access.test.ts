import { afterEach, expect, it, vi } from "vitest";
import { invalidateAccessResponses, request } from "./client";
afterEach(() => vi.unstubAllGlobals());
it("rejects a late private response after workspace access changes", async () => {
  let resolve!: (response: Response) => void;
  vi.stubGlobal(
    "fetch",
    vi.fn(
      () =>
        new Promise<Response>((done) => {
          resolve = done;
        }),
    ),
  );
  const result = request("/private-fixture");
  invalidateAccessResponses();
  resolve(
    new Response(JSON.stringify({ summary: "Private report" }), {
      status: 200,
    }),
  );
  await expect(result).rejects.toMatchObject({
    code: "access_context_changed",
  });
});
it("accepts a request initiated after the new access boundary", async () => {
  invalidateAccessResponses();
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response(JSON.stringify({ id: "new-session" }))),
  );
  await expect(request("/private-fixture")).resolves.toEqual({
    id: "new-session",
  });
});
