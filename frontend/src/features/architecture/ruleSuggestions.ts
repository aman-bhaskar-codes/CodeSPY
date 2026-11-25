import type { ArchitectureRuleSet } from "../../api/client";

export type RuleDraft = ArchitectureRuleSet["rules"][number];
export type RuleKind = RuleDraft["kind"];

export const MAX_RULES = 16;
export const MAX_GROUPS = 8;
export const MAX_PREFIXES = 8;

/** Ancestor directories of every file label, sorted and de-duplicated (no trailing slash). */
export function prefixSuggestions(labels: readonly string[]): string[] {
  const found = new Set<string>();
  for (const label of labels) {
    const parts = label.split("/");
    for (let i = 1; i < parts.length; i += 1) {
      found.add(parts.slice(0, i).join("/"));
    }
  }
  return [...found].sort();
}

function badPrefix(prefix: string): boolean {
  if (!prefix || prefix.includes("\\") || prefix.includes("\0")) return true;
  if (new TextEncoder().encode(prefix).length > 1024) return true;
  return prefix
    .replace(/\/$/, "")
    .split("/")
    .some((part) => part === "" || part === "." || part === "..");
}

/** The first problem with a draft rule, mirroring the server's contract, or null when it is valid. */
export function ruleProblem(
  rule: RuleDraft,
  existingNames: readonly string[],
): string | null {
  if (!/^[A-Za-z0-9][A-Za-z0-9 _.-]{0,63}$/.test(rule.name)) {
    return "Name: 1 to 64 letters, digits, spaces, dots, dashes or underscores, starting with a letter or digit.";
  }
  if (existingNames.includes(rule.name)) return "Rule names must be unique.";
  if (rule.groups.length < 2 || rule.groups.length > MAX_GROUPS) {
    return `A rule needs 2 to ${MAX_GROUPS} groups.`;
  }
  if (rule.kind === "forbidden" && rule.groups.length !== 2) {
    return "A forbidden rule has exactly two groups: importer, then imported.";
  }
  const seen = new Set<string>();
  for (const group of rule.groups) {
    if (group.length < 1 || group.length > MAX_PREFIXES) {
      return `Each group needs 1 to ${MAX_PREFIXES} path prefixes.`;
    }
    for (const prefix of group) {
      if (badPrefix(prefix)) {
        return `"${prefix}" is not a valid path prefix (no empty, ".", ".." segments or backslashes).`;
      }
      const clean = prefix.replace(/\/$/, "");
      if (seen.has(clean)) return `"${clean}" appears more than once.`;
      seen.add(clean);
    }
  }
  return null;
}

/** Normalise prefixes the way the server does (trailing slash removed). */
export function normalizeRule(rule: RuleDraft): RuleDraft {
  return {
    ...rule,
    groups: rule.groups.map((g) => g.map((p) => p.replace(/\/$/, ""))),
  };
}

export const KIND_HINT: Record<RuleKind, string> = {
  forbidden: "Group 1 may not import group 2.",
  layers:
    "Groups run from the top layer to the bottom; a lower layer may not import a higher one.",
  independence: "No group may import another group.",
};

export const RULE_STATUS_LABEL = {
  passed: "Passed",
  violated: "Violated",
  unknown: "Unknown",
} as const;

export const RULE_STATUS_TONE = {
  passed: "success",
  violated: "danger",
  unknown: "warning",
} as const;
