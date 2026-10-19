import pytest
from app.adapters.github.transport import FixedGitHubTransport, public_addresses


def test_dns_pin_rejects_private_mixed_and_unrecognized_origins():
    assert public_addresses(["140.82.113.3"]) == ["140.82.113.3"]
    for addresses in [
        ["127.0.0.1"],
        ["169.254.169.254"],
        ["::1"],
        ["140.82.113.3", "10.0.0.1"],
        ["100.64.0.1"],
        [],
    ]:
        with pytest.raises(ValueError):
            public_addresses(addresses)
    with pytest.raises(ValueError):
        FixedGitHubTransport({"evil.test"})
