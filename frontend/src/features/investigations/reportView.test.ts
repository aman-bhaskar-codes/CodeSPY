import { expect, it } from "vitest";
import { reportClaims } from "./reportView";

it("joins durable claim verification without treating source correspondence as truth", () => {
  const claims = reportClaims(
    {
      claims: [
        {
          claim_id: "claim",
          statement: "Observed call",
          suggested_quote: "untrusted quote",
        },
      ],
      verifications: [
        {
          claim_id: "claim",
          status: "source_correspondence_confirmed",
          resolved_file: "a.py",
          resolved_line_start: 1,
          notes: "Source matches",
        },
      ],
    },
    true,
  );
  expect(claims[0]).toMatchObject({
    claim_id: "claim",
    file_path: "a.py",
    status: "source_correspondence_confirmed",
  });
  expect(claims[0].quoted_source).toBeUndefined();
  expect(
    reportClaims(
      {
        claims: [{ claim_id: "claim", statement: "Observed" }],
        verifications: [
          { claim_id: "claim", status: "source_correspondence_confirmed" },
        ],
      },
      false,
    )[0].status,
  ).toBe("evidence_unavailable");
});

it("preserves unknowns and limitations without treating them as claims", async () => {
  const { reportCaveats } = await import("./reportView");
  expect(
    reportCaveats({
      unknowns: ["No runtime evidence"],
      limitations: ["Only admitted files"],
      claims: [],
    }),
  ).toEqual([
    { label: "Unanswered questions", items: ["No runtime evidence"] },
    { label: "Report limitations", items: ["Only admitted files"] },
  ]);
});
