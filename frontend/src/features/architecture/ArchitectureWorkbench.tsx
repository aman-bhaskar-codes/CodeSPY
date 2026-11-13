import { useRef } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "../../api/client";
import type {
  RepositoryReportV1,
  SectionV1,
  TableV1,
} from "../../api/generated/types.gen";
import { Button } from "../../ui/Button";
import { InlineNotice } from "../../ui/InlineNotice";
import { Panel } from "../../ui/Panel";
import { StatusBadge } from "../../ui/StatusBadge";
import { ComponentGraph } from "./ComponentGraph";
import { RuleChecks } from "./RuleChecks";
import { prefixSuggestions } from "./ruleSuggestions";
import {
  KIND_LABEL,
  KIND_TONE,
  locationOf,
  statementText,
  type Templates,
} from "./statementText";

interface Props {
  repositoryId: string;
  snapshotId: string;
  csrfToken: string;
}

const TITLE: Record<string, string> = {
  overview: "Overview",
  components: "Components",
  dependencies: "Dependencies",
  api_surface: "API surface",
  data_model: "Data model",
  configuration: "Configuration",
  entry_points: "Entry points",
  coverage: "Coverage and limitations",
};
const ORDER = Object.keys(TITLE);

export function ArchitectureWorkbench(props: Props) {
  return (
    <ArchitectureWorkbenchState
      key={`${props.repositoryId}:${props.snapshotId}`}
      {...props}
    />
  );
}

function problem(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.code === "report_inputs_pending")
      return "Some analysis shards are still running. Build the report when every shard has finished.";
    if (error.code === "analysis_unavailable")
      return "Analysis is not available in this deployment.";
    return `Request failed (${error.code}).`;
  }
  return "Request failed.";
}

function ArchitectureWorkbenchState({
  repositoryId,
  snapshotId,
  csrfToken,
}: Props) {
  const queryClient = useQueryClient();
  const buildKey = useRef<string | null>(null);
  const scope = [repositoryId, snapshotId];
  const status = useQuery({
    queryKey: ["architecture-status", ...scope],
    queryFn: () => api.architectureStatus(repositoryId, snapshotId),
    refetchInterval: (query) => {
      const data = query.state.data;
      const started = data?.shards?.some((s) => s.state !== "not_started");
      const stuck = data?.shards?.some(
        (s) => s.state === "failed" || s.state === "cancelled",
      );
      return started && !data?.ready && !stuck ? 4000 : false;
    },
  });
  const latest = useQuery({
    queryKey: ["architecture-latest", ...scope],
    queryFn: () => api.architectureLatest(repositoryId, snapshotId),
  });
  const reportId = latest.data?.id;
  const report = useQuery({
    queryKey: ["architecture-report", ...scope, reportId],
    queryFn: () => api.architectureReport(repositoryId, snapshotId, reportId!),
    enabled: Boolean(reportId),
  });
  const graph = useQuery({
    queryKey: ["architecture-graph", ...scope, reportId],
    queryFn: () => api.architectureGraph(repositoryId, snapshotId, reportId!),
    enabled: Boolean(reportId),
  });
  const templates = useQuery({
    queryKey: ["architecture-templates"],
    queryFn: () => api.architectureTemplates(),
    staleTime: Infinity,
  });
  const prepare = useMutation({
    mutationFn: () =>
      api.architecturePrepare(repositoryId, snapshotId, csrfToken),
    onSuccess: (data) =>
      queryClient.setQueryData(["architecture-status", ...scope], data),
  });
  const build = useMutation({
    mutationFn: () => {
      buildKey.current ??= crypto.randomUUID();
      return api.architectureBuild(
        repositoryId,
        snapshotId,
        csrfToken,
        buildKey.current,
      );
    },
    onSettled: () => {
      buildKey.current = null;
      void queryClient.invalidateQueries({
        queryKey: ["architecture-latest", ...scope],
      });
    },
  });
  const shards = status.data?.shards ?? [];
  const started = shards.some((s) => s.state !== "not_started");
  const finished = shards.filter(
    (s) => s.state === "succeeded" || s.state === "partial",
  );
  return (
    <Panel
      id="architecture-report"
      title="Architecture report"
      actions={
        reportId ? (
          <a
            href={`/api/v1/repositories/${repositoryId}/snapshots/${snapshotId}/architecture-reports/${reportId}/markdown`}
            target="_blank"
            rel="noopener noreferrer"
          >
            Export Markdown
          </a>
        ) : undefined
      }
    >
      <p className="muted">
        An evidence-first report built from this snapshot&apos;s source facts.
        Every statement is labelled observed, derived, inferred or unknown.
      </p>
      {status.isError && (
        <InlineNotice tone="danger">{problem(status.error)}</InlineNotice>
      )}
      {status.data && (
        <div>
          <p role="status">
            {status.data.total_files} analyzable files in {shards.length}{" "}
            {shards.length === 1 ? "shard" : "shards"}
            {started ? `; ${finished.length} published` : "; not started"}.
            {status.data.skipped_files > 0 &&
              ` ${status.data.skipped_files} files cannot be selected and are not covered.`}
          </p>
          {shards.some(
            (s) => s.state === "failed" || s.state === "cancelled",
          ) && (
            <p role="alert">
              Some analyses failed or were cancelled, so the report cannot be
              built for this snapshot yet.
            </p>
          )}
          {started && shards.length > 1 && (
            <table>
              <thead>
                <tr>
                  <th>Shard</th>
                  <th>Files</th>
                  <th>State</th>
                </tr>
              </thead>
              <tbody>
                {shards.map((s) => (
                  <tr key={s.index}>
                    <td>{s.index + 1}</td>
                    <td>{s.files}</td>
                    <td>{s.state.replaceAll("_", " ")}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}
      <div style={{ display: "flex", gap: 8, margin: "12px 0" }}>
        <Button
          pending={prepare.isPending}
          disabled={!status.data || status.data.ready || shards.length === 0}
          onClick={() => prepare.mutate()}
        >
          {started ? "Continue analysis" : "Prepare analysis"}
        </Button>
        <Button
          intent="primary"
          pending={build.isPending}
          disabled={!status.data?.ready}
          onClick={() => build.mutate()}
        >
          Build report
        </Button>
      </div>
      {(prepare.isError || build.isError) && (
        <InlineNotice tone="danger">
          {problem(prepare.error ?? build.error)}
        </InlineNotice>
      )}
      {report.data && (
        <ReportView
          report={report.data}
          templates={templates.data?.templates}
          graph={graph.data}
          rules={{
            repositoryId,
            snapshotId,
            reportId: reportId ?? "",
            csrfToken,
          }}
        />
      )}
    </Panel>
  );
}

function ReportView({
  report,
  templates,
  graph,
  rules,
}: {
  report: RepositoryReportV1;
  templates: Templates | undefined;
  graph: Parameters<typeof ComponentGraph>[0]["graph"] | undefined;
  rules: {
    repositoryId: string;
    snapshotId: string;
    reportId: string;
    csrfToken: string;
  };
}) {
  const cov = report.coverage;
  const complete =
    cov.analyzed_files === cov.eligible_files && cov.omitted_files === 0;
  const sections = [...report.sections].sort(
    (a, b) => ORDER.indexOf(a.name) - ORDER.indexOf(b.name),
  );
  const components = report.sections
    .find((s) => s.name === "components")
    ?.tables?.find((t) => t.name === "components");
  const layers = new Map<string, number>();
  const counts = new Map<string, number>();
  for (const row of components?.rows ?? []) {
    counts.set(String(row[0]), Number(row[1]));
    if (Number(row[5]) >= 0) layers.set(String(row[0]), Number(row[5]));
  }
  return (
    <div>
      {!complete && (
        <InlineNotice tone="danger">
          Partial coverage: {cov.analyzed_files} of {cov.eligible_files}{" "}
          eligible files were analyzed. Everything below describes only those
          files.
        </InlineNotice>
      )}
      {sections.map((section) => (
        <SectionView
          key={section.name}
          section={section}
          templates={templates}
        />
      ))}
      {graph && layers.size > 0 && (
        <section aria-labelledby="architecture-graph-heading">
          <h3 id="architecture-graph-heading">Component graph</h3>
          <ComponentGraph layers={layers} graph={graph} fileCounts={counts} />
        </section>
      )}
      {rules.reportId && (
        <RuleChecks
          {...rules}
          inputsDigest={report.inputs_digest}
          suggestions={prefixSuggestions(
            (graph?.nodes ?? [])
              .filter((n) => n.kind === "file")
              .map((n) => n.label),
          )}
          suggestionsNote={
            graph && graph.nodes_total > graph.nodes.length
              ? `Suggestions are based on the first ${graph.nodes.length} of ${graph.nodes_total} files.`
              : undefined
          }
        />
      )}
    </div>
  );
}

function SectionView({
  section,
  templates,
}: {
  section: SectionV1;
  templates: Templates | undefined;
}) {
  return (
    <section aria-labelledby={`architecture-${section.name}`}>
      <h3 id={`architecture-${section.name}`}>
        {TITLE[section.name] ?? section.name}
      </h3>
      <ul style={{ listStyle: "none", padding: 0 }}>
        {(section.statements ?? []).map((statement, index) => {
          const evidence = statement.evidence ?? [];
          const total = statement.evidence_total ?? evidence.length;
          return (
            <li key={`${statement.code}:${index}`} style={{ margin: "6px 0" }}>
              <StatusBadge tone={KIND_TONE[statement.kind]}>
                {KIND_LABEL[statement.kind]}
              </StatusBadge>{" "}
              {statementText(templates, statement)}
              {evidence.length > 0 && (
                <span className="muted">
                  {" "}
                  (evidence:{" "}
                  {evidence.slice(0, 3).map((ref, i) => (
                    <code key={`${locationOf(ref)}:${i}`}>
                      {i > 0 ? ", " : ""}
                      {locationOf(ref)}
                    </code>
                  ))}
                  {total > 3 ? ` and ${total - 3} more` : ""})
                </span>
              )}
            </li>
          );
        })}
      </ul>
      {(section.tables ?? []).map((table) => (
        <ReportTable key={table.name} table={table} />
      ))}
    </section>
  );
}

function ReportTable({ table }: { table: TableV1 }) {
  const rows = table.rows ?? [];
  if (rows.length === 0) return null;
  const hidden = (table.rows_total ?? rows.length) - rows.length;
  return (
    <div style={{ overflowX: "auto" }}>
      <table aria-label={table.name.replaceAll("_", " ")}>
        <thead>
          <tr>
            {table.columns.map((c) => (
              <th key={c}>{c.replaceAll("_", " ")}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={i}>
              {row.map((cell, j) => (
                <td key={j}>{String(cell)}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {hidden > 0 && <p className="muted">{hidden} more rows not shown.</p>}
    </div>
  );
}
