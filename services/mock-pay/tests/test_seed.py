"""The seed data is the ground the whole project stands on: check it is reproducible and sane."""

import json
from decimal import ROUND_HALF_EVEN, Decimal
from importlib import resources
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest

from mock_pay.app import create_app
from mock_pay.domain import assert_transition
from mock_pay.models import Dataset, TransferStatus
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
