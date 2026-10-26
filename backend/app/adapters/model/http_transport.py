"""Bounded HTTP exchange over an explicitly injected lifespan-owned client."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from urllib.parse import quote

import httpx
from app.contracts.model import ModelRequestV1
from app.contracts.model_configuration import ModelProfileV1
from app.contracts.model_transport import PreparedModelCallV1, ProviderOutcomeV1
from app.ports.model_transport import ProviderCodecPort
from pydantic import SecretStr


class ProviderTransportError(Exception):
    """Stable diagnostic without provider bodies, headers or credentials."""

    def __init__(self, code: str, *, dispatched: bool = True) -> None:
        self.code = code
        self.possibly_dispatched = dispatched
        super().__init__(code)


class HTTPModelTransport:
    """No client creation, credential discovery, retry, redirect or tool execution."""

    def __init__(
        self,
        codec: ProviderCodecPort,
        client: httpx.AsyncClient,
        credentials: Callable[[str], Awaitable[SecretStr]],
    ) -> None:
        self.codec = codec
        self.client = client
        self.credentials = credentials
        self._capacity = asyncio.Semaphore(2)
        self._pending = 0

    @property
    def pending_calls(self) -> int:
        return self._pending

    def prepare(self, request: ModelRequestV1, profile: ModelProfileV1) -> PreparedModelCallV1:
        if profile.endpoint_kind not in ("openai_responses", "gemini_generate_content"):
            raise ValueError("HTTP transport does not support this endpoint")
        if profile.endpoint_kind == "gemini_generate_content" and profile.api_version not in (
            "v1",
            "v1beta",
        ):
            raise ValueError("explicit supported Gemini API version required")
        return self.codec.prepare(request, profile)

    async def invoke(
        self,
        prepared: PreparedModelCallV1,
        *,
        before_dispatch: Callable[[], Awaitable[None]] | None = None,
    ) -> ProviderOutcomeV1:
        verified = self.prepare(prepared.request, prepared.profile)
        if verified.wire_digest != prepared.wire_digest or verified.body_json != prepared.body_json:
            raise ProviderTransportError("provider_request_identity_mismatch", dispatched=False)
        if self._pending >= 10:
            raise ProviderTransportError("provider_capacity_exceeded", dispatched=False)
        self._pending += 1
        sent = False
        try:
            async with asyncio.timeout(prepared.profile.timeouts.total_seconds):
                async with self._capacity:
                    profile = prepared.profile
                    try:
                        secret = await self.credentials(profile.credential_ref)
                    except Exception:
                        raise ProviderTransportError(
                            "provider_credentials_unavailable", dispatched=False
                        ) from None
                    if not secret.get_secret_value():
                        raise ProviderTransportError(
                            "provider_credentials_unavailable", dispatched=False
                        )
                    if profile.endpoint_kind == "openai_responses":
                        url = "https://api.openai.com/v1/responses"
                        headers = {"authorization": "Bearer " + secret.get_secret_value()}
                    else:
                        model = quote(profile.model_id, safe="")
                        url = f"https://generativelanguage.googleapis.com/{profile.api_version}/models/{model}:generateContent"
                        headers = {"x-goog-api-key": secret.get_secret_value()}
                    headers["content-type"] = "application/json"
                    timeout = httpx.Timeout(
                        connect=profile.timeouts.connect_seconds,
                        read=profile.timeouts.read_seconds,
                        write=profile.timeouts.write_seconds,
                        pool=profile.timeouts.pool_seconds,
                    )
                    if before_dispatch is not None:
                        await before_dispatch()
                    sent = True
                    async with self.client.stream(
                        "POST",
                        url,
                        headers=headers,
                        content=prepared.body_json.encode(),
                        timeout=timeout,
                        follow_redirects=False,
                    ) as response:
                        status = response.status_code
                        if status >= 300:
                            code = (
                                "provider_redirect_rejected"
                                if status < 400
                                else "provider_authentication"
                                if status in (401, 403)
                                else "provider_rate_limited"
                                if status == 429
                                else "provider_unavailable"
                                if status >= 500
                                else "provider_request_rejected"
                            )
                            raise ProviderTransportError(code)
                        body = bytearray()
                        async for chunk in response.aiter_bytes(chunk_size=65536):
                            if len(body) + len(chunk) > profile.response_byte_limit:
                                raise ProviderTransportError("provider_response_too_large")
                            body.extend(chunk)
                    try:
                        payload = json.loads(body)
                    except (ValueError, RecursionError):
                        raise ProviderTransportError("provider_invalid_json") from None
                    if not isinstance(payload, dict):
                        raise ProviderTransportError("provider_invalid_response")
                    return self.codec.decode(prepared.request, profile, payload)
        except (TimeoutError, httpx.TimeoutException):
            raise ProviderTransportError("provider_timeout", dispatched=sent) from None
        except httpx.HTTPError:
            raise ProviderTransportError("provider_connection_failed", dispatched=sent) from None
        finally:
            self._pending -= 1

    async def aclose(self) -> None:
        # The composition root owns the injected HTTP client's lifespan.
        return None
