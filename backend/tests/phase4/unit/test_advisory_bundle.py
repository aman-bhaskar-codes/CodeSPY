"""A frozen exact query answer is evidence; missing cache data is not absence."""

from datetime import UTC, datetime, timedelta

import pytest
from app.scanners.advisories import AdvisoryBundle, AdvisoryRecord, ExactAnswer, PackageQuery

NOW = datetime(2026, 10, 20, tzinfo=UTC)
QUERY = PackageQuery(ecosystem="npm", name="example", version="1.2.3")


def answer(**changes):
    return ExactAnswer(
        query=QUERY,
        fetched_at=NOW,
        complete=True,
        advisory_ids=changes.pop("advisory_ids", ()),
        **changes,
    )


def record(**changes):
    return AdvisoryRecord.model_validate(
        {
            "id": "GHSA-aaaa-bbbb-cccc",
            "modified": NOW,
            "source": "https://api.osv.dev/v1/vulns/GHSA-aaaa-bbbb-cccc",
            "document": {
                "id": "GHSA-aaaa-bbbb-cccc",
                "modified": NOW.isoformat(),
                "affected": [
                    {"package": {"ecosystem": "npm", "name": "example"}, "versions": ["1.2.3"]}
                ],
            },
            **changes,
        }
    )


def test_explicit_empty_exact_answer_is_distinct_from_missing_query():
    bundle = AdvisoryBundle(answers=(answer(),), records=())
    assert bundle.evaluate(QUERY, now=NOW).status == "empty"
    assert (
        bundle.evaluate(QUERY.model_copy(update={"version": "1.2.4"}), now=NOW).status == "missing"
    )


def test_complete_exact_answer_binds_full_record_and_preserves_record_digest():
    advisory = record()
    bundle = AdvisoryBundle(answers=(answer(advisory_ids=(advisory.id,)),), records=(advisory,))
    result = bundle.evaluate(QUERY, now=NOW)
    assert result.status == "matched"
    assert result.advisories == ((advisory.id, advisory.digest()),)


@pytest.mark.parametrize(
    "change",
    [
        "stale",
        "future",
        "incomplete",
        "missing-record",
        "withdrawn",
        "foreign-package",
        "record-id",
        "record-modified",
    ],
)
def test_unusable_evidence_cannot_produce_supported_or_empty(change):
    advisory = record()
    response = answer(advisory_ids=(advisory.id,))
    records = (advisory,)
    if change == "stale":
        response = response.model_copy(update={"fetched_at": NOW - timedelta(days=8)})
    elif change == "future":
        response = response.model_copy(update={"fetched_at": NOW + timedelta(minutes=1)})
    elif change == "incomplete":
        response = response.model_copy(update={"complete": False})
    elif change == "missing-record":
        records = ()
    else:
        document = dict(advisory.document)
        if change == "withdrawn":
            document["withdrawn"] = NOW.isoformat()
        elif change == "foreign-package":
            document["affected"] = [{"package": {"ecosystem": "PyPI", "name": "other"}}]
        elif change == "record-id":
            document["id"] = "GHSA-xxxx-yyyy-zzzz"
        elif change == "record-modified":
            document["modified"] = (NOW - timedelta(days=1)).isoformat()
        records = (advisory.model_copy(update={"document": document}),)
    result = AdvisoryBundle(answers=(response,), records=records).evaluate(QUERY, now=NOW)
    assert result.status not in {"matched", "empty"}
    assert result.advisories == ()


def test_query_identity_includes_ecosystem_name_and_exact_version():
    bundle = AdvisoryBundle(answers=(answer(),), records=())
    for change in ({"name": "other"}, {"ecosystem": "PyPI"}, {"version": "1.2.4"}):
        assert bundle.evaluate(QUERY.model_copy(update=change), now=NOW).status == "missing"


def test_duplicate_query_and_record_identities_are_rejected():
    with pytest.raises(ValueError):
        AdvisoryBundle(answers=(answer(), answer()), records=())
    with pytest.raises(ValueError):
        AdvisoryBundle(answers=(), records=(record(), record()))
