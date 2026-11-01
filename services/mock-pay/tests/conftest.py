import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from mock_pay.app import create_app
from mock_pay.settings import Settings

REPO_ROOT = Path(__file__).resolve().parents[3]
LABELS_FILE = REPO_ROOT / "evals" / "scenarios.json"
API_KEY = "test-key-not-a-secret"


@pytest.fixture
def client() -> Iterator[TestClient]:
    app = create_app(Settings(api_key=SecretStr(API_KEY)))
    with TestClient(app, headers={"Authorization": f"Bearer {API_KEY}"}) as test_client:
        yield test_client


@pytest.fixture(scope="session")
def labels() -> dict[str, dict[str, Any]]:
    return {label["id"]: label for label in json.loads(LABELS_FILE.read_text())}


@pytest.fixture(scope="session")
def tid(labels: dict[str, dict[str, Any]]) -> dict[str, str]:
    """First transfer id of each scenario, e.g. tid["S04"]."""
    return {scenario_id: label["transfer_ids"][0] for scenario_id, label in labels.items()}
