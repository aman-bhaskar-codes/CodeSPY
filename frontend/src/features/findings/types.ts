import type {
  FindingSummaryV1,
  TriageTransitionV1,
} from "../../api/generated/types.gen";

export type {
  FeatureTraceV1,
  FindingDetailResponseV1,
  FindingListResponseV1,
  FindingSummaryV1,
  ScanSummaryResponseV1,
  TraceBudgetV1,
  TraceEdgeV1,
  TraceNodeV1,
  TriageHistoryItemV1,
  TriageHistoryV1,
  TriageRecordV1,
  TriageTransitionV1,
} from "../../api/generated/types.gen";

export type FindingCategory = FindingSummaryV1["category"];
export type FindingSeverity = FindingSummaryV1["severity"];
export type FindingDisposition = FindingSummaryV1["disposition"];
export type TriageState = TriageTransitionV1["state"];
export type TriageReason = NonNullable<TriageTransitionV1["reason"]>;
