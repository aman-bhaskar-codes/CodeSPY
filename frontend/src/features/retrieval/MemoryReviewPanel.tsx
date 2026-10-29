import { useRef, useState } from "react";
import { InlineNotice } from "../../ui/InlineNotice";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "../../api/client";
import type {
  MemoryEntryV1,
  MemoryReviewRequestV1,
} from "../../api/generated/types.gen";

type Action = MemoryReviewRequestV1["action"];
interface Props {
  repositoryId: string;
  snapshotId: string;
  csrfToken: string;
}

export function MemoryReviewPanel(props: Props) {
  return (
    <ReviewState key={`${props.repositoryId}:${props.snapshotId}`} {...props} />
  );
}
function ReviewState({ repositoryId, snapshotId, csrfToken }: Props) {
  const client = useQueryClient();
  const [cursor, setCursor] = useState<string>();
  const [pages, setPages] = useState<(string | undefined)[]>([]);
  const [intent, setIntent] = useState<{
    entry: MemoryEntryV1;
    action: Action;
    key: string;
  } | null>(null);
  const [reason, setReason] = useState("");
  const [replacement, setReplacement] = useState("");
  const [historyId, setHistoryId] = useState<string>();
  const [notice, setNotice] = useState("");
  const heading = useRef<HTMLHeadingElement>(null);
  const queue = useQuery({
    queryKey: ["memory-review", repositoryId, snapshotId, cursor],
    queryFn: ({ signal }) =>
      api.memoryReviewPage(repositoryId, snapshotId, cursor, signal),
    retry: false,
  });
  const history = useQuery({
    queryKey: ["memory-history", repositoryId, historyId],
    enabled: !!historyId,
    queryFn: ({ signal }) =>
      api.memoryHistory(repositoryId, historyId!, signal),
    retry: false,
  });
  const review = useMutation({
    mutationFn: async () => {
      if (!intent) throw new Error("No review selected");
      return api.reviewMemory(
        repositoryId,
        intent.entry.memory_id,
        {
          action: intent.action,
          expected_version: intent.entry.version ?? 1,
          request_key: intent.key,
          reason: reason.trim(),
          replacement_id:
            intent.action === "supersede" ? replacement : undefined,
        },
        csrfToken,
      );
    },
    onSuccess: async () => {
      setNotice("Review saved. Eligibility is checked again before reuse.");
      setIntent(null);
      setReason("");
      setReplacement("");
      await Promise.all([
        client.invalidateQueries({
          queryKey: ["memory-review", repositoryId, snapshotId],
        }),
        client.invalidateQueries({
          queryKey: ["repo_memories", repositoryId, snapshotId],
        }),
        client.invalidateQueries({
          queryKey: ["memory-history", repositoryId],
        }),
      ]);
      heading.current?.focus();
    },
  });
  function choose(entry: MemoryEntryV1, action: Action) {
    review.reset();
    setReason("");
    setReplacement("");
    setNotice("");
    setIntent({ entry, action, key: crypto.randomUUID() });
  }
  const error =
    review.error instanceof ApiError
      ? review.error.code
      : "Review could not be saved. Retry this action.";
  return (
    <section aria-labelledby="memory-review-heading">
      <h3 id="memory-review-heading" ref={heading} tabIndex={-1}>
        Memory review
      </h3>
      <p>
        Candidates stay out of model context. Acceptance approves reuse; source
        evidence is checked separately.
      </p>
      {!queue.data?.can_review && (
        <p>
          An owner or administrator with current repository write access can
          review memories.
        </p>
      )}
      {queue.isPending && <p role="status">Loading memory review…</p>}
      {queue.isError && (
        <p role="alert">
          Memory review is unavailable.{" "}
          <button onClick={() => queue.refetch()}>Retry review list</button>
        </p>
      )}
      {notice && <InlineNotice tone="success">{notice}</InlineNotice>}
      {queue.data?.items.length === 0 && <p>No memories on this page.</p>}
      {queue.data?.items.map((entry) => (
        <article key={entry.memory_id} className="card">
          <h4>{entry.topic}</h4>
          <p>
            {entry.state ?? "needs_review"} · Version {entry.version ?? 1} ·
            Snapshot <code>{entry.snapshot_id}</code>
          </p>
          {entry.source_available === false && (
            <p>
              Source unavailable. Stored text is hidden and cannot enter model
              context.
            </p>
          )}
          <p>{entry.summary}</p>
          <p>
            {entry.evidence_pointers?.length
              ? `${entry.evidence_pointers.length} source references`
              : "Human note — does not establish a finding"}
          </p>
          {entry.evidence_pointers?.map((p, i) => (
            <p key={i}>
              <code>
                {p.file_path}:{p.start_line}–{p.end_line}
              </code>
            </p>
          ))}
          {entry.expires_at && (
            <p>
              Expires:{" "}
              <time dateTime={entry.expires_at}>{entry.expires_at}</time>
            </p>
          )}
          {entry.reviewer_id && (
            <p>
              Reviewer: <code>{entry.reviewer_id}</code>
            </p>
          )}
          <button
            onClick={() =>
              setHistoryId(
                historyId === entry.memory_id ? undefined : entry.memory_id,
              )
            }
          >
            History for {entry.topic}
          </button>
          {queue.data.can_review && (
            <div className="row">
              {["candidate", "needs_review"].includes(
                entry.state ?? "needs_review",
              ) && (
                <>
                  <button
                    disabled={review.isPending}
                    onClick={() => choose(entry, "accept")}
                  >
                    Accept {entry.topic}
                  </button>
                  <button
                    disabled={review.isPending}
                    onClick={() => choose(entry, "reject")}
                  >
                    Reject {entry.topic}
                  </button>
                </>
              )}
              {entry.state === "accepted" && (
                <>
                  <button
                    disabled={review.isPending}
                    onClick={() => choose(entry, "archive")}
                  >
                    Archive {entry.topic}
                  </button>
                  <button
                    disabled={review.isPending}
                    onClick={() => choose(entry, "supersede")}
                  >
                    Supersede {entry.topic}
                  </button>
                </>
              )}
              {["expired", "archived"].includes(entry.state ?? "") && (
                <button
                  disabled={review.isPending}
                  onClick={() => choose(entry, "restore")}
                >
                  Restore {entry.topic}
                </button>
              )}
            </div>
          )}
        </article>
      ))}
      {intent && (
        <form
          noValidate
          onSubmit={(event) => {
            event.preventDefault();
            review.mutate();
          }}
        >
          <h4>
            {intent.action}: {intent.entry.topic}
          </h4>
          <label>
            Review reason
            <textarea
              className="resize-none"
              disabled={review.isPending}
              value={reason}
              maxLength={1024}
              onChange={(event) => {
                setReason(event.target.value);
                setIntent({ ...intent, key: crypto.randomUUID() });
              }}
              required
            />
          </label>
          {intent.action === "supersede" && (
            <label>
              Accepted replacement memory ID
              <input
                disabled={review.isPending}
                required
                value={replacement}
                pattern="[0-9a-fA-F-]{36}"
                onChange={(event) => {
                  setReplacement(event.target.value);
                  setIntent({ ...intent, key: crypto.randomUUID() });
                }}
              />
            </label>
          )}
          <p>
            Confirming uses version {intent.entry.version ?? 1}. A concurrent
            change requires a fresh review.
          </p>
          {review.isError && <p role="alert">{error}</p>}
          <button
            type="submit"
            disabled={
              review.isPending ||
              !reason.trim() ||
              (intent.action === "supersede" && !replacement)
            }
          >
            Confirm {intent.action}
          </button>
          <button
            type="button"
            disabled={review.isPending}
            onClick={() => {
              setIntent(null);
              heading.current?.focus();
            }}
          >
            Cancel review
          </button>
        </form>
      )}
      {historyId && (
        <section aria-label="Memory history">
          <h4>Version and review history</h4>
          {history.isPending && <p role="status">Loading history…</p>}
          {history.isError && <p role="alert">History is unavailable.</p>}
          {history.data?.versions.map((v) => (
            <p key={v.version}>
              Version {v.version}: {v.state} — {v.summary}
            </p>
          ))}
          {history.data?.events.map((e) => (
            <p key={e.version}>
              {e.action} by {e.actor_id}: {e.reason}
            </p>
          ))}
          {history.data?.partial && (
            <p>Showing the latest 20 versions and reviews.</p>
          )}
        </section>
      )}
      <div className="row">
        <button
          disabled={!pages.length}
          onClick={() => {
            setCursor(pages.at(-1));
            setPages(pages.slice(0, -1));
          }}
        >
          Previous memories
        </button>
        <button
          disabled={!queue.data?.next_cursor}
          onClick={() => {
            setPages([...pages, cursor]);
            setCursor(queue.data?.next_cursor ?? undefined);
          }}
        >
          Next memories
        </button>
      </div>
    </section>
  );
}
