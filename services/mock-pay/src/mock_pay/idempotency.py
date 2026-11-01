"""Idempotency keys for state-changing endpoints.

A key represents one successful execution. Replaying the same request with the same key returns
the stored response without executing again; reusing a key for a different request is an error.
Only successful (2xx) results are stored, so a refused or failed request can be retried with the
same key once the blocking condition is gone.

A production system would keep this in a database: a unique constraint on the key plus a row
lock (or an "in progress" state) so that two concurrent requests cannot both execute. This mock
is a single process, so one lock around check-and-execute gives the same guarantee.
"""

import hashlib
import json
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class StoredResponse:
    status_code: int
    body: dict[str, Any]


class IdempotencyConflictError(Exception):
    pass


def fingerprint(method: str, path: str, body: dict[str, Any]) -> str:
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(f"{method} {path}\n{canonical}".encode()).hexdigest()


class IdempotencyStore:
    def __init__(self) -> None:
        self._entries: dict[str, tuple[str, StoredResponse]] = {}
        self._lock = threading.Lock()

    def run(
        self, key: str, request_fingerprint: str, action: Callable[[], StoredResponse]
    ) -> tuple[StoredResponse, bool]:
        """Execute ``action`` at most once per key. Returns (response, replayed)."""
        with self._lock:
            stored = self._entries.get(key)
            if stored is not None:
                stored_fingerprint, response = stored
                if stored_fingerprint != request_fingerprint:
                    raise IdempotencyConflictError(key)
                return response, True
            response = action()  # an exception propagates and stores nothing
            if 200 <= response.status_code < 300:
                self._entries[key] = (request_fingerprint, response)
            return response, False
