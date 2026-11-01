"""Retry policy and transfer state machine.

These rules are what stands between "the agent thinks a retry is fine" and a double payout,
so every rule has a test that shows it blocking the dangerous case.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from mock_pay.domain import (
    InvalidTransitionError,
    PartnerFound,
    PartnerNotFound,
    PartnerUnavailable,
    assert_transition,
    evaluate_retry,
)
from mock_pay.models import (
    FailureCode,
    PartnerPayment,
    Provider,
    RailCode,
    StatementLine,
    Transfer,
    TransferStatus,
)

NOW = datetime(2026, 3, 11, 9, 30, tzinfo=UTC)


def make_transfer(**overrides: Any) -> Transfer:
    fields: dict[str, Any] = {
        "id": "tr_test00000001",
        "merchant_id": "mer_test0000001",
        "recipient_id": "rcp_test0000001",
        "quote_id": "quo_test0000001",
        "client_reference": "INV-1",
        "source_amount": Decimal("1000.00"),
        "source_currency": "USD",
        "target_amount": Decimal("911.34"),
        "target_currency": "EUR",
        "rail": RailCode.SEPA,
        "provider": Provider.BANK_ALPHA,
        "provider_reference": "BA-20260311-000001",
        "status": TransferStatus.FAILED,
        "failure_code": FailureCode.PARTNER_TIMEOUT,
        "attempt": 1,
        "created_at": NOW - timedelta(hours=5),
        "updated_at": NOW - timedelta(hours=4),
        "status_history": [],
    }
    fields.update(overrides)
    return Transfer.model_validate(fields)


def partner_payment(status: str) -> PartnerPayment:
    return PartnerPayment.model_validate(
        {
            "provider": "bank_alpha",
            "provider_reference": "BA-20260311-000001",
            "status": status,
            "amount": "911.34",
            "currency": "EUR",
            "updated_at": NOW - timedelta(hours=4),
        }
    )


def debit_line(reference: str = "BA-20260311-000001") -> StatementLine:
    return StatementLine.model_validate(
        {
            "id": "stl_test0000001",
            "provider": "bank_alpha",
            "statement_date": date(2026, 3, 10),
            "provider_reference": reference,
            "direction": "debit",
            "amount": "911.34",
            "currency": "EUR",
            "description": "SEPA CT",
            "matched_transfer_id": None,
            "match_status": "unmatched",
        }
    )


class TestRetryPolicy:
    def test_allowed_when_partner_never_received_the_payment(self) -> None:
        decision = evaluate_retry(make_transfer(), PartnerNotFound(), [])

        assert decision.eligible
        assert decision.reason_code is None
        assert all(check.passed for check in decision.checks)

    def test_blocked_when_partner_already_paid(self) -> None:
        # Our side timed out, but the money left: retrying would pay twice.
        decision = evaluate_retry(make_transfer(), PartnerFound(partner_payment("paid")), [])

        assert not decision.eligible
        assert decision.reason_code == "partner_has_no_payment"

    @pytest.mark.parametrize("status", ["accepted", "rejected", "returned"])
    def test_blocked_when_partner_has_any_record(self, status: str) -> None:
        decision = evaluate_retry(make_transfer(), PartnerFound(partner_payment(status)), [])

        assert not decision.eligible
        assert decision.reason_code == "partner_has_no_payment"

    def test_blocked_when_partner_status_is_unknown(self) -> None:
        # Fail closed: "could not check" must never be treated as "not paid".
        decision = evaluate_retry(make_transfer(), PartnerUnavailable("status API down"), [])

        assert not decision.eligible
        assert decision.reason_code == "partner_status_known"

    def test_blocked_when_there_is_no_reference_to_verify(self) -> None:
        transfer = make_transfer(provider_reference=None)

        decision = evaluate_retry(transfer, None, [])

        assert not decision.eligible
        assert decision.reason_code == "partner_status_known"

    def test_blocked_when_statement_shows_a_debit(self) -> None:
        # Partner API and statement disagree: trust the money trail and refuse.
        decision = evaluate_retry(make_transfer(), PartnerNotFound(), [debit_line()])

        assert not decision.eligible
        assert decision.reason_code == "no_statement_debit"

    def test_debit_for_another_reference_does_not_block(self) -> None:
        decision = evaluate_retry(
            make_transfer(), PartnerNotFound(), [debit_line(reference="BA-20260311-000999")]
        )

        assert decision.eligible

    @pytest.mark.parametrize(
        "code",
        [
            FailureCode.INVALID_ACCOUNT_NUMBER,
            FailureCode.NAME_MISMATCH,
            FailureCode.QUOTE_EXPIRED,
        ],
    )
    def test_blocked_for_non_retryable_failures(self, code: FailureCode) -> None:
        decision = evaluate_retry(make_transfer(failure_code=code), PartnerNotFound(), [])

        assert not decision.eligible
        assert decision.reason_code == "failure_is_retryable"

    @pytest.mark.parametrize(
        "status",
        [
            TransferStatus.PENDING_FUNDS,
            TransferStatus.ON_HOLD,
            TransferStatus.PROCESSING,
            TransferStatus.COMPLETED,
            TransferStatus.RETURNED,
        ],
    )
    def test_blocked_unless_transfer_failed(self, status: TransferStatus) -> None:
        decision = evaluate_retry(
            make_transfer(status=status, failure_code=None), PartnerNotFound(), []
        )

        assert not decision.eligible
        assert decision.reason_code == "transfer_is_failed"

    def test_blocked_at_attempt_limit(self) -> None:
        decision = evaluate_retry(make_transfer(attempt=3), PartnerNotFound(), [])

        assert not decision.eligible
        assert decision.reason_code == "attempts_below_limit"

    def test_reports_every_check_for_explainability(self) -> None:
        decision = evaluate_retry(make_transfer(), PartnerFound(partner_payment("paid")), [])

        assert [check.name for check in decision.checks] == [
            "transfer_is_failed",
            "failure_is_retryable",
            "attempts_below_limit",
            "partner_status_known",
            "partner_has_no_payment",
            "no_statement_debit",
        ]
        assert all(check.detail for check in decision.checks)


class TestStateMachine:
    @pytest.mark.parametrize(
        ("current", "new"),
        [
            (TransferStatus.CREATED, TransferStatus.PENDING_FUNDS),
            (TransferStatus.PENDING_FUNDS, TransferStatus.FAILED),
            (TransferStatus.ON_HOLD, TransferStatus.REJECTED),
            (TransferStatus.PROCESSING, TransferStatus.COMPLETED),
            (TransferStatus.COMPLETED, TransferStatus.RETURNED),
            (TransferStatus.FAILED, TransferStatus.PROCESSING),
        ],
    )
    def test_allowed_transitions(self, current: TransferStatus, new: TransferStatus) -> None:
        assert_transition(current, new)

    @pytest.mark.parametrize(
        ("current", "new"),
        [
            (TransferStatus.COMPLETED, TransferStatus.PROCESSING),
            (TransferStatus.RETURNED, TransferStatus.PROCESSING),
            (TransferStatus.CANCELLED, TransferStatus.PROCESSING),
            (TransferStatus.REJECTED, TransferStatus.PROCESSING),
            (TransferStatus.PENDING_FUNDS, TransferStatus.COMPLETED),
            (TransferStatus.PROCESSING, TransferStatus.PROCESSING),
        ],
    )
    def test_forbidden_transitions(self, current: TransferStatus, new: TransferStatus) -> None:
        with pytest.raises(InvalidTransitionError):
            assert_transition(current, new)
