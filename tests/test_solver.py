"""Solver tests, including a differential check against brute force."""

from __future__ import annotations

import random

import pytest

from app.solver import (
    ExcludedPair,
    LockedGroup,
    Reconciler,
    Txn,
    resolve_excluded_pairs,
    resolve_locked_groups,
)

BASE_TS = 1_758_000_000.0  # fixed epoch for reproducibility


def make_txn(id: str, amount: int, ts: float = BASE_TS) -> Txn:
    return Txn(id=id, ts=ts, amount=amount)


def brute_force(left: list[Txn], right: list[Txn]):
    """Enumerate every packing of legal candidates; return (max weight,
    lexicographically smallest sorted encoding list among max-weight packings)."""
    rec = Reconciler(left, right)
    cands = rec.candidates
    by_txn: list[list[int]] = [[] for _ in rec.txns]
    for ci, c in enumerate(cands):
        m = c.mask
        while m:
            b = m & (-m)
            by_txn[b.bit_length() - 1].append(ci)
            m &= m - 1

    best_weight = 0
    best_enc: list | None = None

    def search(mask: int, weight: int, chosen: list[int]) -> None:
        nonlocal best_weight, best_enc
        if mask == 0:
            return
        b = mask & (-mask)
        t = b.bit_length() - 1
        # t unmatched
        search(mask & ~b, weight, chosen)
        # t inside one candidate
        for ci in by_txn[t]:
            cm = cands[ci].mask
            if cm & mask == cm:
                chosen.append(ci)
                new_weight = weight + cands[ci].weight
                enc = sorted(cands[j].encoding for j in chosen)
                if new_weight > best_weight or (
                    new_weight == best_weight and (best_enc is None or enc < best_enc)
                ):
                    best_weight = new_weight
                    best_enc = enc
                search(mask ^ cm, new_weight, chosen)
                chosen.pop()

    full = (1 << len(rec.txns)) - 1
    search(full, 0, [])
    return best_weight, (best_enc or [])


def solve(left: list[Txn], right: list[Txn]):
    groups = Reconciler(left, right).solve()
    weight = sum(g.weight for g in groups)
    enc = sorted(g.encoding for g in groups)
    return weight, enc


def test_no_candidates():
    assert solve([make_txn("L1", 100)], [make_txn("R1", 101)]) == (0, [])
    assert solve([], []) == (0, [])


def test_simple_match():
    weight, enc = solve([make_txn("L1", 100)], [make_txn("R1", 100)])
    assert weight == 100
    assert enc == [(("L1",), ("R1",))]


@pytest.mark.parametrize("seed", range(250))
def test_against_brute_force(seed: int):
    rng = random.Random(seed)
    n_left = rng.randint(0, 5)
    n_right = rng.randint(0, 5)
    left = [
        Txn(
            id=f"L{i}",
            ts=BASE_TS + rng.randint(0, 4 * 86400),
            amount=rng.choice([-300, -200, -100, -50, 50, 100, 200, 300]),
        )
        for i in range(n_left)
    ]
    right = [
        Txn(
            id=f"R{i}",
            ts=BASE_TS + rng.randint(0, 4 * 86400),
            amount=rng.choice([-300, -200, -100, -50, 50, 100, 200, 300]),
        )
        for i in range(n_right)
    ]
    assert solve(left, right) == brute_force(left, right)


def test_worst_case_full_batch_is_fast():
    # 12 vs 12, all identical amounts and timestamps: every candidate is legal.
    left = [make_txn(f"L{i}", 100) for i in range(12)]
    right = [make_txn(f"R{i}", 100) for i in range(12)]
    weight, enc = solve(left, right)
    assert weight == 1200
    assert len(enc) == 12
    # lexicographically smallest perfect matching pairs L{i} with R{i}
    # (note: encodings sort as strings, so "L10" < "L2")
    assert enc == sorted(((f"L{i}",), (f"R{i}",)) for i in range(12))


def test_large_random_batch_completes():
    rng = random.Random(7)
    left = [
        Txn(f"L{i}", BASE_TS + rng.randint(0, 3 * 86400), rng.randint(-5000, 5000) or 1)
        for i in range(12)
    ]
    right = [
        Txn(f"R{i}", BASE_TS + rng.randint(0, 3 * 86400), rng.randint(-5000, 5000) or 1)
        for i in range(12)
    ]
    weight, enc = solve(left, right)
    # sanity: every transaction appears at most once
    used = [id_ for group in enc for side in group for id_ in side]
    assert len(used) == len(set(used))
    assert weight >= 0


# --------------------------------------------------------------------- #
# locked groups
# --------------------------------------------------------------------- #
def brute_force_locked(
    left: list[Txn],
    right: list[Txn],
    locked_encs: list,
    free_mask: int,
    rec: Reconciler | None = None,
):
    """Like brute_force, but candidates must fit inside ``free_mask`` and the
    locked encodings are folded into the lexicographic comparison.  An existing
    reconciler (e.g. with excluded pairs already applied) may be supplied."""
    if rec is None:
        rec = Reconciler(left, right)
    cands = rec.candidates
    by_txn: list[list[int]] = [[] for _ in rec.txns]
    for ci, c in enumerate(cands):
        m = c.mask
        while m:
            b = m & (-m)
            by_txn[b.bit_length() - 1].append(ci)
            m &= m - 1

    best_weight = 0
    best_enc: list | None = None

    def consider(weight: int, chosen: list[int]) -> None:
        nonlocal best_weight, best_enc
        enc = sorted(locked_encs + [cands[j].encoding for j in chosen])
        if weight > best_weight or (
            weight == best_weight and (best_enc is None or enc < best_enc)
        ):
            best_weight = weight
            best_enc = enc

    def search(mask: int, weight: int, chosen: list[int]) -> None:
        consider(weight, chosen)
        if mask == 0:
            return
        b = mask & (-mask)
        t = b.bit_length() - 1
        search(mask & ~b, weight, chosen)
        for ci in by_txn[t]:
            cm = cands[ci].mask
            if cm & mask == cm:
                chosen.append(ci)
                search(mask ^ cm, weight + cands[ci].weight, chosen)
                chosen.pop()

    search(free_mask, 0, [])
    return best_weight, (best_enc or [])


def solve_locked(left: list[Txn], right: list[Txn], locked_specs):
    rec = Reconciler(left, right)
    locked = [
        LockedGroup(left_ids=tuple(sorted(l)), right_ids=tuple(sorted(r)))
        for l, r in locked_specs
    ]
    locked_cands = resolve_locked_groups(rec, locked)
    groups = rec.solve(locked_cands)
    return sum(g.weight for g in groups), sorted(g.encoding for g in groups)


def test_locked_group_forced_and_remainder_optimal():
    # (A,X) locked; without it (A+B,Y)=150 would win, now only B stays free
    left = [make_txn("A", 100), make_txn("B", 50)]
    right = [make_txn("X", 100), make_txn("Y", 150)]
    weight, enc = solve_locked(left, right, [(("A",), ("X",))])
    assert weight == 100
    assert enc == [(("A",), ("X",))]


def test_locked_group_errors_report_index():
    from app.solver import LockedGroupError

    left = [make_txn("A", 100), make_txn("B", 100)]
    right = [make_txn("X", 100), make_txn("Y", 100)]
    rec = Reconciler(left, right)

    with pytest.raises(LockedGroupError) as ei:
        resolve_locked_groups(rec, [LockedGroup(("A",), ("NOPE",))])
    assert ei.value.index == 0
    with pytest.raises(LockedGroupError) as ei:
        resolve_locked_groups(
            rec,
            [LockedGroup(("A",), ("X",)), LockedGroup(("A",), ("Y",))],
        )
    assert ei.value.index == 1
    with pytest.raises(LockedGroupError) as ei:
        resolve_locked_groups(
            rec, [LockedGroup(("A", "A"), ("X",))]
        )
    assert ei.value.index == 0
    # illegal group: amounts differ
    with pytest.raises(LockedGroupError) as ei:
        resolve_locked_groups(
            Reconciler([make_txn("A", 100)], [make_txn("X", 101)]),
            [LockedGroup(("A",), ("X",))],
        )
    assert ei.value.index == 0


@pytest.mark.parametrize("seed", range(120))
def test_locked_groups_against_brute_force(seed: int):
    rng = random.Random(10_000 + seed)
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

    # pick a random set of mutually disjoint legal groups as the locked set
    pool = list(rec.candidates)
    rng.shuffle(pool)
    used_mask = 0
    locked_cands = []
    for cand in pool:
        if cand.mask & used_mask == 0 and rng.random() < 0.35:
            locked_cands.append(cand)
            used_mask |= cand.mask

    groups = rec.solve(locked_cands)
    weight = sum(g.weight for g in groups)
    enc = sorted(g.encoding for g in groups)

    full = (1 << len(rec.txns)) - 1
    locked_encs = [c.encoding for c in locked_cands]
    assert weight == (
        sum(c.weight for c in locked_cands)
        + rec._max_value(full ^ used_mask)
    )
    assert enc == brute_force_locked(
        left, right, locked_encs, full ^ used_mask
    )[1]


def test_locked_full_batch():
    # 12 vs 12: two locked one-to-many groups, the rest still perfectly packs
    left = [make_txn("L0", 200), make_txn("L1", 100), make_txn("L2", 100)] + [
        make_txn(f"L{i}", 100) for i in range(3, 12)
    ]
    right = [make_txn("R0", 100), make_txn("R1", 100), make_txn("R2", 200)] + [
        make_txn(f"R{i}", 100) for i in range(3, 12)
    ]
    specs = [(("L0",), ("R0", "R1")), (("L1", "L2"), ("R2",))]
    weight, enc = solve_locked(left, right, specs)
    assert weight == 1300  # 400 locked + 900 from the remaining nine 1-1 pairs
    assert (("L0",), ("R0", "R1")) in enc
    assert (("L1", "L2"), ("R2",)) in enc
    used = [id_ for group in enc for side in group for id_ in side]
    assert len(used) == len(set(used)) == 24


# --------------------------------------------------------------------- #
# excluded pairs
# --------------------------------------------------------------------- #
def brute_force_on_rec(rec: Reconciler):
    """Enumerate every packing over the reconciler's (already filtered)
    candidates; return (max weight, lexicographically smallest sorted encoding
    list among max-weight packings)."""
    cands = rec.candidates
    by_txn: list[list[int]] = [[] for _ in rec.txns]
    for ci, c in enumerate(cands):
        m = c.mask
        while m:
            b = m & (-m)
            by_txn[b.bit_length() - 1].append(ci)
            m &= m - 1

    best_weight = 0
    best_enc: list | None = None

    def search(mask: int, weight: int, chosen: list[int]) -> None:
        nonlocal best_weight, best_enc
        if mask == 0:
            return
        b = mask & (-mask)
        t = b.bit_length() - 1
        # t unmatched
        search(mask & ~b, weight, chosen)
        # t inside one candidate
        for ci in by_txn[t]:
            cm = cands[ci].mask
            if cm & mask == cm:
                chosen.append(ci)
                new_weight = weight + cands[ci].weight
                enc = sorted(cands[j].encoding for j in chosen)
                if new_weight > best_weight or (
                    new_weight == best_weight and (best_enc is None or enc < best_enc)
                ):
                    best_weight = new_weight
                    best_enc = enc
                search(mask ^ cm, new_weight, chosen)
                chosen.pop()

    full = (1 << len(rec.txns)) - 1
    search(full, 0, [])
    return best_weight, (best_enc or [])


def solve_excluded(left: list[Txn], right: list[Txn], pair_specs):
    pairs = [ExcludedPair(left_id=l, right_id=r) for l, r in pair_specs]
    left_ids, right_ids = {t.id for t in left}, {t.id for t in right}
    resolved = resolve_excluded_pairs(left_ids, right_ids, pairs)
    rec = Reconciler(left, right, excluded_pairs=resolved)
    groups = rec.solve()
    return rec, sum(g.weight for g in groups), sorted(g.encoding for g in groups)


def test_excluded_pair_removes_only_offending_candidate():
    left = [make_txn("A", 100), make_txn("B", 100)]
    right = [make_txn("X", 100), make_txn("Y", 100)]
    rec, weight, enc = solve_excluded(left, right, [("A", "X")])
    assert not any(c.left_ids == ("A",) and c.right_ids == ("X",) for c in rec.candidates)
    # the other three 1-1 candidates are untouched
    assert len(rec.candidates) == 3
    assert weight == 200
    assert enc == [(("A",), ("Y",)), (("B",), ("X",))]


def test_excluded_pair_prunes_one_to_many_candidate():
    left = [make_txn("L1", 300)]
    right = [make_txn("R1", 100), make_txn("R2", 200), make_txn("R3", 300)]
    rec, weight, enc = solve_excluded(left, right, [("L1", "R1")])
    assert not any(
        c.left_ids == ("L1",) and c.right_ids == ("R1", "R2") for c in rec.candidates
    )
    assert (("L1",), ("R3",)) in enc
    assert weight == 300


def test_excluded_pair_errors_report_index():
    from app.solver import ExcludedPairError

    left = [make_txn("A", 100), make_txn("B", 100)]
    right = [make_txn("X", 100), make_txn("Y", 100)]
    lset, rset = {t.id for t in left}, {t.id for t in right}

    with pytest.raises(ExcludedPairError) as ei:
        resolve_excluded_pairs(lset, rset, [ExcludedPair("NOPE", "X")])
    assert ei.value.index == 0
    assert "unknown left" in str(ei.value)

    with pytest.raises(ExcludedPairError) as ei:
        resolve_excluded_pairs(lset, rset, [ExcludedPair("A", "NOPE")])
    assert ei.value.index == 0
    assert "unknown right" in str(ei.value)

    with pytest.raises(ExcludedPairError) as ei:
        resolve_excluded_pairs(lset, rset, [ExcludedPair("X", "X")])
    assert ei.value.index == 0
    assert "right-side" in str(ei.value)

    with pytest.raises(ExcludedPairError) as ei:
        resolve_excluded_pairs(lset, rset, [ExcludedPair("A", "A")])
    assert ei.value.index == 0
    assert "left-side" in str(ei.value)

    with pytest.raises(ExcludedPairError) as ei:
        resolve_excluded_pairs(
            lset,
            rset,
            [ExcludedPair("A", "X"), ExcludedPair("B", "Y"), ExcludedPair("A", "X")],
        )
    assert ei.value.index == 2
    assert "duplicate" in str(ei.value)


def test_locked_group_containing_excluded_pair_rejected():
    from app.solver import LockedGroupError

    left = [make_txn("L1", 300)]
    right = [make_txn("R1", 100), make_txn("R2", 200)]
    rec = Reconciler(left, right, excluded_pairs={("L1", "R2")})
    with pytest.raises(LockedGroupError) as ei:
        resolve_locked_groups(rec, [LockedGroup(("L1",), ("R1", "R2"))])
    assert ei.value.index == 0
    assert "excluded pair" in str(ei.value)

    # a legal locked group avoiding the pair still resolves fine
    left = [make_txn("A", 100), make_txn("B", 100)]
    right = [make_txn("X", 100), make_txn("Y", 100)]
    rec = Reconciler(left, right, excluded_pairs={("A", "Y")})
    resolved = resolve_locked_groups(rec, [LockedGroup(("A",), ("X",))])
    assert len(resolved) == 1


@pytest.mark.parametrize("seed", range(150))
def test_excluded_pairs_against_brute_force(seed: int):
    rng = random.Random(20_000 + seed)
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

    # a legal candidate is used to seed at least one "bite": pick a random
    # candidate and exclude one of its (left, right) id pairs
    base = Reconciler(left, right)
    pair_specs = set()
    if base.candidates and rng.random() < 0.8:
        c = rng.choice(base.candidates)
        pair_specs.add((rng.choice(c.left_ids), rng.choice(c.right_ids)))
    # plus a few arbitrary pairs
    for _ in range(rng.randint(0, 3)):
        if left and right:
            pair_specs.add(
                (rng.choice(left).id, rng.choice(right).id)
            )

    rec, weight, enc = solve_excluded(left, right, list(pair_specs))
    bf_weight, bf_enc = brute_force_on_rec(rec)
    assert weight == bf_weight
    assert enc == bf_enc
    # no returned group may contain an excluded pair
    for l_ids, r_ids in enc:
        for lid, rid in pair_specs:
            assert not (lid in l_ids and rid in r_ids)


@pytest.mark.parametrize("seed", range(120))
def test_locked_groups_and_excluded_pairs_against_brute_force(seed: int):
    rng = random.Random(30_000 + seed)
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

    # pick excluded pairs first, then mutually disjoint legal candidates that
    # avoid them as the locked set
    pair_specs = set()
    for _ in range(rng.randint(0, 3)):
        if left and right:
            pair_specs.add((rng.choice(left).id, rng.choice(right).id))
    pairs = [ExcludedPair(left_id=l, right_id=r) for l, r in pair_specs]
    resolved_pairs = resolve_excluded_pairs(
        {t.id for t in left}, {t.id for t in right}, pairs
    )
    rec = Reconciler(left, right, excluded_pairs=resolved_pairs)

    pool = list(rec.candidates)
    rng.shuffle(pool)
    used_mask = 0
    locked_cands = []
    for cand in pool:
        if cand.mask & used_mask == 0 and rng.random() < 0.35:
            locked_cands.append(cand)
            used_mask |= cand.mask

    groups = rec.solve(locked_cands)
    weight = sum(g.weight for g in groups)
    enc = sorted(g.encoding for g in groups)

    full = (1 << len(rec.txns)) - 1
    assert weight == (
        sum(c.weight for c in locked_cands)
        + rec._max_value(full ^ used_mask)
    )
    assert enc == brute_force_locked(
        left, right, [c.encoding for c in locked_cands], full ^ used_mask, rec
    )[1]
    # neither free nor locked groups may contain an excluded pair
    for l_ids, r_ids in enc:
        for lid, rid in pair_specs:
            assert not (lid in l_ids and rid in r_ids)
