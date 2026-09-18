"""Tests for the Idempotency-Key support on POST /reconcile."""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from app import main
from app.idempotency import canonical_fingerprint
from app.main import app
from app.models import ReconcileRequest

T0 = "2026-09-17T00:00:00Z"


@pytest.fixture()
def client(tmp_path):
    main.store.configure(tmp_path / "idempotency.db")
    with TestClient(app) as client:
        yield client
    main.store.close()


def txn(id: str, amount: int, time: str = T0) -> dict:
    return {"id": id, "time": time, "amount": amount}


def body_a():
    return {"left": [txn("L1", 100)], "right": [txn("R1", 100)]}


def body_b():
    return {"left": [txn("L1", 200)], "right": [txn("R1", 200)]}


# --------------------------------------------------------------------- #
# basic replay
# --------------------------------------------------------------------- #
def test_no_header_behaves_as_before(client, monkeypatch):
    counter = {"n": 0}
    original = main.solve

    def counting(payload):
        counter["n"] += 1
        return original(payload)

    monkeypatch.setattr(main, "solve", counting)
    resp = client.post("/reconcile", json=body_a())
    assert resp.status_code == 200
    assert "Idempotency-Replayed" not in resp.headers
    assert counter["n"] == 1

    # without a key, nothing is stored and every request re-solves
    client.post("/reconcile", json=body_a())
    assert counter["n"] == 2


def test_first_request_solved_second_replayed_without_resolving(client, monkeypatch):
    counter = {"n": 0}
    original = main.solve

    def counting(payload):
        counter["n"] += 1
        return original(payload)

    monkeypatch.setattr(main, "solve", counting)

    first = client.post("/reconcile", json=body_a(), headers={"Idempotency-Key": "k-1"})
    assert first.status_code == 200, first.text
    assert "Idempotency-Replayed" not in first.headers
    assert counter["n"] == 1

    second = client.post("/reconcile", json=body_a(), headers={"Idempotency-Key": "k-1"})
    assert second.status_code == 200, second.text
    assert second.headers["Idempotency-Replayed"] == "true"
    assert second.text == first.text
    assert second.json() == first.json()
    assert counter["n"] == 1  # never solved again


def test_replay_ignores_json_key_order_and_whitespace(client, monkeypatch):
    counter = {"n": 0}
    original = main.solve

    def counting(payload):
        counter["n"] += 1
        return original(payload)

    monkeypatch.setattr(main, "solve", counting)

    reordered = (
        '{"right":[{"amount":100,"time":"2026-09-17T00:00:00Z","id":"R1"}],'
        '"left":[{"id":"L1","time":"2026-09-17T00:00:00Z","amount":100}]}'
    )
    first = client.post(
        "/reconcile",
        content=reordered,
        headers={"Idempotency-Key": "k-order", "Content-Type": "application/json"},
    )
    assert first.status_code == 200, first.text

    second = client.post(
        "/reconcile", json=body_a(), headers={"Idempotency-Key": "k-order"}
    )
    assert second.status_code == 200
    assert second.headers["Idempotency-Replayed"] == "true"
    assert second.text == first.text
    assert counter["n"] == 1


def test_same_key_different_fingerprint_conflicts(client):
    first = client.post("/reconcile", json=body_a(), headers={"Idempotency-Key": "k-x"})
    assert first.status_code == 200

    conflict = client.post("/reconcile", json=body_b(), headers={"Idempotency-Key": "k-x"})
    assert conflict.status_code == 409, conflict.text

    # the original response is still replayable afterwards
    replay = client.post("/reconcile", json=body_a(), headers={"Idempotency-Key": "k-x"})
    assert replay.status_code == 200
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.text == first.text


def test_distinct_keys_are_independent(client):
    r1 = client.post("/reconcile", json=body_a(), headers={"Idempotency-Key": "k-a"})
    r2 = client.post("/reconcile", json=body_b(), headers={"Idempotency-Key": "k-b"})
    assert r1.status_code == r2.status_code == 200
    assert r1.json()["total_amount"] == 100
    assert r2.json()["total_amount"] == 200


# --------------------------------------------------------------------- #
# header validation
# --------------------------------------------------------------------- #
def test_empty_header_rejected(client):
    resp = client.post("/reconcile", json=body_a(), headers={"Idempotency-Key": ""})
    assert resp.status_code == 400


def test_overlong_header_rejected(client):
    resp = client.post(
        "/reconcile", json=body_a(), headers={"Idempotency-Key": "x" * 129}
    )
    assert resp.status_code == 400
    # 128 chars is the upper bound and must be accepted
    ok = client.post(
        "/reconcile", json=body_a(), headers={"Idempotency-Key": "x" * 128}
    )
    assert ok.status_code == 200


# --------------------------------------------------------------------- #
# failures must not occupy the key
# --------------------------------------------------------------------- #
def test_failed_response_does_not_occupy_key(client):
    dup = {
        "left": [txn("X", 1), txn("X", 2)],
        "right": [],
    }
    first = client.post("/reconcile", json=dup, headers={"Idempotency-Key": "k-fail"})
    assert first.status_code == 400

    # retry with the same key on a valid body solves normally
    ok = client.post("/reconcile", json=body_a(), headers={"Idempotency-Key": "k-fail"})
    assert ok.status_code == 200, ok.text
    assert "Idempotency-Replayed" not in ok.headers


def test_validation_error_not_replayed(client):
    bad = {"left": [txn("L1", 0)], "right": []}
    first = client.post("/reconcile", json=bad, headers={"Idempotency-Key": "k-422"})
    assert first.status_code == 422
    second = client.post("/reconcile", json=bad, headers={"Idempotency-Key": "k-422"})
    assert second.status_code == 422
    assert "Idempotency-Replayed" not in second.headers


# --------------------------------------------------------------------- #
# persistence across restarts
# --------------------------------------------------------------------- #
def test_replay_survives_store_restart(client, tmp_path, monkeypatch):
    db_path = tmp_path / "idempotency.db"
    assert main.store._db_path == str(db_path)
    first = client.post("/reconcile", json=body_a(), headers={"Idempotency-Key": "persist-1"})
    assert first.status_code == 200

    # Simulate a container restart: a brand-new store object opening the same
    # database file left behind by the previous run.
    from app.idempotency import IdempotencyStore

    restarted = IdempotencyStore(db_path)
    monkeypatch.setattr(main, "store", restarted)

    restarted_client = TestClient(app)
    replay = restarted_client.post(
        "/reconcile", json=body_a(), headers={"Idempotency-Key": "persist-1"}
    )
    assert replay.status_code == 200
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.text == first.text

    conflict = restarted_client.post(
        "/reconcile", json=body_b(), headers={"Idempotency-Key": "persist-1"}
    )
    assert conflict.status_code == 409
    restarted.close()


# --------------------------------------------------------------------- #
# concurrency
# --------------------------------------------------------------------- #
def _worker_thread_count() -> int:
    return len(
        [t for t in threading.enumerate() if t is not threading.main_thread() and t.is_alive()]
    )


def _wait_for_threads(baseline: int, extra: int) -> None:
    """Wait until ``extra`` request-handling threads (beyond ``baseline``)
    are alive and blocked."""
    import time

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if _worker_thread_count() >= baseline + extra:
            return
        time.sleep(0.01)
    raise AssertionError("worker threads did not queue in time")


def test_concurrent_same_key_solves_once(client, monkeypatch):
    counter = {"n": 0}
    counter_lock = threading.Lock()
    entered = threading.Event()
    release = threading.Event()
    original = main.solve

    def counting(payload):
        with counter_lock:
            counter["n"] += 1
        entered.set()
        release.wait(timeout=5)
        return original(payload)

    monkeypatch.setattr(main, "solve", counting)

    results: list = []
    results_lock = threading.Lock()

    def worker():
        resp = client.post("/reconcile", json=body_a(), headers={"Idempotency-Key": "k-conc"})
        with results_lock:
            results.append(resp)

    baseline = _worker_thread_count()
    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    entered.wait(timeout=5)
    _wait_for_threads(baseline, 5)  # 1 solving + 4 followers queued
    release.set()
    for t in threads:
        t.join(timeout=10)

    assert counter["n"] == 1
    assert len(results) == 5
    statuses = sorted(r.status_code for r in results)
    assert statuses == [200] * 5
    leaders = [r for r in results if "Idempotency-Replayed" not in r.headers]
    followers = [r for r in results if r.headers.get("Idempotency-Replayed") == "true"]
    assert len(leaders) == 1
    assert len(followers) == 4
    assert all(r.text == leaders[0].text for r in followers)


def test_concurrent_failure_runs_once_then_key_is_free(client, monkeypatch):
    counter = {"n": 0}
    counter_lock = threading.Lock()
    entered = threading.Event()
    release = threading.Event()
    original = main.solve

    def boom(payload):
        with counter_lock:
            counter["n"] += 1
        entered.set()
        release.wait(timeout=5)
        raise main.HTTPException(status_code=500, detail="boom")

    monkeypatch.setattr(main, "solve", boom)

    outcomes: list = []
    results_lock = threading.Lock()

    def worker():
        resp = client.post("/reconcile", json=body_a(), headers={"Idempotency-Key": "k-boom"})
        with results_lock:
            outcomes.append(resp)

    baseline = _worker_thread_count()
    threads = [threading.Thread(target=worker) for _ in range(3)]
    for t in threads:
        t.start()
    entered.wait(timeout=5)
    _wait_for_threads(baseline, 3)
    release.set()
    for t in threads:
        t.join(timeout=10)

    assert counter["n"] == 1
    assert sorted(r.status_code for r in outcomes) == [500, 500, 500]

    # after the in-flight generation, the failure did not occupy the key
    monkeypatch.setattr(main, "solve", original)
    retry = client.post("/reconcile", json=body_a(), headers={"Idempotency-Key": "k-boom"})
    assert retry.status_code == 200
    assert "Idempotency-Replayed" not in retry.headers


def test_concurrent_conflicting_fingerprint_returns_409(client, monkeypatch):
    release = threading.Event()
    entered = threading.Event()
    original = main.solve

    def slow(payload):
        entered.set()
        release.wait(timeout=5)
        return original(payload)

    monkeypatch.setattr(main, "solve", slow)

    conflict_status: list[int] = []

    def conflicting():
        entered.wait(timeout=5)
        resp = client.post(
            "/reconcile", json=body_b(), headers={"Idempotency-Key": "k-diff"}
        )
        conflict_status.append(resp.status_code)

    t = threading.Thread(target=conflicting)
    t.start()
    leader = client.post("/reconcile", json=body_a(), headers={"Idempotency-Key": "k-diff"})
    release.set()
    t.join()

    assert leader.status_code == 200
    assert conflict_status == [409]


# --------------------------------------------------------------------- #
# fingerprint unit check
# --------------------------------------------------------------------- #
def test_canonical_fingerprint_stable_and_distinct():
    req1 = ReconcileRequest.model_validate(body_a())
    req1_reordered = ReconcileRequest.model_validate(body_a())
    req2 = ReconcileRequest.model_validate(body_b())
    assert canonical_fingerprint(req1) == canonical_fingerprint(req1_reordered)
    assert canonical_fingerprint(req1) != canonical_fingerprint(req2)
