import threading
import time

import pytest

from mock_pay.idempotency import (
    IdempotencyConflictError,
    IdempotencyStore,
    StoredResponse,
    fingerprint,
)


def test_same_key_and_request_executes_once_then_replays() -> None:
    store = IdempotencyStore()
    calls = 0

    def action() -> StoredResponse:
        nonlocal calls
        calls += 1
        return StoredResponse(status_code=200, body={"n": calls})

    first, first_replayed = store.run("key-0001", "fp-a", action)
    second, second_replayed = store.run("key-0001", "fp-a", action)

    assert calls == 1
    assert (first_replayed, second_replayed) == (False, True)
    assert second == first


def test_same_key_with_a_different_request_is_rejected() -> None:
    store = IdempotencyStore()
    store.run("key-0001", "fp-a", lambda: StoredResponse(status_code=200, body={}))

    with pytest.raises(IdempotencyConflictError):
        store.run("key-0001", "fp-b", lambda: StoredResponse(status_code=200, body={}))


def test_failures_do_not_consume_the_key() -> None:
    store = IdempotencyStore()

    def failing() -> StoredResponse:
        raise RuntimeError("partner status unknown")

    with pytest.raises(RuntimeError):
        store.run("key-0001", "fp-a", failing)
    response, replayed = store.run(
        "key-0001", "fp-a", lambda: StoredResponse(status_code=200, body={"ok": True})
    )

    assert not replayed
    assert response.body == {"ok": True}


def test_non_2xx_responses_are_not_stored() -> None:
    store = IdempotencyStore()
    store.run("key-0001", "fp-a", lambda: StoredResponse(status_code=409, body={}))

    response, replayed = store.run("key-0001", "fp-a", lambda: StoredResponse(200, {"ok": True}))

    assert not replayed
    assert response.status_code == 200


def test_concurrent_duplicates_execute_once() -> None:
    store = IdempotencyStore()
    calls = 0
    barrier = threading.Barrier(8)
    results: list[bool] = []

    def action() -> StoredResponse:
        nonlocal calls
        calls += 1
        time.sleep(0.01)  # widen the race window
        return StoredResponse(status_code=200, body={})

    def worker() -> None:
        barrier.wait()
        _, replayed = store.run("key-0001", "fp-a", action)
        results.append(replayed)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert calls == 1
    assert sorted(results) == [False] + [True] * 7


def test_fingerprint_ignores_key_order_but_not_values() -> None:
    a = fingerprint("POST", "/v1/transfers/tr_1/retry", {"reason": "x", "requested_by": "ops"})
    b = fingerprint("POST", "/v1/transfers/tr_1/retry", {"requested_by": "ops", "reason": "x"})
    c = fingerprint("POST", "/v1/transfers/tr_1/retry", {"requested_by": "ops", "reason": "y"})
    d = fingerprint("POST", "/v1/transfers/tr_2/retry", {"requested_by": "ops", "reason": "x"})

    assert a == b
    assert len({a, c, d}) == 3
