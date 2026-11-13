"""The deterministic narrative verifier: what it confirms, and the ways a claim can be wrong."""

import hashlib
import json
from uuid import uuid4

from app.contracts.investigation_execution import InvestigationEvidenceV1
from app.contracts.report_narrative import NarrativeConclusionV1, NarrativeSentenceV1
from app.services.narrative_verification import (
    delivered_statements,
    render_narrative,
    verify_narrative,
)

DIGEST = "d" * 64
ORG, INV, SNAP = uuid4(), uuid4(), uuid4()


def entry(sid, kind, text, **params):
    return {"sid": sid, "kind": kind, "code": "x.y", "text": text, "params": params}


STATEMENTS = [
    entry("a" * 16, "derivation", "Imports: 12 resolved, 3 unresolved.", resolved=12, unresolved=3),
    entry("b" * 16, "observation", "app/web.py declares 4 routes.", file="app/web.py", count=4),
    entry(
        "c" * 16, "unknown", "3 imports could not be located; their targets are unknown.", edges=3
    ),
    entry("d" * 16, "inference", "Flask appears to be used.", framework="Flask"),
]


def shown(statements=None, digest=DIGEST, org=ORG, inv=INV, snap=SNAP, tamper=False):
    page = json.dumps({"statements": statements or STATEMENTS}, sort_keys=True)
    rows, delivered = [], {}
    for e in statements or STATEMENTS:
        row = InvestigationEvidenceV1(
            id=uuid4(),
            org_id=org,
            investigation_id=inv,
            snapshot_id=snap,
            observation_type="report_fact",
            fact_ref=f"{digest}:{e['sid']}",
            excerpt_sha256=hashlib.sha256(page.encode()).hexdigest(),
        )
        rows.append(row)
        delivered[row.id] = page + (" " if tamper else "")
    return delivered_statements(
        evidence=rows,
        delivered=delivered,
        org_id=ORG,
        investigation_id=INV,
        snapshot_id=SNAP,
        report_digest=DIGEST,
    )


def sentence(i, text, *sids):
    return NarrativeSentenceV1(claim_id=f"c{i}", text=text, sids=tuple(s * 16 for s in sids))


def verdicts(*sentences, **kw):
    outcome = verify_narrative(NarrativeConclusionV1(sentences=tuple(sentences)), shown(**kw))
    return {v.claim_id: v for v in outcome.verifications}, outcome


def test_a_faithful_paraphrase_is_confirmed_with_its_basis():
    v, outcome = verdicts(
        sentence(1, "The code has 12 resolved imports.", "a"),
        sentence(2, "`app/web.py` defines 4 routes.", "b"),
        sentence(3, "Flask is probably used.", "d"),
    )
    assert [v[c].status for c in ("c1", "c2", "c3")] == ["confirmed"] * 3
    assert (v["c1"].basis, v["c2"].basis, v["c3"].basis) == ("derived", "observed", "inferred")
    assert outcome.grounded and outcome.confirmed == 3


def test_a_sentence_without_a_citation_or_with_a_fabricated_one_is_unsupported():
    v, _ = verdicts(
        NarrativeSentenceV1(claim_id="c1", text="Everything is fine."),
        sentence(2, "There are 12 imports.", "f"),  # a statement id that was never shown
    )
    assert v["c1"].status == v["c2"].status == "unsupported"


def test_wrong_numbers_and_invented_names_are_unsupported():
    v, _ = verdicts(
        sentence(1, "The code has 13 resolved imports.", "a"),
        sentence(2, "`app/other.py` defines 4 routes.", "b"),
        sentence(3, "`app/web.py` defines 5 routes.", "b"),
    )
    assert all(x.status == "unsupported" for x in v.values())
    assert "13" in v["c1"].reason and "app/other.py" in v["c2"].reason


def test_numbers_inside_names_do_not_count_as_claims_and_commas_are_normalised():
    big = [entry("e" * 16, "derivation", "Imports: 1,234 resolved.", resolved=1234)]
    v, _ = verdicts(
        sentence(1, "About 1,234 imports resolve; see `v2/api.py`.", "e"), statements=big
    )
    assert v["c1"].status == "unsupported"  # the name v2/api.py is not in the statement
    v, _ = verdicts(sentence(1, "About 1,234 imports resolve.", "e"), statements=big)
    assert v["c1"].status == "confirmed"


def test_an_unknown_statement_supports_only_a_sentence_that_says_unknown():
    v, _ = verdicts(
        sentence(1, "3 imports are resolved to a file.", "c"),
        sentence(2, "3 imports could not be located, so their targets are unknown.", "c"),
    )
    assert v["c1"].status == "unsupported"
    assert v["c2"].status == "confirmed" and v["c2"].basis == "unknown"


def test_a_mixed_citation_takes_its_weakest_kind():
    v, _ = verdicts(
        sentence(1, "Flask appears to be used and the code has 12 resolved imports.", "a", "d")
    )
    assert v["c1"].status == "confirmed" and v["c1"].basis == "inferred"


def test_receipts_must_belong_to_this_investigation_report_and_page():
    for kw in (
        {"digest": "e" * 64},  # a receipt of another report
        {"org": uuid4()},  # another tenant
        {"inv": uuid4()},  # another investigation
        {"snap": uuid4()},  # another snapshot
        {"tamper": True},  # the delivered page no longer matches its receipt hash
    ):
        assert shown(**kw) == {}, kw
    v, outcome = verdicts(sentence(1, "The code has 12 resolved imports.", "a"), tamper=True)
    assert v["c1"].status == "unsupported" and not outcome.grounded


def test_source_evidence_never_supports_a_narrative_sentence():
    source = InvestigationEvidenceV1(
        id=uuid4(),
        org_id=ORG,
        investigation_id=INV,
        snapshot_id=SNAP,
        source_path="a.py",
        file_sha256="a" * 64,
        original_start_line=1,
        original_end_line=1,
        safe_start_line=1,
        safe_end_line=1,
        excerpt_sha256="b" * 64,
        observation_type="source_read",
    )
    assert (
        delivered_statements(
            evidence=[source],
            delivered={source.id: "1: x"},
            org_id=ORG,
            investigation_id=INV,
            snapshot_id=SNAP,
            report_digest=DIGEST,
        )
        == {}
    )


def test_grounding_needs_most_sentences_confirmed_and_a_narrative_is_never_empty_grounded():
    ok = sentence(1, "The code has 12 resolved imports.", "a")
    bad = sentence(2, "There are 99 files.", "a")
    _, outcome = verdicts(
        ok,
        ok.model_copy(update={"claim_id": "c3"}),
        ok.model_copy(update={"claim_id": "c4"}),
        ok.model_copy(update={"claim_id": "c5"}),
        bad,
    )
    assert outcome.confirmed == 4 and outcome.grounded and outcome.ratio == 0.8
    _, half = verdicts(ok, bad)
    assert not half.grounded
    _, empty = verdicts()
    assert not empty.grounded and empty.total == 0


def test_unconfirmed_sentences_are_marked_in_the_rendered_text():
    good = sentence(1, "The code has 12 resolved imports.", "a")
    bad = sentence(2, "There are 99 files.", "a")
    conclusion = NarrativeConclusionV1(overview="Overview.", sentences=(good, bad))
    outcome = verify_narrative(conclusion, shown())
    assert render_narrative(conclusion, outcome).splitlines() == [
        "Overview.",
        "The code has 12 resolved imports.",
        "[unverified] There are 99 files.",
    ]


def one(text, *sids):
    v, _ = verdicts(sentence(1, text, *(sids or ("a",))))
    return v["c1"]


def test_numbers_with_units_and_leading_dots_are_checked_too():
    for text in (
        "The service has 400k lines.",
        "It handles 5x more load.",
        "Latency is 77ms.",
        "Uses 2FA and v3 tokens.",
        "About .5 of imports resolve.",
        "A 99th-percentile figure.",
    ):
        assert one(text).status == "unsupported", text
    assert one("12 resolved imports, 3 unresolved.").status == "confirmed"


def test_quantity_words_need_support_in_the_cited_statements():
    assert one("There are three unresolved imports.").status == "unsupported"
    assert one("Dozens of files import it.").status == "unsupported"
    assert one("Flask appears to be used.", "d").status == "confirmed"


def test_names_without_backticks_are_checked_when_they_look_like_names():
    for text in (
        "The service lives in src/other.py.",
        "It calls get_user_by_id.",
        "UserService owns it.",
        "See config.yaml for details.",
    ):
        assert one(text, "d").status == "unsupported", text
    assert one("The routes are in app/web.py.", "b").status == "confirmed"
    assert (
        one("Use e.g. flask, i.e. the framework.", "d").status == "confirmed"
    )  # abbreviations are not names


def test_backticked_names_match_whole_tokens_and_cannot_be_tiny():
    assert one("`app/web.py` has routes.", "b").status == "confirmed"
    assert (
        one("`pp/web` is mentioned.", "b").status == "unsupported"
    )  # part of a longer name is not the name
    for tiny in ("`a`", "`.`", "`/`"):
        assert one(f"{tiny} is mentioned.", "b").status == "unsupported"


def test_a_long_model_claim_id_cannot_break_the_stored_narrative():
    long_id = "x" * 64
    sent = NarrativeSentenceV1(
        claim_id=long_id, text="The code has 12 resolved imports.", sids=("a" * 16,)
    )
    assert sent.claim_id == long_id
