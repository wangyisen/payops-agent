"""Store-level scenarios that the seed data cannot reach on its own."""

from datetime import timedelta
from typing import Any

from mock_pay.models import (
    ActionAudit,
    FailureCode,
    PartnerPayment,
    StatusChange,
    TransferStatus,
)
from mock_pay.store import Store

AUDIT = ActionAudit(requested_by="ops_test", reason="approved", idempotency_key="key-0001")


def test_a_late_payment_of_an_earlier_attempt_blocks_the_next_retry(
    tid: dict[str, Any],
) -> None:
    # S03 is retried; the new attempt never reaches the partner. Meanwhile the partner executes
    # the ORIGINAL submission late. A policy that only checked the newest reference would allow
    # a third attempt and pay twice.
    store = Store.from_seed()
    retried, _ = store.retry_transfer(tid["S03"], AUDIT)
    failed_at = store.now + timedelta(minutes=1)
    store.now += timedelta(hours=1)
    store.transfers[retried.id] = retried.model_copy(
        update={
            "status": TransferStatus.FAILED,
            "failure_code": FailureCode.PARTNER_UNAVAILABLE,
            "status_history": [
                *retried.status_history,
                StatusChange(status=TransferStatus.FAILED, at=failed_at, actor="system"),
            ],
        }
    )
    original = retried.submissions[0].provider_reference
    del store.partner_payments[(retried.provider, retried.provider_reference or "")]
    store.partner_payments[(retried.provider, original)] = PartnerPayment(
        provider=retried.provider,
        provider_reference=original,
        status="paid",
        amount=retried.target_amount,
        currency=retried.target_currency,
        updated_at=failed_at,
    )

    decision = store.retry_decision(store.transfers[retried.id])

    assert not decision.eligible
    assert decision.reason_code == "partner_has_no_payment"
    assert original in next(c.detail for c in decision.checks if not c.passed)
