/**
 * Types for Agentic Investigations workbench and streaming events.
 */

export type InvestigationTargetType =
  "finding" | "repository" | "commit" | "diff";

export type InvestigationStatus =
  | "pending"
  | "running"
  | "checkpointed"
  | "awaiting_human_decision"
  | "completed"
  | "failed"
  | "cancelled";

export type InvestigationMode = "short_path" | "two_specialist";

export type InvestigationDepth = "quick" | "standard" | "deep";

export type InvestigationVerdict =
  "true_positive" | "false_positive" | "indeterminate" | "benign";

export interface InvestigationClaimV1 {
  claim_id: string;
  statement: string;
  quoted_source?: string | null;
  file_path?: string | null;
  line_start?: number | null;
  line_end?: number | null;
  status: "pending" | "verified" | "refuted" | "unsupported";
  verification_notes?: string | null;
}

export interface InvestigationEventV1 {
  id: string;
  org_id: string;
  investigation_id: string;
  step_index: number;
  event_type: string;
  node_name: string;
  payload: Record<string, unknown>;
  created_at: string;
}

export interface InvestigationSummaryV1 {
  id: string;
  org_id: string;
  repository_id: string;
  finding_id?: string | null;
  target_type: InvestigationTargetType;
  query: string;
  status: InvestigationStatus;
  mode: InvestigationMode;
  depth: InvestigationDepth;
  current_step: number;
  max_steps: number;
  fencing_token: number;
  worker_id?: string | null;
  budget_limit_micros: number;
  spent_micros: number;
  verdict?: InvestigationVerdict | null;
  verdict_confidence?: number | null;
  summary?: string | null;
  failure_reason?: string | null;
  created_at: string;
  updated_at: string;
}

export interface InvestigationDetailResponseV1 {
  summary: InvestigationSummaryV1;
  latest_state?: {
    history?: Array<{ role: string; content: string }>;
    tool_history?: Array<{
      step: number;
      tool: string;
      arguments: Record<string, unknown>;
      result: Record<string, unknown>;
    }>;
    collected_evidence?: Array<{
      step: number;
      source_tool: string;
      data: Record<string, unknown>;
    }>;
    claims?: InvestigationClaimV1[];
  } | null;
  claims: InvestigationClaimV1[];
  events_count: number;
}

export interface InvestigationCreateRequestV1 {
  repository_id: string;
  query: string;
  target_type?: InvestigationTargetType;
  finding_id?: string | null;
  mode?: InvestigationMode;
  depth?: InvestigationDepth;
  max_steps?: number;
  budget_limit_micros?: number;
}

export interface InvestigationFeedbackRequestV1 {
  feedback_type: "agree" | "disagree" | "needs_review" | "comment";
  notes: string;
  save_to_memory?: boolean;
  memory_topic?: string;
}

export interface InvestigationListResponseV1 {
  items: InvestigationSummaryV1[];
  total_count: number;
}
