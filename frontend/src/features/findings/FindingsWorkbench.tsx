import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../api/client";
import { FindingDetailDrawer } from "./FindingDetailDrawer";
import { FindingsSummaryCard } from "./FindingsSummaryCard";
import { FindingsTable } from "./FindingsTable";
import type { TriageTransitionV1 } from "./types";

interface Props {
  repositoryId: string;
  snapshotId: string;
  runId: string;
  csrfToken: string;
}

export function FindingsWorkbench({
  repositoryId,
  snapshotId,
  runId,
  csrfToken,
}: Props) {
  const [selectedFindingId, setSelectedFindingId] = useState<string | null>(
    null,
  );
  const [cursor, setCursor] = useState<string | null>(null);
  const [filters, setFilters] = useState({
    category: "all",
    severity: "all",
    disposition: "all",
    triage_state: "all",
  });

  const summaryQuery = useQuery({
    queryKey: ["scan-summary", repositoryId, snapshotId, runId],
    queryFn: ({ signal }) =>
      api.scanSummary(repositoryId, snapshotId, runId, signal),
    enabled: !!repositoryId && !!snapshotId && !!runId,
  });

  const findingsQuery = useQuery({
    queryKey: [
      "findings-list",
      repositoryId,
      snapshotId,
      runId,
      filters,
      cursor,
    ],
    queryFn: ({ signal }) =>
      api.findingsList(
        repositoryId,
        snapshotId,
        runId,
        {
          category: filters.category,
          severity: filters.severity,
          disposition: filters.disposition,
          triage_state: filters.triage_state,
          cursor: cursor ?? undefined,
          limit: 50,
        },
        signal,
      ),
    enabled: !!repositoryId && !!snapshotId && !!runId,
  });

  const handleTriageSubmit = async (
    findingId: string,
    transition: TriageTransitionV1,
    requestKey: string,
  ) => {
    await api.triageFinding(
      repositoryId,
      snapshotId,
      findingId,
      transition,
      csrfToken,
      requestKey,
    );
    setCursor(null);
    await findingsQuery.refetch();
    await summaryQuery.refetch();
  };

  const handleNextPage = () => {
    if (findingsQuery.data?.next_cursor) {
      setCursor(findingsQuery.data.next_cursor);
    }
  };

  const handleResetPagination = () => {
    setCursor(null);
  };

  return (
    <div className="findings-workbench" style={{ margin: "24px 0" }}>
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          marginBottom: "16px",
        }}
      >
        <h2 style={{ margin: 0, fontSize: "20px", color: "var(--ink)" }}>
          Deterministic Security Findings
        </h2>
        <span className="muted" style={{ fontSize: "13px" }}>
          Scan run: <code>{runId.slice(0, 8)}…</code>
        </span>
      </div>

      {summaryQuery.isLoading && (
        <div
          style={{
            textAlign: "center",
            padding: "30px",
            color: "var(--muted)",
          }}
        >
          Loading scan findings summary...
        </div>
      )}

      {summaryQuery.isError && (
        <div
          role="alert"
          style={{
            padding: "16px",
            background: "#fde8e8",
            border: "1px solid var(--danger)",
            borderRadius: "6px",
            color: "var(--danger)",
            marginBottom: "20px",
          }}
        >
          Failed to load scan findings summary. Check repository permissions and
          scan run state.
        </div>
      )}

      {summaryQuery.data && <FindingsSummaryCard summary={summaryQuery.data} />}

      {findingsQuery.data && (
        <FindingsTable
          data={findingsQuery.data}
          filters={filters}
          onFilterChange={setFilters}
          onSelectFinding={setSelectedFindingId}
          onTriageFinding={handleTriageSubmit}
          hasNextPage={!!findingsQuery.data.next_cursor}
          onNextPage={handleNextPage}
          onResetPagination={handleResetPagination}
          isLoading={findingsQuery.isFetching}
        />
      )}

      {/* Finding Detail Drawer */}
      {selectedFindingId && (
        <FindingDetailDrawer
          repositoryId={repositoryId}
          snapshotId={snapshotId}
          findingId={selectedFindingId}
          csrfToken={csrfToken}
          isOpen={true}
          onClose={() => setSelectedFindingId(null)}
          onTriageUpdated={() => {
            findingsQuery.refetch();
            summaryQuery.refetch();
          }}
        />
      )}
    </div>
  );
}
