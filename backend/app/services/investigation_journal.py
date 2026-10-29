"""Immutable encrypted execution effects; checkpoints retain references only."""

import json
from typing import Any, Literal, Protocol
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.contracts.investigation_execution import InvestigationScopeV1
from app.contracts.jobs import AttemptLeaseV2
from app.db.transaction import transaction
from app.domain.canonical_json import canonical_bytes, semantic_digest
from app.domain.errors import DuplicateConflict, InvalidTransition
from app.security.authorization import lock_scope
from app.services.model_continuations import ContinuationCipher

EffectKind = Literal["model", "tool", "draft"]


class ExecutionJournal(Protocol):
    async def get(self, scope: InvestigationScopeV1, identity: UUID) -> dict[str, Any] | None: ...

    async def put(
        self,
        scope: InvestigationScopeV1,
        lease: AttemptLeaseV2,
        identity: UUID,
        kind: EffectKind,
        input_digest: str,
        payload: dict[str, Any],
    ) -> None: ...


class InMemoryExecutionJournal:
    """Offline double, never selected by a database-backed executor."""

    def __init__(self) -> None:
        self.records: dict[tuple[str, UUID], dict[str, Any]] = {}

    async def get(self, scope: InvestigationScopeV1, identity: UUID) -> dict[str, Any] | None:
        value = self.records.get((semantic_digest(scope.model_dump(mode="json")), identity))
        return json.loads(canonical_bytes(value)) if value is not None else None

    async def put(
        self,
        scope: InvestigationScopeV1,
        lease: AttemptLeaseV2,
        identity: UUID,
        kind: EffectKind,
        input_digest: str,
        payload: dict[str, Any],
    ) -> None:
        key = (semantic_digest(scope.model_dump(mode="json")), identity)
        record = {"kind": kind, "input_digest": input_digest, "payload": payload}
        if key in self.records and canonical_bytes(self.records[key]) != canonical_bytes(record):
            raise DuplicateConflict("execution replay differs")
        self.records[key] = json.loads(canonical_bytes(record))


class SqlExecutionJournal:
    def __init__(self, factory: async_sessionmaker[AsyncSession], cipher: ContinuationCipher):
        self.factory, self.cipher = factory, cipher

    @staticmethod
    def binding(scope: InvestigationScopeV1, identity: UUID) -> dict[str, Any]:
        return {
            "org": str(scope.org_id),
            "repo": str(scope.repository_id),
            "actor": str(scope.principal_id),
            "inv": str(scope.investigation_id),
            "snapshot": str(scope.snapshot_id),
            "spec": scope.spec_hash,
            "revision": scope.execution_revision,
            "id": str(identity),
        }

    async def get(self, scope: InvestigationScopeV1, identity: UUID) -> dict[str, Any] | None:
        binding = self.binding(scope, identity)
        async with transaction(self.factory) as session:
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT * FROM public.codespy_get_investigation_effect("
                            "CAST(:binding AS jsonb))"
                        ),
                        {"binding": json.dumps(binding)},
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            return None
        raw = self.cipher.decrypt(row["ciphertext"], "execution:" + semantic_digest(binding))
        value: dict[str, Any] = json.loads(raw)
        if semantic_digest(value) != row["payload_digest"]:
            raise InvalidTransition("execution_integrity_failure")
        return value

    async def put(
        self,
        scope: InvestigationScopeV1,
        lease: AttemptLeaseV2,
        identity: UUID,
        kind: EffectKind,
        input_digest: str,
        payload: dict[str, Any],
    ) -> None:
        binding = self.binding(scope, identity)
        record = {"kind": kind, "input_digest": input_digest, "payload": payload}
        raw = canonical_bytes(record)
        if len(raw) > (262144 if kind in ("draft", "tool") else 524288):
            raise InvalidTransition("execution_retention_exceeded")
        ciphertext = self.cipher.encrypt(raw, "execution:" + semantic_digest(binding))
        metadata = {
            **binding,
            "job": str(lease.job_id),
            "attempt": str(lease.attempt_id),
            "fence": lease.fence,
            "owner": lease.owner,
            "kind": kind,
            "input_digest": input_digest,
            "size": len(raw),
        }
        async with transaction(self.factory) as session:
            await lock_scope(session, scope.org_id, scope.principal_id, scope.repository_id)
            await session.execute(
                text(
                    "SELECT public.codespy_put_investigation_effect("
                    "CAST(:binding AS jsonb),:cipher,:digest)"
                ),
                {
                    "binding": json.dumps(metadata),
                    "cipher": ciphertext,
                    "digest": semantic_digest(record),
                },
            )
