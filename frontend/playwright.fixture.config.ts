import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests/e2e",
  testMatch: "**/*.spec.ts",
  testIgnore: "**/indexing.spec.ts",
  outputDir: "./test-results",
  workers: 1,
  retries: 0,
  reporter: [
    ["list"],
    ["html", { open: "never", outputFolder: "playwright-report" }],
  ],
  use: {
    baseURL: "http://127.0.0.1:5192",
    headless: true,
    channel: process.env.CODESPY_PLAYWRIGHT_CHANNEL || "chrome",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  webServer: {
    command: "npx vite --config vite.config.fixture.ts",
    url: "http://127.0.0.1:5192",
    reuseExistingServer: false,
    timeout: 60_000,
  },
});
