import { afterEach, describe, expect, it } from "vitest";
import { act, cleanup, renderHook } from "@testing-library/react";
import {
  findingTargetFromUrl,
  selectedId,
  useSnapshotUrl,
  useWorkspaceSelection,
} from "./useWorkspaceSelection";

type Selection = ReturnType<typeof useWorkspaceSelection>;
const id = (n: number) =>
  `10000000-0000-4000-8000-${String(n).padStart(12, "0")}`;
const [repo, other, submission, job, scan, snapshot, finding, run] = [
  id(1),
  id(2),
  id(3),
  id(4),
  id(5),
  id(6),
  id(7),
  id(8),
];

function at(search: string, hash = "") {
  window.history.replaceState({}, "", `/${search}${hash}`);
}
function navigate(search: string, hash = "") {
  act(() => {
    window.history.pushState({}, "", `/${search}${hash}`);
    window.dispatchEvent(new PopStateEvent("popstate"));
  });
}
const params = () => new URLSearchParams(window.location.search);

afterEach(() => {
  cleanup();
  window.history.replaceState({}, "", "/");
});

describe("selectedId", () => {
  it("accepts only well-formed identifiers", () => {
    at(`?repository=${repo}&job=not-a-uuid&scan=${scan.toUpperCase()}`);
    expect(selectedId("repository")).toBe(repo);
    expect(selectedId("job")).toBeNull();
    expect(selectedId("scan")).toBe(scan.toUpperCase());
    expect(selectedId("missing")).toBeNull();
  });
});

describe("restoring the selection from the URL on load", () => {
  it("starts empty with no parameters", () => {
    at("");
    const { result } = renderHook(() => useWorkspaceSelection());
    expect(result.current.repository).toBe("");
    expect(result.current.submissionId).toBeNull();
    expect(result.current.jobId).toBeNull();
    expect(result.current.scanJobId).toBeNull();
    expect(result.current.restoredSnapshot).toBeNull();
    expect(result.current.findingTarget).toBeNull();
    expect(result.current.analysisJobId).toBeNull();
    expect(result.current.pages).toEqual([null]);
    expect(result.current.confirmCancel).toBe(false);
  });

  it.each([
    ["repository", () => repo, (r: Selection) => r.repository],
    ["submission", () => submission, (r: Selection) => r.submissionId],
    ["job", () => job, (r: Selection) => r.jobId],
    ["scan", () => scan, (r: Selection) => r.scanJobId],
    ["snapshot", () => snapshot, (r: Selection) => r.restoredSnapshot],
  ])("restores %s", (name, value, pick) => {
    at(`?${name}=${value()}`);
    const { result } = renderHook(() => useWorkspaceSelection());
    expect(pick(result.current)).toBe(value());
  });

  it("ignores a malformed identifier", () => {
    at(`?repository=${repo}&job=../etc/passwd&submission=%00`);
    const { result } = renderHook(() => useWorkspaceSelection());
    expect(result.current.repository).toBe(repo);
    expect(result.current.jobId).toBeNull();
    expect(result.current.submissionId).toBeNull();
    // The malformed values are dropped from the address bar as well.
    expect(params().get("job")).toBeNull();
    expect(params().get("submission")).toBeNull();
  });

  it("restores a finding target only when all four identifiers are present", () => {
    at(
      `?repository=${repo}&snapshot=${snapshot}&finding=${finding}&finding_run=${run}`,
    );
    expect(findingTargetFromUrl()).toEqual({
      repositoryId: repo,
      snapshotId: snapshot,
      findingId: finding,
      runId: run,
    });
    const { result } = renderHook(() => useWorkspaceSelection());
    expect(result.current.findingTarget).toEqual({
      repositoryId: repo,
      snapshotId: snapshot,
      findingId: finding,
      runId: run,
    });
    cleanup();
    for (const missing of [
      "repository",
      "snapshot",
      "finding",
      "finding_run",
    ]) {
      const all: Record<string, string> = {
        repository: repo,
        snapshot,
        finding,
        finding_run: run,
      };
      delete all[missing];
      at(`?${new URLSearchParams(all)}`);
      expect(findingTargetFromUrl()).toBeNull();
    }
  });
});

describe("writing the selection back to the URL", () => {
  it("mirrors repository, submission, job and scan and removes cleared ones", () => {
    at("");
    const { result } = renderHook(() => useWorkspaceSelection());
    act(() => {
      result.current.setRepository(repo);
      result.current.setSubmissionId(submission);
      result.current.setJobId(job);
      result.current.setScanJobId(scan);
    });
    expect(params().get("repository")).toBe(repo);
    expect(params().get("submission")).toBe(submission);
    expect(params().get("job")).toBe(job);
    expect(params().get("scan")).toBe(scan);
    act(() => {
      result.current.setSubmissionId(null);
      result.current.setScanJobId(null);
    });
    expect(params().has("submission")).toBe(false);
    expect(params().has("scan")).toBe(false);
    expect(params().get("repository")).toBe(repo);
    expect(result.current.locationSearchRef.current).toBe(
      window.location.search,
    );
  });

  it("leaves unrelated parameters and the hash alone", () => {
    at(`?investigation=${other}&view=x`, "#findings-title");
    const { result } = renderHook(() => useWorkspaceSelection());
    act(() => result.current.setRepository(repo));
    expect(params().get("investigation")).toBe(other);
    expect(params().get("view")).toBe("x");
    expect(window.location.hash).toBe("#findings-title");
  });

  it("replaces rather than adds history entries", () => {
    at("");
    const before = window.history.length;
    const { result } = renderHook(() => useWorkspaceSelection());
    act(() => result.current.setRepository(repo));
    act(() => result.current.setJobId(job));
    expect(window.history.length).toBe(before);
  });

  it("does not write the snapshot; that is a separate effect", () => {
    at(`?snapshot=${snapshot}`);
    const { result } = renderHook(() => useWorkspaceSelection());
    act(() => result.current.setRestoredSnapshot(null));
    expect(params().get("snapshot")).toBe(snapshot);
  });
});

describe("history navigation", () => {
  it("restores every field and resets transient state", () => {
    at(`?repository=${repo}&job=${job}`);
    const { result } = renderHook(() => useWorkspaceSelection());
    act(() => {
      result.current.setAnalysisJobId(id(9));
      result.current.setPages([null, "cursor"]);
      result.current.setConfirmCancel(true);
    });
    navigate(
      `?repository=${other}&submission=${submission}&scan=${scan}&snapshot=${snapshot}`,
    );
    expect(result.current.repository).toBe(other);
    expect(result.current.submissionId).toBe(submission);
    expect(result.current.jobId).toBeNull();
    expect(result.current.scanJobId).toBe(scan);
    expect(result.current.restoredSnapshot).toBe(snapshot);
    expect(result.current.analysisJobId).toBeNull();
    expect(result.current.pages).toEqual([null]);
    expect(result.current.confirmCancel).toBe(false);
    expect(result.current.findingTarget).toBeNull();
  });

  it("restores a finding target from history", () => {
    at("");
    const { result } = renderHook(() => useWorkspaceSelection());
    navigate(
      `?repository=${repo}&snapshot=${snapshot}&finding=${finding}&finding_run=${run}`,
    );
    expect(result.current.findingTarget).toEqual({
      repositoryId: repo,
      snapshotId: snapshot,
      findingId: finding,
      runId: run,
    });
  });

  it("ignores a popstate that did not change the query string", () => {
    at(`?repository=${repo}`);
    const { result } = renderHook(() => useWorkspaceSelection());
    const revision = result.current.navigationRevisionRef.current;
    act(() => {
      result.current.setConfirmCancel(true);
      result.current.setPages([null, "cursor"]);
    });
    // Same search, only the hash moves, as when a pane link is followed.
    navigate(`?repository=${repo}`, "#findings-title");
    expect(result.current.navigationRevisionRef.current).toBe(revision);
    expect(result.current.confirmCancel).toBe(true);
    expect(result.current.pages).toEqual([null, "cursor"]);
  });

  it("invalidates responses requested before the navigation", () => {
    at(`?repository=${repo}`);
    const { result } = renderHook(() => useWorkspaceSelection());
    const current = result.current.responseIsCurrent();
    expect(current()).toBe(true);
    navigate(`?repository=${other}`);
    expect(current()).toBe(false);
    expect(result.current.responseIsCurrent()()).toBe(true);
  });

  it("stops listening after unmount and invalidates pending responses", () => {
    at(`?repository=${repo}`);
    const { result, unmount } = renderHook(() => useWorkspaceSelection());
    const current = result.current.responseIsCurrent();
    unmount();
    expect(current()).toBe(false);
    act(() => {
      window.history.pushState({}, "", `/?repository=${other}`);
      window.dispatchEvent(new PopStateEvent("popstate"));
    });
    expect(result.current.repository).toBe(repo);
  });
});

describe("useSnapshotUrl", () => {
  it("sets and clears the snapshot parameter and records the search", () => {
    at(`?repository=${repo}`);
    const ref = { current: window.location.search };
    const { rerender } = renderHook(
      ({ value }: { value: string | null }) => useSnapshotUrl(value, ref),
      { initialProps: { value: snapshot as string | null } },
    );
    expect(params().get("snapshot")).toBe(snapshot);
    expect(ref.current).toBe(window.location.search);
    rerender({ value: null });
    expect(params().has("snapshot")).toBe(false);
    expect(params().get("repository")).toBe(repo);
    expect(ref.current).toBe(window.location.search);
  });
});
