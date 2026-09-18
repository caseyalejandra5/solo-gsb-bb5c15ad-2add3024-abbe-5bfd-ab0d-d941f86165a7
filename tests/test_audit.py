"""API-level tests for the POST /reconcile/audit endpoint."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

T0 = "2026-09-17T00:00:00Z"


def txn(id: str, amount: int, time: str = T0) -> dict:
    return {"id": id, "time": time, "amount": amount}


def grp(left_ids, right_ids) -> dict:
    return {"left_ids": list(left_ids), "right_ids": list(right_ids)}


def post_audit(left, right, proposed, **extra):
    return client.post(
        "/reconcile/audit",
        json={"left": left, "right": right, "proposed_groups": proposed, **extra},
    )


# --------------------------------------------------------------------- #
# valid and optimal plans
# --------------------------------------------------------------------- #
def test_optimal_plan_passes_audit():
    resp = post_audit(
        [txn("A", 100), txn("B", 100)],
        [txn("X", 100), txn("Y", 100)],
        [grp(["A"], ["X"]), grp(["B"], ["Y"])],
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {
        "valid": True,
        "optimal": True,
        "suboptimal_reason": None,
        "proposed_total": 200,
        "optimal_total": 200,
        "groups": [
            {"left_ids": ["A"], "right_ids": ["X"], "amount": 100},
            {"left_ids": ["B"], "right_ids": ["Y"], "amount": 100},
        ],
    }


def test_optimal_one_to_many_plan():
    resp = post_audit(
        [txn("L1", 300)],
        [txn("R1", 100), txn("R2", 200)],
        [grp(["L1"], ["R1", "R2"])],
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["valid"] is True
    assert body["optimal"] is True
    assert body["proposed_total"] == body["optimal_total"] == 300


def test_proposal_group_order_does_not_matter():
    # canonical groups are sorted by encoding; submitting them reversed is
    # still the same plan
    resp = post_audit(
        [txn("A", 100), txn("B", 100)],
        [txn("X", 100), txn("Y", 100)],
        [grp(["B"], ["Y"]), grp(["A"], ["X"])],
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["optimal"] is True
    assert body["suboptimal_reason"] is None


def test_negative_amount_optimal_plan():
    resp = post_audit(
        [txn("A", -150)],
        [txn("X", -100), txn("Y", -50)],
        [grp(["A"], ["X", "Y"])],
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["optimal"] is True
    assert body["proposed_total"] == body["optimal_total"] == 150
    assert body["groups"] == [
        {"left_ids": ["A"], "right_ids": ["X", "Y"], "amount": -150}
    ]


def test_empty_proposal_is_valid_but_suboptimal_when_groups_exist():
    resp = post_audit([txn("A", 100)], [txn("X", 100)], [])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["valid"] is True
    assert body["optimal"] is False
    assert body["suboptimal_reason"] == "amount"
    assert body["proposed_total"] == 0
    assert body["optimal_total"] == 100
    # canonical groups are returned so the caller can correct the plan
    assert body["groups"] == [
        {"left_ids": ["A"], "right_ids": ["X"], "amount": 100}
    ]


def test_empty_batch_empty_plan_is_optimal():
    resp = post_audit([], [], [])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["valid"] is True
    assert body["optimal"] is True
    assert body["proposed_total"] == body["optimal_total"] == 0
    assert body["groups"] == []


# --------------------------------------------------------------------- #
# sub-optimal plans
# --------------------------------------------------------------------- #
def test_amount_suboptimal_plan():
    # optimum is (A+B,X)=150; proposing (A,Y)=100 leaves B and X unmatched
    resp = post_audit(
        [txn("A", 100), txn("B", 50)],
        [txn("X", 150), txn("Y", 100)],
        [grp(["A"], ["Y"])],
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["valid"] is True
    assert body["optimal"] is False
    assert body["suboptimal_reason"] == "amount"
    assert body["proposed_total"] == 100
    assert body["optimal_total"] == 150
    assert body["groups"] == [
        {"left_ids": ["A", "B"], "right_ids": ["X"], "amount": 150}
    ]


def test_lexicographic_suboptimal_plan_same_total():
    # both perfect matchings reconcile 200; (A,X)+(B,Y) is the canonical one
    resp = post_audit(
        [txn("A", 100), txn("B", 100)],
        [txn("X", 100), txn("Y", 100)],
        [grp(["A"], ["Y"]), grp(["B"], ["X"])],
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["valid"] is True
    assert body["optimal"] is False
    assert body["suboptimal_reason"] == "lexicographic"
    assert body["proposed_total"] == body["optimal_total"] == 200
    assert body["groups"] == [
        {"left_ids": ["A"], "right_ids": ["X"], "amount": 100},
        {"left_ids": ["B"], "right_ids": ["Y"], "amount": 100},
    ]


def test_lexicographic_suboptimal_across_group_shapes():
    # (A,[X,Y]) and (A,[Z]) both reconcile 300; the one-to-many encoding wins
    resp = post_audit(
        [txn("A", 300)],
        [txn("Z", 300), txn("X", 100), txn("Y", 200)],
        [grp(["A"], ["Z"])],
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["optimal"] is False
    assert body["suboptimal_reason"] == "lexicographic"
    assert body["proposed_total"] == body["optimal_total"] == 300
    assert body["groups"] == [
        {"left_ids": ["A"], "right_ids": ["X", "Y"], "amount": 300}
    ]


# --------------------------------------------------------------------- #
# invalid plans: per-group errors stable by index, never aborting
# --------------------------------------------------------------------- #
def post_audit_errors(left, right, proposed, **extra):
    resp = post_audit(left, right, proposed, **extra)
    assert resp.status_code == 422, resp.text
    return resp.json()["detail"]


def test_unknown_id_reports_group_index():
    errs = post_audit_errors(
        [txn("A", 100)], [txn("X", 100)], [grp(["A"], ["NOPE"])]
    )
    assert len(errs) == 1
    assert errs[0]["loc"] == ["body", "proposed_groups", 0]
    assert errs[0]["type"] == "proposed_group.unknown_id"
    assert "NOPE" in errs[0]["msg"]
    assert errs[0]["input"] == grp(["A"], ["NOPE"])


def test_wrong_side_reference():
    errs = post_audit_errors(
        [txn("A", 100)], [txn("X", 100)], [grp(["A"], ["A"])]
    )
    assert errs[0]["loc"] == ["body", "proposed_groups", 0]
    assert errs[0]["type"] == "proposed_group.wrong_side"


def test_duplicate_id_inside_group():
    errs = post_audit_errors(
        [txn("A", 100)], [txn("X", 100)], [grp(["A", "A"], ["X"])]
    )
    assert errs[0]["type"] == "proposed_group.duplicate_id"
    assert errs[0]["loc"] == ["body", "proposed_groups", 0]


def test_bad_shape_2_2():
    errs = post_audit_errors(
        [txn("A", 1), txn("B", 1)],
        [txn("X", 1), txn("Y", 1)],
        [grp(["A", "B"], ["X", "Y"])],
    )
    assert errs[0]["type"] == "proposed_group.bad_shape"


def test_mixed_sign_group():
    errs = post_audit_errors(
        [txn("A", 100)], [txn("X", -100)], [grp(["A"], ["X"])]
    )
    assert errs[0]["type"] == "proposed_group.mixed_sign"


def test_amount_mismatch_group():
    errs = post_audit_errors(
        [txn("A", 100)], [txn("X", 101)], [grp(["A"], ["X"])]
    )
    assert errs[0]["type"] == "proposed_group.amount_mismatch"


def test_outside_48h_group():
    errs = post_audit_errors(
        [txn("A", 100, T0)],
        [txn("X", 100, "2026-09-19T00:00:01Z")],
        [grp(["A"], ["X"])],
    )
    assert errs[0]["type"] == "proposed_group.outside_48h"


def test_48h_boundary_group_accepted():
    resp = post_audit(
        [txn("A", 100, T0)],
        [txn("X", 100, "2026-09-19T00:00:00Z")],
        [grp(["A"], ["X"])],
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["optimal"] is True


def test_group_hitting_excluded_pair():
    errs = post_audit_errors(
        [txn("A", 100)],
        [txn("X", 100)],
        [grp(["A"], ["X"])],
        excluded_pairs=[{"left_id": "A", "right_id": "X"}],
    )
    assert errs[0]["type"] == "proposed_group.excluded_pair"
    assert "A" in errs[0]["msg"] and "X" in errs[0]["msg"]


def test_transaction_reused_between_proposed_groups():
    errs = post_audit_errors(
        [txn("A", 100), txn("B", 100)],
        [txn("X", 100), txn("Y", 100)],
        [grp(["A"], ["X"]), grp(["A"], ["Y"]), grp(["B"], ["X"])],
    )
    # the first group is fine; the later two each reuse one transaction
    assert [(e["loc"][-1], e["type"]) for e in errs] == [
        (1, "proposed_group.transaction_reused"),
        (2, "proposed_group.transaction_reused"),
    ]
    # errors are stably ordered by proposed_groups index
    assert [e["loc"][-1] for e in errs] == sorted(e["loc"][-1] for e in errs)


def test_reuse_of_locked_group_transaction():
    errs = post_audit_errors(
        [txn("A", 100), txn("B", 100)],
        [txn("X", 100), txn("Y", 100)],
        [grp(["A"], ["Y"])],
        locked_groups=[grp(["A"], ["X"])],
    )
    assert errs[0]["type"] == "proposed_group.transaction_reused"
    assert errs[0]["loc"] == ["body", "proposed_groups", 0]


def test_proposal_may_restate_locked_group():
    # a complete plan legitimately includes the locked group itself
    resp = post_audit(
        [txn("A", 100), txn("B", 100)],
        [txn("X", 100), txn("Y", 100)],
        [grp(["A"], ["X"]), grp(["B"], ["Y"])],
        locked_groups=[grp(["A"], ["X"])],
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["valid"] is True
    assert body["optimal"] is True
    assert body["proposed_total"] == body["optimal_total"] == 200


def test_locked_group_restated_twice_is_reuse():
    errs = post_audit_errors(
        [txn("A", 100)],
        [txn("X", 100)],
        [grp(["A"], ["X"]), grp(["A"], ["X"])],
        locked_groups=[grp(["A"], ["X"])],
    )
    assert [(e["loc"][-1], e["type"]) for e in errs] == [
        (1, "proposed_group.transaction_reused"),
    ]


def test_errors_do_not_abort_remaining_group_checks():
    # three independent groups (disjoint transactions) break three different
    # rules; all three must be reported in one response, ordered by index
    errs = post_audit_errors(
        [txn("A", 100), txn("B", 100), txn("C", 100), txn("D", 100)],
        [txn("X", 101), txn("Y", 100), txn("Z", 100), txn("W", 100)],
        [
            grp(["A"], ["NOPE"]),           # 0 unknown id
            grp(["B"], ["X"]),              # 1 amount mismatch (100 vs 101)
            grp(["C", "D"], ["Y", "Z"]),    # 2 bad shape
        ],
    )
    assert [e["loc"][-1] for e in errs] == [0, 1, 2]
    assert [e["type"] for e in errs] == [
        "proposed_group.unknown_id",
        "proposed_group.amount_mismatch",
        "proposed_group.bad_shape",
    ]


def test_only_first_violated_rule_per_group_is_reported():
    # unknown id masks the cross-group reuse in the same group
    errs = post_audit_errors(
        [txn("A", 100), txn("B", 100)],
        [txn("X", 100), txn("Y", 100)],
        [grp(["A"], ["X"]), grp(["A", "B"], ["NOPE"])],
    )
    assert [(e["loc"][-1], e["type"]) for e in errs] == [
        (1, "proposed_group.unknown_id"),
    ]


def test_one_invalid_group_prevents_validity_even_if_others_fine():
    errs = post_audit_errors(
        [txn("A", 100), txn("B", 100)],
        [txn("X", 100), txn("Y", 100)],
        [grp(["A"], ["X"]), grp(["B"], ["ZZZ"])],
    )
    assert [e["loc"][-1] for e in errs] == [1]


# --------------------------------------------------------------------- #
# batch-level validation shared with /reconcile
# --------------------------------------------------------------------- #
def test_duplicate_batch_id_returns_400():
    resp = post_audit(
        [txn("A", 1), txn("A", 2)], [], []
    )
    assert resp.status_code == 400


def test_invalid_excluded_pair_returns_422_with_its_index():
    resp = post_audit(
        [txn("A", 100)],
        [txn("X", 100)],
        [grp(["A"], ["X"])],
        excluded_pairs=[{"left_id": "NOPE", "right_id": "X"}],
    )
    assert resp.status_code == 422
    err = resp.json()["detail"][0]
    assert err["loc"] == ["body", "excluded_pairs", 0]
    assert err["type"] == "excluded_pair.invalid"


def test_invalid_locked_group_returns_422_before_proposal_checks():
    resp = post_audit(
        [txn("A", 100)],
        [txn("X", 100)],
        [grp(["A"], ["X"])],
        locked_groups=[grp(["A"], ["NOPE"])],
    )
    assert resp.status_code == 422
    assert resp.json()["detail"][0]["loc"] == ["body", "locked_groups", 0]


def test_locked_groups_are_part_of_the_canonical_solution():
    # locking (A,Y) makes the optimum 200 over (A,Y)+(B,X); a proposal that
    # leaves B,X unmatched is valid-but-amount-suboptimal against that optimum
    resp = post_audit(
        [txn("A", 100), txn("B", 100)],
        [txn("X", 100), txn("Y", 100)],
        [grp(["A"], ["Y"])],
        locked_groups=[grp(["A"], ["Y"])],
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["valid"] is True
    assert body["optimal"] is False
    assert body["suboptimal_reason"] == "amount"
    assert body["proposed_total"] == 100
    assert body["optimal_total"] == 200
    assert body["groups"] == [
        {"left_ids": ["A"], "right_ids": ["Y"], "amount": 100},
        {"left_ids": ["B"], "right_ids": ["X"], "amount": 100},
    ]


# --------------------------------------------------------------------- #
# schema validation
# --------------------------------------------------------------------- #
def test_missing_proposed_groups_returns_422():
    resp = client.post(
        "/reconcile/audit",
        json={"left": [txn("A", 100)], "right": [txn("X", 100)]},
    )
    assert resp.status_code == 422
    assert "proposed_groups" in str(resp.json()["detail"])


def test_schema_level_bad_groups_422():
    for bad in (
        {"right_ids": ["X"]},
        {"left_ids": [], "right_ids": ["X"]},
        {"left_ids": ["A"], "right_ids": [1]},
        {"left_ids": ["A", "B", "C", "D"], "right_ids": ["X"]},
    ):
        resp = post_audit([txn("A", 1)], [txn("X", 1)], [bad])
        assert resp.status_code == 422, bad
        assert "proposed_groups" in resp.json()["detail"][0]["loc"]


def test_bad_transaction_fields_422():
    resp = post_audit([txn("A", 0)], [], [])
    assert resp.status_code == 422
