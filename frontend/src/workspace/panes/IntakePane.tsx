// Moved verbatim from the workspace composition; behaviour is unchanged.
import type { InfiniteData } from "@tanstack/react-query";
import type { RepositoryPageV1 } from "../../api/generated/types.gen";
import { GitHubImportForm } from "../../features/intake/GitHubImportForm";
import { GitHubUrlImportForm } from "../../features/intake/GitHubUrlImportForm";
import { RepositoryCreateForm } from "../../features/intake/RepositoryCreateForm";
import { problem } from "../problems";
import { useWorkspace } from "../workspaceContext";
export function IntakePane() {
  const {
    client,
    session,
    selection,
    repositories,
    selectedMetadata,
    repositoryItems,
    busy,
    intake,
  } = useWorkspace();
  const {
    repository,
    submissionId,
    setRepository,
    setRestoredSnapshot,
    setSubmissionId,
    setJobId,
    setScanJobId,
    setAnalysisJobId,
    setPages,
    setConfirmCancel,
    navigationRevisionRef,
  } = selection;
  const {
    setCreatingRepository,
    creatingRepository,
    setFile,
    file,
    validation,
    setValidation,
    inputRef,
    uploadKeyRef,
    acquire,
    importUrl,
    upload,
  } = intake;
  return (
    <>
      <section aria-labelledby="upload-title">
        <h2 id="upload-title" tabIndex={-1}>
          Add a source archive
        </h2>
        <p>
          ZIP only, up to 100 MiB. Archive contents are inventoried without
          executing source files.
        </p>
        {repositories.isPending && <p role="status">Loading repositories…</p>}
        {repositories.isError && (
          <p role="alert">
            Repositories could not be loaded.{" "}
            <button onClick={() => void repositories.refetch()}>
              Reload repositories
            </button>
          </p>
        )}
        {selectedMetadata.isError && (
          <p role="alert">
            The selected repository is unavailable. Choose another authorized
            repository.
          </p>
        )}
        {repositories.isSuccess && repositoryItems.length === 0 && (
          <p>No repositories are available.</p>
        )}
        <RepositoryCreateForm
          disabled={(busy && !creatingRepository) || !!submissionId}
          onPendingChange={setCreatingRepository}
          csrfToken={session.data?.csrf_token ?? ""}
          onCreated={(record) => {
            client.setQueryData<InfiniteData<RepositoryPageV1>>(
              ["repositories"],
              (current) =>
                current
                  ? {
                      ...current,
                      pages: [
                        {
                          ...current.pages[0],
                          items: [
                            {
                              id: record.id,
                              org_id: record.org_id,
                              display_name: record.display_name,
                              source_kind: record.source_kind,
                            },
                            ...(current.pages[0]?.items ?? []),
                          ],
                        },
                        ...current.pages.slice(1),
                      ],
                    }
                  : current,
            );
            setRepository(record.id);
            setRestoredSnapshot(null);
            setSubmissionId(null);
            setJobId(null);
            setScanJobId(null);
            setAnalysisJobId(null);
            setPages([null]);
          }}
        />
        <form
          noValidate
          onSubmit={(event) => {
            event.preventDefault();
            if (
              !repository ||
              !file ||
              file.size === 0 ||
              file.size > 104857600 ||
              !file.name.toLowerCase().endsWith(".zip")
            ) {
              setValidation(
                "Choose a repository and a nonempty ZIP no larger than 100 MiB.",
              );
              inputRef.current?.focus();
              return;
            }
            setValidation("");
            upload.mutate();
          }}
        >
          <label htmlFor="repository">Repository</label>
          <select
            id="repository"
            value={repository}
            disabled={busy || !!submissionId}
            onChange={(event) => {
              if (event.target.value === repository) return;
              navigationRevisionRef.current += 1;
              setRepository(event.target.value);
              setRestoredSnapshot(null);
              setSubmissionId(null);
              setJobId(null);
              setScanJobId(null);
              setAnalysisJobId(null);
              setPages([null]);
              setConfirmCancel(false);
              const url = new URL(window.location.href);
              url.searchParams.delete("investigation");
              window.history.replaceState(window.history.state, "", url);
              uploadKeyRef.current = crypto.randomUUID();
            }}
          >
            <option value="">Choose a repository</option>
            {repositoryItems.map((repo) => (
              <option key={repo.id} value={repo.id}>
                {repo.display_name}
              </option>
            ))}
          </select>
          {repositories.hasNextPage && (
            <button
              type="button"
              disabled={repositories.isFetchingNextPage}
              onClick={() => void repositories.fetchNextPage()}
            >
              Load more repositories
            </button>
          )}
          <label htmlFor="archive">Source ZIP</label>
          <input
            ref={inputRef}
            id="archive"
            type="file"
            accept=".zip,application/zip"
            disabled={
              busy ||
              !!submissionId ||
              repositoryItems.find((item) => item.id === repository)
                ?.source_kind === "github"
            }
            aria-invalid={!!validation}
            aria-describedby="upload-help"
            onChange={(event) => {
              setFile(event.target.files?.[0] ?? null);
              uploadKeyRef.current = crypto.randomUUID();
            }}
          />
          <p id="upload-help" className={validation ? "error" : "muted"}>
            {validation ||
              "Paths and hashes will appear after admission. Sensitive paths remain excluded."}
          </p>
          <button
            className="primary"
            type="submit"
            disabled={
              busy ||
              !repositoryItems.length ||
              repositoryItems.find((item) => item.id === repository)
                ?.source_kind === "github" ||
              (!!submissionId && !upload.isError)
            }
            aria-busy={upload.isPending}
          >
            {upload.isPending ? "Uploading…" : "Upload archive"}
          </button>
        </form>
        {repositoryItems.find((item) => item.id === repository)?.source_kind ===
          "github" && (
          <GitHubImportForm
            pending={busy}
            onImport={(ref) => acquire.mutate(ref)}
          />
        )}
        <GitHubUrlImportForm
          pending={busy || importUrl.isPending}
          onImport={(url) => importUrl.mutate(url)}
        />
        {importUrl.isError && <p role="alert">{problem(importUrl.error)}</p>}
        {acquire.isError && <p role="alert">{problem(acquire.error)}</p>}
        {upload.isError && <p role="alert">{problem(upload.error)}</p>}
      </section>
    </>
  );
}
