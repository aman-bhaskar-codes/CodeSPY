import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";

test("workspace links preserve selected repository and local form input", async ({
  page,
}) => {
  const repository = "10000000-0000-4000-8000-000000000011";
  await page.unroute("**/api/v1/**");
  await page.route("**/api/v1/**", (route) =>
    route.fulfill({
      contentType: "application/json",
      body: JSON.stringify(
        route.request().url().endsWith("/session")
          ? {
              principal_id: "fixture-only",
              csrf_token: "fixture-only",
              auth_backend: "local_fixture",
            }
          : {
              items: [
                {
                  id: repository,
                  display_name: "Example source",
                  source_kind: "archive",
                },
              ],
              next_cursor: null,
            },
      ),
    }),
  );
  await page.goto(`/?repository=${repository}#upload-title`);
  await expect(page.getByRole("combobox", { name: "Repository" })).toHaveValue(
    repository,
  );
  await page
    .getByLabel("Source ZIP")
    .setInputFiles({
      name: "example.zip",
      mimeType: "application/zip",
      buffer: Buffer.from("fixture-only"),
    });
  await page.getByRole("link", { name: "CodeSPY home" }).click();
  await expect(page).toHaveURL(
    new RegExp(`repository=${repository}#workspace-home$`),
  );
  await page.getByRole("link", { name: "Add codebase" }).click();
  expect(
    await page
      .getByLabel("Source ZIP")
      .evaluate((input: HTMLInputElement) => input.files?.[0]?.name),
  ).toBe("example.zip");
  await page.goBack();
  await expect(page).toHaveURL(/#workspace-home$/);
  await page.goForward();
  await expect(page).toHaveURL(/#upload-title$/);
  await page.reload();
  await expect(page.getByRole("combobox", { name: "Repository" })).toHaveValue(
    repository,
  );
});

test.beforeEach(async ({ page }) => {
  await page.route("**/api/v1/**", (route) =>
    route.fulfill({
      status: 503,
      contentType: "application/json",
      body: JSON.stringify({ code: "provider_unavailable" }),
    }),
  );
});

for (const theme of ["dark", "light"] as const) {
  test(`${theme} shell is accessible and keeps the session recovery actions`, async ({
    page,
  }) => {
    await page.goto("/");
    await expect(
      page.getByRole("heading", { name: "Sign-in is unavailable" }),
    ).toBeVisible();
    if (theme === "light")
      await page.getByRole("button", { name: "Switch to light theme" }).click();
    await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
    const accessibility = await new AxeBuilder({ page })
      .withTags(["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"])
      .analyze();
    expect(accessibility.violations).toEqual([]);
    await expect(
      page.getByRole("button", { name: "Retry session" }),
    ).toBeVisible();
    await expect(
      page.getByRole("navigation", { name: "Workspace sections" }),
    ).toBeVisible();
    await page.reload();
    await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
  });
}

test("narrow layout reflows and the skip link reaches the main workspace", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "Sign-in is unavailable" }),
  ).toBeVisible();
  await page.keyboard.press("Tab");
  await expect(
    page.getByRole("link", { name: "Skip to workspace" }),
  ).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("main")).toBeFocused();
  const fits = await page.evaluate(
    () => document.documentElement.scrollWidth <= window.innerWidth,
  );
  expect(fits).toBe(true);
  expect(
    (
      await new AxeBuilder({ page })
        .withTags(["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"])
        .analyze()
    ).violations,
  ).toEqual([]);
});

test("authorized empty state uses only local assets and mounted navigation targets", async ({
  page,
  baseURL,
}) => {
  const expectedOrigin = new URL(baseURL!).origin;
  await page.unroute("**/api/v1/**");
  await page.route("**/api/v1/**", (route) =>
    route.fulfill({
      contentType: "application/json",
      body: JSON.stringify(
        route.request().url().endsWith("/session")
          ? {
              principal_id: "fixture-only",
              csrf_token: "fixture-only",
              auth_backend: "local_fixture",
              schema_version: 1,
            }
          : { items: [], next_cursor: null },
      ),
    }),
  );
  const thirdParty: string[] = [];
  const fonts: string[] = [];
  page.on("request", (request) => {
    if (new URL(request.url()).origin !== expectedOrigin)
      thirdParty.push(request.url());
    if (request.url().includes("/fonts/")) fonts.push(request.url());
  });
  await page.goto("/");
  await expect(page.getByText("No repositories are available.")).toBeVisible();
  await page.evaluate(() => document.fonts.ready);
  await page.getByRole("link", { name: "Add codebase" }).click();
  await expect(page).toHaveURL(/#upload-title$/);
  await expect(
    page.getByRole("heading", { name: "Add a source archive" }),
  ).toBeFocused();
  expect(thirdParty).toEqual([]);
  expect(fonts.some((url) => url.endsWith(".woff2"))).toBe(true);
});

test("sign-out clears the protected workspace with mocked HTTP", async ({ page }) => {
  const repo = "10000000-0000-4000-8000-000000000011";
  let signouts = 0;
  await page.unroute("**/api/v1/**");
  await page.route("**/api/v1/**", route => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/session/logout")) { signouts++; expect(route.request().headers()["x-csrf-token"]).toBe("fixture-only"); return route.fulfill({ status: 204 }); }
    if (path.endsWith("/session")) return route.fulfill({ json: { principal_id: "fixture", csrf_token: "fixture-only", auth_backend: "local_fixture" } });
    return route.fulfill({ json: { items: [{ id: repo, display_name: "Private fixture repository", source_kind: "archive" }] } });
  });
  await page.goto(`/?repository=${repo}#upload-title`);
  await expect(page.getByRole("combobox", { name: "Repository", exact: true })).toHaveValue(repo);
  await page.getByRole("button", { name: "Sign out", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Workspace access changed" })).toBeVisible();
  await expect(page.getByRole("combobox", { name: "Repository", exact: true })).toHaveCount(0);
  await expect(page.getByText("Private fixture repository", { exact: true })).toHaveCount(0);
  expect(signouts).toBe(1);
});
