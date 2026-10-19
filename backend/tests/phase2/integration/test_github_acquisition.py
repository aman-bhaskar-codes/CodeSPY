import hashlib
import io
import zipfile
from datetime import UTC, datetime, timedelta

from app.contracts.ingestion import AdmissionPolicyV1
from app.models.phase2_tables import PHASE2_TABLES
from app.models.tables import repositories
from app.services.github_acquisition import GitHubAcquisitionService, RepositoryAccess
from app.services.jobs import JobService
from sqlalchemy import insert, select, update
from tests.phase2.integration.test_submission_ingestion import submission_scenario


class FakeGitHub:
    def __init__(self, body):
        self.body = body
        self.resolves = 0

    async def verify_access(self, principal, repository_id, installation_id):
        return RepositoryAccess(repository_id, installation_id, 123)

    async def resolve(self, access, ref):
        self.resolves += 1
        return "a" * 40

    async def archive(self, access, commit):
        assert commit == "a" * 40
        yield self.body


async def test_github_commit_and_selected_bytes_are_frozen(factory, tmp_path):
    _, ctx, repo, _, _, store = await submission_scenario(factory, tmp_path)
    bindings, grants = (
        PHASE2_TABLES[n] for n in ["github_repository_bindings", "github_user_grants"]
    )
    now = datetime.now(UTC)
    async with factory.begin() as s:
        await s.execute(
            update(repositories)
            .where(repositories.c.id == repo.repository_id)
            .values(source_kind="github")
        )
        await s.execute(
            insert(bindings).values(
                org_id=ctx.org_id,
                repository_id=repo.repository_id,
                github_repository_id=11,
                installation_id=22,
                account_id=123,
                state="active",
                last_checked_at=now,
            )
        )
        await s.execute(
            insert(grants).values(
                principal_id=ctx.principal_id,
                token_cipher=b"fixture",
                key_id="fixture",
                expires_at=now + timedelta(hours=1),
                last_verified_at=now,
            )
        )
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("provider-root/a.py", "print(1)")
    provider = FakeGitHub(out.getvalue())
    service = GitHubAcquisitionService(factory, store, provider)
    sub = await service.request(ctx, repo.repository_id, "main", "github-key")
    jobs = JobService(factory)
    lease = await jobs.claim(ctx.org_id, sub.job_id, 1, "fixture-worker")
    selected = await service.acquire(lease)
    assert selected.sha256 == hashlib.sha256(provider.body).hexdigest()
    provider.body = b"changed and invalid"
    assert await service.acquire(lease) == selected
    assert provider.resolves == 1
    async with factory() as s:
        row = (
            (
                await s.execute(
                    select(PHASE2_TABLES["source_submissions"]).where(
                        PHASE2_TABLES["source_submissions"].c.id == sub.id
                    )
                )
            )
            .mappings()
            .one()
        )
        assert row["resolved_commit"] == "a" * 40
        assert row["policy_hash"] == AdmissionPolicyV1(github_strip_root=True).digest()
