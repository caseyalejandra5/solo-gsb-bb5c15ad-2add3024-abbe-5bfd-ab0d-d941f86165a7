"""API-level tests for the reconciliation endpoint."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

T0 = "2026-09-17T00:00:00Z"


def txn(id: str, amount: int, time: str = T0) -> dict:
    return {"id": id, "time": time, "amount": amount}


def post(left, right):
    resp = client.post("/reconcile", json={"left": left, "right": right})
    assert resp.status_code == 200, resp.text
    return resp.json()


def post_locked(left, right, locked):
    return client.post(
        "/reconcile", json={"left": left, "right": right, "locked_groups": locked}
    )


def post_excluded(left, right, excluded):
    return client.post(
        "/reconcile", json={"left": left, "right": right, "excluded_pairs": excluded}
    )


def post_locked_excluded(left, right, locked, excluded):
    return client.post(
        "/reconcile",
        json={
            "left": left,
            "right": right,
            "locked_groups": locked,
            "excluded_pairs": excluded,
        },
    )


def lock(left_ids, right_ids):
    return {"left_ids": list(left_ids), "right_ids": list(right_ids)}


def excluded(left_id, right_id):
    return {"left_id": left_id, "right_id": right_id}


# --------------------------------------------------------------------- #
# basic matching
# --------------------------------------------------------------------- #
def test_health():
    assert client.get("/health").json() == {"status": "ok"}


def test_empty_batch():
    assert post([], []) == {
        "groups": [],
        "total_amount": 0,
        "unmatched_left_ids": [],
        "unmatched_right_ids": [],
    }


def test_one_to_one():
    body = post([txn("L1", 100)], [txn("R1", 100)])
    assert body["groups"] == [{"left_ids": ["L1"], "right_ids": ["R1"], "amount": 100}]
    assert body["total_amount"] == 100
    assert body["unmatched_left_ids"] == []
    assert body["unmatched_right_ids"] == []


def test_one_to_many_shapes():
    # 1 left vs 2 right
    body = post([txn("L1", 300)], [txn("R1", 100), txn("R2", 200)])
    assert body["groups"] == [{"left_ids": ["L1"], "right_ids": ["R1", "R2"], "amount": 300}]
    # 3 left vs 1 right
    body = post([txn("L1", 100), txn("L2", 200), txn("L3", 50)], [txn("R1", 350)])
    assert body["groups"] == [{"left_ids": ["L1", "L2", "L3"], "right_ids": ["R1"], "amount": 350}]
    # 2 left vs 1 right and 1 left vs 3 right
    body = post([txn("L1", 60), txn("L2", 40)], [txn("R1", 100)])
    assert body["groups"] == [{"left_ids": ["L1", "L2"], "right_ids": ["R1"], "amount": 100}]
    body = post([txn("L1", 600)], [txn("R1", 100), txn("R2", 200), txn("R3", 300)])
    assert body["groups"] == [{"left_ids": ["L1"], "right_ids": ["R1", "R2", "R3"], "amount": 600}]


def test_negative_amounts_group():
    body = post([txn("L1", -150)], [txn("R1", -100), txn("R2", -50)])
    assert body["groups"] == [{"left_ids": ["L1"], "right_ids": ["R1", "R2"], "amount": -150}]
    assert body["total_amount"] == 150


def test_mixed_signs_form_separate_groups():
    # same-sign groups only: L1+R1 (positive) and L2+R2 (negative)
    body = post([txn("L1", 100), txn("L2", -100)], [txn("R1", 100), txn("R2", -100)])
    assert body["groups"] == [
        {"left_ids": ["L1"], "right_ids": ["R1"], "amount": 100},
        {"left_ids": ["L2"], "right_ids": ["R2"], "amount": -100},
    ]
    assert body["total_amount"] == 200


def test_cross_sign_group_not_allowed():
    # L1=100, R1=-100 can never form a group (sums differ and signs differ)
    body = post([txn("L1", 100)], [txn("R1", -100)])
    assert body["groups"] == []


def test_sum_mismatch_no_match():
    body = post([txn("L1", 100)], [txn("R1", 101)])
    assert body["groups"] == []
    assert body["total_amount"] == 0
    assert body["unmatched_left_ids"] == ["L1"]
    assert body["unmatched_right_ids"] == ["R1"]


# --------------------------------------------------------------------- #
# 48-hour window
# --------------------------------------------------------------------- #
def test_48h_window_inclusive():
    body = post([txn("L1", 100, T0)], [txn("R1", 100, "2026-09-19T00:00:00Z")])
    assert len(body["groups"]) == 1


def test_beyond_48h_no_match():
    body = post([txn("L1", 100, T0)], [txn("R1", 100, "2026-09-19T00:00:01Z")])
    assert body["groups"] == []


def test_window_spans_whole_group():
    # two right records 48h apart can pair with one left only if the whole
    # group fits inside 48 hours
    body = post(
        [txn("L1", 300, "2026-09-18T00:00:00Z")],
        [txn("R1", 100, T0), txn("R2", 200, "2026-09-19T00:00:00Z")],
    )
    assert len(body["groups"]) == 1
    # pushing R2 one second past the 48h span breaks the group
    body = post(
        [txn("L1", 300, "2026-09-18T00:00:00Z")],
        [txn("R1", 100, T0), txn("R2", 200, "2026-09-19T00:00:01Z")],
    )
    assert body["groups"] == []


def test_timezone_offsets_normalised():
    body = post(
        [txn("L1", 100, "2026-09-17T08:00:00+08:00")],  # == 2026-09-17T00:00:00Z
        [txn("R1", 100, T0)],
    )
    assert len(body["groups"]) == 1


def test_naive_datetime_treated_as_utc():
    body = post([txn("L1", 100, "2026-09-17T00:00:00")], [txn("R1", 100, T0)])
    assert len(body["groups"]) == 1


# --------------------------------------------------------------------- #
# optimality and tie-breaking
# --------------------------------------------------------------------- #
def test_prefers_larger_total_amount():
    # (A,Y)=100 competes with (A+B,X)=150 -> the latter wins
    body = post([txn("A", 100), txn("B", 50)], [txn("X", 150), txn("Y", 100)])
    assert body["groups"] == [{"left_ids": ["A", "B"], "right_ids": ["X"], "amount": 150}]
    assert body["total_amount"] == 150
    assert body["unmatched_right_ids"] == ["Y"]


def test_each_transaction_used_at_most_once():
    # X could pair with A or B, not both
    body = post([txn("A", 100), txn("B", 100)], [txn("X", 100)])
    assert body["total_amount"] == 100
    assert len(body["groups"]) == 1


def test_tie_break_picks_lexicographically_smallest():
    # two optimal solutions: {(A,X),(B,Y)} vs {(A,Y),(B,X)} -> first wins
    body = post([txn("B", 100), txn("A", 100)], [txn("Y", 100), txn("X", 100)])
    assert body["groups"] == [
        {"left_ids": ["A"], "right_ids": ["X"], "amount": 100},
        {"left_ids": ["B"], "right_ids": ["Y"], "amount": 100},
    ]


def test_tie_break_across_group_shapes():
    # (A,[X,Y]) and (A,[Z]) both reconcile 300 -> ([A],[X,Y]) < ([A],[Z])
    body = post(
        [txn("A", 300)],
        [txn("Z", 300), txn("X", 100), txn("Y", 200)],
    )
    assert body["groups"] == [{"left_ids": ["A"], "right_ids": ["X", "Y"], "amount": 300}]


def test_groups_sorted_and_unmatched_listed():
    body = post(
        [txn("L2", 100), txn("L1", 200), txn("L3", 999)],
        [txn("R2", 100), txn("R1", 200), txn("R9", 888)],
    )
    assert [g["left_ids"] for g in body["groups"]] == [["L1"], ["L2"]]
    assert body["unmatched_left_ids"] == ["L3"]
    assert body["unmatched_right_ids"] == ["R9"]


# --------------------------------------------------------------------- #
# manually confirmed (locked) groups
# --------------------------------------------------------------------- #
def test_locked_group_forced_into_solution():
    # without a lock the (A+B,X)=150 group wins; locking (A,Y)=100 forces it
    resp = post_locked(
        [txn("A", 100), txn("B", 50)],
        [txn("X", 150), txn("Y", 100)],
        [lock(["A"], ["Y"])],
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["groups"] == [{"left_ids": ["A"], "right_ids": ["Y"], "amount": 100}]
    assert body["total_amount"] == 100
    assert body["unmatched_left_ids"] == ["B"]
    assert body["unmatched_right_ids"] == ["X"]


def test_locked_group_one_to_many_shapes():
    resp = post_locked(
        [txn("L1", 300)],
        [txn("R1", 100), txn("R2", 200)],
        [lock(["L1"], ["R1", "R2"])],
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["groups"] == [
        {"left_ids": ["L1"], "right_ids": ["R1", "R2"], "amount": 300}
    ]

    resp = post_locked(
        [txn("L1", 100), txn("L2", 200), txn("L3", 50)],
        [txn("R1", 350)],
        [lock(["L1", "L2", "L3"], ["R1"])],
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["groups"] == [
        {"left_ids": ["L1", "L2", "L3"], "right_ids": ["R1"], "amount": 350}
    ]


def test_locked_negative_group():
    resp = post_locked(
        [txn("L1", -150)],
        [txn("R1", -100), txn("R2", -50)],
        [lock(["L1"], ["R1", "R2"])],
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["groups"] == [
        {"left_ids": ["L1"], "right_ids": ["R1", "R2"], "amount": -150}
    ]
    assert body["total_amount"] == 150


def test_multiple_locked_groups_kept():
    resp = post_locked(
        [txn("A", 100), txn("B", 100)],
        [txn("X", 100), txn("Y", 100)],
        [lock(["A"], ["X"]), lock(["B"], ["Y"])],
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["total_amount"] == 200
    assert len(resp.json()["groups"]) == 2


def test_locked_group_remainder_still_optimised():
    # (A,X) is locked; among the rest the bigger 150 group wins over (B,Z)=100
    resp = post_locked(
        [txn("A", 100), txn("B", 50), txn("C", 100)],
        [txn("X", 100), txn("Y", 150), txn("Z", 100)],
        [lock(["A"], ["X"])],
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["groups"] == [
        {"left_ids": ["A"], "right_ids": ["X"], "amount": 100},
        {"left_ids": ["B", "C"], "right_ids": ["Y"], "amount": 150},
    ]
    assert body["total_amount"] == 250
    assert body["unmatched_right_ids"] == ["Z"]


def test_locked_group_tie_break_applies_to_remainder():
    # locked (A,Y) removes A,Y; the remaining optimum over B/X is fixed.
    # Here the lex tie-break on the free pool chooses (B,X) — the only option.
    # Swap: with (A,X) locked, (B,Y) is the free choice.
    resp = post_locked(
        [txn("A", 100), txn("B", 100)],
        [txn("X", 100), txn("Y", 100)],
        [lock(["A"], ["Y"])],
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["groups"] == [
        {"left_ids": ["A"], "right_ids": ["Y"], "amount": 100},
        {"left_ids": ["B"], "right_ids": ["X"], "amount": 100},
    ]


def test_locked_group_sits_alongside_lex_smaller_free_group():
    # the locked encoding can sort after a free group; output stays sorted
    resp = post_locked(
        [txn("Z", 100), txn("A", 100)],
        [txn("Q", 100), txn("X", 100)],
        [lock(["Z"], ["Q"])],
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["groups"] == [
        {"left_ids": ["A"], "right_ids": ["X"], "amount": 100},
        {"left_ids": ["Z"], "right_ids": ["Q"], "amount": 100},
    ]


def test_total_and_unmatched_computed_from_merged_result():
    # locked (L1,R1) plus leftovers that cannot reconcile
    resp = post_locked(
        [txn("L1", 100), txn("L2", 7)],
        [txn("R1", 100), txn("R2", 9)],
        [lock(["L1"], ["R1"])],
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["groups"] == [{"left_ids": ["L1"], "right_ids": ["R1"], "amount": 100}]
    assert body["total_amount"] == 100
    assert body["unmatched_left_ids"] == ["L2"]
    assert body["unmatched_right_ids"] == ["R2"]


def test_locked_group_unknown_id_422_with_index():
    resp = post_locked(
        [txn("A", 100)],
        [txn("X", 100)],
        [lock(["A"], ["NOPE"])],
    )
    assert resp.status_code == 422
    err = resp.json()["detail"][0]
    assert err["loc"] == ["body", "locked_groups", 0]
    assert "NOPE" in err["msg"]


def test_locked_group_wrong_side_reference_422():
    # a left id used in right_ids counts as an unknown reference
    resp = post_locked(
        [txn("A", 100)],
        [txn("X", 100)],
        [lock(["A"], ["A"])],
    )
    assert resp.status_code == 422
    assert resp.json()["detail"][0]["loc"] == ["body", "locked_groups", 0]


def test_locked_group_reused_across_groups_422_with_index():
    resp = post_locked(
        [txn("A", 100), txn("B", 100)],
        [txn("X", 100), txn("Y", 100)],
        [lock(["A"], ["X"]), lock(["A"], ["Y"])],
    )
    assert resp.status_code == 422
    err = resp.json()["detail"][0]
    assert err["loc"] == ["body", "locked_groups", 1]
    assert "reused" in err["msg"]


def test_locked_group_duplicate_id_inside_group_422():
    resp = post_locked(
        [txn("A", 100)],
        [txn("X", 100)],
        [lock(["A", "A"], ["X"])],
    )
    assert resp.status_code == 422
    err = resp.json()["detail"][0]
    assert err["loc"] == ["body", "locked_groups", 0]
    assert "duplicate" in err["msg"]


def test_locked_group_first_error_reported():
    # group 0 is fine; group 2 breaks first; group 1 references the later group
    resp = post_locked(
        [txn("A", 100), txn("B", 100)],
        [txn("X", 100), txn("Y", 100)],
        [
            lock(["A"], ["X"]),
            lock(["B"], ["MISSING"]),
            lock(["B"], ["Y"]),
        ],
    )
    assert resp.status_code == 422
    assert resp.json()["detail"][0]["loc"] == ["body", "locked_groups", 1]


def test_locked_group_bad_shape_422():
    # 2-2 is not one of the five legal shapes
    resp = post_locked(
        [txn("A", 1), txn("B", 1)],
        [txn("X", 1), txn("Y", 1)],
        [lock(["A", "B"], ["X", "Y"])],
    )
    assert resp.status_code == 422
    assert resp.json()["detail"][0]["loc"] == ["body", "locked_groups", 0]


def test_locked_group_side_too_long_422():
    # a 4-record side is rejected at schema level
    resp = post_locked(
        [txn("A", 1), txn("B", 1), txn("C", 1), txn("D", 1)],
        [txn("X", 1)],
        [lock(["A", "B", "C", "D"], ["X"])],
    )
    assert resp.status_code == 422
    assert "locked_groups" in resp.json()["detail"][0]["loc"]


def test_locked_group_sum_mismatch_422():
    resp = post_locked(
        [txn("A", 100)],
        [txn("X", 101)],
        [lock(["A"], ["X"])],
    )
    assert resp.status_code == 422
    assert resp.json()["detail"][0]["loc"] == ["body", "locked_groups", 0]


def test_locked_group_mixed_sign_422():
    resp = post_locked(
        [txn("A", 100)],
        [txn("X", -100)],
        [lock(["A"], ["X"])],
    )
    assert resp.status_code == 422
    assert resp.json()["detail"][0]["loc"] == ["body", "locked_groups", 0]


def test_locked_group_beyond_48h_422():
    resp = post_locked(
        [txn("A", 100, T0)],
        [txn("X", 100, "2026-09-19T00:00:01Z")],
        [lock(["A"], ["X"])],
    )
    assert resp.status_code == 422
    assert resp.json()["detail"][0]["loc"] == ["body", "locked_groups", 0]


def test_locked_groups_48h_boundary_accepted():
    resp = post_locked(
        [txn("A", 100, T0)],
        [txn("X", 100, "2026-09-19T00:00:00Z")],
        [lock(["A"], ["X"])],
    )
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["groups"]) == 1


def test_empty_locked_groups_same_as_absent():
    left, right = [txn("A", 100)], [txn("X", 100)]
    absent = post(left, right)
    empty = post_locked(left, right, [])
    assert empty.status_code == 200
    assert empty.json() == absent
    explicit_null = client.post(
        "/reconcile", json={"left": left, "right": right, "locked_groups": None}
    )
    assert explicit_null.status_code == 200
    assert explicit_null.json() == absent


def test_locked_group_schema_errors_422():
    # missing side, empty side and non-string ids are rejected at schema level
    for bad in (
        {"right_ids": ["X"]},
        {"left_ids": [], "right_ids": ["X"]},
        {"left_ids": ["A"], "right_ids": [1]},
    ):
        resp = post_locked(
            [txn("A", 100)], [txn("X", 100)], [bad]
        )
        assert resp.status_code == 422, bad


# --------------------------------------------------------------------- #
# operator-declared excluded pairs
# --------------------------------------------------------------------- #
def test_excluded_pair_reroutes_one_to_one_match():
    # without exclusion the lex tie-break picks (A,X),(B,Y); forbidding (A,X)
    # forces the equally optimal (A,Y),(B,X)
    resp = post_excluded(
        [txn("A", 100), txn("B", 100)],
        [txn("X", 100), txn("Y", 100)],
        [excluded("A", "X")],
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["groups"] == [
        {"left_ids": ["A"], "right_ids": ["Y"], "amount": 100},
        {"left_ids": ["B"], "right_ids": ["X"], "amount": 100},
    ]
    assert resp.json()["total_amount"] == 200


def test_excluded_pair_deterministic_tie_break():
    # three equal 100s on each side; forbidding (A,X) and (B,Y) leaves exactly
    # one perfect matching, and it must be chosen deterministically
    resp = post_excluded(
        [txn("A", 100), txn("B", 100), txn("C", 100)],
        [txn("X", 100), txn("Y", 100), txn("Z", 100)],
        [excluded("A", "X"), excluded("B", "Y")],
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["groups"] == [
        {"left_ids": ["A"], "right_ids": ["Y"], "amount": 100},
        {"left_ids": ["B"], "right_ids": ["X"], "amount": 100},
        {"left_ids": ["C"], "right_ids": ["Z"], "amount": 100},
    ]


def test_excluded_pair_one_to_many_group_dropped():
    # (L1, R1+R2)=300 is the natural one-to-many group; excluding (L1,R1)
    # removes it, and L1 must instead pair one-to-one with R3
    resp = post_excluded(
        [txn("L1", 300)],
        [txn("R1", 100), txn("R2", 200), txn("R3", 300)],
        [excluded("L1", "R1")],
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["groups"] == [{"left_ids": ["L1"], "right_ids": ["R3"], "amount": 300}]
    assert body["total_amount"] == 300
    assert body["unmatched_right_ids"] == ["R1", "R2"]


def test_excluded_pair_can_reduce_optimum():
    # natural optimum is (A+B,X)=150; forbidding (A,X) destroys that group,
    # leaving only (A,Y)=100 -- B and X stay unmatched
    resp = post_excluded(
        [txn("A", 100), txn("B", 50)],
        [txn("X", 150), txn("Y", 100)],
        [excluded("A", "X")],
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["groups"] == [{"left_ids": ["A"], "right_ids": ["Y"], "amount": 100}]
    assert body["total_amount"] == 100
    assert body["unmatched_left_ids"] == ["B"]
    assert body["unmatched_right_ids"] == ["X"]


def test_excluded_pair_that_can_only_be_unmatched():
    # (L1,R1) is the only legal group and is forbidden -> no groups at all
    resp = post_excluded(
        [txn("L1", 100)], [txn("R1", 100)], [excluded("L1", "R1")]
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["groups"] == []
    assert body["total_amount"] == 0
    assert body["unmatched_left_ids"] == ["L1"]
    assert body["unmatched_right_ids"] == ["R1"]


def test_excluded_pair_applies_to_negative_groups_too():
    resp = post_excluded(
        [txn("L1", -150)],
        [txn("R1", -100), txn("R2", -50), txn("R3", -150)],
        [excluded("L1", "R1")],
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["groups"] == [
        {"left_ids": ["L1"], "right_ids": ["R3"], "amount": -150}
    ]


def test_irrelevant_excluded_pair_keeps_result():
    # the pair could never group legally (wrong amounts); result is unchanged
    left, right = [txn("A", 100), txn("B", 100)], [txn("X", 100), txn("Y", 100)]
    baseline = post(left, right)
    resp = post_excluded(left, right, [excluded("A", "Y")])
    assert resp.status_code == 200
    assert resp.json() == baseline


def test_empty_or_absent_excluded_pairs_same_result():
    left, right = [txn("A", 100), txn("B", 100)], [txn("X", 100), txn("Y", 100)]
    baseline = post(left, right)
    empty = post_excluded(left, right, [])
    assert empty.status_code == 200
    assert empty.json() == baseline
    explicit_null = client.post(
        "/reconcile", json={"left": left, "right": right, "excluded_pairs": None}
    )
    assert explicit_null.status_code == 200
    assert explicit_null.json() == baseline


def test_locked_group_hitting_excluded_pair_returns_422():
    resp = post_locked_excluded(
        [txn("A", 100)],
        [txn("X", 100)],
        [lock(["A"], ["X"])],
        [excluded("A", "X")],
    )
    assert resp.status_code == 422
    err = resp.json()["detail"][0]
    assert err["loc"] == ["body", "locked_groups", 0]
    assert "excluded pair" in err["msg"]
    assert "A" in err["msg"] and "X" in err["msg"]


def test_locked_group_hitting_excluded_pair_inside_one_to_many():
    # the forbidden pair need not be the whole group; containing it is enough
    resp = post_locked_excluded(
        [txn("L1", 300)],
        [txn("R1", 100), txn("R2", 200)],
        [lock(["L1"], ["R1", "R2"])],
        [excluded("L1", "R2")],
    )
    assert resp.status_code == 422
    err = resp.json()["detail"][0]
    assert err["loc"] == ["body", "locked_groups", 0]
    assert "excluded pair" in err["msg"]


def test_second_locked_group_hitting_excluded_pair_reports_its_index():
    resp = post_locked_excluded(
        [txn("A", 100), txn("B", 100)],
        [txn("X", 100), txn("Y", 100)],
        [lock(["A"], ["X"]), lock(["B"], ["Y"])],
        [excluded("B", "Y")],
    )
    assert resp.status_code == 422
    assert resp.json()["detail"][0]["loc"] == ["body", "locked_groups", 1]


def test_locked_group_coexists_with_unrelated_excluded_pair():
    # (A,X) locked; the (B,Y) exclusion only removes a free candidate, but
    # (B,Y) was the only remaining pairing for B -> B and Y stay unmatched
    resp = post_locked_excluded(
        [txn("A", 100), txn("B", 100)],
        [txn("X", 100), txn("Y", 100)],
        [lock(["A"], ["X"])],
        [excluded("B", "Y")],
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["groups"] == [{"left_ids": ["A"], "right_ids": ["X"], "amount": 100}]
    assert body["total_amount"] == 100
    assert body["unmatched_left_ids"] == ["B"]
    assert body["unmatched_right_ids"] == ["Y"]


def test_locked_group_remainder_optimised_around_excluded_pair():
    # (A,X) locked; among B,C vs Y,Z the 150 group (B+C,Y) wins by amount,
    # and excluding (C,Z) does not disturb it
    resp = post_locked_excluded(
        [txn("A", 100), txn("B", 50), txn("C", 100)],
        [txn("X", 100), txn("Y", 150), txn("Z", 100)],
        [lock(["A"], ["X"])],
        [excluded("C", "Z")],
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["groups"] == [
        {"left_ids": ["A"], "right_ids": ["X"], "amount": 100},
        {"left_ids": ["B", "C"], "right_ids": ["Y"], "amount": 150},
    ]
    assert body["total_amount"] == 250


def test_excluded_pair_unknown_left_id_422_with_index():
    resp = post_excluded(
        [txn("A", 100)], [txn("X", 100)], [excluded("NOPE", "X")]
    )
    assert resp.status_code == 422
    err = resp.json()["detail"][0]
    assert err["loc"] == ["body", "excluded_pairs", 0]
    assert err["type"] == "excluded_pair.invalid"
    assert "NOPE" in err["msg"]


def test_excluded_pair_unknown_right_id_422_with_index():
    resp = post_excluded(
        [txn("A", 100)], [txn("X", 100)], [excluded("A", "NOPE")]
    )
    assert resp.status_code == 422
    err = resp.json()["detail"][0]
    assert err["loc"] == ["body", "excluded_pairs", 0]
    assert "NOPE" in err["msg"]


def test_excluded_pair_wrong_side_left_id_422():
    # X lives on the right but is used as left_id
    resp = post_excluded(
        [txn("A", 100)], [txn("X", 100)], [excluded("X", "X")]
    )
    assert resp.status_code == 422
    err = resp.json()["detail"][0]
    assert err["loc"] == ["body", "excluded_pairs", 0]
    assert "right-side" in err["msg"]


def test_excluded_pair_wrong_side_right_id_422():
    # A lives on the left but is used as right_id
    resp = post_excluded(
        [txn("A", 100)], [txn("X", 100)], [excluded("A", "A")]
    )
    assert resp.status_code == 422
    err = resp.json()["detail"][0]
    assert err["loc"] == ["body", "excluded_pairs", 0]
    assert "left-side" in err["msg"]


def test_excluded_pair_duplicate_422_with_index():
    resp = post_excluded(
        [txn("A", 100)],
        [txn("X", 100), txn("Y", 100)],
        [excluded("A", "X"), excluded("A", "Y"), excluded("A", "X")],
    )
    assert resp.status_code == 422
    err = resp.json()["detail"][0]
    assert err["loc"] == ["body", "excluded_pairs", 2]
    assert "duplicate" in err["msg"]


def test_excluded_pair_first_error_reported():
    # pair 0 is fine; pair 2 breaks first; pair 1 references a missing id
    resp = post_excluded(
        [txn("A", 100)],
        [txn("X", 100)],
        [excluded("A", "X"), excluded("A", "MISSING"), excluded("A", "X")],
    )
    assert resp.status_code == 422
    assert resp.json()["detail"][0]["loc"] == ["body", "excluded_pairs", 1]


def test_excluded_pair_schema_errors_422():
    # missing fields, empty ids and non-string ids are rejected at schema level
    for bad in (
        {"right_id": "X"},
        {"left_id": "A"},
        {"left_id": "", "right_id": "X"},
        {"left_id": "A", "right_id": 1},
    ):
        resp = post_excluded([txn("A", 100)], [txn("X", 100)], [bad])
        assert resp.status_code == 422, bad
        assert "excluded_pairs" in resp.json()["detail"][0]["loc"]


def test_excluded_pair_works_with_idempotency_key():
    # excluded pairs are part of the validated fingerprint: same key with a
    # different exclusion set conflicts
    left, right = [txn("A", 100), txn("B", 100)], [txn("X", 100), txn("Y", 100)]
    first = client.post(
        "/reconcile",
        json={"left": left, "right": right, "excluded_pairs": [excluded("A", "X")]},
        headers={"Idempotency-Key": "excl-key"},
    )
    assert first.status_code == 200, first.text
    replay = client.post(
        "/reconcile",
        json={"left": left, "right": right, "excluded_pairs": [excluded("A", "X")]},
        headers={"Idempotency-Key": "excl-key"},
    )
    assert replay.status_code == 200
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.json() == first.json()
    conflict = client.post(
        "/reconcile",
        json={"left": left, "right": right, "excluded_pairs": [excluded("A", "Y")]},
        headers={"Idempotency-Key": "excl-key"},
    )
    assert conflict.status_code == 409


# --------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------- #
def test_duplicate_ids_return_400():
    dup = client.post("/reconcile", json={"left": [txn("X", 1), txn("X", 2)], "right": []})
    assert dup.status_code == 400
    cross = client.post("/reconcile", json={"left": [txn("X", 1)], "right": [txn("X", 1)]})
    assert cross.status_code == 400


def test_zero_amount_rejected():
    resp = client.post("/reconcile", json={"left": [txn("L1", 0)], "right": []})
    assert resp.status_code == 422


def test_non_integer_amount_rejected():
    for bad in (1.5, True, "100"):
        resp = client.post("/reconcile", json={"left": [txn("L1", bad)], "right": []})
        assert resp.status_code == 422, bad


def test_more_than_12_per_side_rejected():
    resp = client.post(
        "/reconcile",
        json={"left": [txn(f"L{i}", 1) for i in range(13)], "right": []},
    )
    assert resp.status_code == 422


def test_bad_time_rejected():
    resp = client.post("/reconcile", json={"left": [txn("L1", 1, "not-a-time")], "right": []})
    assert resp.status_code == 422


def test_empty_id_rejected():
    resp = client.post("/reconcile", json={"left": [txn("", 1)], "right": []})
    assert resp.status_code == 422


def test_full_batch_of_12_per_side():
    left = [txn(f"L{i}", 100 * (1 if i % 2 == 0 else -1)) for i in range(12)]
    right = [txn(f"R{i}", 100 * (1 if i % 2 == 0 else -1)) for i in range(12)]
    body = post(left, right)
    assert body["total_amount"] == 1200
    assert len(body["groups"]) == 12
