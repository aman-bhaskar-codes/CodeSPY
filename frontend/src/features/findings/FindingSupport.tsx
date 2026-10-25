interface Props {
  category: string;
  disposition: string;
  historicalDisposition: string;
  supportReason: string;
  excerpt?: string | null;
  excerptReason?: string;
}

const explanations: Record<string, string> = {
  source_unavailable: "Source support is no longer available.",
  bundle_unavailable: "The retained advisory bundle is unavailable.",
  advisory_not_current: "The advisory no longer provides current support.",
  reader_unavailable: "The isolated source reader is unavailable.",
  support_unavailable: "A currently supported source span is unavailable.",
  secret_format_excluded: "Secret-format matches are hidden.",
  unsupported_entity: "This finding has no source span to preview.",
  privacy_omitted: "The source privacy policy omits this preview.",
  range_unavailable:
    "The finding span cannot be safely mapped within the preview limit.",
};

export function FindingSupport({
  category,
  disposition,
  historicalDisposition,
  supportReason,
  excerpt,
  excerptReason,
}: Props) {
  const hidden = category === "secret_format";
  const reason = hidden ? "secret_format_excluded" : excerptReason;
  return (
    <section aria-label="Finding evidence">
      <dl>
        <dt>Current support</dt>
        <dd>{disposition}</dd>
        <dt>Historical decision</dt>
        <dd>{historicalDisposition}</dd>
      </dl>
      {supportReason !== "current_support" && (
        <p>
          {explanations[supportReason] ??
            "Current support could not be established."}
        </p>
      )}
      {!hidden && excerpt ? (
        <>
          <h4>Redacted finding span</h4>
          <pre
            style={{
              overflowX: "auto",
              whiteSpace: "pre-wrap",
              overflowWrap: "anywhere",
            }}
          >
            <code>{excerpt}</code>
          </pre>
        </>
      ) : (
        <p className="muted">
          {explanations[reason ?? ""] ?? "A source preview is unavailable."}
        </p>
      )}
    </section>
  );
}
