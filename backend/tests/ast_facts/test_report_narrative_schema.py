# ruff: noqa: E501
"""Migration 0049: narrative mode, report-pinned specs, report_fact evidence and the narrative link guard."""

import os
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from tests.ast_facts.test_architecture_report_service import config_for, publish_shards, setup

pytestmark = pytest.mark.skipif(
    os.environ.get("CODESPY_TEST_SCHEMA_TARGET") != "head",
    reason="requires the current schema head (revision 0049)",
)

HEX = "a" * 64
OTHER = "b" * 64


async def a_report(factory, tmp_path):
    ctx, snapshot, repo, store, service = await setup(factory, tmp_path)
    shards = await service.prepare(ctx, repo, snapshot, config_for)
    await publish_shards(factory, ctx, shards, store)
    ref = await service.build(ctx, repo, snapshot)
    return ctx, snapshot, repo, ref


async def insert_spec(
    s,
    ctx,
    snapshot,
    repo,
    *,
    spec_hash,
    mode="report_narrative",
    target="architecture_report",
    report_id=None,
    digest=None,
):
    await s.execute(
        text(
            "INSERT INTO investigation_specs(org_id,id,spec_hash,repository_id,snapshot_id,principal_id,target_type,query,mode,depth,"
            "limits_json,disclosure_policy_digest,tool_policy_revision,model_route_revision,model_profile_revision,runtime_revision,"
            "privacy_policy_revision,architecture_report_id,architecture_report_digest) VALUES(:o,:i,:h,:r,:s,:p,:t,'q',:m,'standard',"
            "CAST('{}' AS jsonb),:d,'v','v','v',1,'v',:rid,:rd)"
        ),
        {
            "o": ctx.org_id,
            "i": uuid4(),
            "h": spec_hash,
            "r": repo,
            "s": snapshot,
            "p": ctx.principal_id,
            "t": target,
            "m": mode,
            "d": "0" * 64,
            "rid": report_id,
            "rd": digest,
        },
    )


async def refused(s, statement, params, match):
    with pytest.raises(DBAPIError, match=match):
        async with s.begin_nested():
            await s.execute(text(statement), params)


async def test_specs_pin_a_report_exactly_when_the_mode_is_narrative(factory, tmp_path):
    ctx, snapshot, repo, ref = await a_report(factory, tmp_path)
    async with factory() as s, s.begin():
        await insert_spec(
            s, ctx, snapshot, repo, spec_hash=HEX, report_id=ref.id, digest=ref.report_digest
        )
        await insert_spec(
            s, ctx, snapshot, repo, spec_hash=OTHER, mode="short_path", target="repository"
        )  # unchanged kinds still work
        for kwargs in (
            {"report_id": None, "digest": None},  # narrative without a pin
            {"report_id": ref.id, "digest": None},  # half a pin
            {"report_id": uuid4(), "digest": ref.report_digest},  # a report that does not exist
        ):
            with pytest.raises(DBAPIError):
                async with s.begin_nested():
                    await insert_spec(s, ctx, snapshot, repo, spec_hash="c" * 64, **kwargs)
        with pytest.raises(DBAPIError, match="ck_investigation_spec_report_pin"):
            async with s.begin_nested():
                await insert_spec(
                    s,
                    ctx,
                    snapshot,
                    repo,
                    spec_hash="d" * 64,
                    mode="short_path",
                    target="repository",
                    report_id=ref.id,
                    digest=ref.report_digest,
                )  # a pin on a non-narrative spec
        await s.rollback()


async def test_evidence_is_either_source_bound_or_report_bound(factory, tmp_path):
    ctx, snapshot, repo, ref = await a_report(factory, tmp_path)
    inv = uuid4()
    async with factory() as s, s.begin():
        await insert_spec(
            s, ctx, snapshot, repo, spec_hash=HEX, report_id=ref.id, digest=ref.report_digest
        )
        await s.execute(
            text(
                "INSERT INTO investigations(org_id,id,repository_id,principal_id,target_type,query,status,mode,depth,budget_limit_micros,spec_hash,snapshot_id)"
                " VALUES(:o,:i,:r,:p,'architecture_report','q','pending','report_narrative','standard',1000,:h,:s)"
            ),
            {"o": ctx.org_id, "i": inv, "r": repo, "p": ctx.principal_id, "h": HEX, "s": snapshot},
        )
        base = "INSERT INTO investigation_evidence(org_id,id,investigation_id,snapshot_id,{cols},excerpt_sha256,observation_type) VALUES(:o,:i,:v,:s,{vals},:x,:t)"
        common = {"o": ctx.org_id, "v": inv, "s": snapshot, "x": HEX}
        source_cols = "source_path,file_sha256,original_start_line,original_end_line,safe_start_line,safe_end_line"
        source_vals = "'a.py',:f,1,2,1,2"
        await s.execute(
            text(base.format(cols=source_cols, vals=source_vals)),
            {**common, "i": uuid4(), "f": HEX, "t": "source_read"},
        )
        await s.execute(
            text(base.format(cols="fact_ref", vals="'digest:abc'")),
            {**common, "i": uuid4(), "t": "report_fact"},
        )
        await refused(
            s,
            base.format(cols="fact_ref", vals="'x'"),
            {**common, "i": uuid4(), "t": "source_read"},
            "ck_evidence_source_or_fact",
        )  # source row without a source
        await refused(
            s,
            base.format(cols=source_cols + ",fact_ref", vals=source_vals + ",'x'"),
            {**common, "i": uuid4(), "f": HEX, "t": "report_fact"},
            "ck_evidence_source_or_fact",
        )  # fact with source fields
        await refused(
            s,
            base.format(cols="fact_ref", vals="NULL"),
            {**common, "i": uuid4(), "t": "report_fact"},
            "ck_evidence_source_or_fact",
        )  # fact without a ref
        await s.rollback()


async def test_narrative_link_guard_and_immutability(factory, tmp_path):
    ctx, snapshot, repo, ref = await a_report(factory, tmp_path)
    inv, rep = uuid4(), uuid4()
    async with factory() as s, s.begin():
        await insert_spec(
            s, ctx, snapshot, repo, spec_hash=HEX, report_id=ref.id, digest=ref.report_digest
        )
        await s.execute(
            text(
                "INSERT INTO investigations(org_id,id,repository_id,principal_id,target_type,query,status,mode,depth,budget_limit_micros,spec_hash,snapshot_id)"
                " VALUES(:o,:i,:r,:p,'architecture_report','q','pending','report_narrative','standard',1000,:h,:s)"
            ),
            {"o": ctx.org_id, "i": inv, "r": repo, "p": ctx.principal_id, "h": HEX, "s": snapshot},
        )
        await s.execute(text("ALTER TABLE investigation_reports DISABLE TRIGGER USER"))
        await s.execute(
            text(
                "INSERT INTO investigation_reports(org_id,id,investigation_id,spec_hash,job_id,verdict,verdict_confidence,summary,report_json,report_digest,claims_count,evidence_refs_count)"
                " VALUES(:o,:i,:v,:h,:j,'indeterminate',0,'s',CAST('{}' AS jsonb),:d,0,0)"
            ),
            {"o": ctx.org_id, "i": rep, "v": inv, "h": HEX, "j": uuid4(), "d": HEX},
        )
        link = (
            "INSERT INTO architecture_report_narratives(org_id,id,architecture_report_id,repository_id,snapshot_id,investigation_id,investigation_report_id,spec_hash,report_digest)"
            " VALUES(:o,:i,:a,:r,:s,:v,:ir,:h,:d)"
        )
        good = {
            "o": ctx.org_id,
            "i": uuid4(),
            "a": ref.id,
            "r": repo,
            "s": snapshot,
            "v": inv,
            "ir": rep,
            "h": HEX,
            "d": ref.report_digest,
        }
        await refused(s, link, {**good, "d": OTHER}, "differs")  # digest not the pinned report's
        await refused(
            s, link, {**good, "h": OTHER}, "differs|violates"
        )  # spec that does not cite the report
        await refused(s, link, {**good, "s": uuid4()}, "differs|violates")  # wrong snapshot
        await s.execute(text(link), good)
        await refused(
            s, link, {**good, "i": uuid4()}, "duplicate|unique"
        )  # one narrative per report and spec
        await refused(
            s, "UPDATE architecture_report_narratives SET report_digest=:d", {"d": OTHER}, ""
        )
        await refused(s, "DELETE FROM architecture_report_narratives", {}, "")
        await s.rollback()


async def test_downgrade_is_refused_with_narrative_data(factory, db_values, tmp_path):
    from scripts import phase2_db as harness

    ctx, snapshot, repo, ref = await a_report(factory, tmp_path)
    async with factory() as s, s.begin():
        await insert_spec(
            s, ctx, snapshot, repo, spec_hash=HEX, report_id=ref.id, digest=ref.report_digest
        )
    with pytest.raises(Exception, match="report narrative history must be preserved"):
        harness.migrate(
            db_values,
            db_values["CODESPY_TEST_DATABASE"],
            "0048_architecture_reports",
            downgrade=True,
        )


async def test_downgrade_and_upgrade_round_trip_without_narrative_data(factory, db_values):
    from scripts import phase2_db as harness

    name = db_values["CODESPY_TEST_DATABASE"]
    harness.migrate(db_values, name, "0048_architecture_reports", downgrade=True)
    async with factory() as s:
        row = (
            await s.execute(text("SELECT to_regclass('public.architecture_report_narratives')"))
        ).scalar()
        assert row is None
        cols = (
            (
                await s.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns WHERE table_name='investigation_specs'"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert "architecture_report_id" not in cols
    harness.migrate(db_values, name, "head")
    async with factory() as s:
        assert (
            await s.execute(text("SELECT to_regclass('public.architecture_report_narratives')"))
        ).scalar() is not None
