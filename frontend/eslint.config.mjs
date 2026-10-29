import js from "@eslint/js";
import tseslint from "typescript-eslint";
import reactHooks from "eslint-plugin-react-hooks";

export default tseslint.config(
  {
    ignores: [
      "node_modules/**",
      ".venv/**",
      ".tools/**",
      "dist/**",
      "coverage/**",
      "playwright-report/**",
      "test-results/**",
      "src/api/generated/**", // Generator-owned SDK; tsc checks its contracts.
    ],
  },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ["src/**/*.{ts,tsx}"],
    plugins: { "react-hooks": reactHooks },
    rules: reactHooks.configs.recommended.rules,
  },
);
