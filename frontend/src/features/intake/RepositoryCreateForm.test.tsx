import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, expect, it, vi } from "vitest";
import { api } from "../../api/client";
import { RepositoryCreateForm } from "./RepositoryCreateForm";

afterEach(() => { cleanup(); vi.restoreAllMocks(); });
it("keeps an uncertain repository creation idempotent", async () => {
  const create = vi.spyOn(api, "createRepository").mockRejectedValue(new Error("uncertain"));
  render(<QueryClientProvider client={new QueryClient()}><RepositoryCreateForm csrfToken="fixture" onCreated={vi.fn()} /></QueryClientProvider>);
  fireEvent.change(screen.getByLabelText("Repository name"), { target: { value: "My source" } });
  fireEvent.click(screen.getByRole("button", { name: "Create repository" }));
  await screen.findByRole("alert");
  fireEvent.click(screen.getByRole("button", { name: "Create repository" }));
  await waitFor(() => expect(create).toHaveBeenCalledTimes(2));
  expect(create.mock.calls[0][0]).toEqual(create.mock.calls[1][0]);
  expect(create.mock.calls[0][2]).toBe(create.mock.calls[1][2]);
});
