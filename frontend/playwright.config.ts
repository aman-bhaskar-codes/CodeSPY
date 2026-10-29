import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests/e2e",
  use: { baseURL: "http://127.0.0.1:5173", headless: true, channel: process.env.CODESPY_PLAYWRIGHT_CHANNEL },
});
