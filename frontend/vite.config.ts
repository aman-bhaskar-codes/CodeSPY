import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: { host: "127.0.0.1", port: 5173, proxy: { "/api": "http://127.0.0.1:8000" } },
  preview: { host: "127.0.0.1", port: 4173 },
  test: { environment: "jsdom" },
});
