"""Loopback-only SDK clients. No default profile or credential chain is consulted."""

from typing import Any, Literal

from botocore.config import Config
from botocore.configprovider import ConstantProvider
from botocore.session import Session


def emulator_client(service: Literal["s3", "sqs"], endpoint: str) -> Any:
    if service not in {"s3", "sqs"}:
        raise ValueError("unsupported emulator service")
    if endpoint != "http://127.0.0.1:4567":
        raise ValueError("owned emulator endpoint required")
    session = Session()
    store = session.get_component("config_store")
    for name, value in [
        ("profile", None),
        ("config_file", "/dev/null"),
        ("credentials_file", "/dev/null"),
    ]:
        store.set_config_provider(name, ConstantProvider(value))
    session.set_credentials("test", "test", "test")  # Emulator fixtures; never owner credentials.
    # Locked runtime supports this option; the bundled stubs omit it.
    endpoint_options: dict[str, Any] = {"ignore_configured_endpoint_urls": True}
    config = Config(
        region_name="ap-south-1",
        signature_version="s3v4" if service == "s3" else "v4",
        connect_timeout=2,
        read_timeout=30,
        max_pool_connections=4,
        retries={"total_max_attempts": 2, "mode": "standard"},
        proxies={},
        **endpoint_options,
        s3={"addressing_style": "path"} if service == "s3" else None,
    )
    return session.create_client(
        service,
        endpoint_url=endpoint,
        region_name="ap-south-1",
        config=config,
        aws_access_key_id="test",
        aws_secret_access_key="test",  # noqa: S106 -- fixed emulator fixture only
        aws_session_token="test",  # noqa: S106 -- fixed emulator fixture only
    )
