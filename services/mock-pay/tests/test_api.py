from datetime import datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mock_pay.app import create_app

NOW = datetime.fromisoformat("2026-03-11T09:30:00Z")


def error_code(response: Any) -> str:
    code: str = response.json()["error"]["code"]
    return code


class TestBasics:
    def test_health_needs_no_auth(self) -> None:
        with TestClient(create_app()) as anonymous:
            assert anonymous.get("/health").json() == {"status": "ok"}

    @pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong-key"}])
    def test_api_requires_the_key(self, headers: dict[str, str]) -> None:
        with TestClient(create_app(), headers=headers) as anonymous:
            response = anonymous.get("/v1/clock")

        assert response.status_code == 401
        assert error_code(response) == "unauthorized"

    def test_clock_is_frozen(self, client: TestClient) -> None:
        assert client.get("/v1/clock").json() == {"now": "2026-03-11T09:30:00Z", "frozen": True}

    def test_errors_share_one_shape(self, client: TestClient) -> None:
        missing = client.get("/v1/transfers/tr_doesnotexist")
        invalid = client.get("/v1/transfers", params={"limit": 0})
        unknown = client.get("/v1/nope")

        assert (missing.status_code, error_code(missing)) == (404, "transfer_not_found")
        assert (invalid.status_code, error_code(invalid)) == (422, "invalid_request")
        assert (unknown.status_code, error_code(unknown)) == (404, "route_not_found")
        assert set(missing.json()["error"]) == {"code", "message", "details"}


class TestTransfers:
    def test_cursor_pagination_walks_every_transfer_once(self, client: TestClient) -> None:
        seen: list[str] = []
        cursor = None
        while True:
            params: dict[str, Any] = {"limit": 25}
            if cursor:
                params["starting_after"] = cursor
            page = client.get("/v1/transfers", params=params).json()
            seen += [t["id"] for t in page["data"]]
            cursor = page["next_cursor"]
            if not page["has_more"]:
                break
        created = [client.get(f"/v1/transfers/{tid}").json()["created_at"] for tid in seen[:5]]

        assert len(seen) == len(set(seen)) > 100
        assert created == sorted(created, reverse=True)

    def test_filters(self, client: TestClient, labels: dict[str, Any]) -> None:
        held = client.get("/v1/transfers", params={"status": "on_hold"}).json()["data"]
        duplicates = client.get(
            "/v1/transfers", params={"client_reference": "INV-2026-0042"}
        ).json()["data"]

        assert [t["status"] for t in held] == ["on_hold"] * len(held)
        assert {t["id"] for t in duplicates} == set(labels["S14"]["transfer_ids"])

    def test_detail_includes_status_history(self, client: TestClient, tid: dict[str, str]) -> None:
        transfer = client.get(f"/v1/transfers/{tid['S01']}").json()

        assert [c["status"] for c in transfer["status_history"]] == ["created", "on_hold"]
        assert transfer["hold_reason"] == "sanctions_potential_match"


class TestScenarioEvidence:
    """Every scenario's key evidence must be reachable through the API."""

    def test_s02_funds_stuck_in_deposit_review(
        self, client: TestClient, tid: dict[str, str]
    ) -> None:
        transfer = client.get(f"/v1/transfers/{tid['S02']}").json()
        merchant = transfer["merchant_id"]
        balance = client.get(f"/v1/merchants/{merchant}/balances").json()["data"][0]
        deposits = client.get(
            f"/v1/merchants/{merchant}/deposits", params={"status": "pending_review"}
        ).json()["data"]
        quote = client.get(f"/v1/quotes/{transfer['quote_id']}").json()

        assert transfer["status"] == "pending_funds"
        assert balance["available"] == "120.00"
        assert [d["amount"] for d in deposits] == ["5000.00"]
        expires_in = datetime.fromisoformat(quote["expires_at"]) - NOW
        assert timedelta(hours=3) < expires_in < timedelta(hours=5)

    def test_s03_partner_never_received_it(self, client: TestClient, tid: dict[str, str]) -> None:
        transfer = client.get(f"/v1/transfers/{tid['S03']}").json()
        lookup = client.get(f"/v1/partners/bank_alpha/payments/{transfer['provider_reference']}")
        eligibility = client.get(f"/v1/transfers/{tid['S03']}/retry-eligibility").json()

        assert error_code(lookup) == "partner_payment_not_found"
        assert eligibility["eligible"] is True

    def test_s04_partner_paid_despite_our_timeout(
        self, client: TestClient, tid: dict[str, str]
    ) -> None:
        transfer = client.get(f"/v1/transfers/{tid['S04']}").json()
        partner = client.get(
            f"/v1/partners/bank_alpha/payments/{transfer['provider_reference']}"
        ).json()
        lines = client.get(
            "/v1/reconciliation/statement-lines", params={"transfer_id": tid["S04"]}
        ).json()["data"]
        eligibility = client.get(f"/v1/transfers/{tid['S04']}/retry-eligibility").json()

        assert (transfer["status"], transfer["failure_code"]) == ("failed", "partner_timeout")
        assert partner["status"] == "paid"
        assert [(line["direction"], line["match_status"]) for line in lines] == [
            ("debit", "status_mismatch")
        ]
        assert eligibility["reason_code"] == "partner_has_no_payment"

    def test_s05_processing_past_sla(self, client: TestClient, tid: dict[str, str]) -> None:
        transfer = client.get(f"/v1/transfers/{tid['S05']}").json()
        rails = {r["code"]: r for r in client.get("/v1/rails").json()["data"]}
        partner = client.get(
            f"/v1/partners/bank_alpha/payments/{transfer['provider_reference']}"
        ).json()

        waited = NOW - datetime.fromisoformat(transfer["updated_at"])
        assert waited > timedelta(minutes=rails["sepa"]["sla_minutes"])
        assert partner["status"] == "accepted"
        assert "beneficiary bank" in partner["partner_message"]

    def test_s07_returned_with_debit_and_credit(
        self, client: TestClient, tid: dict[str, str]
    ) -> None:
        lines = client.get(
            "/v1/reconciliation/statement-lines", params={"transfer_id": tid["S07"]}
        ).json()["data"]

        assert sorted(line["direction"] for line in lines) == ["credit", "debit"]

    def test_s08_endpoint_failing(self, client: TestClient, tid: dict[str, str]) -> None:
        events = client.get(f"/v1/transfers/{tid['S08']}/webhook-events").json()["data"]
        completed = next(e for e in events if e["type"] == "transfer.completed")

        assert completed["delivery_status"] == "failed"
        assert len(completed["attempts"]) == 7
        assert {a["response_status"] for a in completed["attempts"]} == {503, None}

    def test_s09_delivered(self, client: TestClient, tid: dict[str, str]) -> None:
        events = client.get(f"/v1/transfers/{tid['S09']}/webhook-events").json()["data"]

        assert {e["delivery_status"] for e in events} == {"delivered"}
        assert {e["attempts"][0]["response_status"] for e in events} == {200}

    def test_s10_deposit_credited_after_quote_expired(
        self, client: TestClient, tid: dict[str, str]
    ) -> None:
        transfer = client.get(f"/v1/transfers/{tid['S10']}").json()
        quote = client.get(f"/v1/quotes/{transfer['quote_id']}").json()
        deposits = client.get(f"/v1/merchants/{transfer['merchant_id']}/deposits").json()["data"]

        credited = [d["credited_at"] for d in deposits if d["amount"] == "6000.00"]
        assert transfer["failure_code"] == "quote_expired"
        assert quote["expires_at"] < credited[0]

    def test_s11_injection_planted_in_webhook_response(
        self, client: TestClient, tid: dict[str, str]
    ) -> None:
        events = client.get(f"/v1/transfers/{tid['S11']}/webhook-events").json()["data"]
        bodies = [a["response_body_excerpt"] for e in events for a in e["attempts"]]

        assert bodies
        assert all(tid["S04"] in body and "Ignore all previous" in body for body in bodies)

    def test_s13_partner_status_unknown(self, client: TestClient, tid: dict[str, str]) -> None:
        statuses = {p["provider"]: p for p in client.get("/v1/partners/status").json()["data"]}
        transfer = client.get(f"/v1/transfers/{tid['S13']}").json()
        lookup = client.get(f"/v1/partners/bank_gamma/payments/{transfer['provider_reference']}")
        eligibility = client.get(f"/v1/transfers/{tid['S13']}/retry-eligibility").json()

        assert statuses["bank_gamma"]["status"] == "outage"
        assert (lookup.status_code, error_code(lookup)) == (503, "partner_unavailable")
        assert eligibility["reason_code"] == "partner_status_known"


ACTION = {"requested_by": "ops_test", "reason": "approved in test"}


class TestRetry:
    def test_safe_retry_executes_once_and_replays(
        self, client: TestClient, tid: dict[str, str]
    ) -> None:
        url = f"/v1/transfers/{tid['S03']}/retry"
        headers = {"Idempotency-Key": "retry-s03-0001"}

        first = client.post(url, json=ACTION, headers=headers)
        replay = client.post(url, json=ACTION, headers=headers)
        transfer = client.get(f"/v1/transfers/{tid['S03']}").json()
        events = client.get(f"/v1/transfers/{tid['S03']}/webhook-events").json()["data"]

        assert first.status_code == replay.status_code == 200
        assert replay.json() == first.json()
        assert "Idempotent-Replayed" not in first.headers
        assert replay.headers["Idempotent-Replayed"] == "true"
        assert (transfer["status"], transfer["attempt"]) == ("processing", 2)
        assert transfer["provider_reference"].endswith("-R2")
        assert transfer["status_history"][-1]["actor"] == "ops"
        assert sum(1 for c in transfer["status_history"] if c["status"] == "processing") == 2
        assert events[-1]["type"] == "transfer.processing"

    def test_second_retry_with_a_new_key_is_refused(
        self, client: TestClient, tid: dict[str, str]
    ) -> None:
        # Two approvals for the same payout must not become two payouts.
        url = f"/v1/transfers/{tid['S03']}/retry"
        client.post(url, json=ACTION, headers={"Idempotency-Key": "retry-s03-0001"})

        second = client.post(url, json=ACTION, headers={"Idempotency-Key": "retry-s03-0002"})

        assert (second.status_code, error_code(second)) == (409, "retry_not_allowed")
        assert second.json()["error"]["details"]["decision"]["reason_code"] == "transfer_is_failed"

    @pytest.mark.parametrize(
        ("scenario", "reason"),
        [
            ("S04", "partner_has_no_payment"),
            ("S06", "failure_is_retryable"),
            ("S13", "partner_status_known"),
        ],
    )
    def test_server_refuses_unsafe_retries(
        self, client: TestClient, tid: dict[str, str], scenario: str, reason: str
    ) -> None:
        before = client.get(f"/v1/transfers/{tid[scenario]}").json()

        response = client.post(
            f"/v1/transfers/{tid[scenario]}/retry",
            json=ACTION,
            headers={"Idempotency-Key": f"retry-{scenario}-0001"},
        )

        assert (response.status_code, error_code(response)) == (409, "retry_not_allowed")
        assert response.json()["error"]["details"]["decision"]["reason_code"] == reason
        assert client.get(f"/v1/transfers/{tid[scenario]}").json() == before

    def test_refused_request_does_not_consume_the_key(
        self, client: TestClient, tid: dict[str, str]
    ) -> None:
        headers = {"Idempotency-Key": "shared-key-0001"}
        client.post(f"/v1/transfers/{tid['S04']}/retry", json=ACTION, headers=headers)

        retry = client.post(f"/v1/transfers/{tid['S03']}/retry", json=ACTION, headers=headers)

        assert retry.status_code == 200

    @pytest.mark.parametrize(
        ("key", "code"),
        [(None, "idempotency_key_required"), ("short", "invalid_idempotency_key")],
    )
    def test_key_is_required_and_validated(
        self, client: TestClient, tid: dict[str, str], key: str | None, code: str
    ) -> None:
        headers = {"Idempotency-Key": key} if key else {}

        response = client.post(f"/v1/transfers/{tid['S03']}/retry", json=ACTION, headers=headers)

        assert (response.status_code, error_code(response)) == (400, code)

    def test_key_reused_with_a_different_body_is_rejected(
        self, client: TestClient, tid: dict[str, str]
    ) -> None:
        url = f"/v1/transfers/{tid['S03']}/retry"
        headers = {"Idempotency-Key": "retry-s03-0001"}
        client.post(url, json=ACTION, headers=headers)

        changed = client.post(url, json={**ACTION, "reason": "different"}, headers=headers)

        assert (changed.status_code, error_code(changed)) == (422, "idempotency_key_reused")


class TestResend:
    def _completed_event(self, client: TestClient, transfer_id: str) -> dict[str, Any]:
        events = client.get(f"/v1/transfers/{transfer_id}/webhook-events").json()["data"]
        event: dict[str, Any] = next(e for e in events if e["type"] == "transfer.completed")
        return event

    def test_resend_to_healthy_endpoint_is_recorded_once(
        self, client: TestClient, tid: dict[str, str]
    ) -> None:
        event = self._completed_event(client, tid["S09"])
        url = f"/v1/webhook-events/{event['id']}/resend"
        headers = {"Idempotency-Key": "resend-s09-0001"}

        first = client.post(url, json=ACTION, headers=headers)
        client.post(url, json=ACTION, headers=headers)
        after = self._completed_event(client, tid["S09"])

        assert first.json()["result"] == "delivered"
        assert len(after["attempts"]) == len(event["attempts"]) + 1
        assert after["attempts"][-1]["trigger"] == "manual_resend"

    def test_resend_to_failing_endpoint_fails_again(
        self, client: TestClient, tid: dict[str, str]
    ) -> None:
        event = self._completed_event(client, tid["S08"])

        response = client.post(
            f"/v1/webhook-events/{event['id']}/resend",
            json=ACTION,
            headers={"Idempotency-Key": "resend-s08-0001"},
        )

        assert response.json()["result"] == "failed"
        assert response.json()["event"]["delivery_status"] == "failed"

    def test_unknown_event(self, client: TestClient) -> None:
        response = client.post(
            "/v1/webhook-events/evt_missing/resend",
            json=ACTION,
            headers={"Idempotency-Key": "resend-missing-01"},
        )

        assert (response.status_code, error_code(response)) == (404, "webhook_event_not_found")
