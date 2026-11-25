import { expect, it } from "vitest";
import { reportCaveats } from "./reportView";

it("accepts caveats as lists or as one newline-separated string", () => {
  expect(reportCaveats({ unknowns: ["a", 3, "b"], limitations: [] })).toEqual([
    { label: "Unanswered questions", items: ["a", "b"] },
  ]);
  expect(
    reportCaveats({ unknowns: "first\n\n second ", limitations: "only one" }),
  ).toEqual([
    { label: "Unanswered questions", items: ["first", "second"] },
    { label: "Report limitations", items: ["only one"] },
  ]);
  expect(reportCaveats({ unknowns: 5 })).toEqual([]);
  expect(reportCaveats(null)).toEqual([]);
});
