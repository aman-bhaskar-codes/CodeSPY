import { expect, test } from "@playwright/test";
import { randomUUID } from "node:crypto";

test("published source index survives context assembly and reload", async ({ page }) => {
  test.setTimeout(150_000);
  const repository = process.env.CODESPY_E2E_REPOSITORY;
  const snapshot = process.env.CODESPY_E2E_SNAPSHOT;
  test.skip(!repository || !snapshot, "Owned real-backend fixture required");
  const prefix = `/api/v1/repositories/${repository}/snapshots/${snapshot}`;
  await page.goto(`/?snapshot=${snapshot}`);
  await page.getByLabel("Repository", { exact: true }).selectOption(repository!);
  await expect(page.getByRole("heading", { name: "Source index", exact: true })).toBeVisible();
  const createdResponse = page.waitForResponse(
    (response) => response.url().endsWith(`${prefix}/indexes`) && response.request().method() === "POST",
  );
  await page.getByRole("button", { name: "Build source index", exact: true }).click();
  const created = await createdResponse;
  expect(created.status()).toBe(202);
  const job = (await created.json()).job;
  await expect(page.getByText("Build status: succeeded", { exact: false })).toBeVisible({ timeout: 90_000 });
  const status = await page.request.get(`${prefix}/index-jobs/${job.id}`);
  expect(status.ok()).toBeTruthy();
  const published = await status.json();
  expect(published.receipt.chunk_set_id).toBeTruthy();

  await page.getByPlaceholder("Search code, symbols, or topics...").fill("browser_index_marker");
  const searchResponse = page.waitForResponse((response) => response.url().endsWith("/search/hybrid"));
  await page.getByRole("button", { name: "Search", exact: true }).click();
  const search = await (await searchResponse).json();
  expect(search.chunk_set_id).toBe(published.receipt.chunk_set_id);
  expect(search.hits.length).toBeGreaterThan(0);
  expect(JSON.stringify(search)).not.toContain(process.env.CODESPY_E2E_CANARY!);
  const contextResponse = page.waitForResponse((response) => response.url().endsWith("/context/assemble"));
  await page.getByRole("button", { name: "Pack Top Hits into Context Packet →", exact: true }).click();
  const context = await (await contextResponse).json();
  expect(context.chunk_set_id).toBe(search.chunk_set_id);
  expect(context.items_count).toBeGreaterThan(0);
  expect(context.items[0].file_path).toBe("a.py");
  expect(JSON.stringify(context)).not.toContain(process.env.CODESPY_E2E_CANARY!);

  await page.reload();
  await page.getByLabel("Repository", { exact: true }).selectOption(repository!);
  await expect(page.getByText(published.receipt.chunk_set_id, { exact: true }).first()).toBeVisible();
  const secondResponse = page.waitForResponse(
    (response) => response.url().endsWith(`${prefix}/indexes`) && response.request().method() === "POST",
  );
  await page.getByRole("button", { name: "Build source index", exact: true }).click();
  const queued = await (await secondResponse).json();
  await expect(page.getByRole("button", { name: "Cancel index build", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Cancel index build", exact: true }).click();
  await page.getByRole("group", { name: "Confirm index build cancellation" })
    .getByRole("button", { name: "Confirm cancellation", exact: true }).click();
  await expect(page.getByText("Build status: cancelled", { exact: false })).toBeVisible();
  const cancelled = await page.request.get(`${prefix}/index-jobs/${queued.job.id}`);
  expect((await cancelled.json()).job.state).toBe("cancelled");
  const foreign = await page.request.get(
    `/api/v1/repositories/${randomUUID()}/snapshots/${snapshot}/indexes`,
  );
  expect([403, 404]).toContain(foreign.status());
  const mismatched = await page.request.get(
    `/api/v1/repositories/${repository}/snapshots/${randomUUID()}/index-jobs/${job.id}`,
  );
  expect([403, 404]).toContain(mismatched.status());
});
