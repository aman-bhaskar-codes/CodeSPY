"""Scan wakeups have an explicit version and safe private failure classification."""

import pytest
from app.scanners.process import NativeProcessError


@pytest.mark.parametrize(
    ("private", "public", "transient"),
    [
        ("scanner_runtime_unavailable", "scanner_unavailable", False),
        ("scanner_output_invalid", "scanner_output_invalid", False),
        ("scanner_timeout", "scanner_timeout", False),
        ("scanner_output_limit", "scan_capacity_exceeded", False),
        ("scanner_cleanup_pending", "scanner_process_failed", True),
        ("untrusted text", "scanner_output_invalid", False),
    ],
)
def test_native_failure_mapping_is_bounded(private, public, transient):
    from app.workers import consumer

    assert hasattr(consumer, "scan_failure"), "explicit scan error mapping missing"
    assert consumer.scan_failure(NativeProcessError(private)) == (public, transient)
