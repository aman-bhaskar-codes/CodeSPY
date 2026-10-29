import { expect, test } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

for (const width of [390, 1440]) {
  for (const theme of ["dark", "light"] as const) {
    test(`${theme} investigation recovery at ${width}px and saved results with mocked HTTP`, async ({
      page,
    }) => {
      const repository = "10000000-0000-4000-8000-000000000001";
      const snapshot = "10000000-0000-4000-8000-000000000002";
      const investigation = "10000000-0000-4000-8000-000000000003";
      let status = "pending";
      let starts = 0;
      const summary = () => ({
        id: investigation,
        repository_id: repository,
        snapshot_id: snapshot,
        status,
        query: "Inspect source behavior",
        verdict: status === "completed" ? "indeterminate" : null,
        summary: "A retained source observation",
        current_step: 1,
        max_steps: 10,
        spent_micros: 150,
        held_micros: 0,
        budget_limit_micros: 1000000,
        spec_hash: "a".repeat(64),
        current_job_id: "10000000-0000-4000-8000-000000000004",
        created_at: "2026-10-28T00:00:00Z",
      });
      await page.route("**/api/v1/**", async (route) => {
        const path = new URL(route.request().url()).pathname;
        if (path.endsWith("/session"))
          return route.fulfill({
            json: {
              principal_id: "fixture",
              csrf_token: "fixture-only",
              auth_backend: "local_fixture",
            },
          });
        if (path.endsWith("/repositories"))
          return route.fulfill({
            json: {
              items: [
                {
                  id: repository,
                  display_name: "Example source",
                  source_kind: "archive",
                },
              ],
            },
          });
        if (path.endsWith("/models"))
          return route.fulfill({
            json: { profiles: [], live_access_verified: false },
          });
        if (path.endsWith("/stream"))
          return route.fulfill({
            status: 503,
            json: { code: "stream_unavailable" },
          });
        if (path.endsWith("/events"))
          return route.fulfill({ json: { items: [], next_cursor: null } });
        if (path.endsWith("/execute")) {
          starts++;
          status = "completed";
          return route.fulfill({
            json: {
              investigation_id: investigation,
              job_id: "job",
              status: "completed",
            },
          });
        }
        if (path.endsWith("/investigations"))
          return route.fulfill({
            json: { items: [summary()], total_count: 1, next_cursor: null },
          });
        if (path.endsWith(`/investigations/${investigation}`))
          return route.fulfill({
            json: {
              summary: summary(),
              available_actions:
                status === "pending" ? ["start", "cancel"] : ["retry"],
              current_support_available: true,
              report:
                status === "completed"
                  ? {
                      claims: [
                        { claim_id: "claim", statement: "Observed print call" },
                      ],
                      verifications: [
                        {
                          claim_id: "claim",
                          status: "source_correspondence_confirmed",
                          resolved_file: "a.py",
                          resolved_line_start: 1,
                        },
                      ],
                      evidence_refs: ["evidence"],
                      coverage_notes: "One file inspected",
                    }
                  : null,
            },
          });
        return route.fulfill({
          status: 503,
          json: { code: "fixture_unavailable" },
        });
      });
      await page.setViewportSize({ width, height: 900 });
      await page.goto(
        `/?repository=${repository}&snapshot=${snapshot}#workbench-title`,
      );
      if (theme === "light")
        await page
          .getByRole("button", { name: "Switch to light theme" })
          .click();
      await page
        .getByRole("button", { name: /Inspect source behavior/ })
        .click();
      await page
        .getByRole("button", { name: "Execute Investigation", exact: true })
        .click();
      await expect(page.getByText("Observed print call")).toBeVisible();
      await expect(
        page.getByText("SOURCE CORRESPONDENCE (AT PUBLICATION)"),
      ).toBeVisible();
      await page.reload();
      await expect(page.getByText("Observed print call")).toBeVisible();
      expect(starts).toBe(1);
      await expect(
        page.getByText(
          "Live access is not verified. Configuration status does not prove a provider call will succeed.",
        ),
      ).toBeVisible();
      const overflowing = await page.evaluate(() =>
        [...document.querySelectorAll("main *")]
          .filter(
            (element) => element.getBoundingClientRect().right > innerWidth + 1,
          )
          .slice(0, 25)
          .map((element) => ({
            tag: element.tagName,
            text: element.textContent?.slice(0, 60),
            class: element.className,
            width: element.getBoundingClientRect().width,
          })),
      );
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
        JSON.stringify(overflowing),
      ).toBe(true);
      const audit = await new AxeBuilder({ page })
        .withTags(["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"])
        .analyze();
      expect(audit.violations).toEqual([]);
      await page.screenshot({
        path: test.info().outputPath(`investigation-${theme}.png`),
        fullPage: true,
      });
    });
  }
}
