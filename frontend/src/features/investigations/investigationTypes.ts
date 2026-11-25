import { ApiError } from "../../api/client";
import type { InvestigationClaimV1 } from "./types";

export interface ExtendedSummary {
  held_micros?: number;
  failure_reason?: string | null;
  current_job_id?: string | null;
  spec_hash?: string | null;
}

export interface CitationItem {
  file_path?: string;
  path?: string;
  line_start?: number;
  line?: number;
  statement?: string;
  snippet?: string;
}

export interface ExtendedReport {
  claims?: InvestigationClaimV1[];
  coverage_notes?: string[];
  citations?: CitationItem[];
}

export interface ExtendedDetail {
  claims?: InvestigationClaimV1[];
  report?: ExtendedReport | null;
}

export interface EventShape {
  id?: string;
  sequence?: number;
  event_type?: string;
  type?: string;
  node_name?: string;
  node?: string;
}

export interface Props {
  repositoryId: string;
  snapshotId?: string;
  findingId?: string;
  findingRunId?: string;
  csrfToken: string;
  onFindingTriageRequested?: (findingId: string) => void;
}

export function operationProblem(error: unknown) {
  if (error instanceof ApiError) {
    if (error.status === 401)
      return "Your session expired. Sign in again before continuing.";
    if (error.status === 403)
      return "Access changed. Reload your repository permissions.";
    if (error.status === 409)
      return "The investigation changed. Reload its status before retrying.";
    if (error.status === 429)
      return "A usage limit was reached. Check the budget and try again later.";
    if (error.status === 503)
      return "Investigation execution is unavailable. Check the configured worker and model runtime.";
  }
  return "The request could not be confirmed. Retry the same action to check its outcome.";
}
