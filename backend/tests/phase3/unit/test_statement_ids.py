"""Statement ids: stable for a report, distinct for distinct statements, unaffected by evidence."""

from app.analyzers.graph.statement_ids import find_statement, statement_ids

from tests.phase3.unit.test_architecture_report import build


def test_ids_are_stable_unique_and_found_again():
    report, _ = build()
    first = statement_ids(report)
    again = statement_ids(
        build()[0]
    )  # a different build has different ids of files, not of statements
    assert {n: [s for s, _ in rows] for n, rows in first.items()} == {
        n: [s for s, _ in rows] for n, rows in again.items()
    }
    everything = [sid for rows in first.values() for sid, _ in rows]
    assert len(everything) == len(set(everything)) and all(len(s) == 16 for s in everything)
    sid, statement = first["overview"][0]
    assert find_statement(report, sid) == ("overview", statement)
    assert find_statement(report, "0" * 16) is None


def test_identical_statements_in_one_section_get_distinct_ids():
    from app.contracts.architecture_report import SectionV1, StatementV1

    twin = StatementV1(kind="unknown", code="limitation.syntax_only")
    report, _ = build()
    section = SectionV1(name="coverage", statements=(twin, twin))
    patched = report.model_copy(update={"sections": (*report.sections[:-1], section)})
    ids = [sid for sid, _ in statement_ids(patched)["coverage"]]
    assert len(ids) == 2 and ids[0] != ids[1]


def test_the_id_depends_on_section_code_and_params_only():
    report, _ = build()
    section, rows = next((n, r) for n, r in statement_ids(report).items() if r)
    sid, statement = rows[0]
    changed = statement.model_copy(update={"evidence": (), "evidence_total": 0})
    sections = tuple(
        s.model_copy(update={"statements": (changed, *s.statements[1:])})
        if s.name == section
        else s
        for s in report.sections
    )
    assert statement_ids(report.model_copy(update={"sections": sections}))[section][0][0] == sid
    altered = statement.model_copy(update={"params": {**statement.params, "extra": 1}})
    sections = tuple(
        s.model_copy(update={"statements": (altered, *s.statements[1:])})
        if s.name == section
        else s
        for s in report.sections
    )
    assert statement_ids(report.model_copy(update={"sections": sections}))[section][0][0] != sid
