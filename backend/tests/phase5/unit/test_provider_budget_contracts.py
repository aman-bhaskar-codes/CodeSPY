"""Unit tests for provider disclosure policy and embedding budget contracts."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from app.contracts.provider_budget import (
    BudgetReservationRequestV1,
    DisclosurePolicyV1,
    EmbeddingRequestV1,
    EmbeddingResponseV1,
    PriceScheduleV1,
    ReservationRecordV1,
    ReservationState,
    calculate_charge_micros,
)
from pydantic import ValidationError


def test_integer_micro_calculation_exact_and_ceil() -> None:
    # 1,000 tokens at 20 micros/1k = exactly 20 micros ($0.000020)
    assert calculate_charge_micros(1000, 20) == 20
    # 500 tokens at 20 micros/1k = 10 micros
    assert calculate_charge_micros(500, 20) == 10
    # 1 token at 20 micros/1k = 1 micro (ceil rounding ensures no undercharging)
    assert calculate_charge_micros(1, 20) == 1
    # 0 tokens = 0 micros
    assert calculate_charge_micros(0, 20) == 0


def test_calculate_charge_negative_rejected() -> None:
    with pytest.raises(ValueError, match="tokens cannot be negative"):
        calculate_charge_micros(-1, 20)
    with pytest.raises(ValueError, match="rate cannot be negative"):
        calculate_charge_micros(100, -5)


def test_disclosure_policy_validation() -> None:
    policy = DisclosurePolicyV1(
        allow_external_disclosure=False,
        allowed_providers=("fake",),
        allowed_regions=("us-east-1",),
        policy_version="2026-10-22.1",
    )
    assert not policy.is_provider_permitted("aws_bedrock")
    assert policy.is_provider_permitted("fake")
    assert not policy.is_provider_permitted("google_genai")

    open_policy = DisclosurePolicyV1(
        allow_external_disclosure=True,
        allowed_providers=("aws_bedrock", "fake"),
        allowed_regions=("us-east-1", "ap-south-1"),
        policy_version="2026-10-22.1",
    )
    assert open_policy.is_provider_permitted("aws_bedrock")
    assert open_policy.is_region_permitted("ap-south-1")
    assert not open_policy.is_region_permitted("eu-central-1")


def test_price_schedule_validity_and_expiry() -> None:
    now = datetime.now(UTC)
    active = PriceScheduleV1(
        provider="aws_bedrock",
        model_id="amazon.titan-embed-text-v2:0",
        version="v1",
        input_micros_per_1k=20,
        effective_at=now - timedelta(days=1),
        expires_at=now + timedelta(days=30),
    )
    assert active.is_active(now)

    expired = PriceScheduleV1(
        provider="aws_bedrock",
        model_id="amazon.titan-embed-text-v2:0",
        version="v0",
        input_micros_per_1k=20,
        effective_at=now - timedelta(days=60),
        expires_at=now - timedelta(days=1),
    )
    assert not expired.is_active(now)


def test_reservation_request_contract() -> None:
    req = BudgetReservationRequestV1(
        org_id=uuid4(),
        repository_id=uuid4(),
        principal_id=uuid4(),
        provider="aws_bedrock",
        model_id="amazon.titan-embed-text-v2:0",
        estimated_tokens=5000,
        max_charge_micros=100,
        idempotency_key="idemp_12345",
    )
    assert req.estimated_tokens == 5000
    assert req.max_charge_micros == 100

    # Disallow zero or negative max_charge
    with pytest.raises(ValidationError):
        BudgetReservationRequestV1(
            org_id=uuid4(),
            repository_id=uuid4(),
            principal_id=uuid4(),
            provider="aws_bedrock",
            model_id="amazon.titan-embed-text-v2:0",
            estimated_tokens=5000,
            max_charge_micros=0,
            idempotency_key="idemp_12345",
        )


def test_reservation_record_state_transitions() -> None:
    now = datetime.now(UTC)
    res = ReservationRecordV1(
        reservation_id=uuid4(),
        org_id=uuid4(),
        repository_id=uuid4(),
        principal_id=uuid4(),
        provider="aws_bedrock",
        model_id="amazon.titan-embed-text-v2:0",
        estimated_tokens=1000,
        estimated_micros=20,
        settled_micros=None,
        state=ReservationState.RESERVED,
        idempotency_key="idemp_abc",
        created_at=now,
        updated_at=now,
    )
    assert res.state == ReservationState.RESERVED
    assert res.is_active_hold()

    settled = res.commit(actual_micros=18, timestamp=now + timedelta(seconds=2))
    assert settled.state == ReservationState.COMMITTED
    assert settled.settled_micros == 18
    assert not settled.is_active_hold()

    held = res.hold_uncertain(timestamp=now + timedelta(seconds=5))
    assert held.state == ReservationState.UNCERTAIN_HOLD
    assert held.is_active_hold()

    released = res.release(timestamp=now + timedelta(seconds=2))
    assert released.state == ReservationState.RELEASED
    assert not released.is_active_hold()


def test_embedding_request_and_response_contracts() -> None:
    req = EmbeddingRequestV1(
        text="def calculate_total(): return 42",
        provider="fake",
        model_id="fake-embed-v1",
        dimensions=1024,
        normalize=True,
    )
    assert req.dimensions == 1024

    resp = EmbeddingResponseV1(
        vector=(0.1,) * 1024,
        token_count=12,
        model_id="fake-embed-v1",
        provider="fake",
        provider_request_id="req_fake_123",
    )
    assert len(resp.vector) == 1024
    assert resp.token_count == 12
