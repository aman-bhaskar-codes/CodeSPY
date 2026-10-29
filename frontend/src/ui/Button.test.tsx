import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { Button } from "./Button";

afterEach(cleanup);
it("keeps its accessible name and prevents duplicate activation while pending", async () => {
  const activate = vi.fn();
  render(
    <Button pending onClick={activate}>
      Save review
    </Button>,
  );
  const button = screen.getByRole("button", { name: "Save review" });
  expect(button).toHaveAttribute("type", "button");
  expect(button).toHaveAttribute("aria-busy", "true");
  expect(button).toBeDisabled();
  await userEvent.click(button);
  expect(activate).not.toHaveBeenCalled();
});
