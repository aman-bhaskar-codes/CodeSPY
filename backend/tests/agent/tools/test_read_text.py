"""Truncation keeps head and tail; redaction removes every secret and keeps line numbers."""

import re

import pytest
from app.services.agent.tools.text import (
    evidence_id,
    glob_regex,
    is_sensitive,
    redact,
    truncate_head_tail,
)

from tests.agent.tools.support import AWS, CANARY, CONFIG, GITHUB, SECRETS, SLACK


def test_short_text_is_returned_unchanged():
    out = truncate_head_tail("hello\nworld\n", 1000)
    assert (out.text, out.truncated, out.bytes_total) == ("hello\nworld\n", False, 12)


def test_truncation_keeps_the_start_and_the_end_and_counts_what_it_cut():
    text = "HEAD" + "-" * 996 + "x" * 1000 + "-" * 996 + "TAIL"  # 3000 bytes
    out = truncate_head_tail(text, 1000)
    assert out.truncated and out.bytes_total == 3000
    assert len(out.text.encode()) <= 1000
    assert out.text.startswith("HEAD") and out.text.endswith("TAIL")
    omitted = int(re.search(r"\[\.\.\. (\d+) bytes omitted \.\.\.\]", out.text).group(1))
    head, _, tail = out.text.partition("\n[... ")
    tail = tail.split("]\n", 1)[1]
    assert len(head.encode()) + omitted + len(tail.encode()) == 3000  # nothing unaccounted for


def test_truncation_never_splits_a_multibyte_character():
    for text in ("é" * 5000, "日本語" * 2000, "😀" * 3000):
        out = truncate_head_tail(text, 997)
        assert out.truncated and len(out.text.encode()) <= 997
        assert "\ufffd" not in out.text
        out.text.encode("utf-8")  # still valid text


def test_truncation_of_exactly_the_limit_is_not_truncated():
    assert not truncate_head_tail("a" * 500, 500).truncated
    assert truncate_head_tail("a" * 501, 500).truncated


def test_every_recognised_secret_is_replaced_and_counted():
    text, count = redact(CONFIG)
    for secret in SECRETS:
        assert secret not in text
    assert count == 6  # aws, github, slack, password, canary, private key
    assert "[REDACTED_SECRET:aws_access_key]" in text and "[REDACTED_SECRET:private_key]" in text
    assert "[REDACTED_SECRET:canary]" in text


def test_redaction_keeps_the_number_of_lines_so_line_numbers_stay_true():
    text, _ = redact(CONFIG)
    assert text.count("\n") == CONFIG.count("\n")
    assert text.splitlines()[-1] == "AFTER_THE_KEY = 1"  # the last line is still the last line


def test_text_without_secrets_is_untouched():
    assert redact("def f():\n    return 1\n") == ("def f():\n    return 1\n", 0)


@pytest.mark.parametrize(
    "secret", [AWS, GITHUB, SLACK, CANARY], ids=["aws", "github", "slack", "canary"]
)
def test_a_secret_is_found_wherever_it_sits(secret):
    for text in (secret, f"x={secret};", f"a\n{secret}\nb", f"{secret}{secret}"):
        assert secret not in redact(text)[0]


def test_sensitive_paths_are_recognised():
    for path in (
        ".env",
        ".env.production",
        "certs/server.pem",
        "deploy/id_rsa",
        "a/b/aws-credentials.json",
        ".aws/config",
        "k.key",
    ):
        assert is_sensitive(path), path
    for path in ("src/app.py", "README.md", "environment.md", "docs/credit.md"):
        assert not is_sensitive(path), path


def test_evidence_ids_are_stable_and_distinguish_ranges():
    assert evidence_id("a.py", 1, 5, "h") == evidence_id("a.py", 1, 5, "h")
    assert evidence_id("a.py", 1, 5, "h") != evidence_id("a.py", 1, 6, "h")
    assert re.fullmatch(r"ev-[0-9a-f]{20}", evidence_id("x"))


@pytest.mark.parametrize(
    ("pattern", "yes", "no"),
    [
        ("*.py", ["a.py"], ["src/a.py", "a.pyc"]),
        ("**/*.py", ["a.py", "src/a.py", "src/x/y.py"], ["a.pyc"]),
        ("src/**", ["src/a.py", "src/x/y"], ["srcx/a"]),
        ("src/*/index.ts", ["src/a/index.ts"], ["src/index.ts", "src/a/b/index.ts"]),
        ("file?.txt", ["file1.txt"], ["file12.txt", "file.txt"]),
        ("[ab].md", ["a.md", "b.md"], ["c.md"]),
        ("a.b", ["a.b"], ["axb"]),
    ],
)
def test_glob_patterns(pattern, yes, no):
    regex = glob_regex(pattern)
    assert all(regex.match(p) for p in yes) and not any(regex.match(p) for p in no)
