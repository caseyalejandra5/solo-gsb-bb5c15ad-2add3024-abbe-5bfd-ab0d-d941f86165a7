"""SQLite-backed idempotency key store for ``POST /reconcile``.

When a request carries an ``Idempotency-Key`` header, the *validated* request
body is canonicalised (object keys sorted, compact JSON encoding) and hashed
with SHA-256.  The tuple ``(key, fingerprint, successful response)`` is then
persisted in SQLite:

* the first request for a key runs normally and its response is stored only
  once it succeeds (HTTP 200);
* a repeated request with the same key and fingerprint returns the stored
  response verbatim, flagged as a replay, without solving again;
* the same key paired with a different fingerprint gets HTTP 409;
* concurrent requests sharing a key trigger a single solve: waiters reuse the
  leader's result (success or failure) instead of solving again;
* a failed (non-200) response is never persisted, so the key stays free for a
  later retry (only the in-flight concurrent waiters reuse the failure).

The database file survives container restarts (a Compose volume is mounted at
``/data`` by default), so replays keep working after a restart.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_DB_PATH = os.environ.get("IDEMPOTENCY_DB_PATH", "idempotency.db")


class FingerprintConflict(Exception):
    """Raised when an idempotency key is reused with a different payload."""

    def __init__(self, key: str) -> None:
        self.key = key
        super().__init__(f"idempotency key {key!r} was already used with a different request body")


@dataclass(frozen=True)
class IdempotentResult:
    """Outcome of an idempotent request.

    ``replayed`` is ``False`` for the first (solved) request and ``True`` when
    an existing (in-flight or stored) response was reused.  ``body`` is the
    persisted response as compact JSON text so it can be returned verbatim.
    """

    replayed: bool
    body: str


def canonical_fingerprint(validated_payload: Any) -> str:
    """SHA-256 fingerprint of the validated body.

    The body is dumped to JSON-compatible primitives, object keys are sorted
    and the JSON is encoded compactly before hashing, so the fingerprint only
    depends on the validated content rather than on the request's raw
    formatting or key order.
    """
    if hasattr(validated_payload, "model_dump"):
        data = validated_payload.model_dump(mode="json")
    else:
        data = validated_payload
    raw = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class _Inflight:
    """Outcome holder shared between concurrent requests carrying one key."""

    __slots__ = ("event", "fingerprint", "error", "body")

    def __init__(self, fingerprint: str) -> None:
        self.event = threading.Event()
        self.fingerprint = fingerprint
        self.error: BaseException | None = None
        self.body: str | None = None

    @property
    def done(self) -> bool:
        return self.event.is_set()


class IdempotencyStore:
    """Thread-safe SQLite storage for idempotency keys."""

    def __init__(self, db_path: str | os.PathLike[str] = DEFAULT_DB_PATH) -> None:
        self._db_path = str(db_path)
        self._conn: sqlite3.Connection | None = None
        self._connect_guard = threading.Lock()
        # Serialises the quick read/write statements on the shared connection.
        self._db_lock = threading.Lock()
        # One condition per key coordinates concurrent requests with that key.
        self._conds: dict[str, threading.Condition] = {}
        self._guards_guard = threading.Lock()
        # Keys currently being solved (leader -> outcome holder).
        self._inflight: dict[str, _Inflight] = {}

    # ------------------------------------------------------------------ #
    # connection management
    # ------------------------------------------------------------------ #
    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            with self._connect_guard:
                if self._conn is None:
                    db_path = Path(self._db_path)
                    db_path.parent.mkdir(parents=True, exist_ok=True)
                    conn = sqlite3.connect(self._db_path, timeout=30, check_same_thread=False)
                    conn.execute("PRAGMA journal_mode=WAL")
                    conn.execute("PRAGMA busy_timeout=30000")
                    conn.execute(
                        """
                        CREATE TABLE IF NOT EXISTS idempotency_keys (
                            key         TEXT PRIMARY KEY,
                            fingerprint TEXT NOT NULL,
                            response    TEXT NOT NULL,
                            created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                        )
                        """
                    )
                    conn.commit()
                    self._conn = conn
        return self._conn

    def configure(self, db_path: str | os.PathLike[str]) -> None:
        """Point the store at another database (mainly used by tests)."""
        with self._connect_guard:
            self.close()
            self._db_path = str(db_path)
            with self._guards_guard:
                self._conds.clear()
                self._inflight.clear()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # ------------------------------------------------------------------ #
    # SQL helpers
    # ------------------------------------------------------------------ #
    def _select(self, key: str) -> tuple[str, str] | None:
        conn = self._connect()
        with self._db_lock:
            row = conn.execute(
                "SELECT fingerprint, response FROM idempotency_keys WHERE key = ?",
                (key,),
            ).fetchone()
        return row  # type: ignore[return-value]

    def _insert(self, key: str, fingerprint: str, body: str) -> None:
        conn = self._connect()
        with self._db_lock:
            try:
                conn.execute(
                    "INSERT INTO idempotency_keys (key, fingerprint, response)"
                    " VALUES (?, ?, ?)",
                    (key, fingerprint, body),
                )
                conn.commit()
            except sqlite3.IntegrityError:
                # Another process/worker won the race for this key; the caller
                # re-reads and reconciles against the existing row.
                conn.rollback()
                raise

    def _cond_for(self, key: str) -> threading.Condition:
        with self._guards_guard:
            cond = self._conds.get(key)
            if cond is None:
                cond = threading.Condition()
                self._conds[key] = cond
            return cond

    # ------------------------------------------------------------------ #
    # main entry point
    # ------------------------------------------------------------------ #
    def handle(
        self,
        key: str,
        fingerprint: str,
        producer: Callable[[], str],
    ) -> IdempotentResult:
        """Process a keyed request.

        ``producer`` solves the request and returns its response as compact
        JSON text.  Among concurrent requests with the same key it runs at
        most once; waiters reuse the leader's outcome.  If the producer
        raises, nothing is persisted (the failure does not occupy the key) and
        waiters receive the same failure; a subsequent request after the
        in-flight generation finishes is solved afresh.
        """
        cond = self._cond_for(key)

        with cond:
            # A previously persisted response always wins.
            row = self._select(key)
            if row is not None:
                stored_fingerprint, stored_body = row
                if stored_fingerprint != fingerprint:
                    raise FingerprintConflict(key)
                return IdempotentResult(replayed=True, body=stored_body)

            inflight = self._inflight.get(key)
            if inflight is not None:
                # Concurrent follower: the same key with a different payload
                # conflicts, even while the leader is still solving.
                if inflight.fingerprint != fingerprint:
                    raise FingerprintConflict(key)
                cond.wait_for(lambda: inflight.done)
                if inflight.error is not None:
                    raise inflight.error
                return IdempotentResult(replayed=True, body=inflight.body)  # type: ignore[arg-type]

            # Leader of this in-flight generation.
            inflight = _Inflight(fingerprint)
            self._inflight[key] = inflight

        try:
            body = producer()
        except BaseException as exc:
            # Wake the waiters with the failure, then release the key: the
            # failed response is not persisted and must not occupy it.
            with cond:
                inflight.error = exc
                inflight.event.set()
                self._inflight.pop(key, None)
                cond.notify_all()
            raise

        replayed = False
        try:
            self._insert(key, fingerprint, body)
        except sqlite3.IntegrityError:
            # Lost the race against another process/worker.
            row = self._select(key)
            if row is None:
                raise
            stored_fingerprint, stored_body = row
            if stored_fingerprint != fingerprint:
                raise FingerprintConflict(key) from None
            body, replayed = stored_body, True

        with cond:
            inflight.body = body
            inflight.event.set()
            self._inflight.pop(key, None)
            cond.notify_all()

        return IdempotentResult(replayed=replayed, body=body)


# Process-wide store used by the endpoint.
store = IdempotencyStore()
