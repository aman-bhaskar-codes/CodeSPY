import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { ThemeProvider, ThemeToggle } from "./ThemeProvider";

afterEach(() => {
  cleanup();
  localStorage.clear();
  document.documentElement.removeAttribute("data-theme");
  vi.restoreAllMocks();
});

it("defaults to dark and saves only an explicit appearance preference", async () => {
  render(
    <ThemeProvider>
      <ThemeToggle />
    </ThemeProvider>,
  );
  expect(document.documentElement).toHaveAttribute("data-theme", "dark");
  await userEvent.click(
    screen.getByRole("button", { name: "Switch to light theme" }),
  );
  expect(document.documentElement).toHaveAttribute("data-theme", "light");
  expect(localStorage.getItem("codespy.appearance.theme")).toBe("light");
  expect(localStorage.length).toBe(1);
  expect(
    screen.getByRole("button", { name: "Switch to dark theme" }),
  ).toHaveFocus();
});

it("restores a valid preference and ignores invalid stored content", () => {
  localStorage.setItem("codespy.appearance.theme", "light");
  const view = render(
    <ThemeProvider>
      <ThemeToggle />
    </ThemeProvider>,
  );
  expect(document.documentElement).toHaveAttribute("data-theme", "light");
  view.unmount();
  localStorage.setItem("codespy.appearance.theme", "invalid");
  render(
    <ThemeProvider>
      <ThemeToggle />
    </ThemeProvider>,
  );
  expect(document.documentElement).toHaveAttribute("data-theme", "dark");
});

it("continues to work when browser preference storage is unavailable", async () => {
  vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
    throw new Error("denied");
  });
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
    throw new Error("denied");
  });
  render(
    <ThemeProvider>
      <ThemeToggle />
    </ThemeProvider>,
  );
  await userEvent.click(
    screen.getByRole("button", { name: "Switch to light theme" }),
  );
  expect(document.documentElement).toHaveAttribute("data-theme", "light");
});
