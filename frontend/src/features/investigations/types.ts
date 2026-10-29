/**
 * Types for Agentic Security Investigations workbench, events, and API contracts.
 */

export type {
  InvestigationCancelResponseV1,
  InvestigationClaimV1 as GeneratedInvestigationClaimV1,
  InvestigationCreateRequestV1,
  InvestigationCreateRequestV2,
  InvestigationDepth,
  InvestigationDetailResponseV1,
  InvestigationDetailResponseV2,
  InvestigationEventPageV1,
  InvestigationEventV1 as GeneratedInvestigationEventV1,
  InvestigationEventV2,
  InvestigationFeedbackRequestV1,
  InvestigationJobRecordV1,
  InvestigationListResponseV1,
  InvestigationListResponseV2,
  InvestigationMode,
  InvestigationResumeRequestV1,
  InvestigationRetryRequestV1,
  InvestigationStartResponseV1,
  InvestigationStatus,
  InvestigationSummaryV1,
  InvestigationSummaryV2,
  InvestigationTargetType,
} from "../../api/generated/types.gen";

import type {
  InvestigationDetailResponseV1,
  InvestigationDetailResponseV2,
  InvestigationEventV1 as GeneratedInvestigationEventV1,
  InvestigationEventV2,
  InvestigationSummaryV1,
  InvestigationSummaryV2,
} from "../../api/generated/types.gen";

export type InvestigationVerdict =
  "true_positive" | "false_positive" | "benign" | "inconclusive" | string;

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

export type InvestigationEvent =
  GeneratedInvestigationEventV1 | InvestigationEventV2;
export type InvestigationSummary =
  InvestigationSummaryV1 | InvestigationSummaryV2;
export type InvestigationDetail =
  InvestigationDetailResponseV1 | InvestigationDetailResponseV2;
