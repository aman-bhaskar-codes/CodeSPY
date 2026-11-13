import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";

async function authorized(page: Page) {
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
}

test("the workbench exposes named parts and F6 moves between them", async ({
  page,
}) => {
  await authorized(page);
  await page.goto("/");
  await expect(
    page.getByRole("navigation", { name: "Workspace sections" }),
  ).toBeVisible();
  await expect(
    page.getByRole("complementary", { name: "Explorer" }),
  ).toBeVisible();
  await expect(page.getByRole("main")).toBeVisible();
  await expect(page.getByRole("contentinfo")).toBeVisible();
  await page.getByRole("main").focus();
  await page.keyboard.press("F6");
  await expect(page.getByRole("contentinfo")).toBeFocused();
  await page.keyboard.press("F6");
  await expect(
    page
      .getByRole("navigation", { name: "Workspace sections" })
      .getByRole("link")
      .first(),
  ).toBeFocused();
  await page.keyboard.press("Shift+F6");
  await expect(page.getByRole("contentinfo")).toBeFocused();
});

test("the explorer resizes with the keyboard, hides with Ctrl+B and remembers its width", async ({
  page,
}) => {
  await authorized(page);
  await page.goto("/");
  const bar = page.getByRole("separator", { name: "Resize explorer" });
  await bar.focus();
  const before = Number(await bar.getAttribute("aria-valuenow"));
  await page.keyboard.press("ArrowRight");
  await expect(bar).toHaveAttribute("aria-valuenow", String(before + 16));
  await page.reload();
  await expect(
    page.getByRole("separator", { name: "Resize explorer" }),
  ).toHaveAttribute("aria-valuenow", String(before + 16));
  await page.getByRole("main").focus();
  await page.keyboard.press("Control+b");
  await expect(
    page.getByRole("complementary", { name: "Explorer" }),
  ).toBeHidden();
  await page.keyboard.press("Control+b");
  await expect(
    page.getByRole("complementary", { name: "Explorer" }),
  ).toBeVisible();
});

test("the command palette opens, filters and runs a command", async ({
  page,
}) => {
  await authorized(page);
  await page.goto("/");
  await page.keyboard.press("Control+Shift+P");
  const box = page.getByRole("combobox", { name: "Type a command" });
  await expect(box).toBeFocused();
  await box.fill("add code");
  await expect(page.getByRole("option")).toHaveCount(1);
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/#upload-title$/);
  await expect(box).toBeHidden();
  await page.keyboard.press("Escape");
});

test("the panel opens from the status bar and lists problems", async ({
  page,
}) => {
  await authorized(page);
  await page.goto("/");
  await page.getByRole("button", { name: "Panel", exact: true }).click();
  await expect(page.getByRole("region", { name: "Panel" })).toBeVisible();
  await page.keyboard.press("Control+j");
  await expect(page.getByRole("region", { name: "Panel" })).toBeHidden();
});

for (const theme of ["dark", "light"] as const) {
  test(`${theme} workbench with the panel and palette open is accessible`, async ({
    page,
  }) => {
    await page.addInitScript((value) => {
      window.localStorage.setItem("codespy.appearance.theme", value);
      window.localStorage.setItem(
        "codespy.workbench.layout",
        JSON.stringify({ panelVisible: true, panelTab: "output" }),
      );
    }, theme);
    await authorized(page);
    await page.goto("/");
    await expect(page.getByRole("region", { name: "Panel" })).toBeVisible();
    let report = await new AxeBuilder({ page })
      .withTags(["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"])
      .analyze();
    expect(report.violations).toEqual([]);
    await page.keyboard.press("Control+Shift+P");
    await expect(page.getByRole("combobox")).toBeVisible();
    report = await new AxeBuilder({ page })
      .withTags(["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"])
      .analyze();
    expect(report.violations).toEqual([]);
  });
}

test("at 320px the workbench collapses without horizontal scrolling", async ({
  page,
}) => {
  await page.setViewportSize({ width: 320, height: 640 });
  await authorized(page);
  await page.goto("/");
  await page.keyboard.press("Control+j");
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - window.innerWidth,
  );
  expect(overflow).toBeLessThanOrEqual(0);
  await expect(page.getByRole("link", { name: "CodeSPY home" })).toBeVisible();
  await expect(
    page.getByRole("navigation", { name: "Workspace sections" }),
  ).toBeVisible();
});
