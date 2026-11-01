"""Payment rules: the transfer state machine and the retry policy.

Pure functions over models, no I/O: the API, the seed generator and the tests all use them, so
there is exactly one definition of "a legal transition" and "a safe retry".
"""

from collections.abc import Sequence
from dataclasses import dataclass

from mock_pay.models import (
    FailureCode,
    Model,
    PartnerPayment,
    StatementLine,
    Transfer,
    TransferStatus,
)

S = TransferStatus
ALLOWED_TRANSITIONS: dict[TransferStatus, frozenset[TransferStatus]] = {
    S.CREATED: frozenset({S.PENDING_FUNDS, S.ON_HOLD, S.PROCESSING}),
    S.PENDING_FUNDS: frozenset({S.PROCESSING, S.FAILED, S.CANCELLED}),
    S.ON_HOLD: frozenset({S.PROCESSING, S.REJECTED, S.CANCELLED}),
    S.PROCESSING: frozenset({S.COMPLETED, S.FAILED}),
    S.COMPLETED: frozenset({S.RETURNED}),
    S.FAILED: frozenset({S.PROCESSING}),  # only via an approved, policy-checked retry
    S.RETURNED: frozenset(),
    S.CANCELLED: frozenset(),
    S.REJECTED: frozenset(),
}

RETRYABLE_FAILURES = frozenset({FailureCode.PARTNER_TIMEOUT, FailureCode.PARTNER_UNAVAILABLE})
MAX_ATTEMPTS = 3


class InvalidTransitionError(ValueError):
    pass


def assert_transition(current: TransferStatus, new: TransferStatus) -> None:
    if new not in ALLOWED_TRANSITIONS[current]:
        raise InvalidTransitionError(f"{current} -> {new} is not a legal transition")


@dataclass(frozen=True)
class PartnerFound:
    payment: PartnerPayment


@dataclass(frozen=True)
class PartnerNotFound:
    pass


@dataclass(frozen=True)
class PartnerUnavailable:
    reason: str


# None means there was nothing to look up (the transfer has no provider reference).
type PartnerLookup = PartnerFound | PartnerNotFound | PartnerUnavailable


class RetryCheck(Model):
    name: str
    passed: bool
    detail: str


class RetryDecision(Model):
    transfer_id: str
    eligible: bool
    reason_code: str | None  # name of the first failed check
    checks: list[RetryCheck]


def evaluate_retry(
    transfer: Transfer,
    partner: PartnerLookup | None,
    statement_lines: Sequence[StatementLine],
) -> RetryDecision:
    """Decide whether retrying a payout is safe. Every check must pass.

    The checks run in a fixed order and all of them are reported, so a human (or the agent)
    can see exactly why a retry is blocked, not just that it is.
    """
    debits = [
        line
        for line in statement_lines
        if line.direction == "debit"
        and (
            line.matched_transfer_id == transfer.id
            or (
                transfer.provider_reference is not None
                and line.provider_reference == transfer.provider_reference
            )
        )
    ]
    checks = [
        RetryCheck(
            name="transfer_is_failed",
            passed=transfer.status == S.FAILED,
            detail=f"status is {transfer.status}",
        ),
        RetryCheck(
            name="failure_is_retryable",
            passed=transfer.failure_code in RETRYABLE_FAILURES,
            detail=f"failure_code is {transfer.failure_code}; retryable: "
            + ", ".join(sorted(RETRYABLE_FAILURES)),
        ),
        RetryCheck(
            name="attempts_below_limit",
            passed=transfer.attempt < MAX_ATTEMPTS,
            detail=f"attempt {transfer.attempt} of {MAX_ATTEMPTS}",
        ),
        _partner_known(partner),
        _partner_has_no_payment(partner),
        RetryCheck(
            name="no_statement_debit",
            passed=not debits,
            detail=(
                "statement debit(s) found: " + ", ".join(line.id for line in debits)
                if debits
                else "no debit on any partner statement"
            ),
        ),
    ]
    failed = next((check.name for check in checks if not check.passed), None)
    return RetryDecision(
        transfer_id=transfer.id, eligible=failed is None, reason_code=failed, checks=checks
    )


def _partner_known(partner: PartnerLookup | None) -> RetryCheck:
    # Fail closed: "could not check" is never treated as "not paid".
    match partner:
        case PartnerUnavailable(reason=reason):
            return RetryCheck(name="partner_status_known", passed=False, detail=reason)
        case None:
            return RetryCheck(
                name="partner_status_known",
                passed=False,
                detail="no provider reference to look up",
            )
        case _:
            return RetryCheck(
                name="partner_status_known", passed=True, detail="partner status API answered"
            )


def _partner_has_no_payment(partner: PartnerLookup | None) -> RetryCheck:
    match partner:
        case PartnerNotFound():
            return RetryCheck(
                name="partner_has_no_payment",
                passed=True,
                detail="partner has no record of this payment",
            )
        case PartnerFound(payment=payment):
            return RetryCheck(
                name="partner_has_no_payment",
                passed=False,
                detail=f"partner reports the payment as {payment.status}",
            )
        case _:
            return RetryCheck(
                name="partner_has_no_payment",
                passed=False,
                detail="unknown: partner status could not be checked",
            )
