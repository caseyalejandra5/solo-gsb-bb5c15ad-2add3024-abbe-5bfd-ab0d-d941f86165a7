"""API-level tests for the POST /reconcile/diagnostics endpoint."""

from __future__ import annotations

import random

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.solver import Reconciler, Txn, diagnose_unmatched

client = TestClient(app)

T0 = "2026-09-17T00:00:00Z"
T_PLUS_48H = "2026-09-19T00:00:00Z"
BASE_TS = 1_758_000_000.0  # fixed epoch for reproducibility


def txn(id: str, amount: int, time: str = T0) -> dict:
    return {"id": id, "time": time, "amount": amount}


def lock(left_ids, right_ids) -> dict:
    return {"left_ids": list(left_ids), "right_ids": list(right_ids)}


def excluded(left_id, right_id) -> dict:
    return {"left_id": left_id, "right_id": right_id}


def diag(id: str, side: str, reason: str, candidates=()) -> dict:
    return {"id": id, "side": side, "reason": reason, "candidates": list(candidates)}


def post_diag(left, right, **extra):
    resp = client.post(
        "/reconcile/diagnostics", json={"left": left, "right": right, **extra}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# --------------------------------------------------------------------- #
# standard response fields are reused unchanged
# --------------------------------------------------------------------- #
def test_standard_fields_match_reconcile():
    payload = {
        "left": [txn("L1", 300), txn("L2", 7)],
        "right": [txn("R1", 100), txn("R2", 200), txn("R3", 9)],
    }
    expected = client.post("/reconcile", json=payload).json()
    body = post_diag(payload["left"], payload["right"])
    assert body["groups"] == expected["groups"]
    assert body["total_amount"] == expected["total_amount"]
    assert body["unmatched_left_ids"] == expected["unmatched_left_ids"]
    assert body["unmatched_right_ids"] == expected["unmatched_right_ids"]


def test_empty_batch():
    assert post_diag([], []) == {
        "groups": [],
        "total_amount": 0,
        "unmatched_left_ids": [],
        "unmatched_right_ids": [],
        "diagnostics": [],
    }


def test_fully_matched_batch_has_no_diagnostics():
    body = post_diag([txn("L1", 100)], [txn("R1", 100)])
    assert body["groups"] == [{"left_ids": ["L1"], "right_ids": ["R1"], "amount": 100}]
    assert body["diagnostics"] == []


def test_diagnostics_cover_exactly_the_unmatched_in_order():
    # left input order L2, L1, L3 is preserved; the left side comes first
    body = post_diag(
        [txn("L2", 2), txn("L1", 1), txn("L3", 3)],
        [txn("R2", 20), txn("R1", 10)],
    )
    assert body["unmatched_left_ids"] == ["L1", "L2", "L3"]
    assert body["unmatched_right_ids"] == ["R1", "R2"]
    assert [(d["id"], d["side"]) for d in body["diagnostics"]] == [
        ("L2", "left"),
        ("L1", "left"),
        ("L3", "left"),
        ("R2", "right"),
        ("R1", "right"),
    ]


# --------------------------------------------------------------------- #
# no_counterparty
# --------------------------------------------------------------------- #
def test_no_counterparty_empty_opposite_side():
    body = post_diag([txn("L1", 100), txn("L2", -50)], [])
    assert body["diagnostics"] == [
        diag("L1", "left", "no_counterparty"),
        diag("L2", "left", "no_counterparty"),
    ]


def test_no_counterparty_all_opposite_side_locked():
    # B's only conceivable counterpart X is locked with A, and locked
    # transactions are never used to build diagnostic combinations
    resp = client.post(
        "/reconcile/diagnostics",
        json={
            "left": [txn("A", 100), txn("B", 100)],
            "right": [txn("X", 100)],
            "locked_groups": [lock(["A"], ["X"])],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["unmatched_left_ids"] == ["B"]
    assert body["diagnostics"] == [diag("B", "left", "no_counterparty")]


# --------------------------------------------------------------------- #
# sign_mismatch
# --------------------------------------------------------------------- #
def test_sign_mismatch_when_opposite_side_has_only_opposite_sign():
    body = post_diag([txn("L1", 100)], [txn("R1", -100)])
    assert body["diagnostics"] == [
        diag("L1", "left", "sign_mismatch"),
        diag("R1", "right", "sign_mismatch"),
    ]


def test_sign_mismatch_with_matched_group_present():
    # (L1,R1) match; the negative L2 sees positive rights only, while R2 has
    # same-sign counterparts but no equal-sum combination
    body = post_diag(
        [txn("L1", 100), txn("L2", -50)],
        [txn("R1", 100), txn("R2", 200)],
    )
    assert body["unmatched_left_ids"] == ["L2"]
    assert body["unmatched_right_ids"] == ["R2"]
    assert body["diagnostics"] == [
        diag("L2", "left", "sign_mismatch"),
        diag("R2", "right", "amount_mismatch"),
    ]


# --------------------------------------------------------------------- #
# amount_mismatch
# --------------------------------------------------------------------- #
def test_amount_mismatch_one_to_one():
    body = post_diag([txn("L1", 100)], [txn("R1", 101)])
    assert body["diagnostics"] == [
        diag("L1", "left", "amount_mismatch"),
        diag("R1", "right", "amount_mismatch"),
    ]


def test_amount_mismatch_covers_one_to_many_combinations():
    # same sign everywhere, but no subset sum is equal across the sides
    body = post_diag(
        [txn("L1", 100), txn("L2", 200)],
        [txn("R1", 150), txn("R2", 400)],
    )
    assert body["diagnostics"] == [
        diag("L1", "left", "amount_mismatch"),
        diag("L2", "left", "amount_mismatch"),
        diag("R1", "right", "amount_mismatch"),
        diag("R2", "right", "amount_mismatch"),
    ]


def test_amount_mismatch_when_no_combination_sums_equal():
    # L1+R1 match; neither L2 nor R2 can complete an equal-sum combination
    body = post_diag(
        [txn("L1", 100), txn("L2", 50)],
        [txn("R1", 100), txn("R2", 75)],
    )
    assert body["diagnostics"] == [
        diag("L2", "left", "amount_mismatch"),
        diag("R2", "right", "amount_mismatch"),
    ]


# --------------------------------------------------------------------- #
# time_window
# --------------------------------------------------------------------- #
def test_time_window_beyond_48h():
    body = post_diag([txn("L1", 100, T0)], [txn("R1", 100, "2026-09-19T00:00:01Z")])
    assert body["diagnostics"] == [
        diag("L1", "left", "time_window"),
        diag("R1", "right", "time_window"),
    ]


def test_time_window_boundary_is_legal():
    # exactly 48h apart: the pair is legal, so the optimum matches it
    body = post_diag([txn("L1", 100, T0)], [txn("R1", 100, T_PLUS_48H)])
    assert body["groups"] == [{"left_ids": ["L1"], "right_ids": ["R1"], "amount": 100}]
    assert body["diagnostics"] == []


def test_time_window_uses_matched_counterparty_timestamps():
    # L1 matches R1 (same day); L2 is amount-compatible with R1 but more than
    # 48h away from it -- matched transactions still clock the window
    body = post_diag(
        [txn("L1", 100, T0), txn("L2", 100, "2026-09-21T00:00:01Z")],
        [txn("R1", 100, T0)],
    )
    assert body["unmatched_left_ids"] == ["L2"]
    assert body["diagnostics"] == [diag("L2", "left", "time_window")]


# --------------------------------------------------------------------- #
# excluded_pair
# --------------------------------------------------------------------- #
def test_excluded_pair_only_reason():
    resp = client.post(
        "/reconcile/diagnostics",
        json={
            "left": [txn("L1", 100)],
            "right": [txn("R1", 100)],
            "excluded_pairs": [excluded("L1", "R1")],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["groups"] == []
    assert body["diagnostics"] == [
        diag("L1", "left", "excluded_pair"),
        diag("R1", "right", "excluded_pair"),
    ]


def test_excluded_pair_inside_one_to_many_group():
    resp = client.post(
        "/reconcile/diagnostics",
        json={
            "left": [txn("L1", 300)],
            "right": [txn("R1", 100), txn("R2", 200)],
            "excluded_pairs": [excluded("L1", "R2")],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["groups"] == []
    assert body["diagnostics"] == [
        diag("L1", "left", "excluded_pair"),
        diag("R1", "right", "excluded_pair"),
        diag("R2", "right", "excluded_pair"),
    ]


def test_excluded_pair_beats_optimization_conflict():
    # L1's only legal combination contains the excluded (L1, R1) pair, so the
    # exclusion -- not the lost optimisation -- is the reported reason
    resp = client.post(
        "/reconcile/diagnostics",
        json={
            "left": [txn("L1", 100), txn("L2", 100)],
            "right": [txn("R1", 100)],
            "excluded_pairs": [excluded("L1", "R1")],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["groups"] == [{"left_ids": ["L2"], "right_ids": ["R1"], "amount": 100}]
    assert body["diagnostics"] == [diag("L1", "left", "excluded_pair")]


# --------------------------------------------------------------------- #
# optimization_conflict
# --------------------------------------------------------------------- #
def test_optimization_conflict_lists_legal_counterparts():
    # (A,X) and (B,X) are both legal; the optimum can only take one
    body = post_diag([txn("A", 100), txn("B", 100)], [txn("X", 100)])
    assert body["total_amount"] == 100
    assert len(body["diagnostics"]) == 1
    conflict = body["diagnostics"][0]
    assert conflict["reason"] == "optimization_conflict"
    assert conflict["candidates"] == ["X"]
    # the loser of the lex tie-break is the one left unmatched
    assert body["unmatched_left_ids"] == [conflict["id"]]


def test_optimization_conflict_candidates_follow_input_order():
    # Z precedes X in the right side's input; the candidate list keeps that
    # order instead of sorting alphabetically
    body = post_diag(
        [txn("A", 100), txn("B", 100), txn("C", 100)],
        [txn("Z", 100), txn("X", 100)],
    )
    assert body["unmatched_left_ids"] == ["C"]
    assert body["diagnostics"] == [
        diag("C", "left", "optimization_conflict", ["Z", "X"])
    ]


def test_optimization_conflict_reports_all_legal_counterparts():
    # optimum: (A,X),(B,Z); C can legally pair with X or Z, W with nothing
    body = post_diag(
        [txn("A", 100), txn("B", 100), txn("C", 100)],
        [txn("X", 100), txn("Z", 100), txn("W", 7)],
    )
    assert body["unmatched_left_ids"] == ["C"]
    assert body["unmatched_right_ids"] == ["W"]
    assert body["diagnostics"] == [
        diag("C", "left", "optimization_conflict", ["X", "Z"]),
        diag("W", "right", "amount_mismatch"),
    ]


def test_optimization_conflict_candidates_from_one_to_many():
    # L1 (300) can legally pair with R1+R2 or with R3; the optimum takes the
    # one-to-many group and R3 reports L1 as its legal counterpart
    body = post_diag(
        [txn("L1", 300)],
        [txn("R1", 100), txn("R2", 200), txn("R3", 300)],
    )
    assert body["groups"] == [
        {"left_ids": ["L1"], "right_ids": ["R1", "R2"], "amount": 300}
    ]
    assert body["diagnostics"] == [
        diag("R3", "right", "optimization_conflict", ["L1"])
    ]


def test_optimization_conflict_candidates_deduplicated():
    # R3 joins two different legal 1-3 combinations with L1; L1 is listed once
    body = post_diag(
        [txn("L1", 100)],
        [txn("R1", 50), txn("R2", 50), txn("R3", 25), txn("R4", 25)],
    )
    assert body["groups"] == [
        {"left_ids": ["L1"], "right_ids": ["R1", "R2"], "amount": 100}
    ]
    assert body["diagnostics"] == [
        diag("R3", "right", "optimization_conflict", ["L1"]),
        diag("R4", "right", "optimization_conflict", ["L1"]),
    ]


def test_matched_transactions_may_complete_a_diagnostic_combination():
    # L2 is unmatched; its only legal pair R1 is matched to L1, which is
    # exactly the optimisation conflict being reported
    body = post_diag([txn("L1", 100), txn("L2", 100)], [txn("R1", 100)])
    assert body["diagnostics"] == [
        diag("L2", "left", "optimization_conflict", ["R1"])
    ]


# --------------------------------------------------------------------- #
# locked groups
# --------------------------------------------------------------------- #
def test_locked_group_transactions_are_not_diagnosed():
    resp = client.post(
        "/reconcile/diagnostics",
        json={
            "left": [txn("A", 100), txn("B", 7)],
            "right": [txn("X", 100)],
            "locked_groups": [lock(["A"], ["X"])],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["groups"] == [{"left_ids": ["A"], "right_ids": ["X"], "amount": 100}]
    assert body["diagnostics"] == [diag("B", "left", "no_counterparty")]


# --------------------------------------------------------------------- #
# validation behaviour is identical to /reconcile
# --------------------------------------------------------------------- #
def test_duplicate_ids_return_400():
    resp = client.post(
        "/reconcile/diagnostics",
        json={"left": [txn("X", 1), txn("X", 2)], "right": []},
    )
    assert resp.status_code == 400


def test_invalid_locked_group_returns_422():
    resp = client.post(
        "/reconcile/diagnostics",
        json={
            "left": [txn("A", 100)],
            "right": [txn("X", 101)],
            "locked_groups": [lock(["A"], ["X"])],
        },
    )
    assert resp.status_code == 422
    assert resp.json()["detail"][0]["loc"] == ["body", "locked_groups", 0]


def test_invalid_excluded_pair_returns_422():
    resp = client.post(
        "/reconcile/diagnostics",
        json={
            "left": [txn("A", 100)],
            "right": [txn("X", 100)],
            "excluded_pairs": [excluded("NOPE", "X")],
        },
    )
    assert resp.status_code == 422
    assert resp.json()["detail"][0]["loc"] == ["body", "excluded_pairs", 0]


def test_batch_schema_validation_reused():
    for bad_left in (
        [txn("L1", 0)],  # zero amount
        [txn("L1", 1.5)],  # non-integer amount
        [txn("", 1)],  # empty id
        [txn("L1", 1, "not-a-time")],  # bad timestamp
        [txn(f"L{i}", 1) for i in range(13)],  # more than 12 per side
    ):
        resp = client.post(
            "/reconcile/diagnostics", json={"left": bad_left, "right": []}
        )
        assert resp.status_code == 422, bad_left


def test_reconcile_endpoint_unchanged_by_diagnostics_route():
    resp = client.post(
        "/reconcile", json={"left": [txn("L1", 100)], "right": [txn("R1", 100)]}
    )
    assert resp.status_code == 200
    assert resp.json() == {
        "groups": [{"left_ids": ["L1"], "right_ids": ["R1"], "amount": 100}],
        "total_amount": 100,
        "unmatched_left_ids": [],
        "unmatched_right_ids": [],
    }


# --------------------------------------------------------------------- #
# solver-level differential check against the candidate pool
# --------------------------------------------------------------------- #
@pytest.mark.parametrize("seed", range(100))
def test_diagnostics_against_candidate_pool(seed: int):
    """Without locked groups, ``optimization_conflict`` must hold exactly for
    the unmatched transactions that appear in at least one legal candidate,
    and the reported candidates must be the opposite-side ids co-occurring
    with the transaction in the candidate pool (input order, deduplicated)."""
    rng = random.Random(40_000 + seed)
    n_left = rng.randint(0, 5)
    n_right = rng.randint(0, 5)
    left = [
        Txn(f"L{i}", BASE_TS + rng.randint(0, 4 * 86400),
            rng.choice([-300, -200, -100, -50, 50, 100, 200, 300]))
        for i in range(n_left)
    ]
    right = [
        Txn(f"R{i}", BASE_TS + rng.randint(0, 4 * 86400),
            rng.choice([-300, -200, -100, -50, 50, 100, 200, 300]))
        for i in range(n_right)
    ]
    rec = Reconciler(left, right)
    solution = rec.solve()
    matched = {i for c in solution for i in c.left_ids + c.right_ids}

    diagnostics = diagnose_unmatched(rec, solution)

    # exactly the unmatched transactions, left side first, input order kept
    expected_order = [t.id for t in rec.txns if t.id not in matched]
    assert [d.id for d in diagnostics] == expected_order

    for d in diagnostics:
        t = rec.left_index.get(d.id)
        if t is None:
            t = rec.n_left + rec.right_index[d.id]
        pool = [c for c in rec.candidates if c.mask & (1 << t)]
        if pool:
            assert d.reason == "optimization_conflict"
            opp_ids = (
                [i for c in pool for i in c.right_ids]
                if d.side == "left"
                else [i for c in pool for i in c.left_ids]
            )
            opp_index = rec.right_index if d.side == "left" else rec.left_index
            expected = sorted(set(opp_ids), key=opp_index.get)
            assert list(d.candidates) == expected
        else:
            assert d.reason != "optimization_conflict"
            assert d.candidates == ()
