# ruff: noqa: E501
"""A whole agent session in a test: real database, real event log and journal, fake sandbox and scripted model."""

import asyncio
import hashlib
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from app.adapters.model.scripted import ScriptedAgentModel, ScriptedTurn
from app.adapters.sandbox.fake import FakeSandbox, MappingSnapshots
from app.contracts.agent_session import AgentSessionCreateRequestV1, AgentSessionV1
from app.domain.scope import TenantContext
from app.services.agent.approvals import ApprovalService
from app.services.agent.commands import AgentCommands
from app.services.agent.control import SessionControl
from app.services.agent.event_log import AgentEventLog, EventCipher
from app.services.agent.journal import AgentJournal, JournalCipher
from app.services.agent.policy import PolicyV1
from app.services.agent.runner import AgentDeps, RunOutcome, SessionRunner
from app.services.agent.sessions import AgentSessionService
from app.services.workspace.service import WorkspaceService
from sqlalchemy import text

from tests.ast_facts.test_ast_facts_publication import admitted

FILES = {
    "src/app.py": b"def main():\n    return 1\n",
    "tests/test_app.py": b"def test_main():\n    assert main() == 2\n",
    "pyproject.toml": b"[tool.pytest.ini_options]\n",
    "README.md": b"# demo\n",
}
APP_HASH = hashlib.sha256(FILES["src/app.py"]).hexdigest()
LOG = AgentEventLog(EventCipher(bytes(range(32))))
JOURNAL_KEY = bytes(range(32, 64))


class InlineScheduler:
    """Starts the loop in the background, as a queue would, and lets a test wait for it."""

    def __init__(self) -> None:
        self.runner: SessionRunner | None = None
        self.tasks: list[asyncio.Task[RunOutcome]] = []

    async def __call__(self, ctx: TenantContext, session_id: UUID) -> None:
        assert self.runner is not None
        self.tasks.append(asyncio.create_task(self.runner.run(ctx, session_id)))

    async def drain(self) -> list[RunOutcome]:
        results = await asyncio.gather(*self.tasks)
        self.tasks.clear()
        return list(results)


@dataclass
class World:
    factory: Any
    ctx: TenantContext
    session: AgentSessionV1
    sandbox: FakeSandbox
    model: ScriptedAgentModel
    deps: AgentDeps
    runner: SessionRunner
    commands: AgentCommands
    scheduler: InlineScheduler
    journal: AgentJournal
    approvals: ApprovalService
    control: SessionControl
    workspaces: WorkspaceService

    async def events(self) -> list[Any]:
        async with self.factory() as s, s.begin():
            return await LOG.read_after(s, self.ctx.org_id, self.session.id, 0, limit=2000)

    async def types(self) -> list[str]:
        return [e.type for e in await self.events()]

    async def status(self) -> str:
        return (await self.control.load(self.ctx.org_id, self.session.id)).status

    async def say(self, text_: str, key: str = "k1") -> None:
        await self.commands.send_message(self.ctx, self.session.id, text_, request_key=key)
        await self.scheduler.drain()

    async def answer(self, approval_id: str, decision: str = "approved", **kw: Any) -> None:
        await self.commands.resolve_approval(self.ctx, self.session.id, approval_id, decision, **kw)

    async def sql(self, query: str, **params: Any) -> list[Any]:
        async with self.factory() as s, s.begin():
            return list((await s.execute(text(query), params)).mappings().all())


async def build_world(
    factory,
    tmp_path,
    turns: list[ScriptedTurn],
    *,
    mode: str = "plan",
    policy: PolicyV1 | None = None,
    files=None,
    owner: str = "worker-1",
    lease_seconds: int = 60,
) -> World:
    ctx, snapshot, repo, _store = await admitted(factory, tmp_path)
    sessions = AgentSessionService(factory, LOG)
    session = await sessions.create(
        ctx,
        AgentSessionCreateRequestV1(repository_id=repo, snapshot_id=snapshot, mode=mode),
        "journey",
    )
    sandbox = FakeSandbox(MappingSnapshots({snapshot: files or FILES}))
    model = ScriptedAgentModel(turns)
    journal = AgentJournal(factory, JournalCipher(JOURNAL_KEY))
    control = SessionControl(factory, LOG, owner, lease_seconds=lease_seconds)
    approvals = ApprovalService(factory, LOG)
    workspaces = WorkspaceService(factory, sandbox, image="test:latest", log=LOG)
    deps = AgentDeps(
        factory,
        LOG,
        journal,
        control,
        approvals,
        model,
        sandbox,
        workspaces,
        policy=policy or PolicyV1(),
    )
    runner = SessionRunner(deps)
    scheduler = InlineScheduler()
    scheduler.runner = runner
    commands = AgentCommands(factory, LOG, control, approvals, runner, scheduler)
    return World(
        factory,
        ctx,
        session,
        sandbox,
        model,
        deps,
        runner,
        commands,
        scheduler,
        journal,
        approvals,
        control,
        workspaces,
    )


def call(call_id: str, name: str, **args: Any) -> tuple[str, str, str]:
    import json

    return call_id, name, json.dumps(args)


def patch_call(call_id: str = "c2") -> tuple[str, str, str]:
    return call(
        call_id,
        "apply_patch",
        edits=[
            {
                "path": "src/app.py",
                "expected_hash": APP_HASH,
                "search": "return 1",
                "replace": "return 2",
            }
        ],
    )
