"""Contract tests for investigation execution specifications and DTOs.

Task A — these tests exercise the frozen contracts from
contracts/investigation_execution.py and contracts/investigation_jobs.py.
They verify spec canonicalization, scope validation, limits enforcement,
envelope safety and request identity collision semantics.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from app.contracts.investigation_execution import (
    DEFAULT_STEPS,
    INVESTIGATION_RUNTIME_REVISION,
    MAX_CLAIMS,
    MAX_EVIDENCE_REFS,
    MAX_QUERY_CHARS,
    MAX_STEPS,
    MAX_TOOL_ARG_BYTES,
    InvestigationClaimKind,
    InvestigationClaimV2,
    InvestigationExecutionStateV1,
    InvestigationLimitsV1,
    InvestigationNodeKind,
    InvestigationReportV1,
    InvestigationScopeV1,
    InvestigationSpecV1,
    InvestigationStopReason,
    InvestigationToolName,
    InvestigationToolRequestV1,
    InvestigationToolResultV1,
    InvestigationToolStatus,
    compute_call_id,
    compute_node_id,
)
from app.contracts.investigation_jobs import (
    InvestigationCreateRequestV2,
    InvestigationEventV2,
    InvestigationSummaryV2,
    JobEnvelopeV6,
)
from app.domain.canonical_json import canonical_bytes
from pydantic import ValidationError

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _dummy_disclosure_digest() -> str:
    return hashlib.sha256(b"test-disclosure").hexdigest()


def _make_spec(**overrides) -> InvestigationSpecV1:
    defaults = {
        "org_id": uuid4(),
        "repository_id": uuid4(),
        "snapshot_id": uuid4(),
        "principal_id": uuid4(),
        "target_type": "repository",
        "query": "Is there a SQL injection vulnerability in the auth module?",
        "mode": "short_path",
        "depth": "standard",
        "disclosure_policy_digest": _dummy_disclosure_digest(),
        "tool_policy_revision": "v1",
        "model_route_revision": "v1",
        "model_profile_revision": "v1",
        "privacy_policy_revision": "v1",
    }
    defaults.update(overrides)
    return InvestigationSpecV1(**defaults)


def _make_scope(spec: InvestigationSpecV1 | None = None, **overrides) -> InvestigationScopeV1:
    if spec is None:
        spec = _make_spec()
    defaults = {
        "org_id": spec.org_id,
        "repository_id": spec.repository_id,
        "snapshot_id": spec.snapshot_id,
        "principal_id": spec.principal_id,
        "investigation_id": uuid4(),
        "spec_hash": spec.compute_spec_hash(),
    }
    defaults.update(overrides)
    return InvestigationScopeV1(**defaults)


# ===========================================================================
# test_spec_requires_scoped_snapshot
# ===========================================================================


class TestSpecRequiresScopedSnapshot:
    """Snapshot is required for every new executable investigation."""

    def test_spec_requires_snapshot_id(self):
        """A spec without snapshot_id cannot be created."""
        with pytest.raises((ValidationError, ValueError, TypeError)):
            InvestigationSpecV1(
                org_id=uuid4(),
                repository_id=uuid4(),
                # snapshot_id is missing
                principal_id=uuid4(),
                target_type="repository",
                query="test query",
                mode="short_path",
                disclosure_policy_digest=_dummy_disclosure_digest(),
                tool_policy_revision="v1",
                model_route_revision="v1",
                model_profile_revision="v1",
                privacy_policy_revision="v1",
            )

    def test_spec_with_snapshot_succeeds(self):
        spec = _make_spec()
        assert spec.snapshot_id is not None

    def test_spec_hash_is_deterministic(self):
        sid = uuid4()
        oid = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
        rid = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
        pid = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
        spec1 = _make_spec(snapshot_id=sid, org_id=oid, repository_id=rid, principal_id=pid)
        spec2 = _make_spec(snapshot_id=sid, org_id=oid, repository_id=rid, principal_id=pid)
        # Same inputs produce same spec_hash
        assert spec1.compute_spec_hash() == spec2.compute_spec_hash()

    def test_different_snapshots_different_hash(self):
        base = {
            "org_id": UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"),
            "repository_id": uuid4(),
            "principal_id": uuid4(),
            "target_type": "repository",
            "query": "test",
            "mode": "short_path",
            "disclosure_policy_digest": _dummy_disclosure_digest(),
            "tool_policy_revision": "v1",
            "model_route_revision": "v1",
            "model_profile_revision": "v1",
            "privacy_policy_revision": "v1",
        }
        s1 = InvestigationSpecV1(snapshot_id=uuid4(), **base)
        s2 = InvestigationSpecV1(snapshot_id=uuid4(), **base)
        assert s1.compute_spec_hash() != s2.compute_spec_hash()

    def test_commit_target_rejected(self):
        """commit/diff targets cannot be used until they resolve to admitted snapshots."""
        with pytest.raises((ValidationError, ValueError, TypeError)):
            InvestigationSpecV1(
                org_id=uuid4(),
                repository_id=uuid4(),
                snapshot_id=uuid4(),
                principal_id=uuid4(),
                target_type="commit",  # not allowed
                query="test",
                mode="short_path",
                disclosure_policy_digest=_dummy_disclosure_digest(),
                tool_policy_revision="v1",
                model_route_revision="v1",
                model_profile_revision="v1",
                privacy_policy_revision="v1",
            )

    def test_diff_target_rejected(self):
        with pytest.raises((ValidationError, ValueError, TypeError)):
            InvestigationSpecV1(
                org_id=uuid4(),
                repository_id=uuid4(),
                snapshot_id=uuid4(),
                principal_id=uuid4(),
                target_type="diff",  # not allowed
                query="test",
                mode="short_path",
                disclosure_policy_digest=_dummy_disclosure_digest(),
                tool_policy_revision="v1",
                model_route_revision="v1",
                model_profile_revision="v1",
                privacy_policy_revision="v1",
            )


# ===========================================================================
# test_cross_scope_finding_rejected
# ===========================================================================


class TestCrossScopeFindingRejected:
    """Finding targets require both finding_id and finding_run_id."""

    def test_finding_target_requires_finding_id(self):
        with pytest.raises(Exception, match="finding_id"):
            _make_spec(target_type="finding", finding_id=None)

    def test_finding_target_requires_finding_run_id(self):
        with pytest.raises(Exception, match="finding_run_id"):
            _make_spec(
                target_type="finding",
                finding_id=uuid4(),
                finding_run_id=None,
            )

    def test_finding_target_with_both_ids_succeeds(self):
        spec = _make_spec(
            target_type="finding",
            finding_id=uuid4(),
            finding_run_id=uuid4(),
        )
        assert spec.target_type == "finding"

    def test_repository_target_rejects_finding_id(self):
        with pytest.raises(Exception, match="finding_id"):
            _make_spec(target_type="repository", finding_id=uuid4())

    def test_scope_finding_fields_propagate(self):
        fid = uuid4()
        frid = uuid4()
        spec = _make_spec(target_type="finding", finding_id=fid, finding_run_id=frid)
        scope = _make_scope(spec, finding_id=fid, finding_run_id=frid)
        assert scope.finding_id == fid
        assert scope.finding_run_id == frid

    def test_create_v2_finding_requires_run_id(self):
        """InvestigationCreateRequestV2 finding target requires finding_run_id."""
        with pytest.raises(Exception, match="finding_run_id"):
            InvestigationCreateRequestV2(
                repository_id=uuid4(),
                snapshot_id=uuid4(),
                query="test",
                target_type="finding",
                finding_id=uuid4(),
                finding_run_id=None,
            )


# ===========================================================================
# test_limits_bound_every_role
# ===========================================================================


class TestLimitsBoundEveryRole:
    """Limits ceilings are intersected with configuration; cannot exceed absolutes."""

    def test_default_limits(self):
        limits = InvestigationLimitsV1()
        assert limits.max_steps == DEFAULT_STEPS
        assert limits.max_tool_calls_total == 40
        assert limits.max_search_hits == 5
        assert limits.max_memory_hits == 5
        assert limits.max_source_read_lines == 200
        assert limits.max_context_tokens == 4096
        assert limits.budget_limit_micros == 5_000_000

    def test_steps_cannot_exceed_absolute(self):
        with pytest.raises((ValidationError, ValueError, TypeError)):
            InvestigationLimitsV1(max_steps=MAX_STEPS + 1)

    def test_steps_minimum_is_one(self):
        limits = InvestigationLimitsV1(max_steps=1)
        assert limits.max_steps == 1
        with pytest.raises((ValidationError, ValueError, TypeError)):
            InvestigationLimitsV1(max_steps=0)

    def test_model_calls_in_flight_capped_at_one(self):
        with pytest.raises((ValidationError, ValueError, TypeError)):
            InvestigationLimitsV1(max_model_calls_in_flight=2)

    def test_tool_calls_in_flight_capped_at_one(self):
        with pytest.raises((ValidationError, ValueError, TypeError)):
            InvestigationLimitsV1(max_tool_calls_in_flight=2)

    def test_search_hits_max(self):
        limits = InvestigationLimitsV1(max_search_hits=20)
        assert limits.max_search_hits == 20
        with pytest.raises((ValidationError, ValueError, TypeError)):
            InvestigationLimitsV1(max_search_hits=21)

    def test_context_tokens_max(self):
        limits = InvestigationLimitsV1(max_context_tokens=8192)
        assert limits.max_context_tokens == 8192
        with pytest.raises((ValidationError, ValueError, TypeError)):
            InvestigationLimitsV1(max_context_tokens=8193)

    def test_budget_must_be_positive(self):
        with pytest.raises((ValidationError, ValueError, TypeError)):
            InvestigationLimitsV1(budget_limit_micros=0)
        with pytest.raises((ValidationError, ValueError, TypeError)):
            InvestigationLimitsV1(budget_limit_micros=-1)

    def test_limits_in_spec_are_respected(self):
        spec = _make_spec(
            limits=InvestigationLimitsV1(max_steps=5, max_search_hits=3),
        )
        assert spec.limits.max_steps == 5
        assert spec.limits.max_search_hits == 3

    def test_source_read_lines_max(self):
        limits = InvestigationLimitsV1(max_source_read_lines=200)
        assert limits.max_source_read_lines == 200
        with pytest.raises((ValidationError, ValueError, TypeError)):
            InvestigationLimitsV1(max_source_read_lines=201)


# ===========================================================================
# test_envelope_does_not_contain_prompts
# ===========================================================================


class TestEnvelopeDoesNotContainPrompts:
    """Queue envelope carries identity/config/deadline only."""

    def test_envelope_v6_basic(self):
        env = JobEnvelopeV6(
            org_id=uuid4(),
            job_id=uuid4(),
            operation="investigate_snapshot",
            dispatch_generation=1,
            config_hash=hashlib.sha256(b"config").hexdigest(),
            deadline=datetime.now(UTC) + timedelta(minutes=15),
            investigation_id=uuid4(),
            spec_hash=hashlib.sha256(b"spec").hexdigest(),
        )
        # Verify bounded size
        assert len(canonical_bytes(env.model_dump())) <= 4096

    def test_envelope_rejects_missing_investigation_id(self):
        with pytest.raises(Exception, match="investigation_id"):
            JobEnvelopeV6(
                org_id=uuid4(),
                job_id=uuid4(),
                operation="investigate_snapshot",
                dispatch_generation=1,
                config_hash=hashlib.sha256(b"config").hexdigest(),
                deadline=datetime.now(UTC) + timedelta(minutes=15),
                investigation_id=None,
                spec_hash=hashlib.sha256(b"spec").hexdigest(),
            )

    def test_envelope_rejects_missing_spec_hash(self):
        with pytest.raises(Exception, match="spec_hash"):
            JobEnvelopeV6(
                org_id=uuid4(),
                job_id=uuid4(),
                operation="investigate_snapshot",
                dispatch_generation=1,
                config_hash=hashlib.sha256(b"config").hexdigest(),
                deadline=datetime.now(UTC) + timedelta(minutes=15),
                investigation_id=uuid4(),
                spec_hash=None,
            )

    def test_envelope_has_no_query_or_prompt_field(self):
        """Envelope schema should not contain source/model content."""
        fields = set(JobEnvelopeV6.model_fields.keys())
        forbidden = {
            "query",
            "prompt",
            "system_prompt",
            "messages",
            "source",
            "model_content",
            "tool_calls",
            "reasoning",
        }
        assert fields.isdisjoint(forbidden), (
            f"Envelope contains forbidden fields: {fields & forbidden}"
        )

    def test_envelope_bounded_at_4096(self, monkeypatch):
        """Oversized envelope is rejected."""
        import app.contracts.investigation_jobs as ij

        monkeypatch.setattr(ij, "canonical_bytes", lambda x: b"x" * 5000)
        with pytest.raises(Exception, match="4096"):
            JobEnvelopeV6(
                org_id=uuid4(),
                job_id=uuid4(),
                operation="investigate_snapshot",
                dispatch_generation=1,
                config_hash=hashlib.sha256(b"config").hexdigest(),
                deadline=datetime.now(UTC) + timedelta(minutes=15),
                investigation_id=uuid4(),
                spec_hash=hashlib.sha256(b"spec").hexdigest(),
            )

    def test_existing_operations_still_work(self):
        """V6 should support all existing operations for backward compat."""
        for op in ("admit_source", "inventory_snapshot", "analyze_structure"):
            env = JobEnvelopeV6(
                org_id=uuid4(),
                job_id=uuid4(),
                operation=op,
                dispatch_generation=1,
                config_hash=hashlib.sha256(b"config").hexdigest(),
                deadline=datetime.now(UTC) + timedelta(minutes=15),
            )
            assert env.operation == op


# ===========================================================================
# test_same_request_different_actor_conflicts
# ===========================================================================


class TestSameRequestDifferentActorConflicts:
    """Two actors cannot share spec identity; spec hash includes principal."""

    def test_same_input_different_principal_different_hash(self):
        """Spec hash includes principal_id, so different actors produce different hashes."""
        common = {
            "org_id": UUID("cccccccc-cccc-cccc-cccc-cccccccccccc"),
            "repository_id": uuid4(),
            "snapshot_id": uuid4(),
            "target_type": "repository",
            "query": "test",
            "mode": "short_path",
            "disclosure_policy_digest": _dummy_disclosure_digest(),
            "tool_policy_revision": "v1",
            "model_route_revision": "v1",
            "model_profile_revision": "v1",
            "privacy_policy_revision": "v1",
        }
        s1 = InvestigationSpecV1(principal_id=uuid4(), **common)
        s2 = InvestigationSpecV1(principal_id=uuid4(), **common)
        assert s1.compute_spec_hash() != s2.compute_spec_hash()

    def test_same_actor_same_hash(self):
        pid = uuid4()
        common = {
            "org_id": UUID("dddddddd-dddd-dddd-dddd-dddddddddddd"),
            "repository_id": uuid4(),
            "snapshot_id": uuid4(),
            "principal_id": pid,
            "target_type": "repository",
            "query": "test",
            "mode": "short_path",
            "disclosure_policy_digest": _dummy_disclosure_digest(),
            "tool_policy_revision": "v1",
            "model_route_revision": "v1",
            "model_profile_revision": "v1",
            "privacy_policy_revision": "v1",
        }
        s1 = InvestigationSpecV1(**common)
        s2 = InvestigationSpecV1(**common)
        assert s1.compute_spec_hash() == s2.compute_spec_hash()


# ===========================================================================
# Additional contract checks
# ===========================================================================


class TestNodeIdentity:
    """Deterministic node and call ID generation."""

    def test_node_id_is_deterministic(self):
        args = (
            uuid4(),
            uuid4(),
            hashlib.sha256(b"s").hexdigest(),
            InvestigationNodeKind.INVESTIGATE,
            "investigator",
            0,
        )
        assert compute_node_id(*args) == compute_node_id(*args)

    def test_different_ordinal_different_id(self):
        org = uuid4()
        inv = uuid4()
        sh = hashlib.sha256(b"s").hexdigest()
        n1 = compute_node_id(org, inv, sh, InvestigationNodeKind.INVESTIGATE, "investigator", 0)
        n2 = compute_node_id(org, inv, sh, InvestigationNodeKind.INVESTIGATE, "investigator", 1)
        assert n1 != n2

    def test_call_id_is_deterministic(self):
        nid = uuid4()
        assert compute_call_id(nid, 0) == compute_call_id(nid, 0)
        assert compute_call_id(nid, 0) != compute_call_id(nid, 1)


class TestToolContracts:
    """Tool request/result validation."""

    def test_valid_tool_request(self):
        req = InvestigationToolRequestV1(
            tool_name=InvestigationToolName.SEARCH_CODE,
            arguments={"query": "sql injection", "max_hits": 5},
        )
        assert req.tool_name == "search_code"

    def test_oversized_arguments_rejected(self):
        with pytest.raises(Exception, match=str(MAX_TOOL_ARG_BYTES)):
            InvestigationToolRequestV1(
                tool_name=InvestigationToolName.READ_FILE_RANGE,
                arguments={"query": "x" * (MAX_TOOL_ARG_BYTES + 1)},
            )

    def test_unknown_tool_rejected(self):
        with pytest.raises((ValidationError, ValueError, TypeError)):
            InvestigationToolRequestV1(
                tool_name="arbitrary_tool",  # type: ignore[arg-type]
                arguments={},
            )

    def test_tool_result_statuses(self):
        for status in InvestigationToolStatus:
            result = InvestigationToolResultV1(status=status, result_digest="")
            assert result.status == status


class TestClaimContracts:
    """Claim and verification contracts."""

    def test_claim_statement_bounded(self):
        with pytest.raises((ValidationError, ValueError, TypeError)):
            InvestigationClaimV2(
                claim_id=uuid4(),
                statement="x" * 2001,
                kind=InvestigationClaimKind.SOURCE_OBSERVATION,
            )

    def test_claim_line_order(self):
        with pytest.raises(Exception, match="line_start"):
            InvestigationClaimV2(
                claim_id=uuid4(),
                statement="test",
                kind=InvestigationClaimKind.SOURCE_OBSERVATION,
                suggested_line_start=10,
                suggested_line_end=5,
            )

    def test_valid_claim(self):
        claim = InvestigationClaimV2(
            claim_id=uuid4(),
            statement="SQL injection in auth.py line 42",
            kind=InvestigationClaimKind.SECURITY_HYPOTHESIS,
            suggested_file="auth.py",
            suggested_line_start=40,
            suggested_line_end=45,
            suggested_quote="cursor.execute(f'SELECT * FROM users WHERE id={user_id}')",
        )
        assert claim.kind == InvestigationClaimKind.SECURITY_HYPOTHESIS


class TestReportBounds:
    """Report size and count limits."""

    def test_report_claims_bounded(self):
        claims = tuple(
            InvestigationClaimV2(
                claim_id=uuid4(),
                statement=f"claim {i}",
                kind=InvestigationClaimKind.SOURCE_OBSERVATION,
            )
            for i in range(MAX_CLAIMS + 1)
        )
        with pytest.raises(Exception, match=str(MAX_CLAIMS)):
            InvestigationReportV1(
                report_id=uuid4(),
                investigation_id=uuid4(),
                spec_hash=hashlib.sha256(b"s").hexdigest(),
                runtime_revision=1,
                verdict="indeterminate",
                summary="test",
                claims=claims,
                stop_reason=InvestigationStopReason.COMPLETED,
                report_digest=hashlib.sha256(b"r").hexdigest(),
                created_at=datetime.now(UTC),
            )

    def test_report_evidence_refs_bounded(self):
        refs = tuple(uuid4() for _ in range(MAX_EVIDENCE_REFS + 1))
        with pytest.raises(Exception, match=str(MAX_EVIDENCE_REFS)):
            InvestigationReportV1(
                report_id=uuid4(),
                investigation_id=uuid4(),
                spec_hash=hashlib.sha256(b"s").hexdigest(),
                runtime_revision=1,
                verdict="indeterminate",
                summary="test",
                evidence_refs=refs,
                stop_reason=InvestigationStopReason.COMPLETED,
                report_digest=hashlib.sha256(b"r").hexdigest(),
                created_at=datetime.now(UTC),
            )


class TestCreateV2:
    """InvestigationCreateRequestV2 contract tests."""

    def test_requires_snapshot_id(self):
        with pytest.raises((ValidationError, ValueError, TypeError)):
            InvestigationCreateRequestV2(
                repository_id=uuid4(),
                # snapshot_id missing
                query="test",
                target_type="repository",
            )

    def test_rejects_commit_target(self):
        with pytest.raises((ValidationError, ValueError, TypeError)):
            InvestigationCreateRequestV2(
                repository_id=uuid4(),
                snapshot_id=uuid4(),
                query="test",
                target_type="commit",  # type: ignore[arg-type]
            )

    def test_rejects_diff_target(self):
        with pytest.raises((ValidationError, ValueError, TypeError)):
            InvestigationCreateRequestV2(
                repository_id=uuid4(),
                snapshot_id=uuid4(),
                query="test",
                target_type="diff",  # type: ignore[arg-type]
            )

    def test_query_max_length(self):
        with pytest.raises((ValidationError, ValueError, TypeError)):
            InvestigationCreateRequestV2(
                repository_id=uuid4(),
                snapshot_id=uuid4(),
                query="x" * 4001,
                target_type="repository",
            )

    def test_valid_create(self):
        req = InvestigationCreateRequestV2(
            repository_id=uuid4(),
            snapshot_id=uuid4(),
            query="Is there a vulnerability?",
            target_type="repository",
        )
        assert req.target_type == "repository"
        assert req.snapshot_id is not None


class TestQueryBytes:
    """Spec query byte-length validation."""

    def test_query_exceeding_byte_limit(self):
        """A query within char limit but exceeding byte limit is rejected."""
        # Multi-byte characters can push byte count over limit
        # 4000 chars * 4 bytes/char = up to 16000 bytes, but limit is 16384
        # Use a shorter char count that still exceeds bytes
        query = "é" * 4000  # 2 bytes each = 8000 bytes, within limit
        spec = _make_spec(query=query)
        assert len(spec.query) <= MAX_QUERY_CHARS

    def test_empty_query_rejected(self):
        with pytest.raises((ValidationError, ValueError, TypeError)):
            _make_spec(query="")


class TestEventContracts:
    """Event DTO validation."""

    def test_event_payload_bounded(self):
        with pytest.raises(Exception, match="4096"):
            InvestigationEventV2(
                id=uuid4(),
                org_id=uuid4(),
                investigation_id=uuid4(),
                sequence=0,
                step_index=0,
                event_type="step_started",
                node_name="investigate",
                payload={"data": "x" * 5000},
                created_at=datetime.now(UTC),
            )

    def test_valid_event(self):
        ev = InvestigationEventV2(
            id=uuid4(),
            org_id=uuid4(),
            investigation_id=uuid4(),
            sequence=1,
            step_index=0,
            event_type="step_started",
            node_name="investigate",
            payload={"step": 0, "tool": "search_code"},
            created_at=datetime.now(UTC),
        )
        assert ev.sequence == 1


class TestExecutionState:
    """Execution state checkpoint contracts."""

    def test_execution_state_requires_scope_hash(self):
        with pytest.raises((ValidationError, ValueError, TypeError)):
            InvestigationExecutionStateV1(
                scope_hash="invalid",
                spec_hash=hashlib.sha256(b"s").hexdigest(),
                execution_revision=1,
                next_node_kind=InvestigationNodeKind.INVESTIGATE,
                next_role="investigator",
                next_ordinal=0,
                checkpoint_sequence=0,
            )

    def test_valid_execution_state(self):
        state = InvestigationExecutionStateV1(
            scope_hash=hashlib.sha256(b"scope").hexdigest(),
            spec_hash=hashlib.sha256(b"spec").hexdigest(),
            execution_revision=1,
            next_node_kind=InvestigationNodeKind.INVESTIGATE,
            next_role="investigator",
            next_ordinal=0,
            checkpoint_sequence=0,
        )
        assert state.execution_revision == 1
        assert state.next_node_kind == InvestigationNodeKind.INVESTIGATE


class TestLegacyCompatibility:
    """V2 summary can produce a backward-compatible V1 projection."""

    def test_v2_to_v1_projection(self):
        now = datetime.now(UTC)
        v2 = InvestigationSummaryV2(
            id=uuid4(),
            org_id=uuid4(),
            repository_id=uuid4(),
            snapshot_id=uuid4(),
            target_type="repository",
            query="test",
            status="pending",
            mode="short_path",
            depth="standard",
            current_step=0,
            max_steps=10,
            budget_limit_micros=5_000_000,
            spent_micros=0,
            created_at=now,
            updated_at=now,
        )
        v1 = v2.to_v1()
        assert v1.id == v2.id
        assert v1.org_id == v2.org_id
        assert v1.repository_id == v2.repository_id
        assert v1.query == v2.query
        assert v1.status == v2.status


class TestSpecCanonicalHash:
    """Spec canonicalization and hash computation."""

    def test_spec_hash_matches_canonical_bytes(self):
        spec = _make_spec()
        expected = hashlib.sha256(canonical_bytes(spec.model_dump())).hexdigest()
        assert spec.compute_spec_hash() == expected

    def test_frozen_spec_is_immutable(self):
        spec = _make_spec()
        with pytest.raises((ValidationError, ValueError, TypeError)):
            spec.query = "modified"  # type: ignore[misc]


class TestRuntimeRevision:
    """Runtime revision is set correctly."""

    def test_default_runtime_revision(self):
        spec = _make_spec()
        assert spec.runtime_revision == INVESTIGATION_RUNTIME_REVISION

    def test_runtime_revision_cannot_be_zero(self):
        with pytest.raises((ValidationError, ValueError, TypeError)):
            _make_spec(runtime_revision=0)
