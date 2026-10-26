"""Versioned pure mappings from provider counters to disjoint billing categories."""

import json
from typing import Any

from app.contracts.model import TokenUsageV1
from app.contracts.model_transport import ProviderUsageEvidenceV1, UsageCategory

_LIMIT = 2147483647


def _count(payload: dict[str, Any], key: str, *, optional: bool = False) -> int:
    if key not in payload and optional:
        return 0
    value = payload.get(key)
    if type(value) is not int or not 0 <= value <= _LIMIT:
        raise ValueError("missing or invalid counter")
    return value


def _details(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise ValueError("missing counter details")
    return value


def _numeric_evidence(payload: dict[str, Any]) -> str:
    keys = {
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cached_tokens",
        "reasoning_tokens",
        "cache_write_tokens",
        "input_tokens_details",
        "output_tokens_details",
        "inputTokens",
        "outputTokens",
        "totalTokens",
        "cacheReadInputTokens",
        "cacheWriteInputTokens",
        "promptTokenCount",
        "cachedContentTokenCount",
        "candidatesTokenCount",
        "thoughtsTokenCount",
        "totalTokenCount",
        "toolUsePromptTokenCount",
    }

    def select(value: dict[str, Any], depth: int = 0) -> dict[str, Any]:
        selected: dict[str, Any] = {}
        for key, item in value.items():
            if key not in keys:
                continue
            if type(item) is int:
                selected[key] = item
            elif isinstance(item, dict) and depth < 1:
                selected[key] = select(item, depth + 1)
        return selected

    result = json.dumps(select(payload), sort_keys=True, separators=(",", ":"))
    return result if len(result.encode()) <= 16384 else "{}"


def normalize_usage(
    provider: str,
    payload: dict[str, Any],
    *,
    reasoning_enabled: bool = False,
    normalization_version: str = "usage-v1",
) -> ProviderUsageEvidenceV1:
    evidence = _numeric_evidence(payload)
    if normalization_version != "usage-v1":
        return ProviderUsageEvidenceV1(
            quality="unknown", raw_counters_json=evidence, reason="unsupported_usage"
        )
    na: list[UsageCategory] = []
    try:
        for detail_name in ("input_tokens_details", "output_tokens_details"):
            detail = payload.get(detail_name, {})
            if isinstance(detail, dict):
                for category in ("audio_tokens", "image_tokens", "video_tokens"):
                    if _count(detail, category, optional=True):
                        raise ValueError("unsupported billable modality")
        for detail_name in ("promptTokensDetails", "cacheTokensDetails", "candidatesTokensDetails"):
            modalities = payload.get(detail_name, [])
            if not isinstance(modalities, list):
                raise ValueError("invalid modality details")
            for detail in modalities:
                if not isinstance(detail, dict) or detail.get("modality") != "TEXT":
                    raise ValueError("unsupported billable modality")
                _count(detail, "tokenCount")
        thinking = cache_read = cache_write = 0
        aggregate = False
        if provider == "openai":
            total_input = _count(payload, "input_tokens")
            details = _details(payload, "input_tokens_details")
            cache_read = _count(details, "cached_tokens")
            cache_write = _count(details, "cache_write_tokens", optional=True)
            # Distinct write tariffs need another explicitly qualified normalization version.
            if cache_write:
                raise ValueError("unsupported write tariff")
            total_output = _count(payload, "output_tokens")
            thinking = _count(_details(payload, "output_tokens_details"), "reasoning_tokens")
            input_count = total_input - cache_read
            output_count = total_output - thinking
            na.append("cache_write_tokens")
            if (
                "total_tokens" in payload
                and _count(payload, "total_tokens") != total_input + total_output
            ):
                raise ValueError("inconsistent total")
        elif provider == "bedrock_claude":
            input_count = _count(payload, "inputTokens")
            output_count = _count(payload, "outputTokens")
            cache_read = _count(payload, "cacheReadInputTokens", optional=True)
            cache_write = _count(payload, "cacheWriteInputTokens", optional=True)
            # Explicit cache writes are not admitted until TTL pricing is qualified.
            if cache_write or payload.get("cacheDetails"):
                raise ValueError("unsupported cache write tariff")
            optional_categories: tuple[tuple[str, UsageCategory], ...] = (
                ("cacheReadInputTokens", "cache_read_tokens"),
                ("cacheWriteInputTokens", "cache_write_tokens"),
            )
            for key, category in optional_categories:
                if key not in payload:
                    na.append(category)
            aggregate = True
            if (
                "totalTokens" in payload
                and _count(payload, "totalTokens") != input_count + output_count + cache_read
            ):
                raise ValueError("inconsistent total")
        elif provider == "google_gemini":
            total_input = _count(payload, "promptTokenCount")
            cache_read = _count(payload, "cachedContentTokenCount", optional=True)
            output_count = _count(payload, "candidatesTokenCount")
            thinking = _count(payload, "thoughtsTokenCount", optional=not reasoning_enabled)
            if _count(payload, "toolUsePromptTokenCount", optional=True):
                raise ValueError("unqualified tool prompt accounting")
            input_count = total_input - cache_read
            na.append("cache_write_tokens")
            if "cachedContentTokenCount" not in payload:
                na.append("cache_read_tokens")
            if "thoughtsTokenCount" not in payload:
                na.append("thinking_tokens")
            if (
                "totalTokenCount" in payload
                and _count(payload, "totalTokenCount") != total_input + output_count + thinking
            ):
                raise ValueError("inconsistent total")
        else:
            return ProviderUsageEvidenceV1(
                quality="unknown", raw_counters_json=evidence, reason="unsupported_usage"
            )
        if min(input_count, output_count) < 0:
            raise ValueError("subset exceeds total")
        usage = TokenUsageV1(
            input_tokens=input_count,
            output_tokens=output_count,
            thinking_tokens=thinking,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
        )
        return ProviderUsageEvidenceV1(
            quality="aggregate_output" if aggregate else "measured",
            usage=usage,
            raw_counters_json=evidence,
            not_applicable=tuple(na),
            unavailable_breakdowns=("thinking_tokens",) if aggregate else (),
        )
    except ValueError:
        return ProviderUsageEvidenceV1(
            quality="unknown", raw_counters_json=evidence, reason="missing_or_inconsistent_usage"
        )
