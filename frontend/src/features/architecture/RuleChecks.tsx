import { useId, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { api, ApiError, type ArchitectureRuleSet } from "../../api/client";
import type { RuleCheckV1 } from "../../api/generated/types.gen";
import { Button } from "../../ui/Button";
import { InlineNotice } from "../../ui/InlineNotice";
import { StatusBadge } from "../../ui/StatusBadge";
import {
  KIND_HINT,
  MAX_GROUPS,
  MAX_PREFIXES,
  MAX_RULES,
  RULE_STATUS_LABEL,
  RULE_STATUS_TONE,
  normalizeRule,
  ruleProblem,
  type RuleDraft,
  type RuleKind,
} from "./ruleSuggestions";
import { locationOf } from "./statementText";

interface Props {
  repositoryId: string;
  snapshotId: string;
  reportId: string;
  inputsDigest: string;
  csrfToken: string;
  suggestions: readonly string[];
  suggestionsNote?: string;
}

function ruleError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 422) return "The server rejected these rules.";
    if (error.status === 404) return "This report is no longer available.";
    return `Request failed (${error.code}).`;
  }
  return "Request failed.";
}

export function RuleChecks(props: Props) {
  // Rules and results belong to one report: a new report starts clean.
  return <RuleChecksState key={props.reportId} {...props} />;
}

function RuleChecksState({
  repositoryId,
  snapshotId,
  reportId,
  inputsDigest,
  csrfToken,
  suggestions,
  suggestionsNote,
}: Props) {
  const ids = useId();
  const listId = `${ids}-prefixes`;
  const [rules, setRules] = useState<RuleDraft[]>([]);
  const [name, setName] = useState("");
  const [kind, setKind] = useState<RuleKind>("forbidden");
  const [groups, setGroups] = useState<string[]>(["", ""]);
  const [indirect, setIndirect] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  const check = useMutation({
    mutationFn: () =>
      api.architectureRuleCheck(
        repositoryId,
        snapshotId,
        reportId,
        { rules } satisfies ArchitectureRuleSet,
        csrfToken,
      ),
  });

  function parsedGroups(): string[][] {
    return groups.map((g) =>
      g
        .split(",")
        .map((p) => p.trim())
        .filter((p) => p.length > 0),
    );
  }

  function addRule() {
    const draft = normalizeRule({
      name: name.trim(),
      kind,
      groups: parsedGroups(),
      ...(kind !== "layers" && indirect ? { allow_indirect: true } : {}),
    });
    if (rules.length >= MAX_RULES) {
      setFormError(`At most ${MAX_RULES} rules.`);
      return;
    }
    const problem = ruleProblem(
      draft,
      rules.map((r) => r.name),
    );
    if (problem) {
      setFormError(problem);
      return;
    }
    setFormError(null);
    setRules([...rules, draft]);
    setName("");
    setGroups(["", ""]);
    setIndirect(false);
    check.reset();
  }

  const result: RuleCheckV1 | undefined = check.data;
  const stale = result !== undefined && result.inputs_digest !== inputsDigest;

  return (
    <section aria-labelledby={`${ids}-heading`}>
      <h3 id={`${ids}-heading`}>Architecture rules</h3>
      <p className="muted">
        Declare which parts of the code may import which. Only resolved imports
        count; rules whose files have unlocated imports stay unknown rather than
        passing.
      </p>
      <form
        noValidate
        onSubmit={(event) => {
          event.preventDefault();
          addRule();
        }}
      >
        <label htmlFor={`${ids}-name`}>Rule name</label>
        <input
          id={`${ids}-name`}
          value={name}
          onChange={(event) => setName(event.target.value)}
          aria-invalid={formError ? true : undefined}
          aria-describedby={formError ? `${ids}-error` : undefined}
        />
        <label htmlFor={`${ids}-kind`}>Kind</label>
        <select
          id={`${ids}-kind`}
          value={kind}
          onChange={(event) => {
            const next = event.target.value as RuleKind;
            setKind(next);
            if (next === "forbidden") setGroups(groups.slice(0, 2));
          }}
        >
          <option value="forbidden">Forbidden</option>
          <option value="layers">Layers</option>
          <option value="independence">Independence</option>
        </select>
        <p className="muted">{KIND_HINT[kind]}</p>
        {groups.map((value, index) => (
          <div key={index}>
            <label htmlFor={`${ids}-group-${index}`}>
              Group {index + 1} path prefixes (comma separated, up to{" "}
              {MAX_PREFIXES})
            </label>
            <input
              id={`${ids}-group-${index}`}
              list={listId}
              value={value}
              onChange={(event) =>
                setGroups(
                  groups.map((g, i) => (i === index ? event.target.value : g)),
                )
              }
            />
          </div>
        ))}
        <datalist id={listId}>
          {suggestions.slice(0, 500).map((s) => (
            <option key={s} value={s} />
          ))}
        </datalist>
        {suggestionsNote && <p className="muted">{suggestionsNote}</p>}
        {kind !== "forbidden" && (
          <div style={{ display: "flex", gap: 8 }}>
            <Button
              disabled={groups.length >= MAX_GROUPS}
              onClick={() => setGroups([...groups, ""])}
            >
              Add group
            </Button>
            <Button
              disabled={groups.length <= 2}
              onClick={() => setGroups(groups.slice(0, -1))}
            >
              Remove last group
            </Button>
          </div>
        )}
        {kind !== "layers" && (
          <label>
            <input
              type="checkbox"
              checked={indirect}
              onChange={(event) => setIndirect(event.target.checked)}
            />{" "}
            Direct imports only (ignore chains through other files)
          </label>
        )}
        {formError && (
          <p id={`${ids}-error`} role="alert">
            {formError}
          </p>
        )}
        <Button type="submit" intent="primary">
          Add rule
        </Button>
      </form>

      {rules.length > 0 && (
        <ul aria-label="Rules to check">
          {rules.map((rule) => (
            <li key={rule.name}>
              <strong>{rule.name}</strong> ({rule.kind}):{" "}
              {rule.groups.map((g) => g.join(", ")).join(" | ")}{" "}
              <Button
                aria-label={`Remove rule ${rule.name}`}
                onClick={() => {
                  setRules(rules.filter((r) => r.name !== rule.name));
                  check.reset();
                }}
              >
                Remove
              </Button>
            </li>
          ))}
        </ul>
      )}

      <div style={{ margin: "12px 0" }}>
        <Button
          intent="primary"
          pending={check.isPending}
          disabled={rules.length === 0}
          onClick={() => check.mutate()}
        >
          Check rules
        </Button>
      </div>
      {check.isError && (
        <InlineNotice tone="danger">{ruleError(check.error)}</InlineNotice>
      )}
      {stale && (
        <InlineNotice tone="info">
          These results were computed for a different report than the one shown.
        </InlineNotice>
      )}
      {result && (
        <ul aria-label="Rule results">
          {result.results.map((r) => (
            <li key={r.name}>
              <StatusBadge tone={RULE_STATUS_TONE[r.status]}>
                {RULE_STATUS_LABEL[r.status]}
              </StatusBadge>{" "}
              <strong>{r.name}</strong> ({r.kind}), {r.files_in_scope ?? 0}{" "}
              files in scope
              {(r.unknown_imports ?? 0) > 0 &&
                `; ${r.unknown_imports} imports from these files could not be located, so this result is incomplete`}
              .
              {(r.violations ?? []).length > 0 && (
                <ul aria-label={`Violations of ${r.name}`}>
                  {(r.violations ?? []).map((v, index) => (
                    <li key={`${v.source}>${v.target}>${index}`}>
                      <code>{v.source}</code> imports <code>{v.target}</code>
                      {v.chain.length > 2 && <> via {v.chain.join(" → ")}</>}
                      {v.evidence && (
                        <>
                          {" "}
                          (<code>{locationOf(v.evidence)}</code>)
                        </>
                      )}
                    </li>
                  ))}
                  {r.violations_total > (r.violations ?? []).length && (
                    <li>
                      {r.violations_total - (r.violations ?? []).length} more
                      violations not shown.
                    </li>
                  )}
                </ul>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
