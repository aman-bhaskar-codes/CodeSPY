import type { StatementV1 } from "../../api/generated/types.gen";

export type Templates = Record<string, string>;

/** Render a typed statement from the server's templates; unknown codes fall back to code and params. */
export function statementText(
  templates: Templates | undefined,
  statement: StatementV1,
): string {
  const params = statement.params ?? {};
  const template = templates?.[statement.code];
  if (template === undefined) {
    const pairs = Object.entries(params)
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([key, value]) => `${key}=${String(value)}`)
      .join(", ");
    return pairs ? `${statement.code} (${pairs})` : statement.code;
  }
  return template.replace(/\{(\w+)\}/g, (_match, name: string) =>
    name in params ? String(params[name]) : "?",
  );
}

export const KIND_LABEL: Record<StatementV1["kind"], string> = {
  observation: "Observed",
  derivation: "Derived",
  inference: "Inferred",
  unknown: "Unknown",
};

export const KIND_TONE: Record<
  StatementV1["kind"],
  "neutral" | "success" | "warning" | "danger"
> = {
  observation: "success",
  derivation: "neutral",
  inference: "warning",
  unknown: "danger",
};

export function locationOf(ref: {
  path: string;
  range?: { start_line: number } | null;
}): string {
  return ref.range ? `${ref.path}:${ref.range.start_line}` : ref.path;
}
