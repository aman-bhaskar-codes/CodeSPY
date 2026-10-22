"""Unit tests for privacy transformations, secret redaction, and interval mappings."""

from app.contracts.source_view import IntervalMappingV1, PrivacyPolicyV1
from app.services.privacy_transform import (
    is_sensitive_path,
    map_original_offset_to_safe,
    map_safe_offset_to_original,
    slice_mappings_for_safe_range,
    transform_model_safe,
)


def test_sensitive_path_detection() -> None:
    patterns = (
        ".env*",
        "*.pem",
        "*.key",
        "id_rsa*",
        "id_ed25519*",
        "*credentials*",
        ".aws/*",
        ".ssh/*",
    )
    # Positive matches
    assert is_sensitive_path(".env", patterns)
    assert is_sensitive_path(".env.production", patterns)
    assert is_sensitive_path("config/.env.local", patterns)
    assert is_sensitive_path("server.key", patterns)
    assert is_sensitive_path("certs/ca.pem", patterns)
    assert is_sensitive_path("id_rsa", patterns)
    assert is_sensitive_path(".ssh/id_rsa", patterns)
    assert is_sensitive_path(".aws/credentials", patterns)
    assert is_sensitive_path("auth/google_credentials.json", patterns)

    # Negative matches
    assert not is_sensitive_path("src/index.ts", patterns)
    assert not is_sensitive_path("README.md", patterns)
    assert not is_sensitive_path("app/services/auth.py", patterns)


def test_empty_input_transform() -> None:
    policy = PrivacyPolicyV1()
    res = transform_model_safe(b"", policy)
    assert res.safe_bytes == b""
    assert res.safe_text == ""
    assert res.mappings == ()
    assert res.redaction_count == 0


def test_clean_ascii_without_secrets() -> None:
    policy = PrivacyPolicyV1()
    raw = b"def add(a: int, b: int) -> int:\n    return a + b\n"
    res = transform_model_safe(raw, policy)

    assert res.safe_bytes == raw
    assert res.safe_text == raw.decode("utf-8")
    assert res.redaction_count == 0
    assert len(res.mappings) == 1
    assert res.mappings[0] == IntervalMappingV1(
        safe_start_byte=0,
        safe_end_byte=len(raw),
        original_start_byte=0,
        original_end_byte=len(raw),
        is_redacted=False,
    )


def test_canary_secret_redaction_and_offset_mapping() -> None:
    policy = PrivacyPolicyV1()
    canary = "codespy_sec_canary_alpha_9999"
    raw_text = f"prefix_code();\nconst token = '{canary}';\nsuffix_code();\n"
    raw_bytes = raw_text.encode()

    res = transform_model_safe(raw_bytes, policy)

    # Canary must NEVER appear in safe bytes or text
    assert res.safe_text is not None
    assert canary not in res.safe_text
    assert canary.encode() not in res.safe_bytes
    assert "[REDACTED_SECRET:canary]" in res.safe_text
    assert res.redaction_count == 1

    # Verify interval mappings
    assert len(res.mappings) == 3
    m_pre, m_redacted, m_post = res.mappings

    assert not m_pre.is_redacted
    assert m_pre.original_start_byte == 0
    assert m_pre.safe_start_byte == 0
    assert (
        raw_bytes[m_pre.original_start_byte : m_pre.original_end_byte]
        == (res.safe_bytes[m_pre.safe_start_byte : m_pre.safe_end_byte])
    )

    assert m_redacted.is_redacted
    assert m_redacted.redaction_type == "canary"
    assert res.safe_bytes[m_redacted.safe_start_byte : m_redacted.safe_end_byte] == (
        b"[REDACTED_SECRET:canary]"
    )
    assert raw_bytes[m_redacted.original_start_byte : m_redacted.original_end_byte] == (
        canary.encode()
    )

    assert not m_post.is_redacted
    assert (
        raw_bytes[m_post.original_start_byte : m_post.original_end_byte]
        == (res.safe_bytes[m_post.safe_start_byte : m_post.safe_end_byte])
    )


def test_multibyte_unicode_with_secret() -> None:
    """Ensure Japanese and emoji multibyte characters preserve exact byte offsets."""
    policy = PrivacyPolicyV1()
    canary = "codespy_sec_canary_multibyte_77"
    # 'こんにちは' is 5 chars, 15 bytes. '🚀' is 1 char, 4 bytes.
    raw_text = f"# 日本語テスト こんにちは 🚀\napi_key = '{canary}'\n# 終了\n"
    raw_bytes = raw_text.encode()

    res = transform_model_safe(raw_bytes, policy)

    assert res.safe_text is not None
    assert canary not in res.safe_text
    assert canary.encode() not in res.safe_bytes
    assert "[REDACTED_SECRET:canary]" in res.safe_text

    # Test bidirectional offset translation
    # 1. Start of file (inside Japanese comment)
    orig_pos_1 = raw_bytes.find("こんにちは".encode())
    safe_pos_1 = map_original_offset_to_safe(res.mappings, orig_pos_1)
    assert safe_pos_1 == orig_pos_1
    assert map_safe_offset_to_original(res.mappings, safe_pos_1) == orig_pos_1

    # 2. Inside redacted canary
    orig_canary_pos = raw_bytes.find(canary.encode())
    safe_canary_pos = map_original_offset_to_safe(res.mappings, orig_canary_pos)
    # Inside redacted span maps to start of canary in original
    assert map_safe_offset_to_original(res.mappings, safe_canary_pos) == orig_canary_pos

    # 3. Post-redaction multibyte comment ('# 終了')
    orig_pos_post = raw_bytes.find("終了".encode())
    safe_pos_post = map_original_offset_to_safe(res.mappings, orig_pos_post)
    # The safe offset is shifted by difference in length between canary and replacement
    diff = len(b"[REDACTED_SECRET:canary]") - len(canary.encode())
    assert safe_pos_post == orig_pos_post + diff
    assert map_safe_offset_to_original(res.mappings, safe_pos_post) == orig_pos_post


def test_crlf_and_tabs_preserved() -> None:
    policy = PrivacyPolicyV1()
    raw = b"\t\tlet x = 10;\r\n\t\tlet y = 20;\r\n"
    res = transform_model_safe(raw, policy)

    assert res.safe_bytes == raw
    assert b"\r\n" in res.safe_bytes
    assert b"\t" in res.safe_bytes
    assert res.redaction_count == 0


def test_multiple_and_adjacent_secrets() -> None:
    policy = PrivacyPolicyV1()
    c1 = "codespy_sec_canary_first_11"
    c2 = "codespy_sec_canary_second_22"
    raw = f"var a = '{c1}';\nvar b = '{c2}';\n".encode()

    res = transform_model_safe(raw, policy)
    assert c1.encode() not in res.safe_bytes
    assert c2.encode() not in res.safe_bytes
    assert res.redaction_count == 2
    assert len(res.mappings) == 5  # pre, red1, mid, red2, post


def test_private_key_redaction() -> None:
    policy = PrivacyPolicyV1()
    hdr = "-----" + "BEGIN RSA PRIVATE " + "KEY-----"
    ftr = "-----" + "END RSA PRIVATE " + "KEY-----"
    key_body = f"{hdr}\nMIIEowIBAAKCAQEA0Y1+exampleFakeKeyPayloadHere...\n{ftr}"
    raw = f"// Config\nconst key = `{key_body}`;\n".encode()

    res = transform_model_safe(raw, policy)
    assert res.safe_text is not None
    assert "MIIEow" not in res.safe_text
    assert "[REDACTED_SECRET:private_key]" in res.safe_text
    assert res.redaction_count == 1


def test_slice_mappings_for_safe_range() -> None:
    mappings = (
        IntervalMappingV1(
            safe_start_byte=0,
            safe_end_byte=20,
            original_start_byte=0,
            original_end_byte=20,
            is_redacted=False,
        ),
        IntervalMappingV1(
            safe_start_byte=20,
            safe_end_byte=45,
            original_start_byte=20,
            original_end_byte=50,
            is_redacted=True,
            redaction_type="canary",
        ),
        IntervalMappingV1(
            safe_start_byte=45,
            safe_end_byte=70,
            original_start_byte=50,
            original_end_byte=75,
            is_redacted=False,
        ),
    )

    sliced = slice_mappings_for_safe_range(mappings, 10, 30)
    # Should include first (overlaps 10-20) and second (overlaps 20-30)
    assert len(sliced) == 2
    assert sliced[0].safe_start_byte == 0
    assert sliced[1].safe_start_byte == 20
