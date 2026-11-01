"""The seed data is the ground the whole project stands on: check it is reproducible and sane."""

import json
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal
from importlib import resources
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest

from mock_pay.app import create_app
from mock_pay.domain import assert_transition
from mock_pay.ledger import LedgerEntry, ledger_entries
from mock_pay.models import Dataset, FailureCode, Transfer, TransferStatus
from mock_pay.seed.build import build, render
from mock_pay.store import load_seed_dataset

DATA_DIR = resources.files("mock_pay") / "data"
LABELS_FILE = Path(__file__).resolve().parents[3] / "evals" / "scenarios.json"


@pytest.fixture(scope="module")
def dataset() -> Dataset:
    return load_seed_dataset()


def test_generator_is_deterministic() -> None:
    assert render(*build()) == render(*build())


def test_committed_files_match_the_generator() -> None:
    files, labels = render(*build())

    for name, content in files.items():
        committed = (DATA_DIR / name).read_text(encoding="utf-8")
        assert committed == content, f"{name} is stale: run `make seed`"
    assert LABELS_FILE.read_text(encoding="utf-8") == labels, "labels are stale: run `make seed`"


def test_money_is_never_a_float() -> None:
    def reject(literal: str) -> Any:
        raise AssertionError(f"float literal {literal} in seed data; use a decimal string")

    for name in render(*build())[0]:
        json.loads((DATA_DIR / name).read_text(encoding="utf-8"), parse_float=reject)


def test_status_histories_follow_the_state_machine(dataset: Dataset) -> None:
    for transfer in dataset.transfers:
        history = transfer.status_history
        assert history[0].status == TransferStatus.CREATED
        for before, after in pairwise(history):
            assert_transition(before.status, after.status)
            assert before.at <= after.at, transfer.id
        assert transfer.status == history[-1].status
        assert transfer.updated_at == history[-1].at


def test_references_resolve_and_amounts_add_up(dataset: Dataset) -> None:
    merchants = {m.id for m in dataset.merchants}
    recipients = {r.id: r for r in dataset.recipients}
    quotes = {q.id: q for q in dataset.quotes}
    transfers = {t.id for t in dataset.transfers}

    for transfer in dataset.transfers:
        assert transfer.merchant_id in merchants
        assert recipients[transfer.recipient_id].merchant_id == transfer.merchant_id
        quote = quotes[transfer.quote_id]
        expected = ((quote.source_amount - quote.fee) * quote.rate).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_EVEN
        )
        assert quote.target_amount == expected == transfer.target_amount
    assert all(event.transfer_id in transfers for event in dataset.webhook_events)
    assert all(line.matched_transfer_id in transfers for line in dataset.statement_lines)
    accounts = {va.id: va.merchant_id for va in dataset.virtual_accounts}
    assert all(accounts[d.virtual_account_id] == d.merchant_id for d in dataset.deposits)


def test_nothing_happens_after_now(dataset: Dataset) -> None:
    now = dataset.now
    for transfer in dataset.transfers:
        assert all(change.at <= now for change in transfer.status_history)
    for event in dataset.webhook_events:
        assert all(attempt.at <= now for attempt in event.attempts)
    assert all(d.received_at <= now for d in dataset.deposits)
    assert all(p.updated_at <= now for p in dataset.partner_payments)
    # Daily statements arrive the next morning: nothing dated today yet.
    assert all(line.statement_date < now.date() for line in dataset.statement_lines)


def test_every_label_points_at_existing_data(dataset: Dataset) -> None:
    transfers = {t.id for t in dataset.transfers}
    merchants = {m.id for m in dataset.merchants}
    labels = json.loads(LABELS_FILE.read_text(encoding="utf-8"))

    assert [label["id"] for label in labels] == [f"S{n:02d}" for n in range(1, 15)]
    for label in labels:
        assert label["scope_merchant_id"] in merchants
        assert set(label["transfer_ids"]) <= transfers


def test_labels_are_never_served() -> None:
    assert not any("scenario" in name for name in render(*build())[0])
    paths = [getattr(route, "path", "") for route in create_app().routes]
    assert not any("scenario" in path or "label" in path for path in paths)


def test_submissions_match_the_current_reference(dataset: Dataset) -> None:
    for transfer in dataset.transfers:
        if transfer.submissions:
            assert transfer.provider_reference == transfer.submissions[-1].provider_reference
            numbers = [s.attempt for s in transfer.submissions]
            assert numbers == list(range(1, len(numbers) + 1)), transfer.id
        else:
            assert transfer.provider_reference is None, transfer.id


def _balance_at(available: Decimal, entries: list[LedgerEntry], at: datetime) -> Decimal:
    """Balance right after `at`, traced back from today's available balance."""
    return available - sum((e.amount for e in entries if e.at > at), Decimal(0))


def _available(dataset: Dataset, merchant_id: str) -> Decimal:
    return next(b.available for b in dataset.balances if b.merchant_id == merchant_id)


def test_balances_never_go_negative_when_traced_back_from_today(dataset: Dataset) -> None:
    # An investigator who walks the money backward must never find a negative balance.
    for merchant in dataset.merchants:
        available = _available(dataset, merchant.id)
        entries = ledger_entries(dataset.transfers, dataset.deposits, merchant.id)
        assert entries, merchant.name
        balance = available
        for entry in reversed(entries):
            assert balance >= 0, (merchant.name, entry)
            balance -= entry.amount
        assert balance >= 0, f"{merchant.name}: negative opening balance"


def _transfer(dataset: Dataset, transfer_id: str) -> Transfer:
    return next(t for t in dataset.transfers if t.id == transfer_id)


@pytest.mark.parametrize("scenario", ["S02", "S10"])
def test_funding_shortages_really_happened(
    dataset: Dataset, labels: dict[str, dict[str, Any]], scenario: str
) -> None:
    # "Waiting for funds" must hold up: the balance stayed below the payout for the whole wait,
    # and no other payout of the merchant was funded meanwhile.
    transfer = _transfer(dataset, labels[scenario]["transfer_ids"][0])
    waited_until = transfer.updated_at if transfer.status == TransferStatus.FAILED else dataset.now
    quote = next(q for q in dataset.quotes if q.id == transfer.quote_id)
    short_until = min(waited_until, quote.expires_at)  # money may arrive late (S10)
    entries = ledger_entries(dataset.transfers, dataset.deposits, transfer.merchant_id)
    available = _available(dataset, transfer.merchant_id)
    checkpoints = [transfer.created_at] + [
        e.at for e in entries if transfer.created_at <= e.at < short_until
    ]

    for at in checkpoints:
        assert _balance_at(available, entries, at) < transfer.source_amount, at
    assert not [
        e for e in entries if e.kind == "funding" and transfer.created_at <= e.at < waited_until
    ]


def test_closed_or_invalid_accounts_are_never_paid_again(dataset: Dataset) -> None:
    final = {FailureCode.INVALID_ACCOUNT_NUMBER, FailureCode.NAME_MISMATCH}
    for transfer in dataset.transfers:
        if transfer.status == TransferStatus.RETURNED or transfer.failure_code in final:
            dead_since = transfer.updated_at
            later = [
                other.id
                for other in dataset.transfers
                if other.recipient_id == transfer.recipient_id
                and other.id != transfer.id
                and any(
                    c.status == TransferStatus.PROCESSING and c.at > dead_since
                    for c in other.status_history
                )
            ]
            assert not later, f"{transfer.id}: recipient paid again by {later}"


def test_scenario_recipients_only_receive_scenario_payouts(
    dataset: Dataset, labels: dict[str, dict[str, Any]]
) -> None:
    scenario_ids = {tid for label in labels.values() for tid in label["transfer_ids"]}
    reserved = {t.recipient_id for t in dataset.transfers if t.id in scenario_ids}

    intruders = [t.id for t in dataset.transfers if t.recipient_id in reserved]

    assert set(intruders) <= scenario_ids


def test_duplicate_submission_left_a_trace(
    dataset: Dataset, labels: dict[str, dict[str, Any]]
) -> None:
    duplicates = [_transfer(dataset, tid) for tid in labels["S14"]["transfer_ids"]]

    assert [t.request_idempotency_key for t in duplicates] == [None, None]
    assert any(t.request_idempotency_key for t in dataset.transfers)
