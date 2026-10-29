import type {
  ClaimVerificationStatus,
  ClaimVerificationV1,
  InvestigationClaimV2,
} from "../../api/generated/types.gen";

export interface ClaimView {
  claim_id: string;
  statement: string;
  status: ClaimVerificationStatus | "pending" | "verified";
  file_path?: string | null;
  line_start?: number | null;
  line_end?: number | null;
  quoted_source?: string | null;
  verification_notes?: string | null;
}

function record(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

export function reportClaims(
  report: unknown,
  currentSupport: boolean,
): ClaimView[] {
  if (!record(report) || !Array.isArray(report.claims)) return [];
  const verifications = Array.isArray(report.verifications)
    ? report.verifications
    : [];
  const statuses: ReadonlySet<string> = new Set<ClaimVerificationStatus>([
    "source_correspondence_confirmed",
    "unsupported",
    "refuted",
    "location_valid_quote_missing",
    "evidence_unavailable",
  ]);
  return report.claims
    .filter(
      (value): value is InvestigationClaimV2 =>
        record(value) &&
        typeof value.claim_id === "string" &&
        typeof value.statement === "string",
    )
    .slice(0, 100)
    .map((claim) => {
      const verification = verifications.find(
        (value): value is ClaimVerificationV1 =>
          record(value) &&
          value.claim_id === claim.claim_id &&
          typeof value.status === "string" &&
          statuses.has(value.status),
      );
      return {
        claim_id: claim.claim_id,
        statement: claim.statement,
        status: !currentSupport
          ? "evidence_unavailable"
          : (verification?.status ?? "unsupported"),
        file_path:
          currentSupport && typeof verification?.resolved_file === "string"
            ? verification.resolved_file
            : undefined,
        line_start:
          currentSupport &&
          typeof verification?.resolved_line_start === "number"
            ? verification.resolved_line_start
            : undefined,
        line_end:
          currentSupport && typeof verification?.resolved_line_end === "number"
            ? verification.resolved_line_end
            : undefined,
        verification_notes:
          typeof verification?.notes === "string"
            ? verification.notes
            : undefined,
      };
    });
}

export function reportCaveats(
  report: unknown,
): { label: string; items: string[] }[] {
  if (!record(report)) return [];
  return (
    [
      ["unknowns", "Unanswered questions"],
      ["limitations", "Report limitations"],
    ] as const
  )
    .map(([key, label]) => ({
      label,
      items: Array.isArray(report[key])
        ? report[key]
            .filter((value): value is string => typeof value === "string")
            .slice(0, 100)
        : [],
    }))
    .filter((group) => group.items.length > 0);
}
