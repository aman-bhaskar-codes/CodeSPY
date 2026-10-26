"""Executor admission uses injected policy and complete tool schemas."""

from app.contracts.model import ToolDefinitionV1
from app.contracts.provider_budget import DisclosurePolicyV1
from app.services.investigation_executor import (
    InMemoryInvestigationAuthority,
    InMemoryInvestigationCheckpointRepo,
    InMemoryInvestigationStepReceiptRepo,
    InvestigationExecutorService,
)

from tests.investigations.unit.test_executor import (
    DummyModelGateway,
    DummyToolService,
    _make_context,
    _make_spec,
)


async def test_executor_passes_registered_schemas_and_server_policy():
    spec = _make_spec()
    ctx = _make_context(spec)
    policy = DisclosurePolicyV1(policy_version="test-policy")
    schema = ToolDefinitionV1(
        name="check_memory",
        description="Read reviewed memory",
        parameters_schema={"type": "object", "properties": {}},
    )

    class Tools(DummyToolService):
        def get_tool_definitions(self):
            return (schema,)

    class Gateway(DummyModelGateway):
        async def execute_call(self, **kwargs):
            assert kwargs["disclosure_policy"] == policy
            assert kwargs["tools"] == (schema,)
            return await super().execute_call(**kwargs)

    executor = InvestigationExecutorService(
        Gateway(),
        Tools(),
        InMemoryInvestigationAuthority(ctx.scope),
        InMemoryInvestigationCheckpointRepo(),
        InMemoryInvestigationStepReceiptRepo(),
        spec_provider=lambda session, org, digest: spec,
        policy_resolver=lambda scope: policy,
    )
    await executor.advance(ctx)
    await executor.advance(ctx)


def test_scoped_path_filter_uses_directory_boundary():
    from app.services.investigation_tools import path_matches_prefix

    assert path_matches_prefix("src/auth/login.py", "src/auth")
    assert path_matches_prefix("src/auth/login.py", "src/auth/")
    assert not path_matches_prefix("src/authorization.py", "src/auth")
    assert path_matches_prefix("src/auth.py", "src/auth.py")
