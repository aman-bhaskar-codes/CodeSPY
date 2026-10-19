from datetime import UTC, datetime, timedelta

import pytest
from app.models.phase2_tables import PHASE2_TABLES
from app.security.oauth import GrantCipher, OAuthRejected, OAuthService, ProviderIdentity
from sqlalchemy import func, select


class Provider:
    def authorization_url(self, state, challenge):
        self.state, self.challenge = state, challenge
        return "https://github.com/login/oauth/authorize?state=" + state

    async def exchange(self, code, verifier):
        assert code == "fixture-code" and len(verifier) >= 43
        return ProviderIdentity(
            42, "Fixture", "synthetic-token", datetime.now(UTC) + timedelta(hours=1)
        )


async def test_oauth_one_time_browser_binding_encrypted_grant_and_identity_race(factory):
    provider = Provider()
    service = OAuthService(factory, provider, GrantCipher("fixture", b"k" * 32))
    begun = await service.begin("/repositories")
    with pytest.raises(OAuthRejected):
        await service.callback(begun.state, "wrong-browser", "fixture-code")
    principal = await service.callback(begun.state, begun.browser, "fixture-code")
    with pytest.raises(OAuthRejected):
        await service.callback(begun.state, begun.browser, "fixture-code")
    second = await service.begin("/")
    assert await service.callback(second.state, second.browser, "fixture-code") == principal
    async with factory() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(PHASE2_TABLES["external_identities"])
            )
            == 1
        )
        row = (await session.execute(select(PHASE2_TABLES["github_user_grants"]))).mappings().one()
        assert b"synthetic-token" not in row["token_cipher"]
        assert service.cipher.decrypt(row["token_cipher"], str(principal)) == b"synthetic-token"
    for path in ["//evil.test", "https://evil.test", "/\\evil", "/%2f%2fevil", "/bad\nheader"]:
        with pytest.raises(ValueError):
            await service.begin(path)
