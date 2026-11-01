"""Assemble the dataset and serialize it to the files committed in the repository."""

import json
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from mock_pay.models import Dataset
from mock_pay.seed.generator import NOW, SEED, Builder, ScenarioLabel
from mock_pay.seed.scenarios import add_scenarios

COLLECTIONS = (
    "rails",
    "partner_statuses",
    "merchants",
    "balances",
    "virtual_accounts",
    "deposits",
    "recipients",
    "quotes",
    "transfers",
    "webhook_events",
    "partner_payments",
    "statement_lines",
)


def build(seed: int = SEED, now: datetime = NOW) -> tuple[Dataset, list[ScenarioLabel]]:
    builder = Builder(seed, now)
    builder.add_merchants()
    add_scenarios(builder)  # before background traffic, so scenario data never depends on it
    builder.add_background()
    builder.add_balances()
    return builder.dataset(), builder.labels


def render(dataset: Dataset, labels: Sequence[ScenarioLabel]) -> tuple[dict[str, str], str]:
    """Serialize to (data files by name, labels file). Pure, so tests can diff it."""
    files = {"meta.json": _dumps({"now": dataset.model_dump(mode="json")["now"], "seed": SEED})}
    for name in COLLECTIONS:
        items = getattr(dataset, name)
        files[f"{name}.json"] = _dumps([item.model_dump(mode="json") for item in items])
    return files, _dumps([label.model_dump(mode="json") for label in labels])


def _dumps(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"
