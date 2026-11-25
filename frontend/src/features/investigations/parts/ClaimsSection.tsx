import { reportClaims, reportCaveats, type ClaimView } from "../reportView";
import { type CitationItem, type ExtendedDetail } from "../investigationTypes";
import { useInvestigations } from "../investigationsContext";

/** Verified claims with their citations. */
export function ClaimsSection() {
  const { detail: maybeDetail, getClaimBadgeStyle } = useInvestigations();
  if (!maybeDetail) return null;
  const detail = maybeDetail;
  return (
    <>
      {(() => {
        const extDetail = detail as unknown as ExtendedDetail;
        const claims: ClaimView[] = Array.isArray(extDetail?.claims)
          ? extDetail.claims
          : reportClaims(
              detail && "report" in detail ? detail.report : undefined,
              !("current_support_available" in (detail ?? {})) ||
                (detail &&
                  "current_support_available" in detail &&
                  detail.current_support_available === true),
            );

        const coverageNotes: string[] = Array.isArray(
          extDetail?.report?.coverage_notes,
        )
          ? (extDetail.report.coverage_notes ?? [])
          : typeof extDetail?.report?.coverage_notes === "string"
            ? [extDetail.report.coverage_notes]
            : [];

        const evidenceRefs =
          detail &&
          "report" in detail &&
          Array.isArray(detail.report?.evidence_refs)
            ? detail.report.evidence_refs.filter(
                (value): value is string => typeof value === "string",
              )
            : [];

        const citations: CitationItem[] = Array.isArray(
          extDetail?.report?.citations,
        )
          ? (extDetail.report.citations ?? [])
          : [];

        return (
          <div>
            <p className="muted">
              Source correspondence checks a citation against retained evidence
              at publication. It does not establish that a security conclusion
              is true or remains current.
            </p>
            <h4
              style={{
                margin: "0 0 12px",
                fontSize: "14px",
                fontWeight: 600,
                color: "var(--cs-action)",
              }}
            >
              Evidence Claims ({claims.length})
            </h4>
            {claims.length === 0 ? (
              <p
                style={{
                  fontSize: "13px",
                  color: "var(--cs-muted)",
                  fontStyle: "italic",
                }}
              >
                No specific factual claims extracted yet.
              </p>
            ) : (
              <div
                style={{
                  display: "flex",
                  flexDirection: "column",
                  gap: "10px",
                }}
              >
                {claims.map((claim) => {
                  const badge = getClaimBadgeStyle(claim.status);
                  return (
                    <div
                      key={claim.claim_id}
                      style={{
                        padding: "12px",
                        background: "var(--cs-panel)",
                        borderRadius: "6px",
                        border: "1px solid var(--cs-border)",
                      }}
                    >
                      <div
                        style={{
                          display: "flex",
                          justifyContent: "space-between",
                          alignItems: "center",
                          marginBottom: "6px",
                        }}
                      >
                        <strong
                          style={{
                            fontSize: "13px",
                            color: "var(--cs-text)",
                          }}
                        >
                          {claim.statement}
                        </strong>
                        <span
                          style={{
                            fontSize: "10px",
                            fontWeight: 700,
                            padding: "2px 6px",
                            borderRadius: "4px",
                            background: badge.background,
                            color: badge.color,
                          }}
                        >
                          {badge.label}
                        </span>
                      </div>
                      {claim.file_path && (
                        <div
                          style={{
                            fontSize: "12px",
                            color: "var(--cs-muted)",
                            marginBottom: "4px",
                          }}
                        >
                          Source:{" "}
                          <code>
                            {claim.file_path}:{claim.line_start ?? 1}
                          </code>
                        </div>
                      )}
                      {claim.quoted_source && (
                        <pre
                          style={{
                            margin: "6px 0",
                            padding: "8px",
                            background: "var(--cs-canvas)",
                            borderRadius: "4px",
                            fontSize: "11px",
                            color: "var(--cs-action)",
                            overflowX: "auto",
                          }}
                        >
                          {claim.quoted_source}
                        </pre>
                      )}
                      {claim.verification_notes && (
                        <div
                          style={{
                            fontSize: "11px",
                            color: "var(--cs-muted)",
                            fontStyle: "italic",
                          }}
                        >
                          Verification check: {claim.verification_notes}
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            )}

            {/* Coverage Notes */}
            {coverageNotes.length > 0 && (
              <div
                id="coverage-notes-section"
                style={{
                  marginTop: "16px",
                  padding: "12px",
                  background: "var(--cs-panel)",
                  borderRadius: "6px",
                  border: "1px solid var(--cs-border)",
                }}
              >
                <h5
                  style={{
                    margin: "0 0 8px",
                    fontSize: "12px",
                    color: "var(--cs-action)",
                    fontWeight: 600,
                  }}
                >
                  Coverage Notes
                </h5>
                <ul
                  style={{
                    margin: 0,
                    paddingLeft: "18px",
                    fontSize: "12px",
                    color: "var(--cs-muted)",
                  }}
                >
                  {coverageNotes.map((note, idx) => (
                    <li key={idx} style={{ marginBottom: "4px" }}>
                      {note}
                    </li>
                  ))}
                </ul>
              </div>
            )}

            {reportCaveats(
              detail && "report" in detail ? detail.report : undefined,
            ).map((group) => (
              <section key={group.label} aria-label={group.label}>
                <h4>{group.label}</h4>
                <ul>
                  {group.items.map((item, index) => (
                    <li key={index}>{item}</li>
                  ))}
                </ul>
              </section>
            ))}
            {evidenceRefs.length > 0 && (
              <div>
                <h4>Evidence references ({evidenceRefs.length})</h4>
                <ul>
                  {evidenceRefs.map((id) => (
                    <li key={id}>
                      <code>{id}</code>
                    </li>
                  ))}
                </ul>
              </div>
            )}
            {/* Citations */}
            {citations.length > 0 && (
              <div
                id="citations-section"
                style={{
                  marginTop: "16px",
                  padding: "12px",
                  background: "var(--cs-panel)",
                  borderRadius: "6px",
                  border: "1px solid var(--cs-border)",
                }}
              >
                <h5
                  style={{
                    margin: "0 0 8px",
                    fontSize: "12px",
                    color: "var(--cs-action)",
                    fontWeight: 600,
                  }}
                >
                  Citations & References ({citations.length})
                </h5>
                <div
                  style={{
                    display: "flex",
                    flexDirection: "column",
                    gap: "8px",
                  }}
                >
                  {citations.map((c, idx) => (
                    <div
                      key={idx}
                      style={{
                        fontSize: "11px",
                        color: "var(--cs-muted)",
                        padding: "6px",
                        background: "var(--cs-canvas)",
                        borderRadius: "4px",
                      }}
                    >
                      <code>
                        {c.file_path || c.path || "source"}:
                        {c.line_start ?? c.line ?? 1}
                      </code>
                      {c.statement && (
                        <div
                          style={{
                            color: "var(--cs-text)",
                            marginTop: "2px",
                          }}
                        >
                          {c.statement}
                        </div>
                      )}
                      {c.snippet && (
                        <pre
                          style={{
                            margin: "4px 0 0",
                            padding: "4px 6px",
                            background: "var(--cs-panel)",
                            borderRadius: "3px",
                            color: "var(--cs-action)",
                            overflowX: "auto",
                          }}
                        >
                          {c.snippet}
                        </pre>
                      )}
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
        );
      })()}
    </>
  );
}
