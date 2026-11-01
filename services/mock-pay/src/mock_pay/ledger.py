"""Merchant balance movements, derived from transfers and deposits.

The rules live in one place, so the seed generator and the tests agree on them:
- a payout is funded (debited) each time it enters ``processing``, including on a retry;
- the funds are released (credited) when a funded payout fails;
- a returned payout is credited back when it is returned;
- a deposit counts once it is credited.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal

from mock_pay.models import Deposit, Transfer, TransferStatus


@dataclass(frozen=True)
class LedgerEntry:
    at: datetime
    amount: Decimal  # positive: added to the available balance
    kind: Literal["funding", "release", "return", "deposit"]
    source_id: str


def ledger_entries(
    transfers: Iterable[Transfer], deposits: Iterable[Deposit], merchant_id: str
) -> list[LedgerEntry]:
    entries: list[LedgerEntry] = []
    for transfer in transfers:
        if transfer.merchant_id != merchant_id:
            continue
        previous: TransferStatus | None = None
        for change in transfer.status_history:
            if change.status == TransferStatus.PROCESSING:
                entries.append(
                    LedgerEntry(change.at, -transfer.source_amount, "funding", transfer.id)
                )
            elif change.status == TransferStatus.FAILED and previous == TransferStatus.PROCESSING:
                entries.append(
                    LedgerEntry(change.at, transfer.source_amount, "release", transfer.id)
                )
            elif change.status == TransferStatus.RETURNED:
                entries.append(
                    LedgerEntry(change.at, transfer.source_amount, "return", transfer.id)
                )
            previous = change.status
    for deposit in deposits:
        if deposit.merchant_id == merchant_id and deposit.credited_at is not None:
            entries.append(LedgerEntry(deposit.credited_at, deposit.amount, "deposit", deposit.id))
    return sorted(entries, key=lambda e: (e.at, e.source_id))
