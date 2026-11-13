import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { api } from "../../api/client";
import { SourceEditor } from "./SourceEditor";
import { useOpenFiles } from "./useOpenFiles";

beforeEach(() => useOpenFiles.getState().reset());
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function mount() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <SourceEditor repositoryId="r" snapshotId="s" />
    </QueryClientProvider>,
  );
}

it("asks for a file when none is open", () => {
  mount();
  expect(screen.getByText(/Select a file in the explorer/)).toBeInTheDocument();
});

it("shows numbered, read-only lines from the redacted representation", async () => {
  useOpenFiles.getState().open({ fileId: "f1", path: "src/app.py" });
  const spy = vi.spyOn(api, "sourceView").mockResolvedValue({
    content_text: "import os\nprint(1)\n",
    representation: "model_safe",
    policy_version: "v1",
    content_sha256: "a".repeat(64),
    is_omitted: false,
  } as never);
  mount();
  expect(
    await screen.findByRole("region", { name: "Source of src/app.py" }),
  ).toBeInTheDocument();
  expect(screen.getByText("import os")).toBeInTheDocument();
  expect(screen.getByRole("rowheader", { name: "2" })).toBeInTheDocument();
  expect(spy.mock.calls[0]?.[3]).toEqual({ representation: "model_safe" });
  expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
});

it("explains omitted and unreadable files", async () => {
  useOpenFiles.getState().open({ fileId: "f2", path: "big.bin" });
  vi.spyOn(api, "sourceView").mockResolvedValue({
    content_text: null,
    representation: "model_safe",
    policy_version: "v1",
    content_sha256: "b".repeat(64),
    is_omitted: true,
    omission_reason: "sensitive_path",
  } as never);
  mount();
  expect(
    await screen.findByText(/This file is not shown \(sensitive_path\)/),
  ).toBeInTheDocument();
  cleanup();
  vi.restoreAllMocks();
  vi.spyOn(api, "sourceView").mockRejectedValue(new Error("gone"));
  mount();
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "could not be read: gone",
  );
});
