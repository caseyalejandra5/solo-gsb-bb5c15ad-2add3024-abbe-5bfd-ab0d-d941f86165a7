"""API-level tests for the /reconcile/unmatched-summary endpoint."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

T0 = "2026-09-17T00:00:00Z"


def txn(id: str, amount: int, time: str = T0) -> dict:
    return {"id": id, "time": time, "amount": amount}


def post_summary(left, right, timezone: str = "UTC", **extra):
    resp = client.post(
        "/reconcile/unmatched-summary",
        json={"left": left, "right": right, "timezone": timezone, **extra},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def sign(count: int = 0, amount: int = 0) -> dict:
    return {"count": count, "amount": amount}


# --------------------------------------------------------------------- #
# basic aggregation
# --------------------------------------------------------------------- #
def test_empty_batch():
    body = post_summary([], [], timezone="Asia/Shanghai")
    assert body == {
        "timezone": "Asia/Shanghai",
        "days": [],
        "total": {"positive": sign(), "negative": sign()},
    }


def test_all_matched_gives_empty_summary():
    body = post_summary([txn("L1", 100)], [txn("R1", 100)])
    assert body["days"] == []
    assert body["total"] == {"positive": sign(), "negative": sign()}


def test_unmatched_aggregated_by_sign_across_both_sides():
    # no cross-side sum matches, so all four stay unmatched
    body = post_summary(
        [txn("L1", 100), txn("L2", -50)],
        [txn("R1", 200), txn("R2", -25)],
    )
    assert body["days"] == [
        {"date": "2026-09-17", "positive": sign(2, 300), "negative": sign(2, 75)}
    ]
    assert body["total"] == {"positive": sign(2, 300), "negative": sign(2, 75)}


def test_amounts_aggregated_as_absolute_values():
    body = post_summary([txn("L1", -150), txn("L2", -50)], [])
    assert body["days"][0]["negative"] == sign(2, 200)
    assert body["total"]["negative"] == sign(2, 200)


def test_matched_transactions_excluded():
    body = post_summary(
        [txn("L1", 100), txn("L2", 7)],
        [txn("R1", 100), txn("R2", 9)],
    )
    assert body["days"] == [
        {"date": "2026-09-17", "positive": sign(2, 16), "negative": sign()}
    ]
    assert body["total"] == {"positive": sign(2, 16), "negative": sign()}


def test_days_sorted_ascending_and_total_spans_days():
    body = post_summary(
        [
            txn("L3", 30, "2026-09-19T12:00:00Z"),
            txn("L1", 10, "2026-09-17T12:00:00Z"),
            txn("L2", -20, "2026-09-18T12:00:00Z"),
        ],
        [],
    )
    assert [d["date"] for d in body["days"]] == [
        "2026-09-17",
        "2026-09-18",
        "2026-09-19",
    ]
    assert body["total"] == {"positive": sign(2, 40), "negative": sign(1, 20)}


# --------------------------------------------------------------------- #
# local-date conversion
# --------------------------------------------------------------------- #
def test_local_dates_follow_requested_timezone():
    # Asia/Shanghai is UTC+8: the local day starts at 16:00Z of the day before
    body = post_summary(
        [
            txn("L1", 1, "2026-09-16T16:00:00Z"),  # 2026-09-17 00:00 +08
            txn("L2", 2, "2026-09-17T15:59:59Z"),  # 2026-09-17 23:59:59 +08
            txn("L3", 4, "2026-09-17T16:00:00Z"),  # 2026-09-18 00:00 +08
        ],
        [],
        timezone="Asia/Shanghai",
    )
    assert body["days"] == [
        {"date": "2026-09-17", "positive": sign(2, 3), "negative": sign()},
        {"date": "2026-09-18", "positive": sign(1, 4), "negative": sign()},
    ]
    assert body["total"] == {"positive": sign(3, 7), "negative": sign()}
    # the same instants are a single day in UTC
    body = post_summary(
        [
            txn("L1", 1, "2026-09-16T16:00:00Z"),
            txn("L2", 2, "2026-09-17T15:59:59Z"),
            txn("L3", 4, "2026-09-17T16:00:00Z"),
        ],
        [],
        timezone="UTC",
    )
    assert [d["date"] for d in body["days"]] == ["2026-09-16", "2026-09-17"]


def test_naive_timestamps_treated_as_utc_before_localisation():
    body = post_summary(
        [txn("L1", 1, "2026-09-16T20:00:00")],  # naive == 20:00Z -> 04:00 +08
        [],
        timezone="Asia/Shanghai",
    )
    assert [d["date"] for d in body["days"]] == ["2026-09-17"]


# --------------------------------------------------------------------- #
# DST transitions attribute the true local date
# --------------------------------------------------------------------- #
def test_dst_spring_forward_true_local_date():
    # America/New_York sprang forward on 2026-03-08 (UTC-5 -> UTC-4), so
    # 2026-03-09T04:30:00Z is already 00:30 EDT on the 9th; a fixed UTC-5
    # conversion would wrongly keep it on the 8th.
    body = post_summary(
        [
            txn("L1", 1, "2026-03-09T03:30:00Z"),  # 2026-03-08 23:30 EDT
            txn("L2", 2, "2026-03-09T04:30:00Z"),  # 2026-03-09 00:30 EDT
        ],
        [],
        timezone="America/New_York",
    )
    assert body["days"] == [
        {"date": "2026-03-08", "positive": sign(1, 1), "negative": sign()},
        {"date": "2026-03-09", "positive": sign(1, 2), "negative": sign()},
    ]


def test_dst_fall_back_true_local_date():
    # America/New_York fell back on 2026-11-01 (UTC-4 -> UTC-5), so
    # 2026-11-02T04:30:00Z is still 23:30 EST on the 1st; a fixed UTC-4
    # conversion would wrongly push it to the 2nd.
    body = post_summary(
        [
            txn("L1", 1, "2026-11-02T04:30:00Z"),  # 2026-11-01 23:30 EST
            txn("L2", 2, "2026-11-02T05:30:00Z"),  # 2026-11-02 00:30 EST
        ],
        [],
        timezone="America/New_York",
    )
    assert body["days"] == [
        {"date": "2026-11-01", "positive": sign(1, 1), "negative": sign()},
        {"date": "2026-11-02", "positive": sign(1, 2), "negative": sign()},
    ]


# --------------------------------------------------------------------- #
# timezone validation
# --------------------------------------------------------------------- #
def test_invalid_timezone_returns_422():
    for bad in ("Not/AZone", "UTC+8", "Mars/Olympus_Mons"):
        resp = client.post(
            "/reconcile/unmatched-summary",
            json={"left": [], "right": [], "timezone": bad},
        )
        assert resp.status_code == 422, bad
        assert resp.json()["detail"][0]["loc"] == ["body", "timezone"]


def test_missing_or_empty_timezone_returns_422():
    resp = client.post(
        "/reconcile/unmatched-summary", json={"left": [], "right": []}
    )
    assert resp.status_code == 422
    resp = client.post(
        "/reconcile/unmatched-summary",
        json={"left": [], "right": [], "timezone": ""},
    )
    assert resp.status_code == 422


def test_non_string_timezone_returns_422():
    resp = client.post(
        "/reconcile/unmatched-summary",
        json={"left": [], "right": [], "timezone": 8},
    )
    assert resp.status_code == 422


# --------------------------------------------------------------------- #
# batch validation and solving are reused from /reconcile
# --------------------------------------------------------------------- #
def test_duplicate_ids_return_400():
    resp = client.post(
        "/reconcile/unmatched-summary",
        json={"left": [txn("X", 1), txn("X", 2)], "right": [], "timezone": "UTC"},
    )
    assert resp.status_code == 400


def test_locked_groups_still_validated_and_applied():
    # invalid locked group -> same 422 shape as /reconcile
    resp = client.post(
        "/reconcile/unmatched-summary",
        json={
            "left": [txn("A", 100)],
            "right": [txn("X", 101)],
            "timezone": "UTC",
            "locked_groups": [{"left_ids": ["A"], "right_ids": ["X"]}],
        },
    )
    assert resp.status_code == 422
    assert resp.json()["detail"][0]["loc"] == ["body", "locked_groups", 0]
    # a valid lock removes the pair from the unmatched summary
    body = post_summary(
        [txn("A", 100), txn("B", 7)],
        [txn("X", 100)],
        locked_groups=[{"left_ids": ["A"], "right_ids": ["X"]}],
    )
    assert body["days"] == [
        {"date": "2026-09-17", "positive": sign(1, 7), "negative": sign()}
    ]


def test_excluded_pairs_still_validated_and_applied():
    resp = client.post(
        "/reconcile/unmatched-summary",
        json={
            "left": [txn("A", 100)],
            "right": [txn("X", 100)],
            "timezone": "UTC",
            "excluded_pairs": [{"left_id": "NOPE", "right_id": "X"}],
        },
    )
    assert resp.status_code == 422
    assert resp.json()["detail"][0]["loc"] == ["body", "excluded_pairs", 0]
    # excluding the only possible pair leaves both sides unmatched
    body = post_summary(
        [txn("A", 100)],
        [txn("X", 100)],
        excluded_pairs=[{"left_id": "A", "right_id": "X"}],
    )
    assert body["total"] == {"positive": sign(2, 200), "negative": sign()}


def test_batch_schema_validation_reused():
    base = {"left": [], "right": [], "timezone": "UTC"}
    for bad_left in (
        [txn("L1", 0)],  # zero amount
        [txn("L1", 1.5)],  # non-integer amount
        [txn("", 1)],  # empty id
        [txn("L1", 1, "not-a-time")],  # bad timestamp
        [txn(f"L{i}", 1) for i in range(13)],  # more than 12 per side
    ):
        resp = client.post(
            "/reconcile/unmatched-summary", json={**base, "left": bad_left}
        )
        assert resp.status_code == 422, bad_left


def test_reconcile_endpoint_unchanged_by_summary_route():
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
