"""Provider numeric evidence is normalized without overlapping billable counts."""

import pytest
from app.services.model_usage import normalize_usage


def test_openai_separates_subsets():
    result = normalize_usage(
        "openai",
        {
            "input_tokens": 100,
            "input_tokens_details": {"cached_tokens": 30},
            "output_tokens": 50,
            "output_tokens_details": {"reasoning_tokens": 20},
        },
    )
    assert result.quality == "measured"
    assert result.usage.model_dump() == {
        "input_tokens": 70,
        "output_tokens": 30,
        "thinking_tokens": 20,
        "cache_read_tokens": 30,
        "cache_write_tokens": 0,
    }
    assert result.usage.total_tokens == 150


def test_bedrock_keeps_uncached_input_and_marks_aggregate_output():
    result = normalize_usage(
        "bedrock_claude",
        {
            "inputTokens": 70,
            "cacheReadInputTokens": 30,
            "cacheWriteInputTokens": 0,
            "outputTokens": 50,
            "totalTokens": 150,
        },
    )
    assert result.quality == "aggregate_output"
    assert result.unavailable_breakdowns == ("thinking_tokens",)
    assert result.usage.input_tokens == 70
    assert result.usage.cache_read_tokens == 30
    assert result.usage.output_tokens == 50


def test_gemini_separates_prompt_cache_and_thoughts():
    result = normalize_usage(
        "google_gemini",
        {
            "promptTokenCount": 100,
            "cachedContentTokenCount": 30,
            "candidatesTokenCount": 50,
            "thoughtsTokenCount": 20,
            "totalTokenCount": 170,
        },
    )
    assert result.quality == "measured"
    assert result.usage.input_tokens == 70
    assert result.usage.output_tokens == 50
    assert result.usage.thinking_tokens == 20


@pytest.mark.parametrize(
    "provider,payload",
    [
        ("openai", {}),
        ("google_gemini", {}),
        ("bedrock_claude", {}),
        (
            "openai",
            {
                "input_tokens": 10,
                "input_tokens_details": {"cached_tokens": 11},
                "output_tokens": 5,
                "output_tokens_details": {"reasoning_tokens": 0},
            },
        ),
        (
            "openai",
            {
                "input_tokens": True,
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens": 5,
                "output_tokens_details": {"reasoning_tokens": 0},
            },
        ),
        (
            "openai",
            {
                "input_tokens": -1,
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens": 5,
                "output_tokens_details": {"reasoning_tokens": 0},
            },
        ),
        (
            "google_gemini",
            {
                "promptTokenCount": 100,
                "candidatesTokenCount": 10,
                "thoughtsTokenCount": 0,
                "toolUsePromptTokenCount": 2,
            },
        ),
        (
            "bedrock_claude",
            {
                "inputTokens": 1,
                "outputTokens": 2,
                "cacheWriteInputTokens": 5,
                "cacheDetails": [{"ttl": "5m", "inputTokens": 2}, {"ttl": "1h", "inputTokens": 3}],
            },
        ),
    ],
)
def test_untrustworthy_usage_remains_unknown(provider, payload):
    result = normalize_usage(provider, payload)
    assert result.quality == "unknown"
    assert result.usage is None


def test_omitted_optional_cache_is_distinct_from_measured_zero():
    result = normalize_usage(
        "bedrock_claude", {"inputTokens": 10, "outputTokens": 5, "totalTokens": 15}
    )
    assert "cache_read_tokens" in result.not_applicable
    assert result.usage.cache_read_tokens == 0


def test_raw_payload_content_is_never_retained():
    result = normalize_usage(
        "google_gemini",
        {
            "promptTokenCount": 10,
            "candidatesTokenCount": 5,
            "thoughtsTokenCount": 0,
            "private_prompt": "secret-canary",
        },
    )
    assert "secret-canary" not in result.model_dump_json()


def test_missing_thoughts_for_reasoning_gemini_is_unknown():
    assert (
        normalize_usage(
            "google_gemini",
            {"promptTokenCount": 10, "candidatesTokenCount": 5},
            reasoning_enabled=True,
        ).quality
        == "unknown"
    )


@pytest.mark.parametrize(
    "provider,payload",
    [
        (
            "openai",
            {
                "input_tokens": 10,
                "input_tokens_details": {"cached_tokens": 0, "audio_tokens": 4},
                "output_tokens": 5,
                "output_tokens_details": {"reasoning_tokens": 0},
            },
        ),
        (
            "google_gemini",
            {
                "promptTokenCount": 10,
                "candidatesTokenCount": 5,
                "thoughtsTokenCount": 0,
                "candidatesTokensDetails": [{"modality": "AUDIO", "tokenCount": 5}],
            },
        ),
    ],
)
def test_unsupported_billable_modalities_are_unknown(provider, payload):
    assert normalize_usage(provider, payload).quality == "unknown"
