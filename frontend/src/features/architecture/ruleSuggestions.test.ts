import { describe, expect, it } from "vitest";
import {
  normalizeRule,
  prefixSuggestions,
  ruleProblem,
  type RuleDraft,
} from "./ruleSuggestions";

const base: RuleDraft = {
  name: "ui-not-data",
  kind: "forbidden",
  groups: [["ui"], ["data"]],
};

describe("prefixSuggestions", () => {
  it("lists every ancestor directory once, sorted", () => {
    expect(
      prefixSuggestions(["a/b/c.py", "a/b/d.py", "a/e.py", "top.py"]),
    ).toEqual(["a", "a/b"]);
  });
  it("is empty for root-level files", () => {
    expect(prefixSuggestions(["x.py"])).toEqual([]);
  });
});

describe("ruleProblem", () => {
  it("accepts a valid rule", () => {
    expect(ruleProblem(base, [])).toBeNull();
  });
  it("rejects bad names and duplicates", () => {
    expect(ruleProblem({ ...base, name: "" }, [])).toMatch(/Name/);
    expect(ruleProblem({ ...base, name: "bad\nname" }, [])).toMatch(/Name/);
    expect(ruleProblem(base, ["ui-not-data"])).toMatch(/unique/);
  });
  it("enforces group counts per kind", () => {
    expect(ruleProblem({ ...base, groups: [["a"]] }, [])).toMatch(/2 to 8/);
    expect(ruleProblem({ ...base, groups: [["a"], ["b"], ["c"]] }, [])).toMatch(
      /exactly two/,
    );
    expect(
      ruleProblem(
        { ...base, kind: "layers", groups: [["a"], ["b"], ["c"]] },
        [],
      ),
    ).toBeNull();
  });
  it("rejects unsafe or repeated prefixes", () => {
    expect(ruleProblem({ ...base, groups: [["../x"], ["b"]] }, [])).toMatch(
      /not a valid/,
    );
    expect(ruleProblem({ ...base, groups: [["a\\b"], ["b"]] }, [])).toMatch(
      /not a valid/,
    );
    expect(ruleProblem({ ...base, groups: [["a"], ["a/"]] }, [])).toMatch(
      /more than once/,
    );
    expect(ruleProblem({ ...base, groups: [[], ["b"]] }, [])).toMatch(/1 to 8/);
  });
});

describe("normalizeRule", () => {
  it("drops trailing slashes", () => {
    expect(
      normalizeRule({ ...base, groups: [["ui/"], ["data/"]] }).groups,
    ).toEqual([["ui"], ["data"]]);
  });
});
