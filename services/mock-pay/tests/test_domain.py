"""Retry policy and transfer state machine.

These rules are what stands between "the agent thinks a retry is fine" and a double payout,
so every rule has a test that shows it blocking the dangerous case.
"""

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from mock_pay.domain import (
    InvalidTransitionError,
    PartnerFound,
    PartnerLookup,
    PartnerNotFound,
    PartnerUnavailable,
    RetryDecision,
    assert_transition,
    evaluate_retry,
)
from mock_pay.models import (
    FailureCode,
    PartnerPayment,
    Quote,
    StatementLine,
    Transfer,
    TransferStatus,
)

NOW = datetime(2026, 3, 11, 9, 30, tzinfo=UTC)
REF_1 = "BA-20260311-000001"
REF_2 = "BA-20260311-000001-R2"


def make_transfer(**overrides: Any) -> Transfer:
    """A payout that timed out four hours ago after one submission to the partner."""
    failed_at = overrides.pop("failed_at", NOW - timedelta(hours=4))
    fields: dict[str, Any] = {
        "id": "tr_test00000001",
        "merchant_id": "mer_test0000001",
        "recipient_id": "rcp_test0000001",
        "quote_id": "quo_test0000001",
        "client_reference": "INV-1",
        "request_idempotency_key": None,
        "source_amount": "1000.00",
        "source_currency": "USD",
        "target_amount": "911.34",
        "target_currency": "EUR",
        "rail": "sepa",
        "provider": "bank_alpha",
        "provider_reference": REF_1,
        "submissions": [{"attempt": 1, "provider_reference": REF_1, "submitted_at": failed_at}],
        "status": "failed",
        "failure_code": "partner_timeout",
        "attempt": 1,
        "created_at": failed_at - timedelta(minutes=2),
        "updated_at": failed_at,
        "status_history": [
            {"status": "created", "at": failed_at - timedelta(minutes=2), "actor": "system"},
            {"status": "processing", "at": failed_at - timedelta(minutes=1), "actor": "system"},
            {"status": "failed", "at": failed_at, "actor": "system"},
        ],
    }
    fields.update(overrides)
    return Transfer.model_validate(fields)


def retried_transfer() -> Transfer:
    """The same payout after one approved retry that also failed: two references exist."""
    return make_transfer(
        attempt=2,
        provider_reference=REF_2,
        submissions=[
            {"attempt": 1, "provider_reference": REF_1, "submitted_at": NOW - timedelta(hours=6)},
            {"attempt": 2, "provider_reference": REF_2, "submitted_at": NOW - timedelta(hours=4)},
        ],
    )


def make_quote(expires_at: datetime = NOW + timedelta(hours=20)) -> Quote:
    return Quote.model_validate(
        {
            "id": "quo_test0000001",
            "merchant_id": "mer_test0000001",
            "source_currency": "USD",
            "target_currency": "EUR",
            "source_amount": "1000.00",
            "fee": "4.00",
            "rate": "0.9150",
            "target_amount": "911.34",
            "created_at": expires_at - timedelta(hours=24),
            "expires_at": expires_at,
        }
    )


def partner_payment(status: str, reference: str = REF_1) -> PartnerPayment:
    return PartnerPayment.model_validate(
        {
            "provider": "bank_alpha",
            "provider_reference": reference,
            "status": status,
            "amount": "911.34",
            "currency": "EUR",
            "updated_at": NOW - timedelta(hours=3),
        }
    )


def debit_line(reference: str | None = REF_1, matched_to: str | None = None) -> StatementLine:
    return StatementLine.model_validate(
        {
            "id": "stl_test0000001",
            "provider": "bank_alpha",
            "statement_date": date(2026, 3, 10),
            "provider_reference": reference,
            "direction": "debit",
            "amount": "911.34",
            "currency": "EUR",
            "description": "SEPA Credit Transfer",
            "matched_transfer_id": matched_to,
            "match_status": "matched" if matched_to else "unmatched",
        }
    )


def decide(
    transfer: Transfer | None = None,
    *,
    partner: Mapping[str, PartnerLookup] | None = None,
    lines: Sequence[StatementLine] = (),
    now: datetime = NOW,
    quote: Quote | None = None,
    available: str = "50000.00",
) -> RetryDecision:
    transfer = transfer or make_transfer()
    if partner is None:  # default: no submitted reference is known to the partner
        partner = {s.provider_reference: PartnerNotFound() for s in transfer.submissions}
    return evaluate_retry(
        transfer,
        now=now,
        quote=quote or make_quote(),
        available=Decimal(available),
        partner=partner,
        statement_lines=lines,
    )


class TestRetryPolicy:
    def test_allowed_when_the_partner_never_received_the_payment(self) -> None:
        decision = decide()

        assert decision.eligible
        assert decision.reason_code is None
        assert all(check.passed for check in decision.checks)

    def test_blocked_when_the_partner_already_paid(self) -> None:
        # Our side timed out, but the money left: retrying would pay twice.
        decision = decide(partner={REF_1: PartnerFound(partner_payment("paid"))})

        assert not decision.eligible
        assert decision.reason_code == "partner_has_no_payment"

    @pytest.mark.parametrize("status", ["accepted", "rejected", "returned"])
    def test_blocked_when_the_partner_has_any_record(self, status: str) -> None:
        decision = decide(partner={REF_1: PartnerFound(partner_payment(status))})

        assert not decision.eligible
        assert decision.reason_code == "partner_has_no_payment"

    def test_blocked_when_the_partner_status_is_unknown(self) -> None:
        # Fail closed: "could not check" must never be treated as "not paid".
        decision = decide(partner={REF_1: PartnerUnavailable("status API down")})

        assert not decision.eligible
        assert decision.reason_code == "partner_status_known"

    def test_blocked_when_nothing_was_ever_submitted(self) -> None:
        decision = decide(make_transfer(provider_reference=None, submissions=[]))

        assert not decision.eligible
        assert decision.reason_code == "partner_status_known"

    def test_blocked_when_an_earlier_attempt_was_paid_late(self) -> None:
        # The first submission executed after we gave up on it; the newest reference is unknown
        # to the partner. Checking only the newest reference would allow a double payout.
        decision = decide(
            retried_transfer(),
            partner={REF_1: PartnerFound(partner_payment("paid")), REF_2: PartnerNotFound()},
        )

        assert not decision.eligible
        assert decision.reason_code == "partner_has_no_payment"

    def test_blocked_when_any_attempt_cannot_be_checked(self) -> None:
        decision = decide(
            retried_transfer(),
            partner={REF_1: PartnerUnavailable("status API down"), REF_2: PartnerNotFound()},
        )

        assert not decision.eligible
        assert decision.reason_code == "partner_status_known"

    def test_blocked_when_a_lookup_is_missing_for_a_submitted_reference(self) -> None:
        decision = decide(retried_transfer(), partner={REF_2: PartnerNotFound()})

        assert not decision.eligible
        assert decision.reason_code == "partner_status_known"

    @pytest.mark.parametrize(
        "line",
        [
            debit_line(reference=REF_1),  # carries our reference
            debit_line(reference=None, matched_to="tr_test00000001"),  # matched by reconciliation
        ],
        ids=["by_reference", "by_match"],
    )
    def test_blocked_when_the_statement_shows_a_debit(self, line: StatementLine) -> None:
        # Partner API and statement disagree: trust the money trail and refuse.
        decision = decide(lines=[line])

        assert not decision.eligible
        assert decision.reason_code == "no_statement_debit"

    def test_blocked_when_a_debit_carries_an_earlier_reference(self) -> None:
        decision = decide(retried_transfer(), lines=[debit_line(reference=REF_1)])

        assert decision.reason_code == "no_statement_debit"

    def test_a_debit_for_another_payment_does_not_block(self) -> None:
        decision = decide(lines=[debit_line(reference="BA-20260311-000999")])

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
        decision = decide(make_transfer(failure_code=code))

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
    def test_blocked_unless_the_transfer_failed(self, status: TransferStatus) -> None:
        decision = decide(make_transfer(status=status, failure_code=None))

        assert not decision.eligible
        assert decision.reason_code == "transfer_is_failed"

    def test_a_second_attempt_is_still_allowed(self) -> None:
        assert decide(retried_transfer()).eligible

    def test_blocked_at_the_attempt_limit(self) -> None:
        decision = decide(make_transfer(attempt=3))

        assert not decision.eligible
        assert decision.reason_code == "attempts_below_limit"

    def test_blocked_when_the_quote_has_expired(self) -> None:
        decision = decide(quote=make_quote(expires_at=NOW - timedelta(minutes=1)))

        assert not decision.eligible
        assert decision.reason_code == "quote_still_valid"

    def test_blocked_without_enough_available_balance(self) -> None:
        # A retry funds the payout again, so the merchant must still have the money.
        decision = decide(available="999.99")

        assert not decision.eligible
        assert decision.reason_code == "funds_available"

    def test_a_timeout_must_settle_before_a_retry(self) -> None:
        # Right after a timeout the partner may still be executing the payment, so
        # "not found" is not yet trustworthy.
        recent = make_transfer(failed_at=NOW - timedelta(minutes=10))

        decision = decide(recent)

        assert not decision.eligible
        assert decision.reason_code == "timeout_settled"

    def test_a_timeout_settles_after_the_waiting_period(self) -> None:
        assert decide(make_transfer(failed_at=NOW - timedelta(minutes=30))).eligible

    def test_a_rejected_submission_needs_no_waiting_period(self) -> None:
        # partner_unavailable means the partner refused the request outright (HTTP 503).
        recent = make_transfer(
            failure_code=FailureCode.PARTNER_UNAVAILABLE, failed_at=NOW - timedelta(minutes=1)
        )

        assert decide(recent).eligible

    def test_double_payout_risks_are_reported_before_commercial_problems(self) -> None:
        decision = decide(
            partner={REF_1: PartnerFound(partner_payment("paid"))},
            quote=make_quote(expires_at=NOW - timedelta(hours=1)),
        )

        assert decision.reason_code == "partner_has_no_payment"

    def test_reports_every_check_in_order(self) -> None:
        decision = decide(partner={REF_1: PartnerFound(partner_payment("paid"))})

        assert [check.name for check in decision.checks] == [
            "transfer_is_failed",
            "failure_is_retryable",
            "attempts_below_limit",
            "timeout_settled",
            "partner_status_known",
            "partner_has_no_payment",
            "no_statement_debit",
            "quote_still_valid",
            "funds_available",
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
