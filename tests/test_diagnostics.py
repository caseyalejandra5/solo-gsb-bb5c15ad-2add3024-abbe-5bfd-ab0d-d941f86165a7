"""API-level tests for ``POST /reconcile/diagnostics``."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

T0 = "2026-09-17T00:00:00Z"


def txn(id: str, amount: int, time: str = T0) -> dict:
    return {"id": id, "time": time, "amount": amount}


def post_diag(body: dict):
    resp = client.post("/reconcile/diagnostics", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def diag_map(body: dict) -> dict[str, str]:
    return {entry["transaction_id"]: entry["reason"] for entry in body["diagnostics"]}


def sides(body: dict) -> dict[str, str]:
    return {entry["transaction_id"]: entry["side"] for entry in body["diagnostics"]}


# --------------------------------------------------------------------- #
# standard reconciliation fields
# --------------------------------------------------------------------- #
def test_empty_batch_has_empty_diagnostics():
    assert post_diag({"left": [], "right": []}) == {
        "groups": [],
        "total_amount": 0,
        "unmatched_left_ids": [],
        "unmatched_right_ids": [],
        "diagnostics": [],
    }


def test_standard_fields_match_reconcile():
    payload = {
        "left": [txn("A", 100), txn("B", 50)],
        "right": [txn("X", 150), txn("Y", 100)],
    }
    diag = post_diag(payload)
    recon = client.post("/reconcile", json=payload).json()
    for field in ("groups", "total_amount", "unmatched_left_ids", "unmatched_right_ids"):
        assert diag[field] == recon[field]
    assert diag["groups"] == [
        {"left_ids": ["A", "B"], "right_ids": ["X"], "amount": 150}
    ]
    assert diag["unmatched_right_ids"] == ["Y"]


def test_diagnostics_cover_exactly_the_unmatched():
    body = post_diag(
        {
            "left": [txn("A", 100), txn("B", 50), txn("Lone", 7)],
            "right": [txn("X", 150), txn("Y", 100), txn("Rone", 8)],
        }
    )
    ids = {e["transaction_id"] for e in body["diagnostics"]}
    assert ids == set(body["unmatched_left_ids"] + body["unmatched_right_ids"])
    # A, B, X are matched and must not appear
    assert ids.isdisjoint({"A", "B", "X"})


# --------------------------------------------------------------------- #
# reason codes
# --------------------------------------------------------------------- #
def test_no_counterparty_when_opposite_side_empty():
    body = post_diag({"left": [txn("L1", 100)], "right": []})
    assert diag_map(body) == {"L1": "no_counterparty"}

    body = post_diag({"left": [], "right": [txn("R1", 100)]})
    assert diag_map(body) == {"R1": "no_counterparty"}


def test_no_counterparty_when_only_counterparty_is_locked():
    body = post_diag(
        {
            "left": [txn("A", 100), txn("B", 100)],
            "right": [txn("X", 100)],
            "locked_groups": [{"left_ids": ["A"], "right_ids": ["X"]}],
        }
    )
    assert diag_map(body) == {"B": "no_counterparty"}


def test_sign_mismatch():
    body = post_diag({"left": [txn("L1", 100)], "right": [txn("R1", -100)]})
    reasons = diag_map(body)
    assert reasons == {"L1": "sign_mismatch", "R1": "sign_mismatch"}


def test_sign_mismatch_one_to_many_enumeration():
    # only one-to-many shapes are available; all mix signs
    body = post_diag(
        {
            "left": [txn("L1", 300)],
            "right": [txn("R1", -100), txn("R2", -200), txn("R3", -300)],
        }
    )
    assert diag_map(body)["L1"] == "sign_mismatch"


def test_sign_filter_takes_precedence_over_amount_filter():
    # sums differ AND signs differ -> sign_mismatch is reported first
    body = post_diag({"left": [txn("L1", 100)], "right": [txn("R1", -101)]})
    assert diag_map(body)["L1"] == "sign_mismatch"


def test_amount_mismatch():
    body = post_diag({"left": [txn("L1", 100)], "right": [txn("R1", 101)]})
    reasons = diag_map(body)
    assert reasons == {"L1": "amount_mismatch", "R1": "amount_mismatch"}


def test_amount_mismatch_same_sign_counterparties():
    body = post_diag(
        {
            "left": [txn("L1", 100)],
            "right": [txn("R1", 50), txn("R2", 60)],
        }
    )
    assert diag_map(body)["L1"] == "amount_mismatch"


def test_time_window():
    body = post_diag(
        {
            "left": [txn("L1", 100, T0)],
            "right": [txn("R1", 100, "2026-09-20T00:00:00Z")],
        }
    )
    reasons = diag_map(body)
    assert reasons == {"L1": "time_window", "R1": "time_window"}


def test_time_window_boundary_is_not_a_reason():
    # exactly 48 hours apart still groups
    body = post_diag(
        {
            "left": [txn("L1", 100, T0)],
            "right": [txn("R1", 100, "2026-09-19T00:00:00Z")],
        }
    )
    assert body["diagnostics"] == []


def test_time_window_whole_group_spans_too_far():
    # the only equal-sum shape is 1-2; the whole group spans >48h
    body = post_diag(
        {
            "left": [txn("L1", 300, T0)],
            "right": [
                txn("R1", 100, "2026-09-20T00:00:00Z"),
                txn("R2", 200, "2026-09-21T00:00:00Z"),
            ],
        }
    )
    assert diag_map(body)["L1"] == "time_window"


def test_excluded_pair_reason():
    body = post_diag(
        {
            "left": [txn("L1", 100)],
            "right": [txn("R1", 100)],
            "excluded_pairs": [{"left_id": "L1", "right_id": "R1"}],
        }
    )
    reasons = diag_map(body)
    assert reasons == {"L1": "excluded_pair", "R1": "excluded_pair"}


def test_excluded_pair_reason_one_to_many():
    # (L1, R1+R2) is the only shape; both possible pairs touching L1 excluded
    body = post_diag(
        {
            "left": [txn("L1", 300)],
            "right": [txn("R1", 100), txn("R2", 200)],
            "excluded_pairs": [
                {"left_id": "L1", "right_id": "R1"},
                {"left_id": "L1", "right_id": "R2"},
            ],
        }
    )
    assert diag_map(body)["L1"] == "excluded_pair"


def test_excluded_pair_filter_runs_after_time_window():
    # equal-sum combinations are both excluded and outside 48h; the time
    # window filter empties the set first
    body = post_diag(
        {
            "left": [txn("L1", 100, T0)],
            "right": [txn("R1", 100, "2026-09-20T00:00:00Z")],
            "excluded_pairs": [{"left_id": "L1", "right_id": "R1"}],
        }
    )
    assert diag_map(body)["L1"] == "time_window"


def test_optimization_conflict_legal_group_not_chosen():
    # optimum is (A+B, X)=150; Y could legally group with A but stays unmatched
    body = post_diag(
        {
            "left": [txn("A", 100), txn("B", 50)],
            "right": [txn("X", 150), txn("Y", 100)],
        }
    )
    assert diag_map(body) == {"Y": "optimization_conflict"}


def test_optimization_conflict_losing_side_of_amount_tradeoff():
    # Z=100 can pair with C=100, but C is needed for the bigger (B+C,Y)=150;
    # Z is avoidably unmatched in every amount-optimal solution
    body = post_diag(
        {
            "left": [txn("A", 100), txn("B", 50), txn("C", 100)],
            "right": [txn("X", 100), txn("Y", 150), txn("Z", 100)],
        }
    )
    assert body["groups"] == [
        {"left_ids": ["A"], "right_ids": ["X"], "amount": 100},
        {"left_ids": ["B", "C"], "right_ids": ["Y"], "amount": 150},
    ]
    assert diag_map(body) == {"Z": "optimization_conflict"}


def test_optimization_conflict_when_matched_txns_are_counterparties():
    # the unmatched transaction's only legal groups use transactions consumed
    # by the optimum -- those are still enumerated, so the reason is conflict,
    # not no_counterparty/amount_mismatch
    body = post_diag(
        {
            "left": [txn("A", 100)],
            "right": [txn("X", 100), txn("Y", 100)],
        }
    )
    # lex tie-break pairs A with X; Y has the fully legal (A, Y)
    assert diag_map(body) == {"Y": "optimization_conflict"}


# --------------------------------------------------------------------- #
# ordering: left first then right, each side in input order
# --------------------------------------------------------------------- #
def test_diagnostics_ordering_left_then_right_input_order():
    body = post_diag(
        {
            # L2 matches R2; L1 and R1 stay unmatched (amounts differ)
            "left": [txn("L2", 9), txn("L1", 8)],
            "right": [txn("R2", 9), txn("R1", 7)],
        }
    )
    entries = body["diagnostics"]
    assert [e["transaction_id"] for e in entries] == ["L1", "R1"]
    assert [e["side"] for e in entries] == ["left", "right"]
    # unmatched id lists stay lexicographically sorted independently
    assert body["unmatched_left_ids"] == ["L1"]
    assert body["unmatched_right_ids"] == ["R1"]


def test_diagnostics_ordering_preserves_input_order_within_side():
    body = post_diag(
        {
            "left": [txn("z9", 1), txn("a8", 2), txn("m7", 3)],
            "right": [txn("Z9", 9), txn("A8", 8), txn("M7", 7)],
        }
    )
    entries = body["diagnostics"]
    assert [e["transaction_id"] for e in entries] == [
        "z9",
        "a8",
        "m7",
        "Z9",
        "A8",
        "M7",
    ]
    assert all(e["side"] == "left" for e in entries[:3])
    assert all(e["side"] == "right" for e in entries[3:])
    # the unmatched id lists are still sorted independently
    assert body["unmatched_left_ids"] == ["a8", "m7", "z9"]
    assert body["unmatched_right_ids"] == ["A8", "M7", "Z9"]


# --------------------------------------------------------------------- #
# locked groups and excluded pairs interact with diagnostics
# --------------------------------------------------------------------- #
def test_locked_remainder_diagnostics():
    body = post_diag(
        {
            "left": [txn("A", 100), txn("B", 50), txn("C", 100)],
            "right": [txn("X", 100), txn("Y", 150), txn("Z", 100)],
            "locked_groups": [{"left_ids": ["A"], "right_ids": ["X"]}],
        }
    )
    assert body["groups"][0] == {"left_ids": ["A"], "right_ids": ["X"], "amount": 100}
    # the free optimum (B+C, Y)=150 leaves Z unmatched with a legal (C, Z)
    assert diag_map(body) == {"Z": "optimization_conflict"}


def test_locked_transactions_never_act_as_counterparties():
    # A is locked with X and therefore unavailable to B; with A hidden,
    # B=50 has no same-sign equal-sum combination against the free rights,
    # and the free rights Y=150 / Z=99 cannot be matched either.
    body = post_diag(
        {
            "left": [txn("A", 100), txn("B", 50)],
            "right": [txn("X", 100), txn("Y", 150), txn("Z", 99)],
            "locked_groups": [{"left_ids": ["A"], "right_ids": ["X"]}],
        }
    )
    reasons = diag_map(body)
    assert reasons["B"] == "amount_mismatch"
    assert reasons["Y"] == "amount_mismatch"
    assert reasons["Z"] == "amount_mismatch"


def test_excluded_pair_reroutes_then_conflict():
    # forbidding (A, X) forces (A, Y); B and X each keep fully legal
    # alternatives ((A, X) / (B, X)) that the optimum does not select
    body = post_diag(
        {
            "left": [txn("A", 100), txn("B", 100)],
            "right": [txn("X", 100), txn("Y", 100)],
            "excluded_pairs": [{"left_id": "A", "right_id": "X"}],
        }
    )
    assert body["groups"] == [
        {"left_ids": ["A"], "right_ids": ["Y"], "amount": 100},
        {"left_ids": ["B"], "right_ids": ["X"], "amount": 100},
    ]
    # everyone is matched -> no diagnostics
    assert body["diagnostics"] == []


# --------------------------------------------------------------------- #
# validation parity with POST /reconcile
# --------------------------------------------------------------------- #
def test_duplicate_ids_return_400():
    resp = client.post(
        "/reconcile/diagnostics",
        json={"left": [txn("X", 1), txn("X", 2)], "right": []},
    )
    assert resp.status_code == 400


def test_locked_group_invalid_returns_422():
    resp = client.post(
        "/reconcile/diagnostics",
        json={
            "left": [txn("A", 100)],
            "right": [txn("X", 101)],
            "locked_groups": [{"left_ids": ["A"], "right_ids": ["X"]}],
        },
    )
    assert resp.status_code == 422
    assert resp.json()["detail"][0]["loc"] == ["body", "locked_groups", 0]


def test_excluded_pair_invalid_returns_422():
    resp = client.post(
        "/reconcile/diagnostics",
        json={
            "left": [txn("A", 100)],
            "right": [txn("X", 100)],
            "excluded_pairs": [{"left_id": "NOPE", "right_id": "X"}],
        },
    )
    assert resp.status_code == 422
    err = resp.json()["detail"][0]
    assert err["loc"] == ["body", "excluded_pairs", 0]
    assert err["type"] == "excluded_pair.invalid"


def test_schema_validation_identical_to_reconcile():
    # more than 12 per side, zero amount and bad time are all 422
    resp = client.post(
        "/reconcile/diagnostics",
        json={"left": [txn(f"L{i}", 1) for i in range(13)], "right": []},
    )
    assert resp.status_code == 422
    resp = client.post(
        "/reconcile/diagnostics", json={"left": [txn("L0", 0)], "right": []}
    )
    assert resp.status_code == 422
    resp = client.post(
        "/reconcile/diagnostics", json={"left": [txn("L0", 1, "nope")], "right": []}
    )
    assert resp.status_code == 422


def test_no_idempotency_headers():
    # the endpoint ignores Idempotency-Key: it solves on every call and never
    # emits replay headers (same convention as audit/unmatched-summary)
    payload = {"left": [txn("L1", 100)], "right": []}
    resp = client.post(
        "/reconcile/diagnostics", json=payload, headers={"Idempotency-Key": "diag-1"}
    )
    assert resp.status_code == 200
    assert "Idempotency-Replayed" not in resp.headers
