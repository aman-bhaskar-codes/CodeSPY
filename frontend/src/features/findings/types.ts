export type FindingCategory = "sast" | "secrets" | "sca" | "iac";

export type FindingSeverity = "critical" | "high" | "medium" | "low" | "info";

export type FindingDisposition =
  | "proposed"
  | "supported-static"
  | "contradicted"
  | "unresolved";

export type TriageState =
  | "unreviewed"
  | "confirmed"
  | "false_positive"
  | "mitigated"
  | "accepted_risk";

export type TriageReason =
  | "none"
  | "code_refactor"
  | "compensating_control"
  | "business_context"
  | "test_code"
  | "vendor_dependency";

export interface FindingSummaryV1 {
  id: string;
  rule_id: string;
  category: FindingCategory;
  severity: FindingSeverity;
  disposition: FindingDisposition;
  file_path: string;
  line_number: number | null;
  message: string;
  triage_state: TriageState;
  triage_reason: TriageReason;
  triage_updated_at: string | null;
  triage_version: number;
}

export interface FindingListResponseV1 {
  schema_version: 1;
  items: FindingSummaryV1[];
  next_cursor: string | null;
  total_count: number;
}

export interface CodeExcerptV1 {
  start_line: number;
  end_line: number;
  content: string;
  highlighted_lines: number[];
}

export interface FindingSupportV1 {
  evidence_id: string;
  rule_id: string;
  message: string;
  disposition: FindingDisposition;
}

export interface FindingDetailResponseV1 {
  schema_version: 1;
  finding: FindingSummaryV1;
  excerpt: CodeExcerptV1 | null;
  remediation: string | null;
  support: FindingSupportV1[];
}

export interface ScanSummaryResponseV1 {
  schema_version: 1;
  run_id: string;
  snapshot_id: string;
  status: string;
  created_at: string;
  completed_at: string | null;
  counts_by_severity: Record<string, number>;
  counts_by_category: Record<string, number>;
  counts_by_disposition: Record<string, number>;
  counts_by_triage_state: Record<string, number>;
  total_findings: number;
}

export interface TriageTransitionV1 {
  state: TriageState;
  reason: TriageReason;
  expected_version: number;
}

export interface TriageRecordV1 {
  schema_version: 1;
  finding_id: string;
  current_state: TriageState;
  current_reason: TriageReason;
  updated_at: string;
  version: number;
}

export interface TriageHistoryItemV1 {
  event_id: string;
  state: TriageState;
  reason: TriageReason;
  principal_id: string;
  created_at: string;
}

export interface TriageHistoryV1 {
  schema_version: 1;
  finding_id: string;
  events: TriageHistoryItemV1[];
}

export interface TraceBudgetV1 {
  max_hops?: number;
  max_nodes?: number;
  max_edges?: number;
}

export interface TraceNodeV1 {
  id: string;
  kind: string;
  label: string;
  file_path: string | null;
  line_number: number | null;
}

export interface TraceEdgeV1 {
  source: string;
  target: string;
  kind: string;
}

export interface FeatureTraceV1 {
  schema_version: 1;
  finding_id: string;
  nodes: TraceNodeV1[];
  edges: TraceEdgeV1[];
  truncated: boolean;
  route_endpoints: string[];
}
