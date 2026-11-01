"""In-memory state of the mock API, loaded from the committed seed files.

Each app instance gets its own Store, so tests never share state. Actions that change state run
under one lock and re-check their rules inside it (no gap between "check" and "act").
"""

import functools
import hashlib
import json
import threading
from importlib import resources
from typing import Any, Literal

from mock_pay.domain import (
    PartnerFound,
    PartnerLookup,
    PartnerNotFound,
    PartnerUnavailable,
    RetryDecision,
    assert_transition,
    evaluate_retry,
)
from mock_pay.idempotency import IdempotencyStore
from mock_pay.models import (
    Dataset,
    DeliveryAttempt,
    PartnerPayment,
    Provider,
    StatementLine,
    StatusChange,
    Transfer,
    TransferStatus,
    WebhookEvent,
)

COLLECTIONS = tuple(name for name in Dataset.model_fields if name != "now")


class RetryNotAllowedError(Exception):
    def __init__(self, decision: RetryDecision) -> None:
        super().__init__(decision.reason_code)
        self.decision = decision


@functools.cache
def _seed_payload() -> dict[str, Any]:
    data = resources.files("mock_pay") / "data"
    payload: dict[str, Any] = {
        "now": json.loads((data / "meta.json").read_text(encoding="utf-8"))["now"]
    }
    for name in COLLECTIONS:
        payload[name] = json.loads((data / f"{name}.json").read_text(encoding="utf-8"))
    return payload


def load_seed_dataset() -> Dataset:
    # Validation builds fresh model objects, so the cached payload is never mutated.
    return Dataset.model_validate(_seed_payload())


def derived_id(prefix: str, key: str) -> str:
    return f"{prefix}_{hashlib.blake2b(key.encode(), digest_size=6).hexdigest()}"


class Store:
    def __init__(self, dataset: Dataset) -> None:
        self.now = dataset.now
        self.rails = dataset.rails
        self.partner_statuses = {p.provider: p for p in dataset.partner_statuses}
        self.merchants = {m.id: m for m in dataset.merchants}
        self.balances = dataset.balances
        self.virtual_accounts = dataset.virtual_accounts
        self.deposits = dataset.deposits
        self.recipients = {r.id: r for r in dataset.recipients}
        self.quotes = {q.id: q for q in dataset.quotes}
        self.transfers = {t.id: t for t in dataset.transfers}
        self.events = {e.id: e for e in dataset.webhook_events}
        self.partner_payments = {
            (p.provider, p.provider_reference): p for p in dataset.partner_payments
        }
        self.statement_lines = dataset.statement_lines
        self.idempotency = IdempotencyStore()
        self._lock = threading.Lock()

    @classmethod
    def from_seed(cls) -> "Store":
        return cls(load_seed_dataset())

    # --- queries ----------------------------------------------------------------------------

    def list_transfers(
        self,
        *,
        merchant_id: str | None = None,
        status: TransferStatus | None = None,
        client_reference: str | None = None,
        limit: int = 20,
        starting_after: str | None = None,
    ) -> tuple[list[Transfer], bool]:
        items = sorted(self.transfers.values(), key=lambda t: (t.created_at, t.id), reverse=True)
        items = [
            t
            for t in items
            if (merchant_id is None or t.merchant_id == merchant_id)
            and (status is None or t.status == status)
            and (client_reference is None or t.client_reference == client_reference)
        ]
        if starting_after is not None:
            ids = [t.id for t in items]
            items = items[ids.index(starting_after) + 1 :] if starting_after in ids else []
        return items[:limit], len(items) > limit

    def events_for(self, transfer_id: str) -> list[WebhookEvent]:
        return sorted(
            (e for e in self.events.values() if e.transfer_id == transfer_id),
            key=lambda e: (e.created_at, e.id),
        )

    def partner_lookup(self, provider: Provider, reference: str) -> PartnerLookup:
        status = self.partner_statuses[provider]
        if status.status == "outage":
            since = f" since {status.since:%Y-%m-%d %H:%M} UTC" if status.since else ""
            return PartnerUnavailable(f"{provider} status API unavailable{since}")
        payment = self.partner_payments.get((provider, reference))
        return PartnerFound(payment) if payment else PartnerNotFound()

    def statement_lines_for(self, transfer: Transfer) -> list[StatementLine]:
        return [
            line
            for line in self.statement_lines
            if line.matched_transfer_id == transfer.id
            or (
                transfer.provider_reference is not None
                and line.provider_reference == transfer.provider_reference
            )
        ]

    def retry_decision(self, transfer: Transfer) -> RetryDecision:
        partner = (
            self.partner_lookup(transfer.provider, transfer.provider_reference)
            if transfer.provider_reference
            else None
        )
        return evaluate_retry(transfer, partner, self.statement_lines_for(transfer))

    # --- actions ----------------------------------------------------------------------------

    def retry_transfer(
        self, transfer_id: str, requested_by: str, reason: str
    ) -> tuple[Transfer, RetryDecision]:
        with self._lock:
            transfer = self.transfers[transfer_id]
            decision = self.retry_decision(transfer)  # re-checked here, not trusted from caller
            if not decision.eligible:
                raise RetryNotAllowedError(decision)
            assert_transition(transfer.status, TransferStatus.PROCESSING)
            attempt = transfer.attempt + 1
            base_reference = (transfer.provider_reference or transfer.id).split("-R")[0]
            reference = f"{base_reference}-R{attempt}"
            updated = transfer.model_copy(
                update={
                    "status": TransferStatus.PROCESSING,
                    "failure_code": None,
                    "attempt": attempt,
                    "provider_reference": reference,
                    "updated_at": self.now,
                    "status_history": [
                        *transfer.status_history,
                        StatusChange(
                            status=TransferStatus.PROCESSING,
                            at=self.now,
                            actor="ops",
                            note=f"Retry (attempt {attempt}) requested by {requested_by}: {reason}",
                        ),
                    ],
                }
            )
            self.transfers[transfer_id] = updated
            self.partner_payments[(updated.provider, reference)] = PartnerPayment(
                provider=updated.provider,
                provider_reference=reference,
                status="accepted",
                amount=updated.target_amount,
                currency=updated.target_currency,
                updated_at=self.now,
            )
            self._emit_event(updated, f"retry:{attempt}")
            return updated, decision

    def resend_event(self, event_id: str) -> tuple[WebhookEvent, Literal["delivered", "failed"]]:
        with self._lock:
            event = self.events[event_id]
            attempt = self._deliver(event.merchant_id, len(event.attempts) + 1, "manual_resend")
            delivered = attempt.response_status == 200
            updated = event.model_copy(
                update={
                    "attempts": [*event.attempts, attempt],
                    "delivery_status": "delivered" if delivered else event.delivery_status,
                    "next_retry_at": None if delivered else event.next_retry_at,
                }
            )
            self.events[event_id] = updated
            return updated, "delivered" if delivered else "failed"

    def _emit_event(self, transfer: Transfer, key: str) -> None:
        attempt = self._deliver(transfer.merchant_id, 1, "automatic")
        delivered = attempt.response_status == 200
        event = WebhookEvent(
            id=derived_id("evt", f"{transfer.id}:{key}"),
            merchant_id=transfer.merchant_id,
            transfer_id=transfer.id,
            type=f"transfer.{transfer.status}",
            created_at=self.now,
            delivery_status="delivered" if delivered else "pending",
            attempts=[attempt],
        )
        self.events[event.id] = event

    def _deliver(
        self, merchant_id: str, number: int, trigger: Literal["automatic", "manual_resend"]
    ) -> DeliveryAttempt:
        """Simulate one delivery: healthy endpoints answer 200, failing ones fail the same way
        they have been failing (copied from their latest failed attempt)."""
        merchant = self.merchants[merchant_id]
        if merchant.webhook_endpoint_status == "healthy":
            return DeliveryAttempt(
                attempt=number,
                trigger=trigger,
                at=self.now,
                url=merchant.webhook_url,
                response_status=200,
                duration_ms=120,
                response_body_excerpt='{"received":true}',
            )
        failures = [
            a
            for e in self.events.values()
            if e.merchant_id == merchant_id
            for a in e.attempts
            if a.response_status != 200
        ]
        template = max(failures, key=lambda a: a.at) if failures else None
        return DeliveryAttempt(
            attempt=number,
            trigger=trigger,
            at=self.now,
            url=merchant.webhook_url,
            response_status=template.response_status if template else 503,
            error=template.error if template else None,
            duration_ms=template.duration_ms if template else 50,
            response_body_excerpt=template.response_body_excerpt if template else None,
        )
