"""Payment rules: the transfer state machine and the retry policy.

Pure functions over models, no I/O: the API, the seed generator and the tests all use them, so
there is exactly one definition of "a legal transition" and "a safe retry".
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from mock_pay.models import (
    FailureCode,
    Model,
    PartnerPayment,
    Quote,
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
# After a timeout the partner may still execute the payment; wait before trusting "not found".
TIMEOUT_SETTLE_PERIOD = timedelta(minutes=30)


class InvalidTransitionError(ValueError):
    pass


def assert_transition(current: TransferStatus, new: TransferStatus) -> None:
    if new not in ALLOWED_TRANSITIONS[current]:
        raise InvalidTransitionError(f"{current} -> {new} is not a legal transition")


def related_statement_lines(
    transfer: Transfer, lines: Iterable[StatementLine]
) -> list[StatementLine]:
    """Lines matched to the transfer, or carrying any reference it was ever submitted with."""
    references = {s.provider_reference for s in transfer.submissions}
    return [
        line
        for line in lines
        if line.matched_transfer_id == transfer.id or line.provider_reference in references
    ]


@dataclass(frozen=True)
class PartnerFound:
    payment: PartnerPayment


@dataclass(frozen=True)
class PartnerNotFound:
    pass


@dataclass(frozen=True)
class PartnerUnavailable:
    reason: str


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
    *,
    now: datetime,
    quote: Quote,
    available: Decimal,
    partner: Mapping[str, PartnerLookup],
    statement_lines: Sequence[StatementLine],
) -> RetryDecision:
    """Decide whether retrying a payout is safe. Every check must pass.

    ``partner`` must hold a lookup for every reference in ``transfer.submissions``: a partner
    can execute an earlier attempt late, so checking only the newest reference is not enough.
    The checks run in a fixed order and all are reported, so a human (or the agent) can see
    exactly why a retry is blocked, not just that it is. Double-payout checks come before the
    commercial ones (quote, funds), so ``reason_code`` names the most dangerous problem.
    """
    debits = [
        line
        for line in related_statement_lines(transfer, statement_lines)
        if line.direction == "debit"
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
        _timeout_settled(transfer, now),
        _partner_known(transfer, partner),
        _partner_has_no_payment(transfer, partner),
        RetryCheck(
            name="no_statement_debit",
            passed=not debits,
            detail=(
                "statement debit(s) found: " + ", ".join(line.id for line in debits)
                if debits
                else "no debit on any partner statement"
            ),
        ),
        RetryCheck(
            name="quote_still_valid",
            passed=quote.expires_at > now,
            detail=f"quote {quote.id} expires at {quote.expires_at.isoformat()}",
        ),
        RetryCheck(
            name="funds_available",
            passed=available >= transfer.source_amount,
            detail=f"available {available} {transfer.source_currency}, "
            f"needs {transfer.source_amount}",
        ),
    ]
    failed = next((check.name for check in checks if not check.passed), None)
    return RetryDecision(
        transfer_id=transfer.id, eligible=failed is None, reason_code=failed, checks=checks
    )


def _timeout_settled(transfer: Transfer, now: datetime) -> RetryCheck:
    if transfer.failure_code != FailureCode.PARTNER_TIMEOUT:
        return RetryCheck(name="timeout_settled", passed=True, detail="not a timeout")
    failed_at = next(
        (c.at for c in reversed(transfer.status_history) if c.status == S.FAILED),
        transfer.updated_at,
    )
    waited = now - failed_at
    return RetryCheck(
        name="timeout_settled",
        passed=waited >= TIMEOUT_SETTLE_PERIOD,
        detail=f"timed out {int(waited.total_seconds() // 60)} min ago; "
        f"wait at least {int(TIMEOUT_SETTLE_PERIOD.total_seconds() // 60)} min",
    )


def _partner_known(transfer: Transfer, partner: Mapping[str, PartnerLookup]) -> RetryCheck:
    # Fail closed: "could not check" is never treated as "not paid".
    if not transfer.submissions:
        return RetryCheck(
            name="partner_status_known", passed=False, detail="never submitted to a partner"
        )
    problems = []
    for submission in transfer.submissions:
        lookup = partner.get(submission.provider_reference)
        if lookup is None:
            problems.append(f"{submission.provider_reference}: not checked")
        elif isinstance(lookup, PartnerUnavailable):
            problems.append(f"{submission.provider_reference}: {lookup.reason}")
    return RetryCheck(
        name="partner_status_known",
        passed=not problems,
        detail="; ".join(problems) or f"partner answered for {len(transfer.submissions)} ref(s)",
    )


def _partner_has_no_payment(transfer: Transfer, partner: Mapping[str, PartnerLookup]) -> RetryCheck:
    found = []
    unknown = False
    for submission in transfer.submissions:
        match partner.get(submission.provider_reference):
            case PartnerFound(payment=payment):
                found.append(f"{submission.provider_reference} is {payment.status}")
            case PartnerNotFound():
                pass
            case _:
                unknown = True
    if found:
        detail = "partner has: " + ", ".join(found)
    elif unknown or not transfer.submissions:
        detail = "unknown: partner status could not be checked"
    else:
        detail = "partner has no record of any attempt"
    return RetryCheck(
        name="partner_has_no_payment",
        passed=bool(transfer.submissions) and not found and not unknown,
        detail=detail,
    )
