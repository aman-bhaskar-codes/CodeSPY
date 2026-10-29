import { expect, test } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

for (const width of [390, 1280]) {
  test(`memory review keyboard flow at ${width}px with mocked HTTP`, async ({
    page,
  }) => {
    await page.setViewportSize({ width, height: 900 });
    const repository = "00000000-0000-0000-0000-000000000001";
    const snapshot = "00000000-0000-0000-0000-000000000002";
    let state = "candidate";
    const entry = () => ({
      memory_id: "00000000-0000-0000-0000-000000000003",
      org_id: "org",
      repository_id: repository,
      snapshot_id: snapshot,
      memory_kind: "architecture_fact",
      topic: "Scoped routing",
      summary: "Human note about repository routing",
      state,
      version: state === "candidate" ? 1 : 2,
      evidence_pointers: [],
      created_at: "2026-10-26T00:00:00Z",
      updated_at: "2026-10-26T00:00:00Z",
    });
    await page.route("**/api/v1/**", async (route) => {
      const url = new URL(route.request().url());
      if (url.pathname.endsWith("/session"))
        return route.fulfill({
          json: {
            principal_id: "principal",
            csrf_token: "synthetic-csrf",
            auth_backend: "local_fixture",
          },
        });
      if (url.pathname.endsWith("/repositories"))
        return route.fulfill({
          json: {
            items: [
              {
                id: repository,
                org_id: "org",
                display_name: "Owned UI fixture",
                source_kind: "archive",
              },
            ],
            next_cursor: null,
          },
        });
      if (url.pathname.endsWith("/memory-review"))
        return route.fulfill({
          json: { items: [entry()], can_review: true, next_cursor: null },
        });
      if (url.pathname.endsWith("/reviews")) {
        const body = route.request().postDataJSON();
        expect(body.expected_version).toBe(1);
        expect(body.reason).toBe("Checked source");
        state = "accepted";
        return route.fulfill({ json: entry() });
      }
      if (url.pathname.endsWith("/memories"))
        return route.fulfill({
          json: {
            org_id: "org",
            repository_id: repository,
            query: "memory",
            items: [],
            total_candidates: 0,
          },
        });
      if (url.pathname.endsWith("/investigations"))
        return route.fulfill({ json: { items: [], total_count: 0 } });
      return route.fulfill({
        status: 503,
        json: { code: "provider_unconfigured" },
      });
    });
    await page.goto(`/?repository=${repository}&snapshot=${snapshot}#retrieval-title`);
    await page
      .getByRole("tab", { name: "Memory", exact: false })
      .click();
    await page
      .getByRole("button", { name: "Accept Scoped routing", exact: true })
      .click();
    const reason = page.getByLabel("Review reason");
    await reason.fill("Checked source");
    await page
      .getByRole("button", { name: "Confirm accept", exact: true })
      .focus();
    await page.keyboard.press("Enter");
    await expect(
      page.getByText(
        "Review saved. Eligibility is checked again before reuse.",
      ),
    ).toBeVisible();
    await expect(
      page.getByRole("button", { name: "Archive Scoped routing", exact: true }),
    ).toBeVisible();
    const panel = page.locator(
      'section[aria-labelledby="memory-review-heading"]',
    );
    const audit = await new AxeBuilder({ page })
      .include('section[aria-labelledby="memory-review-heading"]')
      .withTags(["wcag2a", "wcag2aa"])
      .analyze();
    expect(audit.violations).toEqual([]);
    await expect(panel).toBeVisible();

    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    ).toBeTruthy();
  });
}
