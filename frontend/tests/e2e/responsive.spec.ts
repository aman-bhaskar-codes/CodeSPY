import { expect, test } from "@playwright/test";

const widths = [320, 390, 768, 1440] as const;
const themes = ["dark", "light"] as const;

for (const width of widths) {
  for (const theme of themes) {
    test(`responsive layout at ${width}px (${theme} theme) with long text and reflow`, async ({
      page,
    }) => {
      await page.setViewportSize({ width, height: 900 });

      const longRepoName =
        "codespy-very-long-repository-name-that-exceeds-standard-column-bounds-and-needs-to-wrap-properly";
      const repoId = "20000000-0000-4000-8000-000000000001";
      const snapId = "20000000-0000-4000-8000-000000000002";
      const findingId = "20000000-0000-4000-8000-000000000003";
      const scanJob = "20000000-0000-4000-8000-000000000007";
      const runId = "20000000-0000-4000-8000-000000000005";
      const invId = "20000000-0000-4000-8000-000000000004";

      const longPath =
        "src/very/deeply/nested/directory/structure/with/extremely/long/path/to/some/complex/security/authentication/handler/module/FileWithVeryLongName.tsx";

      await page.route("**/api/v1/**", async (route) => {
        const url = new URL(route.request().url());
        const path = url.pathname;

        if (path.endsWith("/session")) {
          return route.fulfill({
            json: {
              principal_id: "fixture-user",
              csrf_token: "fixture-token",
              auth_backend: "local_fixture",
            },
          });
        }

        if (path.endsWith("/repositories")) {
          return route.fulfill({
            json: {
              items: [
                {
                  id: repoId,
                  org_id: "org",
                  display_name: longRepoName,
                  source_kind: "archive",
                },
              ],
              next_cursor: null,
            },
          });
        }

        if (path.endsWith(`/scan-jobs/${scanJob}`))
          return route.fulfill({
            json: { id: scanJob, state: "succeeded", result_run_id: runId },
          });
        if (path.endsWith(`/scans/${runId}/summary`))
          return route.fulfill({
            json: {
              scan_id: runId,
              repository_id: repoId,
              snapshot_id: snapId,
              status: "completed",
              total_findings: 1,
              by_category: { source_pattern: 1 },
              by_severity: { high: 1 },
              by_disposition: { unresolved: 1 },
              by_triage: { unreviewed: 1 },
              created_at: null,
            },
          });

        if (path.endsWith("/findings")) {
          return route.fulfill({
            json: {
              items: [
                {
                  id: findingId,
                  run_id: "20000000-0000-4000-8000-000000000005",
                  rule_id:
                    "python.very-long-rule-identifier-for-security-injection-detection",
                  category: "source_pattern",
                  severity: "high",
                  title:
                    "Extremely long finding title describing an intricate command injection path across multiple modules",
                  file_path: longPath,
                  line_number: 42,
                  disposition: "unresolved",
                  triage_state: "unreviewed",
                  triage_version: 1,
                  triage_reason: "none",
                },
              ],
              next_cursor: null,
              current_support_available: true,
            },
          });
        }

        if (path.endsWith(`/findings/${findingId}`)) {
          return route.fulfill({
            json: {
              id: findingId,
              run_id: "20000000-0000-4000-8000-000000000005",
              rule_id:
                "python.very-long-rule-identifier-for-security-injection-detection",
              category: "source_pattern",
              severity: "high",
              title:
                "Extremely long finding title describing an intricate command injection path across multiple modules",
              file_path: longPath,
              line_number: 42,
              disposition: "unresolved",
              triage: {
                current_state: "unreviewed",
                history: [],
              },
              source_available: true,
              omission_details: [
                "very_long_omission_reason_explaining_why_certain_tokens_were_bounded",
              ],
            },
          });
        }

        if (path.endsWith("/memory-review")) {
          return route.fulfill({
            json: {
              items: [
                {
                  memory_id: "20000000-0000-4000-8000-000000000006",
                  org_id: "org",
                  repository_id: repoId,
                  snapshot_id: snapId,
                  memory_kind: "architecture_fact",
                  topic:
                    "Multimodal and distributed architecture design considerations for secure data processing",
                  summary:
                    "This is an extensive multiline proposal summary detailing how components interface with external services and how authentication invariants are preserved.\nIt contains multiple lines of notes and structural references.\nAll tokens must reflow without causing document-level horizontal overflow.",
                  state: "candidate",
                  version: 1,
                  evidence_pointers: [
                    {
                      file_path: longPath,
                      start_line: 10,
                      end_line: 50,
                    },
                  ],
                  created_at: "2026-10-28T00:00:00Z",
                  updated_at: "2026-10-28T00:00:00Z",
                },
              ],
              can_review: true,
              next_cursor: null,
            },
          });
        }

        if (path.endsWith("/memories")) {
          return route.fulfill({
            json: {
              org_id: "org",
              repository_id: repoId,
              query: "multiline",
              items: [],
              total_candidates: 1,
            },
          });
        }

        if (path.endsWith("/investigations")) {
          return route.fulfill({
            json: {
              items: [
                {
                  id: invId,
                  repository_id: repoId,
                  snapshot_id: snapId,
                  status: "completed",
                  query:
                    "Investigate whether long filenames cause overflow in the investigation result panel",
                  verdict: "true_positive",
                  summary:
                    "Verified that text wraps properly and scroll regions contain table contents.",
                  current_step: 5,
                  max_steps: 10,
                  spent_micros: 250000,
                  held_micros: 0,
                  budget_limit_micros: 1000000,
                  spec_hash: "b".repeat(64),
                  created_at: "2026-10-28T00:00:00Z",
                },
              ],
              total_count: 1,
              next_cursor: "next-inv-page-token",
            },
          });
        }

        if (path.endsWith(`/investigations/${invId}`)) {
          return route.fulfill({
            json: {
              summary: {
                id: invId,
                repository_id: repoId,
                snapshot_id: snapId,
                status: "completed",
                query:
                  "Investigate whether long filenames cause overflow in the investigation result panel",
                verdict: "true_positive",
                summary:
                  "Verified that text wraps properly and scroll regions contain table contents.",
                current_step: 5,
                max_steps: 10,
                spent_micros: 250000,
                held_micros: 0,
                budget_limit_micros: 1000000,
                spec_hash: "b".repeat(64),
                created_at: "2026-10-28T00:00:00Z",
              },
              available_actions: ["retry"],
              current_support_available: true,
              report: {
                claims: [
                  {
                    claim_id: "claim-1",
                    statement:
                      "A very long security observation claim statement demonstrating graceful text wrapping in narrow viewports",
                  },
                ],
                verifications: [
                  {
                    claim_id: "claim-1",
                    status: "source_correspondence_confirmed",
                    resolved_file: longPath,
                    resolved_line_start: 42,
                    resolved_line_end: 45,
                    notes: "Verified at publication line boundary.",
                  },
                ],
                coverage_notes: ["Full coverage achieved without overflow."],
              },
            },
          });
        }

        if (path.endsWith("/events")) {
          return route.fulfill({
            json: {
              items: [
                {
                  id: "evt-1",
                  event_type: "step_started",
                  timestamp: "2026-10-28T00:00:01Z",
                  payload: { step: 1, note: "Initial step started" },
                },
              ],
              next_cursor: 1,
            },
          });
        }

        if (path.endsWith("/models")) {
          return route.fulfill({
            json: { profiles: [], live_access_verified: false },
          });
        }

        return route.fulfill({
          status: 503,
          json: { code: "fixture_unavailable" },
        });
      });

      // 1. Visit overview
      await page.goto(
        `/?repository=${repoId}&snapshot=${snapId}#workspace-home`,
      );
      if (theme === "light") {
        await page
          .getByRole("button", { name: "Switch to light theme" })
          .click();
      }
      await expect(page.locator("html")).toHaveAttribute("data-theme", theme);

      // Check document-wide scrollWidth
      const overflowHome = await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      );
      expect(overflowHome).toBe(true);

      // 2. Visit Findings pane
      await page.goto(
        `/?repository=${repoId}&snapshot=${snapId}&scan=${scanJob}#findings-title`,
      );
      await expect(
        page.getByText("Extremely long finding title").first(),
      ).toBeVisible();

      const overflowFindings = await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      );
      expect(overflowFindings).toBe(true);

      // 3. Visit Search & Memory pane
      await page.goto(
        `/?repository=${repoId}&snapshot=${snapId}#retrieval-title`,
      );
      await page.getByRole("tab", { name: "Memory", exact: false }).click();
      await expect(
        page.getByRole("heading", {
          name: "Multimodal and distributed architecture",
          exact: false,
        }),
      ).toBeVisible();

      const overflowMemory = await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      );
      const overflowingMemory = await page.evaluate(() =>
        [...document.querySelectorAll("main *")]
          .filter(
            (element) => element.getBoundingClientRect().right > innerWidth + 1,
          )
          .slice(0, 12)
          .map((element) => ({
            tag: element.tagName,
            className: element.className,
            text: element.textContent?.slice(0, 90),
            width: element.getBoundingClientRect().width,
          })),
      );
      expect(overflowMemory, JSON.stringify(overflowingMemory)).toBe(true);

      // 4. Visit Investigations pane
      await page.goto(
        `/?repository=${repoId}&snapshot=${snapId}#workbench-title`,
      );
      await page
        .getByRole("button", { name: /Investigate whether long filenames/ })
        .click();
      await expect(
        page.getByText("A very long security observation claim statement"),
      ).toBeVisible();

      const overflowInvestigation = await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      );
      expect(overflowInvestigation).toBe(true);

      // Essential controls remain clickable and visible
      await expect(
        page.getByRole("link", { name: "CodeSPY home" }),
      ).toBeVisible();
      await expect(
        page.getByRole("navigation", { name: "Workspace sections" }),
      ).toBeVisible();
    });
  }
}
