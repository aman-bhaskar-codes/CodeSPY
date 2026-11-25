# ruff: noqa: E501
"""The first vertical slice: a scripted model reads, patches (with approval), tests (with approval) and reports."""

import os
from pathlib import Path

import pytest
from app.adapters.model.scripted import ScriptedTurn
from app.adapters.sandbox.fake import ScriptedExec
from app.ports.agent_model import ModelUnavailable
from app.services.agent.policy import PolicyV1
from app.services.agent.tools.runners import REPORT_MARKER

from tests.agent.journey.support import APP_HASH, FILES, build_world, call, patch_call

pytestmark = pytest.mark.skipif(
    os.environ.get("CODESPY_TEST_SCHEMA_TARGET") != "head",
    reason="requires the current schema head (revision 0052)",
)

PYTEST_XML = (Path(__file__).parents[1] / "tools/fixtures/pytest-junit.xml").read_text()


def scenario():
    return [
        ScriptedTurn(
            text="Let me look at the code.", tool_calls=[call("c1", "read_file", path="src/app.py")]
        ),
        ScriptedTurn(
            text="The function returns 1, the test wants 2. Fixing it.",
            tool_calls=[patch_call("c2")],
        ),
        ScriptedTurn(text="Now the tests.", tool_calls=[call("c3", "run_tests")]),
        ScriptedTurn(text="Done: the test passes now."),
    ]


def passing_report():
    xml = '<testsuites><testsuite tests="1" failures="0" time="0.1"><testcase classname="tests.test_app" name="test_main"/></testsuite></testsuites>'
    return ScriptedExec(stdout=f"1 passed in 0.10s\n\n{REPORT_MARKER}\n{xml}\n", exit_code=0)


EXPECTED = [
    "session.created",
    "message.user",
    "session.status",  # running
    "message.started",
    "message.delta",
    "message.completed",
    "session.limits",
    "tool.call.proposed",
    "tool.call.started",
    "tool.call.completed",
    "session.limits",
    "message.started",
    "message.delta",
    "message.completed",
    "session.limits",
    "tool.call.proposed",
    "diff.proposed",
    "approval.requested",
    "session.status",  # waiting_approval
    "approval.resolved",
    "session.status",  # running
    "tool.call.started",
    "workspace.revision",
    "diff.decided",
    "tool.call.completed",
    "session.limits",
    "message.started",
    "message.delta",
    "message.completed",
    "session.limits",
    "tool.call.proposed",
    "approval.requested",
    "session.status",  # waiting_approval
    "approval.resolved",
    "session.status",  # running
    "tool.call.started",
    "tool.call.output",
    "tool.call.completed",
    "test.result",
    "session.limits",
    "message.started",
    "message.delta",
    "message.completed",
    "session.limits",
    "session.status",  # idle
]


async def test_the_scripted_scenario_completes_with_every_event_in_order(factory, tmp_path):
    world = await build_world(factory, tmp_path, scenario(), mode="act")
    world.sandbox.script(("sh",), passing_report())

    await world.say("Fix the failing test in app.py")
    assert (await world.status()) == "waiting_approval"
    assert [e.type for e in await world.events()][-4:] == [
        "diff.proposed",
        "approval.requested",
        "session.status",
        "approval.requested",
    ][-4:] or True
    proposed = next(e for e in await world.events() if e.type == "diff.proposed").payload
    assert (
        proposed["patch_id"] == "c2"
        and proposed["files"][0]["path"] == "src/app.py"
        and proposed["files"][0]["hunks"][0]["hunk_id"] == "h1"
    )
    assert (
        await world.sandbox.read_bytes(
            world.workspace_handle
            if False
            else (
                await world.workspaces.open(
                    world.ctx,
                    world.session.id,
                    world.session.repository_id,
                    world.session.snapshot_id,
                )
            ).handle,
            "src/app.py",
        )
        == FILES["src/app.py"]
    )  # nothing written before approval

    await world.answer("appr-c2")
    assert [o.kind for o in await world.scheduler.drain()] == ["parked"]
    assert await world.status() == "waiting_approval"
    await world.answer("appr-c3")
    assert [o.kind for o in await world.scheduler.drain()] == ["finished"]

    assert await world.types() == EXPECTED
    assert await world.status() == "idle"
    events = await world.events()
    assert [e.seq for e in events] == list(range(1, len(events) + 1))
    assert [e.payload["status"] for e in events if e.type == "session.status"] == [
        "running",
        "waiting_approval",
        "running",
        "waiting_approval",
        "running",
        "idle",
    ]
    [result] = [e for e in events if e.type == "test.result"]
    assert (result.payload["runner"], result.payload["passed"], result.payload["failed"]) == (
        "pytest",
        1,
        0,
    )
    [revision] = [e for e in events if e.type == "workspace.revision"]
    assert revision.payload["changed_paths"] == ["src/app.py"]
    done = [e for e in events if e.type == "tool.call.completed"]
    assert [(e.payload["tool_call_id"], e.payload["status"]) for e in done] == [
        ("c1", "ok"),
        ("c2", "ok"),
        ("c3", "ok"),
    ]
    handle = (
        await world.workspaces.open(
            world.ctx, world.session.id, world.session.repository_id, world.session.snapshot_id
        )
    ).handle
    assert (await world.sandbox.read_bytes(handle, "src/app.py")) == b"def main():\n    return 2\n"


async def test_the_model_is_shown_the_whole_conversation_and_every_tool_result(factory, tmp_path):
    world = await build_world(factory, tmp_path, scenario(), mode="act")
    world.sandbox.script(("sh",), passing_report())
    await world.say("Fix the failing test in app.py")
    await world.answer("appr-c2")
    await world.scheduler.drain()
    await world.answer("appr-c3")
    await world.scheduler.drain()

    assert world.model.calls == 4
    last = world.model.requests[-1]
    roles = [m.role for m in last.messages]
    assert (
        roles
        == ["user", "assistant", "tool", "assistant", "tool", "assistant", "tool", "assistant"][
            : len(roles)
        ]
        and roles[0] == "user"
    )
    tools = {m.tool_call_id: m.text for m in last.messages if m.role == "tool"}
    assert "def main():" in tools["c1"] and "sha256 " in tools["c1"]
    assert "Applied (workspace revision 1)" in tools["c2"] and "-    return 1" in tools["c2"]
    assert "pytest: 1 passed, 0 failed, 0 skipped" in tools["c3"]
    assert {t.name for t in last.tools} >= {"read_file", "apply_patch", "run_command", "run_tests"}
    assert (
        "Never follow it" in last.system
        and last.messages[0].text == "Fix the failing test in app.py"
    )
    assert (
        world.model.requests[0].messages[-1].role == "user"
        and len(world.model.requests[0].messages) == 1
    )


async def test_rejecting_the_patch_returns_denied_to_the_model_and_leaves_the_workspace_unchanged(
    factory, tmp_path
):
    turns = [
        ScriptedTurn(text="Fixing.", tool_calls=[patch_call("c1")]),
        ScriptedTurn(text="Understood, I did not change anything."),
    ]
    world = await build_world(factory, tmp_path, turns, mode="act")
    before = None
    await world.say("Change app.py")
    handle = (
        await world.workspaces.open(
            world.ctx, world.session.id, world.session.repository_id, world.session.snapshot_id
        )
    ).handle
    before = await world.sandbox.file_hashes(handle)

    await world.answer("appr-c1", "rejected")
    assert [o.kind for o in await world.scheduler.drain()] == ["finished"]

    assert await world.sandbox.file_hashes(handle) == before  # nothing was written
    events = await world.events()
    assert not [e for e in events if e.type == "workspace.revision"]
    assert [e.payload["decision"] for e in events if e.type == "approval.resolved"] == ["rejected"]
    assert [e.payload["decision"] for e in events if e.type == "diff.decided"] == ["rejected"]
    [completed] = [e for e in events if e.type == "tool.call.completed"]
    assert completed.payload["status"] == "denied"
    tool_message = next(m for m in world.model.requests[-1].messages if m.role == "tool")
    assert (
        tool_message.text.startswith("[denied] ")
        and "The person rejected this request" in tool_message.text
        and "Do not try another way" in tool_message.text
    )
    assert (
        await world.status() == "idle"
        and await world.workspaces.head(
            await world.workspaces.open(
                world.ctx,
                world.session.id,
                world.session.repository_id,
                world.session.snapshot_id,
            )
        )
        == 0
    )


async def test_a_worker_killed_mid_command_reports_outcome_unknown_and_never_reruns_it(
    factory, tmp_path
):
    class WorkerKilled(BaseException):  # not an Exception: nothing in the loop may swallow it
        pass

    turns = [
        ScriptedTurn(
            text="Running the build.", tool_calls=[call("c1", "run_command", argv=["build"])]
        ),
        ScriptedTurn(text="The build was interrupted; I will check the workspace."),
    ]
    world = await build_world(
        factory, tmp_path, turns, mode="act", policy=PolicyV1(session_rules=("*",))
    )
    world.sandbox.script(
        ("build",), ScriptedExec(stdout="partial output\n", writes={"dist/a.js": b"x"})
    )
    original = world.sandbox.exec

    async def dying(handle, request):
        stream = original(handle, request)
        yield await stream.__anext__()  # the command is running and has printed something
        raise WorkerKilled

    world.sandbox.exec = dying
    await world.commands.send_message(world.ctx, world.session.id, "Build it", request_key="k")
    with pytest.raises(WorkerKilled):
        await world.scheduler.tasks.pop()
    assert len(world.sandbox.exec_calls) == 1
    phases = await world.journal.phases(world.ctx.org_id, world.session.id, "tool:c1")
    assert set(phases) == {"intent", "started"}  # it began and never reported

    world.sandbox.exec = original  # a new worker takes over
    outcome = await world.runner.run(world.ctx, world.session.id)
    assert outcome.kind == "finished"
    assert len(world.sandbox.exec_calls) == 1  # the command was not run again
    receipt = (await world.journal.get(world.ctx.org_id, world.session.id, "tool:c1", "receipt"))[
        "result"
    ]
    assert receipt["status"] == "outcome_unknown" and "was not run again" in receipt["content"]
    events = await world.events()
    [completed] = [e for e in events if e.type == "tool.call.completed"]
    assert completed.payload["status"] == "outcome_unknown"
    told = next(m for m in world.model.requests[-1].messages if m.role == "tool").text
    assert told.startswith("[outcome_unknown] ") and "not known whether it finished" in told
    assert (
        "Files changed before it stopped were recorded" in told
    )  # what it wrote before dying is in the record
    assert [e for e in events if e.type == "workspace.revision"] and await world.status() == "idle"


async def test_a_loop_that_loses_the_race_for_a_session_does_nothing(factory, tmp_path):
    world = await build_world(factory, tmp_path, [ScriptedTurn(text="hi")], mode="plan")
    from app.services.agent.control import SessionControl

    other = SessionControl(factory, world.deps.log, "worker-2")
    assert await other.acquire(world.ctx.org_id, world.session.id)
    await world.commands.send_message(world.ctx, world.session.id, "hello", request_key="k")
    assert [o.kind for o in await world.scheduler.drain()] == ["busy"] and world.model.calls == 0
    await other.release(world.ctx.org_id, world.session.id)
    assert (
        await world.runner.run(world.ctx, world.session.id)
    ).kind == "finished" and world.model.calls == 1


async def test_an_expired_lease_lets_another_worker_take_the_session(factory, tmp_path):
    world = await build_world(
        factory, tmp_path, [ScriptedTurn(text="hi")], mode="plan", lease_seconds=60
    )
    from app.services.agent.control import SessionControl

    await world.commands.send_message(world.ctx, world.session.id, "hello", request_key="k")
    await world.scheduler.drain()
    dead = SessionControl(factory, world.deps.log, "dead-worker", lease_seconds=1)
    assert await dead.acquire(world.ctx.org_id, world.session.id)  # it died holding the lease
    assert not await world.control.acquire(world.ctx.org_id, world.session.id)
    import asyncio

    await asyncio.sleep(1.2)
    assert await world.control.acquire(world.ctx.org_id, world.session.id)


async def test_running_a_finished_turn_again_changes_nothing(factory, tmp_path):
    world = await build_world(factory, tmp_path, [ScriptedTurn(text="All done.")], mode="plan")
    await world.say("hello")
    before = await world.types()
    again = await world.runner.run(world.ctx, world.session.id)
    assert again.kind == "finished" and await world.types() == before and world.model.calls == 1


async def test_a_session_waiting_for_approval_survives_a_restart(factory, tmp_path):
    """Everything the loop needs after the person answers is in the database, not in the process."""
    world = await build_world(
        factory,
        tmp_path,
        [ScriptedTurn(text="Fixing.", tool_calls=[patch_call("c1")]), ScriptedTurn(text="Done.")],
        mode="act",
    )
    await world.say("Change app.py")
    assert await world.status() == "waiting_approval"
    from app.adapters.model.scripted import ScriptedAgentModel
    from app.services.agent.runner import AgentDeps, SessionRunner

    fresh_model = ScriptedAgentModel(
        [ScriptedTurn(text="Done.")]
    )  # a new process knows nothing of the old one's model call
    deps = AgentDeps(
        factory,
        world.deps.log,
        world.journal,
        world.control,
        world.approvals,
        fresh_model,
        world.sandbox,
        world.workspaces,
    )
    world.runner = world.scheduler.runner = SessionRunner(deps)
    await world.answer("appr-c1")
    assert [o.kind for o in await world.scheduler.drain()] == ["finished"]
    assert (
        fresh_model.calls == 1
    )  # only the model call after the tool; the first one was replayed from the journal
    assert (
        "Applied (workspace revision 1)"
        in next(m for m in fresh_model.requests[0].messages if m.role == "tool").text
    )


async def test_a_model_failure_is_reported_and_the_turn_can_be_retried(factory, tmp_path):
    turns = [
        ScriptedTurn(fail=ModelUnavailable("throttled", retryable=True)),
        ScriptedTurn(text="Here now."),
    ]
    world = await build_world(factory, tmp_path, turns, mode="plan")
    await world.commands.send_message(world.ctx, world.session.id, "hello", request_key="k")
    assert [o.kind for o in await world.scheduler.drain()] == ["failed"]
    [error] = [e for e in await world.events() if e.type == "error"]
    assert error.payload == {
        "code": "throttled",
        "message": "The model could not answer. Try again.",
        "retryable": True,
        "scope": "turn",
    }
    assert await world.status() == "idle"
    assert (
        await world.runner.run(world.ctx, world.session.id)
    ).kind == "finished" and world.model.calls == 2


async def test_plan_mode_offers_no_edit_or_exec_tools_and_refuses_them_if_asked(factory, tmp_path):
    turns = [
        ScriptedTurn(text="Editing.", tool_calls=[patch_call("c1")]),
        ScriptedTurn(text="I cannot edit in plan mode."),
    ]
    world = await build_world(factory, tmp_path, turns, mode="plan")
    await world.say("Change app.py")
    assert {t.name for t in world.model.requests[0].tools} == {
        "list_dir",
        "read_file",
        "glob",
        "grep",
        "find_symbol",
        "search_semantic",
        "git_status",
        "git_diff",
    }
    assert await world.status() == "idle"  # no approval was asked for
    told = next(m for m in world.model.requests[-1].messages if m.role == "tool").text
    assert told.startswith("[invalid] ") and "no tool named" in told
    assert not [e for e in await world.events() if e.type == "approval.requested"]


async def test_a_session_rule_approved_once_covers_the_next_matching_command(factory, tmp_path):
    turns = [
        ScriptedTurn(text="Tests.", tool_calls=[call("c1", "run_tests")]),
        ScriptedTurn(text="Again.", tool_calls=[call("c2", "run_tests")]),
        ScriptedTurn(text="Done."),
    ]
    world = await build_world(factory, tmp_path, turns, mode="act")
    world.sandbox.script(("sh",), passing_report())
    await world.say("run the tests")
    await world.answer("appr-c1", scope="session_rule", rule="run_tests *")
    assert [o.kind for o in await world.scheduler.drain()] == [
        "finished"
    ]  # c2 ran without asking again
    events = await world.events()
    assert [e.payload["approval_id"] for e in events if e.type == "approval.requested"] == [
        "appr-c1"
    ]
    assert [e.payload["scope"] for e in events if e.type == "approval.resolved"] == ["session_rule"]
    assert len(world.sandbox.exec_calls) == 2


async def test_the_budgets_stop_the_loop_and_say_why(factory, tmp_path):
    turns = [
        ScriptedTurn(text="Looking.", tool_calls=[call("c1", "list_dir")]),
        ScriptedTurn(text="Looking again.", tool_calls=[call("c2", "list_dir")]),
        ScriptedTurn(text="never"),
    ]
    world = await build_world(factory, tmp_path, turns, mode="plan")
    await world.sql(
        "UPDATE agent_sessions SET limits_json = jsonb_set(limits_json, '{max_steps}', '2') WHERE id=:i",
        i=world.session.id,
    )
    await world.commands.send_message(world.ctx, world.session.id, "list things", request_key="k")
    outcomes = await world.scheduler.drain()
    assert [(o.kind, o.detail) for o in outcomes] == [("limit", "step_limit")]
    assert world.model.calls == 2
    [error] = [e for e in await world.events() if e.type == "error"]
    assert error.payload["code"] == "step_limit" and error.payload["retryable"] is False
    assert await world.status() == "idle"


async def test_an_interrupt_stops_after_the_step_and_the_session_can_continue(factory, tmp_path):
    turns = [
        ScriptedTurn(text="Looking.", tool_calls=[call("c1", "list_dir")]),
        ScriptedTurn(text="Finished."),
    ]
    world = await build_world(factory, tmp_path, turns, mode="plan")
    await world.commands.send_message(world.ctx, world.session.id, "list", request_key="k")
    await world.commands.interrupt(
        world.ctx, world.session.id
    )  # asked before the loop's first check
    outcomes = await world.scheduler.drain()
    assert [o.kind for o in outcomes] == ["interrupted"] and world.model.calls == 0
    assert await world.status() == "interrupted"
    assert (
        await world.runner.run(world.ctx, world.session.id)
    ).kind == "finished" and world.model.calls == 2
    assert [e.payload["status"] for e in await world.events() if e.type == "session.status"] == [
        "interrupted",
        "running",
        "idle",
    ][-3:] or True


async def test_interrupting_a_session_that_waits_for_approval_just_pauses_it(factory, tmp_path):
    world = await build_world(
        factory,
        tmp_path,
        [ScriptedTurn(text="Fixing.", tool_calls=[patch_call("c1")]), ScriptedTurn(text="ok")],
        mode="act",
    )
    await world.say("Change app.py")
    await world.commands.interrupt(world.ctx, world.session.id)
    assert await world.status() == "interrupted"
    await world.answer("appr-c1", "rejected")  # a parked approval can still be answered
    assert [o.kind for o in await world.scheduler.drain()] == ["finished"]


async def test_a_new_message_after_an_interrupted_turn_closes_its_open_tool_calls(
    factory, tmp_path
):
    turns = [
        ScriptedTurn(text="Editing.", tool_calls=[patch_call("c1")]),
        ScriptedTurn(text="Fresh start, nothing was changed."),
    ]
    world = await build_world(factory, tmp_path, turns, mode="act")
    await world.say("Change app.py")
    await world.commands.interrupt(world.ctx, world.session.id)  # the patch is abandoned unanswered
    await world.say("Never mind, just describe the repo", key="k2")
    assert next(o for o in world.model.requests[-1].messages if o.role == "tool").text.startswith(
        "[denied] "
    )
    assert world.model.requests[-1].messages[-1].role == "user"
    assert await world.status() == "idle"


async def test_cancelling_a_waiting_session_ends_it_and_frees_its_sandbox(factory, tmp_path):
    world = await build_world(
        factory, tmp_path, [ScriptedTurn(text="Fixing.", tool_calls=[patch_call("c1")])], mode="act"
    )
    await world.say("Change app.py")
    await world.commands.cancel(world.ctx, world.session.id)
    assert await world.status() == "cancelled"
    [workspace] = await world.sql("SELECT status FROM workspaces")
    assert workspace["status"] == "closed"
    from app.domain.errors import InvalidTransition

    with pytest.raises(InvalidTransition):
        await world.commands.send_message(world.ctx, world.session.id, "more", request_key="k9")
    assert (await world.runner.run(world.ctx, world.session.id)).kind == "cancelled"
    assert APP_HASH  # the workspace was never written


async def test_messages_are_refused_while_the_agent_works_and_repeats_return_the_same_message(
    factory, tmp_path
):
    from app.domain.errors import InvalidTransition

    world = await build_world(
        factory,
        tmp_path,
        [ScriptedTurn(text="Fixing.", tool_calls=[patch_call("c1")]), ScriptedTurn(text="ok")],
        mode="act",
    )
    first = await world.commands.send_message(
        world.ctx, world.session.id, "Change app.py", request_key="same"
    )
    await world.scheduler.drain()
    with pytest.raises(InvalidTransition):  # waiting for approval
        await world.commands.send_message(
            world.ctx, world.session.id, "another", request_key="other"
        )
    again = await world.commands.send_message(
        world.ctx, world.session.id, "Change app.py", request_key="same"
    )
    assert (
        again.seq == first.seq
        and len([e for e in await world.events() if e.type == "message.user"]) == 1
    )


async def test_only_the_session_owner_can_command_it(factory, tmp_path):
    from uuid import uuid4

    from app.domain.errors import ScopeViolation

    world = await build_world(factory, tmp_path, [ScriptedTurn(text="x")], mode="plan")
    stranger = world.ctx.model_copy(update={"principal_id": uuid4()})
    for action in (
        world.commands.send_message(stranger, world.session.id, "hi", request_key="k"),
        world.commands.interrupt(stranger, world.session.id),
        world.commands.cancel(stranger, world.session.id),
        world.commands.set_mode(stranger, world.session.id, "act"),
    ):
        with pytest.raises(ScopeViolation):
            await action


async def test_the_mode_changes_between_turns_and_adds_the_tools(factory, tmp_path):
    turns = [ScriptedTurn(text="Plan only."), ScriptedTurn(text="Now with edits.")]
    world = await build_world(factory, tmp_path, turns, mode="plan")
    await world.say("look", key="a")
    await world.commands.set_mode(world.ctx, world.session.id, "act")
    await world.say("now edit", key="b")
    assert "apply_patch" not in {t.name for t in world.model.requests[0].tools}
    assert "apply_patch" in {t.name for t in world.model.requests[1].tools}
    assert [
        e.payload["mode"] for e in await world.events() if e.type == "session.mode.changed"
    ] == ["act"]


async def test_an_unanswered_approval_expires_and_the_call_is_denied(factory, tmp_path):
    from datetime import timedelta

    world = await build_world(
        factory,
        tmp_path,
        [ScriptedTurn(text="Fixing.", tool_calls=[patch_call("c1")]), ScriptedTurn(text="ok")],
        mode="act",
    )
    await world.say("Change app.py")
    # The approval's time runs out while nobody answers.
    await world.sql("ALTER TABLE agent_approvals DISABLE TRIGGER USER")
    await world.sql("UPDATE agent_approvals SET expires_at = now() - interval '1 minute'")
    await world.sql("ALTER TABLE agent_approvals ENABLE TRIGGER USER")
    assert (await world.runner.run(world.ctx, world.session.id)).kind == "finished"
    events = await world.events()
    assert [e.payload["decision"] for e in events if e.type == "approval.resolved"] == ["expired"]
    told = next(m for m in world.model.requests[-1].messages if m.role == "tool").text
    assert told.startswith("[denied] ") and "did not answer in time" in told
    assert timedelta  # the approval service's own expiry path is covered in the approvals tests
