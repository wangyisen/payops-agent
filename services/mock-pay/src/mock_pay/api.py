"""HTTP routes. Thin: validate input, call the store, map domain outcomes to HTTP."""

import re
import secrets
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import Field

from mock_pay.domain import PartnerFound, PartnerUnavailable, RetryDecision
from mock_pay.errors import ApiError, not_found
from mock_pay.idempotency import IdempotencyConflictError, StoredResponse, fingerprint
from mock_pay.models import (
    ActionAudit,
    Balance,
    Deposit,
    FailureCode,
    HoldReason,
    Merchant,
    Model,
    PartnerPayment,
    PartnerStatus,
    Provider,
    Quote,
    Rail,
    RailCode,
    Recipient,
    ReturnCode,
    StatementLine,
    Transfer,
    TransferStatus,
    VirtualAccount,
    WebhookEvent,
)
from mock_pay.settings import Settings
from mock_pay.store import InvalidCursorError, RetryNotAllowedError, Store

IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{8,255}$")
bearer = HTTPBearer(auto_error=False)


def get_store(request: Request) -> Store:
    store: Store = request.app.state.store
    return store


def require_api_key(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> None:
    settings: Settings = request.app.state.settings
    expected = settings.api_key.get_secret_value()
    # Compare bytes: compare_digest raises on non-ASCII str, which would turn a bad key into a 500.
    if credentials is None or not secrets.compare_digest(
        credentials.credentials.encode(), expected.encode()
    ):
        raise ApiError(401, "unauthorized", "Missing or invalid API key")


StoreDep = Annotated[Store, Depends(get_store)]
router = APIRouter(prefix="/v1", dependencies=[Depends(require_api_key)])


class ListResponse[T](Model):
    data: list[T]


class Clock(Model):
    now: datetime
    frozen: bool = Field(description="Always true: the mock runs on a fixed clock.")


class TransferSummary(Model):
    id: str
    merchant_id: str
    status: TransferStatus
    failure_code: FailureCode | None
    hold_reason: HoldReason | None
    return_code: ReturnCode | None
    source_amount: Decimal
    source_currency: str
    target_amount: Decimal
    target_currency: str
    rail: RailCode
    client_reference: str
    created_at: datetime
    updated_at: datetime


class TransferPage(Model):
    data: list[TransferSummary]
    has_more: bool
    next_cursor: str | None


class ActionRequest(Model):
    requested_by: str = Field(min_length=1, max_length=100, description="Who asked for this")
    reason: str = Field(min_length=1, max_length=500, description="Why, for the audit trail")


class RetryResponse(Model):
    transfer: Transfer
    decision: RetryDecision


class ResendResponse(Model):
    event: WebhookEvent
    result: Literal["delivered", "failed"]


# --- reference data --------------------------------------------------------------------------


@router.get("/clock", summary="Current time (frozen)")
def get_clock(store: StoreDep) -> Clock:
    return Clock(now=store.now, frozen=True)


@router.get("/rails", summary="Payout rails and their SLAs")
def list_rails(store: StoreDep) -> ListResponse[Rail]:
    return ListResponse(data=store.rails)


@router.get("/partners/status", summary="Whether each banking partner's API is up")
def list_partner_statuses(store: StoreDep) -> ListResponse[PartnerStatus]:
    return ListResponse(data=list(store.partner_statuses.values()))


# --- merchants -------------------------------------------------------------------------------


@router.get("/merchants", summary="List merchants")
def list_merchants(store: StoreDep) -> ListResponse[Merchant]:
    return ListResponse(data=list(store.merchants.values()))


def _merchant(store: Store, merchant_id: str) -> Merchant:
    merchant = store.merchants.get(merchant_id)
    if merchant is None:
        raise not_found("merchant", merchant_id)
    return merchant


@router.get("/merchants/{merchant_id}", summary="Get a merchant")
def get_merchant(merchant_id: str, store: StoreDep) -> Merchant:
    return _merchant(store, merchant_id)


@router.get("/merchants/{merchant_id}/balances", summary="Merchant balances")
def list_balances(merchant_id: str, store: StoreDep) -> ListResponse[Balance]:
    _merchant(store, merchant_id)
    return ListResponse(data=store.balances_for(merchant_id))


@router.get("/merchants/{merchant_id}/virtual-accounts", summary="Merchant virtual accounts")
def list_virtual_accounts(merchant_id: str, store: StoreDep) -> ListResponse[VirtualAccount]:
    _merchant(store, merchant_id)
    return ListResponse(data=[v for v in store.virtual_accounts if v.merchant_id == merchant_id])


@router.get("/merchants/{merchant_id}/deposits", summary="Deposits into a merchant's accounts")
def list_deposits(
    merchant_id: str,
    store: StoreDep,
    status: Annotated[Literal["credited", "pending_review", "returned"] | None, Query()] = None,
) -> ListResponse[Deposit]:
    _merchant(store, merchant_id)
    deposits = [
        d
        for d in store.deposits
        if d.merchant_id == merchant_id and (status is None or d.status == status)
    ]
    return ListResponse(data=sorted(deposits, key=lambda d: d.received_at, reverse=True))


# --- transfers -------------------------------------------------------------------------------


def _transfer(store: Store, transfer_id: str) -> Transfer:
    transfer = store.transfers.get(transfer_id)
    if transfer is None:
        raise not_found("transfer", transfer_id)
    return transfer


@router.get("/transfers", summary="List transfers, newest first")
def list_transfers(
    store: StoreDep,
    merchant_id: str | None = None,
    status: TransferStatus | None = None,
    client_reference: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    starting_after: Annotated[str | None, Query(description="Cursor: a transfer id")] = None,
) -> TransferPage:
    try:
        items, has_more = store.list_transfers(
            merchant_id=merchant_id,
            status=status,
            client_reference=client_reference,
            limit=limit,
            starting_after=starting_after,
        )
    except InvalidCursorError as exc:
        raise ApiError(400, "invalid_cursor", f"No transfer with id {starting_after!r}") from exc
    summaries = [TransferSummary.model_validate(t.model_dump(include=_SUMMARY)) for t in items]
    return TransferPage(
        data=summaries,
        has_more=has_more,
        next_cursor=summaries[-1].id if has_more and summaries else None,
    )


_SUMMARY = set(TransferSummary.model_fields)


@router.get("/transfers/{transfer_id}", summary="Get a transfer with its status history")
def get_transfer(transfer_id: str, store: StoreDep) -> Transfer:
    return _transfer(store, transfer_id)


@router.get(
    "/transfers/{transfer_id}/webhook-events",
    summary="Webhook events for a transfer, with every delivery attempt",
)
def list_transfer_events(transfer_id: str, store: StoreDep) -> ListResponse[WebhookEvent]:
    _transfer(store, transfer_id)
    return ListResponse(data=store.events_for(transfer_id))


@router.get(
    "/transfers/{transfer_id}/retry-eligibility",
    summary="Dry run of the retry policy (read-only)",
)
def get_retry_eligibility(transfer_id: str, store: StoreDep) -> RetryDecision:
    return store.retry_decision(_transfer(store, transfer_id))


@router.post(
    "/transfers/{transfer_id}/retry",
    summary="Retry a failed payout (requires Idempotency-Key; the policy is re-checked)",
    response_model=RetryResponse,
)
def retry_transfer(
    transfer_id: str,
    body: ActionRequest,
    request: Request,
    store: StoreDep,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> JSONResponse:
    def action(key: str) -> StoredResponse:
        _transfer(store, transfer_id)
        audit = ActionAudit(requested_by=body.requested_by, reason=body.reason, idempotency_key=key)
        try:
            transfer, decision = store.retry_transfer(transfer_id, audit)
        except RetryNotAllowedError as exc:
            raise ApiError(
                409,
                "retry_not_allowed",
                f"Retry blocked by check {exc.decision.reason_code!r}",
                {"decision": exc.decision.model_dump(mode="json")},
            ) from exc
        return StoredResponse(
            200, RetryResponse(transfer=transfer, decision=decision).model_dump(mode="json")
        )

    return _run_idempotent(store, request, body, idempotency_key, action)


# --- partner and reconciliation ------------------------------------------------------------------


@router.get(
    "/partners/{provider}/payments/{provider_reference}",
    summary="The banking partner's view of a payment",
)
def get_partner_payment(
    provider: Provider, provider_reference: str, store: StoreDep
) -> PartnerPayment:
    lookup = store.partner_lookup(provider, provider_reference)
    if isinstance(lookup, PartnerUnavailable):
        raise ApiError(503, "partner_unavailable", lookup.reason)
    if isinstance(lookup, PartnerFound):
        return lookup.payment
    raise ApiError(
        404,
        "partner_payment_not_found",
        f"{provider} has no payment with reference {provider_reference!r}",
    )


@router.get("/reconciliation/statement-lines", summary="Partner statement lines")
def list_statement_lines(
    store: StoreDep,
    transfer_id: str | None = None,
    provider: Provider | None = None,
    provider_reference: str | None = None,
    match_status: Literal["matched", "unmatched", "status_mismatch"] | None = None,
    statement_date: date | None = None,
) -> ListResponse[StatementLine]:
    lines = store.statement_lines
    if transfer_id is not None:  # same matching rule as the retry policy uses
        transfer = store.transfers.get(transfer_id)
        lines = store.statement_lines_for(transfer) if transfer else []
    return ListResponse(
        data=[
            line
            for line in lines
            if (provider is None or line.provider == provider)
            and (provider_reference is None or line.provider_reference == provider_reference)
            and (match_status is None or line.match_status == match_status)
            and (statement_date is None or line.statement_date == statement_date)
        ]
    )


# --- other resources -------------------------------------------------------------------------


@router.get("/quotes/{quote_id}", summary="Get a quote")
def get_quote(quote_id: str, store: StoreDep) -> Quote:
    quote = store.quotes.get(quote_id)
    if quote is None:
        raise not_found("quote", quote_id)
    return quote


@router.get("/recipients/{recipient_id}", summary="Get a recipient")
def get_recipient(recipient_id: str, store: StoreDep) -> Recipient:
    recipient = store.recipients.get(recipient_id)
    if recipient is None:
        raise not_found("recipient", recipient_id)
    return recipient


@router.post(
    "/webhook-events/{event_id}/resend",
    summary="Resend a webhook event (requires Idempotency-Key)",
    response_model=ResendResponse,
)
def resend_webhook_event(
    event_id: str,
    body: ActionRequest,
    request: Request,
    store: StoreDep,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> JSONResponse:
    def action(_: str) -> StoredResponse:
        if event_id not in store.events:
            raise not_found("webhook_event", event_id)
        event, result = store.resend_event(event_id)
        return StoredResponse(
            200, ResendResponse(event=event, result=result).model_dump(mode="json")
        )

    return _run_idempotent(store, request, body, idempotency_key, action)


def _run_idempotent(
    store: Store,
    request: Request,
    body: ActionRequest,
    key: str | None,
    action: Callable[[str], StoredResponse],
) -> JSONResponse:
    if key is None:
        raise ApiError(400, "idempotency_key_required", "Send an Idempotency-Key header")
    if not IDEMPOTENCY_KEY_PATTERN.fullmatch(key):
        raise ApiError(
            400,
            "invalid_idempotency_key",
            "Idempotency-Key must be 8-255 characters of letters, digits, '.', '_', ':' or '-'",
        )
    try:
        response, replayed = store.idempotency.run(
            key,
            fingerprint("POST", request.url.path, body.model_dump(mode="json")),
            lambda: action(key),
        )
    except IdempotencyConflictError as exc:
        raise ApiError(
            422,
            "idempotency_key_reused",
            "This Idempotency-Key was already used for a different request",
        ) from exc
    headers = {"Idempotent-Replayed": "true"} if replayed else {}
    return JSONResponse(response.body, status_code=response.status_code, headers=headers)
