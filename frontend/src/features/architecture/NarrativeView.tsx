import { useEffect, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { api, ApiError } from "../../api/client";
import type { ArchitectureNarrativeV1 } from "../../api/generated/types.gen";
import { Button } from "../../ui/Button";
import { InlineNotice } from "../../ui/InlineNotice";
import { StatusBadge } from "../../ui/StatusBadge";

interface Props {
  repositoryId: string;
  snapshotId: string;
  reportId: string;
  csrfToken: string;
}

const BASIS_LABEL = {
  observed: "Observed",
  derived: "Derived",
  inferred: "Inferred",
  unknown: "Unknown",
} as const;
const BASIS_TONE = {
  observed: "success",
  derived: "neutral",
  inferred: "warning",
  unknown: "danger",
} as const;
const WAIT_MS = 10 * 60 * 1000;

function problem(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.code === "inference_unavailable" || error.status === 503)
      return "Narrative writing is not available in this deployment: no qualified model runtime is configured.";
    if (error.status === 403)
      return "You need write access to start a narrative.";
    return `Request failed (${error.code}).`;
  }
  return "Request failed.";
}

export function NarrativeView(props: Props) {
  // A narrative belongs to one report; a new report starts clean.
  return <NarrativeState key={props.reportId} {...props} />;
}

function NarrativeState({
  repositoryId,
  snapshotId,
  reportId,
  csrfToken,
}: Props) {
  const [requestedAt, setRequestedAt] = useState<number | null>(null);
  // The narrative on screen when the request was made: a different one means the new run finished.
  const [baseline, setBaseline] = useState<string | null>(null);
  const [expired, setExpired] = useState(false);
  const narrative = useQuery({
    queryKey: ["architecture-narrative", repositoryId, snapshotId, reportId],
    queryFn: () =>
      api.architectureNarrative(repositoryId, snapshotId, reportId),
    refetchInterval: (query) => {
      const shown = query.state.data?.investigation_report_id ?? null;
      return requestedAt !== null && !expired && shown === baseline
        ? 4000
        : false;
    },
  });
  const shown = narrative.data?.investigation_report_id ?? null;
  const waiting = requestedAt !== null && !expired && shown === baseline;
  const start = useMutation({
    mutationFn: () =>
      api.architectureNarrativeStart(
        repositoryId,
        snapshotId,
        reportId,
        csrfToken,
      ),
    onSuccess: () => {
      setBaseline(narrative.data?.investigation_report_id ?? null);
      setExpired(false);
      setRequestedAt(Date.now());
    },
  });
  useEffect(() => {
    if (requestedAt === null || !waiting) return;
    const timer = window.setTimeout(
      () => setExpired(true),
      Math.max(0, requestedAt + WAIT_MS - Date.now()),
    );
    return () => window.clearTimeout(timer);
  }, [requestedAt, waiting]);

  return (
    <section aria-labelledby="architecture-narrative-heading">
      <h3 id="architecture-narrative-heading">Narrative</h3>
      <p className="muted">
        A short written summary of this report. Every sentence cites report
        statements and is checked against them by code; the wording is the
        model&apos;s, so a sentence that cannot be confirmed is marked
        unverified.
      </p>
      <div style={{ margin: "12px 0" }}>
        <Button
          intent="primary"
          pending={start.isPending}
          disabled={waiting}
          onClick={() => start.mutate()}
        >
          {narrative.data ? "Write a new narrative" : "Write narrative"}
        </Button>
      </div>
      {start.isError && (
        <InlineNotice tone="danger">{problem(start.error)}</InlineNotice>
      )}
      {waiting && (
        <p role="status">
          Narrative requested. It appears here when the investigation finishes.
        </p>
      )}
      {expired && shown === baseline && (
        <InlineNotice tone="info">
          No narrative has been published yet. It may still be running or may
          have stopped; check the investigation list.
        </InlineNotice>
      )}
      {narrative.isError && (
        <InlineNotice tone="danger">{problem(narrative.error)}</InlineNotice>
      )}
      {narrative.data && <NarrativeBody narrative={narrative.data} />}
    </section>
  );
}

function NarrativeBody({ narrative }: { narrative: ArchitectureNarrativeV1 }) {
  const draft = narrative.narrative;
  const verdicts = new Map(
    (draft.verifications ?? []).map((v) => [v.claim_id, v]),
  );
  const cited = new Map((narrative.cited ?? []).map((c) => [c.sid, c]));
  return (
    <div>
      <p role="status">
        {draft.confirmed} of {draft.total} sentences confirmed against the
        report
        {draft.grounded ? "." : "; this narrative is not verified overall."}
      </p>
      {(draft.unnarrated_sections ?? []).length > 0 && (
        <InlineNotice tone="info">
          Not narrated: {(draft.unnarrated_sections ?? []).join(", ")}.
        </InlineNotice>
      )}
      {(draft.unparsed_units ?? 0) > 0 && (
        <InlineNotice tone="info">
          {draft.unparsed_units ?? 0} model replies could not be read and were
          left out.
        </InlineNotice>
      )}
      <ol aria-label="Narrative sentences">
        {(draft.sentences ?? []).map((sentence) => {
          const verdict = verdicts.get(sentence.claim_id);
          const confirmed = verdict?.status === "confirmed" && verdict.basis;
          const statements = (sentence.sids ?? [])
            .map((sid) => cited.get(sid))
            .filter((c) => c !== undefined);
          return (
            <li key={sentence.claim_id}>
              {confirmed ? (
                <StatusBadge tone={BASIS_TONE[verdict.basis!]}>
                  {BASIS_LABEL[verdict.basis!]}
                </StatusBadge>
              ) : (
                <StatusBadge tone="warning">Unverified</StatusBadge>
              )}{" "}
              <span className={confirmed ? undefined : "muted"}>
                {sentence.text}
              </span>
              {!confirmed && verdict?.reason && (
                <span className="muted"> ({verdict.reason})</span>
              )}
              {statements.length > 0 && (
                <details>
                  <summary>Cited statements ({statements.length})</summary>
                  <ul>
                    {statements.map((c) => (
                      <li key={c.sid}>
                        <StatusBadge>{c.kind}</StatusBadge> {c.text}{" "}
                        {(c.evidence ?? []).map((e) => (
                          <code key={e}>{e} </code>
                        ))}
                      </li>
                    ))}
                  </ul>
                </details>
              )}
            </li>
          );
        })}
      </ol>
    </div>
  );
}
