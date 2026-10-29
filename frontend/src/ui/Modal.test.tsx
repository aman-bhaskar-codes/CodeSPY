import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { Modal } from "./Modal";

afterEach(cleanup);
it("names the dialog, confines keyboard focus and supports Escape", async () => {
  const close = vi.fn();
  const user = userEvent.setup();
  render(
    <>
      <button onClick={vi.fn()}>Outside</button>
      <Modal open title="Review evidence" onClose={close}>
        <button onClick={vi.fn()}>First action</button>
        <button onClick={vi.fn()}>Last action</button>
      </Modal>
    </>,
  );
  expect(screen.getByRole("dialog", { name: "Review evidence" })).toBeVisible();
  await user.tab();
  expect(
    screen.getByRole("button", { name: "Outside", hidden: true }),
  ).not.toHaveFocus();
  await user.keyboard("{Escape}");
  expect(close).toHaveBeenCalledOnce();
});
it("prevents dismissal during an uncertain mutation", async () => {
  const close = vi.fn();
  render(
    <Modal open title="Saving review" onClose={close} busy>
      <button disabled>Pending</button>
    </Modal>,
  );
  await userEvent.keyboard("{Escape}");
  expect(close).not.toHaveBeenCalled();
});

it("cycles focus in both directions and returns it to the opener", async () => {
  const { useState } = await import("react");
  function Fixture() {
    const [open, setOpen] = useState(false);
    return (
      <>
        <button onClick={() => setOpen(true)}>Open review</button>
        <Modal
          open={open}
          title="Review fixture"
          onClose={() => setOpen(false)}
        >
          <button onClick={vi.fn()}>First</button>
          <button onClick={vi.fn()}>Last</button>
        </Modal>
      </>
    );
  }
  const user = userEvent.setup();
  render(<Fixture />);
  const opener = screen.getByRole("button", { name: "Open review" });
  await user.click(opener);
  screen.getByRole("button", { name: "Last" }).focus();
  await user.tab();
  expect(
    screen.getByRole("button", { name: "Close Review fixture" }),
  ).toHaveFocus();
  await user.tab({ shift: true });
  expect(screen.getByRole("button", { name: "Last" })).toHaveFocus();
  await user.keyboard("{Escape}");
  expect(opener).toHaveFocus();
});
