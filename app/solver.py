"""Reconciliation engine.

Builds every legal reconciliation group (candidate) from the two sides, then
selects a mutually exclusive set of candidates maximising the total absolute
reconciled amount.  Ties are broken by choosing the solution whose sorted list
of group encodings -- ``(sorted left ids, sorted right ids)`` -- is
lexicographically smallest.

A batch may additionally carry *locked groups*: groups an operator has
confirmed manually.  Locked groups must themselves be legal, each transaction
may be locked at most once, and every locked group is forced into the solution;
the global maximum / lexicographic tie-break is then computed over the
remaining (unlocked) transactions only.

A batch may also carry *excluded pairs*: (left id, right id) pairs an operator
declares must never share a reconciliation group.  Any candidate containing
both members of an excluded pair is dropped before optimisation, and a locked
group containing one is rejected outright.

``diagnose_unmatched`` complements the optimum with per-transaction
diagnostics: for every unmatched flow it replays the legality filters in a
fixed order over every combination that could have contained it and reports
the first filter that leaves nothing, or an optimisation conflict when legal
candidates existed but the optimum selected none of them.

Group legality rules:
  * shapes: 1-1, or a single record on one side with 2-3 on the other;
  * every amount in the group shares the same sign;
  * the sums of both sides are equal;
  * the span between the earliest and latest timestamps is <= 48 hours.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from typing import Literal, Sequence

MAX_SPAN_SECONDS = 48 * 3600  # 48 hours, inclusive
# (left count, right count) shapes: 1-1 plus one-to-many (2..3) in both directions.
GROUP_SHAPES = ((1, 1), (1, 2), (1, 3), (2, 1), (3, 1))


@dataclass(frozen=True)
class Txn:
    id: str
    ts: float  # UTC epoch seconds
    amount: int  # signed cents, non-zero


@dataclass(frozen=True)
class Candidate:
    """A legal reconciliation group."""

    mask: int  # bitmask over the concatenated transaction list (left first)
    weight: int  # absolute reconciled amount
    left_ids: tuple[str, ...]  # ascending
    right_ids: tuple[str, ...]  # ascending
    amount: int  # signed reconciled amount (sum of either side)

    @property
    def encoding(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        return (self.left_ids, self.right_ids)


@dataclass(frozen=True)
class LockedGroup:
    """An operator-confirmed group, referenced by batch transaction ids."""

    left_ids: tuple[str, ...]
    right_ids: tuple[str, ...]


@dataclass(frozen=True)
class ExcludedPair:
    """A (left id, right id) pair that must never share a reconciliation group."""

    left_id: str
    right_id: str


# Reasons why an unmatched transaction stayed out of the optimal solution,
# reported by ``diagnose_unmatched``.
DiagnosticReason = Literal[
    "no_counterparty",
    "sign_mismatch",
    "amount_mismatch",
    "time_window",
    "excluded_pair",
    "optimization_conflict",
]


@dataclass(frozen=True)
class UnmatchedDiagnostic:
    """Diagnosis for one transaction the optimal solution left unmatched.

    ``candidates`` lists the transaction ids of the opposite side that appear
    in at least one fully legal group with the diagnosed transaction (empty
    unless the reason is ``optimization_conflict``); they are deduplicated and
    ordered by the opposite side's input order.
    """

    id: str
    side: Literal["left", "right"]
    reason: DiagnosticReason
    candidates: tuple[str, ...]


class LockedGroupError(ValueError):
    """A locked group fails validation; ``index`` is its 0-based list index."""

    def __init__(self, index: int, message: str) -> None:
        self.index = index
        super().__init__(f"locked_groups[{index}]: {message}")


class ExcludedPairError(ValueError):
    """An excluded pair fails validation; ``index`` is its 0-based list index."""

    def __init__(self, index: int, message: str) -> None:
        self.index = index
        super().__init__(f"excluded_pairs[{index}]: {message}")


class ProposedGroupError(ValueError):
    """A proposed group fails audit validation.

    ``index`` is the group's 0-based index in ``proposed_groups`` and
    ``code`` is a stable machine-readable error type used verbatim as the
    FastAPI error ``type``.
    """

    def __init__(self, index: int, code: str, message: str) -> None:
        self.index = index
        self.code = code
        super().__init__(f"proposed_groups[{index}]: {message}")


def resolve_excluded_pairs(
    left_ids: set[str],
    right_ids: set[str],
    pairs: Sequence[ExcludedPair],
) -> set[tuple[str, str]]:
    """Validate operator-declared excluded pairs.

    Checks, in input order: the left id must exist on the left side and the
    right id on the right side (an id on the wrong side is reported as such),
    and the same (left_id, right_id) pair may not appear twice.  The first
    offending pair's 0-based index is reported.  Returns the validated pairs.
    """
    seen: set[tuple[str, str]] = set()
    resolved: set[tuple[str, str]] = set()
    for pi, pair in enumerate(pairs):
        lid, rid = pair.left_id, pair.right_id
        if lid not in left_ids:
            if lid in right_ids:
                raise ExcludedPairError(
                    pi, f"left_id {lid!r} references a right-side transaction"
                )
            raise ExcludedPairError(pi, f"unknown left transaction id: {lid}")
        if rid not in right_ids:
            if rid in left_ids:
                raise ExcludedPairError(
                    pi, f"right_id {rid!r} references a left-side transaction"
                )
            raise ExcludedPairError(pi, f"unknown right transaction id: {rid}")
        if (lid, rid) in seen:
            raise ExcludedPairError(pi, f"duplicate excluded pair: ({lid}, {rid})")
        seen.add((lid, rid))
        resolved.add((lid, rid))
    return resolved


def resolve_locked_groups(
    rec: "Reconciler",
    locked: Sequence[LockedGroup],
) -> list[Candidate]:
    """Validate operator-confirmed groups and resolve them to candidates.

    Checks, in input order: duplicated ids inside a single group, references to
    ids that do not exist on the referenced side, reuse of a transaction across
    groups, presence of an operator-declared excluded pair, and the common
    legality rules (shape / sign / equal sums / 48h).
    The first offending group's 0-based index is reported.
    """
    left_ids = {t.id for t in rec.left}
    right_ids = {t.id for t in rec.right}
    resolved: list[Candidate] = []
    seen: set[str] = set()
    for gi, grp in enumerate(locked):
        lids = list(grp.left_ids)
        rids = list(grp.right_ids)
        if len(set(lids)) != len(lids) or len(set(rids)) != len(rids):
            raise LockedGroupError(gi, "duplicate transaction id inside group")
        unknown = [i for i in lids if i not in left_ids]
        unknown += [i for i in rids if i not in right_ids]
        if unknown:
            raise LockedGroupError(gi, f"unknown transaction id: {sorted(set(unknown))[0]}")
        reused = sorted({i for i in lids + rids if i in seen})
        if reused:
            raise LockedGroupError(gi, f"transaction reused by another group: {reused[0]}")
        seen.update(lids)
        seen.update(rids)
        excluded_hit = sorted(
            (lid, rid)
            for lid, rid in rec.excluded_pairs
            if lid in lids and rid in rids
        )
        if excluded_hit:
            lid, rid = excluded_hit[0]
            raise LockedGroupError(
                gi, f"group contains an excluded pair: ({lid}, {rid})"
            )
        enc = (tuple(sorted(lids)), tuple(sorted(rids)))
        cand = rec.by_encoding.get(enc)
        if cand is None:
            raise LockedGroupError(gi, "group violates shape/sign/amount/48h rules")
        resolved.append(cand)
    return resolved


def resolve_proposed_groups(
    rec: "Reconciler",
    proposed: Sequence[LockedGroup],
) -> tuple[list[Candidate | None], list[ProposedGroupError]]:
    """Audit an externally supplied complete reconciliation plan.

    Every group is checked independently -- one bad group never aborts the
    checks for the others -- and at most one error per group is returned,
    reporting its first violated rule in a fixed priority order.  The result
    pair is ``(resolved, errors)``: ``resolved[i]`` is the legal candidate for
    group ``i`` (or ``None`` when the group is illegal), and ``errors`` is
    sorted by group index.

    Per-group checks, in priority order:
      1. a duplicated id inside one side of the group;
      2. a reference to an id that does not exist on the referenced side
         (an id from the opposite side is reported as a wrong-side reference);
      3. reuse of a transaction that an earlier group (in array order) or a
         locked group already uses;
      4. containing both members of an operator-declared excluded pair;
      5. the common legality rules: shape, common sign, equal side sums,
         and the 48-hour timestamp span.

    Cross-group reuse is attributed to the later group claiming the
    transaction; a group with an earlier (per-group) error still claims its
    correctly-referenced transactions, so downstream reuse stays visible.
    """
    left_txns = {t.id: t for t in rec.left}
    right_txns = {t.id: t for t in rec.right}

    # Transactions consumed by locked groups; a proposed group whose encoding
    # is exactly one of the locked groups restates it (part of the complete
    # plan) and is accepted instead of being reported as reuse.
    locked_by_enc = {cand.encoding: cand for cand in rec.locked}
    locked_ids: set[str] = set()
    for cand in rec.locked:
        locked_ids.update(cand.left_ids)
        locked_ids.update(cand.right_ids)
    # Ids already used by earlier proposed groups (in array order).
    claimed: set[str] = set()
    # Locked encodings already restated by an earlier proposed group.
    restated: set[tuple[tuple[str, ...], tuple[str, ...]]] = set()

    resolved: list[Candidate | None] = []
    errors: list[ProposedGroupError] = []
    for gi, grp in enumerate(proposed):
        lids = list(grp.left_ids)
        rids = list(grp.right_ids)
        ok = True

        def fail(code: str, message: str) -> None:
            nonlocal ok
            ok = False
            errors.append(ProposedGroupError(gi, code, message))

        # 1. duplicated id inside one side of the group
        if len(set(lids)) != len(lids) or len(set(rids)) != len(rids):
            fail("proposed_group.duplicate_id", "duplicate transaction id inside group")
        else:
            # 2. references: opposite-side ids first, then truly unknown ids
            wrong = next((i for i in lids if i in right_txns), None)
            if wrong is not None:
                fail(
                    "proposed_group.wrong_side",
                    f"left_ids entry {wrong!r} references a right-side transaction",
                )
            else:
                wrong = next((i for i in rids if i in left_txns), None)
                if wrong is not None:
                    fail(
                        "proposed_group.wrong_side",
                        f"right_ids entry {wrong!r} references a left-side transaction",
                    )
                else:
                    unknown = next((i for i in lids if i not in left_txns), None)
                    if unknown is None:
                        unknown = next((i for i in rids if i not in right_txns), None)
                    if unknown is not None:
                        fail(
                            "proposed_group.unknown_id",
                            f"unknown transaction id: {unknown}",
                        )

        # Correctly-referenced ids are claimed even when the group already has
        # an error (e.g. a duplicate on the other side), keeping later reuse
        # deterministic.
        valid_lids = [i for i in lids if i in left_txns and i not in right_txns]
        valid_rids = [i for i in rids if i in right_txns and i not in left_txns]

        if ok:
            # 3. cross-group mutual exclusion.  A group restating a locked
            # group (same encoding) is that locked group, not reuse; every
            # other overlap with a locked group or an earlier proposed group
            # is blamed on this (later) group.  A locked group may be restated
            # at most once.
            enc = (tuple(sorted(valid_lids)), tuple(sorted(valid_rids)))
            restating_locked = enc in locked_by_enc and enc not in restated
            if not restating_locked:
                used = locked_ids | claimed
                reused = next((i for i in valid_lids + valid_rids if i in used), None)
                if reused is not None:
                    fail(
                        "proposed_group.transaction_reused",
                        f"transaction reused by another group: {reused}",
                    )

        if ok:
            # 4. operator-declared excluded pair
            excluded_hit = sorted(
                (lid, rid)
                for lid, rid in rec.excluded_pairs
                if lid in set(valid_lids) and rid in set(valid_rids)
            )
            if excluded_hit:
                lid, rid = excluded_hit[0]
                fail(
                    "proposed_group.excluded_pair",
                    f"group contains an excluded pair: ({lid}, {rid})",
                )

        if ok:
            # 5. shape
            if (len(lids), len(rids)) not in GROUP_SHAPES:
                fail(
                    "proposed_group.bad_shape",
                    f"illegal group shape: {len(lids)}-{len(rids)}",
                )

        if ok:
            # common sign
            amounts = [left_txns[i].amount for i in valid_lids]
            amounts += [right_txns[i].amount for i in valid_rids]
            if not (all(a > 0 for a in amounts) or all(a < 0 for a in amounts)):
                fail(
                    "proposed_group.mixed_sign",
                    "group mixes positive and negative amounts",
                )

        if ok:
            # equal side sums
            sum_l = sum(left_txns[i].amount for i in valid_lids)
            sum_r = sum(right_txns[i].amount for i in valid_rids)
            if sum_l != sum_r:
                fail(
                    "proposed_group.amount_mismatch",
                    f"side sums differ: left {sum_l} != right {sum_r}",
                )

        if ok:
            # 48-hour span (inclusive)
            times = [left_txns[i].ts for i in valid_lids]
            times += [right_txns[i].ts for i in valid_rids]
            if max(times) - min(times) > MAX_SPAN_SECONDS:
                fail(
                    "proposed_group.outside_48h",
                    "group timestamps span more than 48 hours",
                )

        cand: Candidate | None = None
        if ok:
            cand = rec.by_encoding.get(enc)
            if cand is None:  # pragma: no cover - defensive: checks 1-5 are exhaustive
                fail(
                    "proposed_group.illegal",
                    "group violates shape/sign/amount/48h rules",
                )

        if cand is not None:
            resolved.append(cand)
            if restating_locked:
                restated.add(enc)
            else:
                claimed.update(cand.left_ids)
                claimed.update(cand.right_ids)
        else:
            resolved.append(None)
            claimed.update(valid_lids)
            claimed.update(valid_rids)

    return resolved, errors


def diagnose_unmatched(
    rec: "Reconciler",
    solution: Sequence[Candidate],
) -> list[UnmatchedDiagnostic]:
    """Explain why each unmatched transaction stayed out of ``solution``.

    ``solution`` must be the reconciler's optimal packing (locked groups
    included).  For every transaction the solution leaves unmatched, all
    1-1 / 1-2 / 1-3 / 2-1 / 3-1 combinations are enumerated that contain the
    transaction and no locked-group transaction, and the legality filters are
    applied in a fixed order:

      1. opposite side present (``no_counterparty``);
      2. common sign (``sign_mismatch``);
      3. equal side sums (``amount_mismatch``);
      4. 48-hour span (``time_window``);
      5. operator-declared excluded pairs (``excluded_pair``).

    The reason is the first filter that empties the candidate list; when fully
    legal candidates exist but the optimum picked none of them, the reason is
    ``optimization_conflict``.  The result is ordered left side first, each
    side in input order.
    """
    locked_mask = 0
    for cand in rec.locked:
        locked_mask |= cand.mask
    matched_mask = locked_mask
    for cand in solution:
        matched_mask |= cand.mask

    # (same-side companions, opposite-side members) choices reproducing the
    # five legal group shapes for a group built around one transaction
    SHAPE_CHOICES = ((0, 1), (0, 2), (0, 3), (1, 1), (2, 1))

    n_left, amounts, txns = rec.n_left, rec.amounts, rec.txns
    diagnostics: list[UnmatchedDiagnostic] = []
    for t, txn in enumerate(txns):
        if matched_mask & (1 << t):
            continue
        if t < n_left:
            side: Literal["left", "right"] = "left"
            same = [
                i for i in range(n_left) if i != t and not locked_mask & (1 << i)
            ]
            opp = [
                n_left + j
                for j in range(len(rec.right))
                if not locked_mask & (1 << (n_left + j))
            ]
        else:
            side = "right"
            same = [
                n_left + j
                for j in range(len(rec.right))
                if n_left + j != t and not locked_mask & (1 << (n_left + j))
            ]
            opp = [i for i in range(n_left) if not locked_mask & (1 << i)]
        if not opp:
            diagnostics.append(UnmatchedDiagnostic(txn.id, side, "no_counterparty", ()))
            continue

        # Same-sign combinations only: with every amount non-zero a group
        # summing to the same value on both sides can never mix signs, so the
        # amount filter would discard mixed-sign combinations anyway.
        sign_same = [i for i in same if amounts[i] * amounts[t] > 0]
        sign_opp = [i for i in opp if amounts[i] * amounts[t] > 0]
        combos = [
            (t,) + companions + partners
            for n_same, n_opp in SHAPE_CHOICES
            for companions in combinations(sign_same, n_same)
            for partners in combinations(sign_opp, n_opp)
        ]
        if not combos:
            diagnostics.append(UnmatchedDiagnostic(txn.id, side, "sign_mismatch", ()))
            continue

        combos = [
            combo
            for combo in combos
            if sum(amounts[i] for i in combo if i < n_left)
            == sum(amounts[i] for i in combo if i >= n_left)
        ]
        if not combos:
            diagnostics.append(UnmatchedDiagnostic(txn.id, side, "amount_mismatch", ()))
            continue

        combos = [
            combo
            for combo in combos
            if max(txns[i].ts for i in combo) - min(txns[i].ts for i in combo)
            <= MAX_SPAN_SECONDS
        ]
        if not combos:
            diagnostics.append(UnmatchedDiagnostic(txn.id, side, "time_window", ()))
            continue

        combos = [
            combo
            for combo in combos
            if not any(
                li in combo and ri in combo for li, ri in rec.excluded_index_pairs
            )
        ]
        if not combos:
            diagnostics.append(UnmatchedDiagnostic(txn.id, side, "excluded_pair", ()))
            continue

        t_on_left = t < n_left
        counterparties = tuple(
            dict.fromkeys(
                i for combo in combos for i in combo if (i < n_left) != t_on_left
            )
        )
        diagnostics.append(
            UnmatchedDiagnostic(
                txn.id,
                side,
                "optimization_conflict",
                tuple(txns[i].id for i in counterparties),
            )
        )
    return diagnostics


class Reconciler:
    def __init__(
        self,
        left: list[Txn],
        right: list[Txn],
        locked: Sequence[Candidate] = (),
        excluded_pairs: Sequence[tuple[str, str]] = (),
    ) -> None:
        self.left = left
        self.right = right
        self.txns = left + right
        self.n_left = len(left)
        self.locked: list[Candidate] = list(locked)
        self.left_index = {t.id: i for i, t in enumerate(left)}
        self.right_index = {t.id: i for i, t in enumerate(right)}
        # excluded pairs kept both as (left id, right id) -- for locked-group
        # validation -- and as global transaction index pairs used to prune
        # generated candidates
        self.excluded_pairs: frozenset[tuple[str, str]] = frozenset(excluded_pairs)
        self.excluded_index_pairs: frozenset[tuple[int, int]] = frozenset(
            (self.left_index[lid], self.n_left + self.right_index[rid])
            for lid, rid in self.excluded_pairs
        )
        self.amounts = [t.amount for t in self.txns]
        self.candidates: list[Candidate] = self._generate_candidates()
        # encoding -> candidate; each encoding maps to exactly one candidate
        self.by_encoding: dict[tuple[tuple[str, ...], tuple[str, ...]], Candidate] = {
            c.encoding: c for c in self.candidates
        }
        # candidate indices touching each transaction, heavier first
        self.cand_by_txn: list[list[int]] = [[] for _ in self.txns]
        for ci, cand in enumerate(self.candidates):
            m = cand.mask
            while m:
                b = m & (-m)
                self.cand_by_txn[b.bit_length() - 1].append(ci)
                m &= m - 1
        for lst in self.cand_by_txn:
            lst.sort(key=lambda ci: -self.candidates[ci].weight)
        self._memo: dict[int, int] = {0: 0}

    # ------------------------------------------------------------------ #
    # candidate generation
    # ------------------------------------------------------------------ #
    def _contains_excluded_pair(self, lcomb, rcomb) -> bool:
        """True if the group formed by the two index combinations includes
        both members of any operator-declared excluded pair."""
        rglobal = {self.n_left + j for j in rcomb}
        return any(
            li in lcomb and ri in rglobal for li, ri in self.excluded_index_pairs
        )

    def _generate_candidates(self) -> list[Candidate]:
        cands: list[Candidate] = []
        left, right, n_left = self.left, self.right, self.n_left
        for size_l, size_r in GROUP_SHAPES:
            if size_l > len(left) or size_r > len(right):
                continue
            for lcomb in combinations(range(len(left)), size_l):
                sum_l = sum(left[i].amount for i in lcomb)
                for rcomb in combinations(range(len(right)), size_r):
                    sum_r = sum(right[j].amount for j in rcomb)
                    if sum_l != sum_r:
                        continue
                    amounts = [left[i].amount for i in lcomb] + [right[j].amount for j in rcomb]
                    if not (all(a > 0 for a in amounts) or all(a < 0 for a in amounts)):
                        continue
                    if self._contains_excluded_pair(lcomb, rcomb):
                        continue
                    times = [left[i].ts for i in lcomb] + [right[j].ts for j in rcomb]
                    if max(times) - min(times) > MAX_SPAN_SECONDS:
                        continue
                    mask = 0
                    for i in lcomb:
                        mask |= 1 << i
                    for j in rcomb:
                        mask |= 1 << (n_left + j)
                    cands.append(
                        Candidate(
                            mask=mask,
                            weight=abs(sum_l),
                            left_ids=tuple(sorted(left[i].id for i in lcomb)),
                            right_ids=tuple(sorted(right[j].id for j in rcomb)),
                            amount=sum_l,
                        )
                    )
        return cands

    # ------------------------------------------------------------------ #
    # maximum total weight over a transaction subset (bitmask)
    # ------------------------------------------------------------------ #
    def _max_value(self, mask: int) -> int:
        memo = self._memo
        cached = memo.get(mask)
        if cached is not None:
            return cached

        # Upper bound: every group consumes |amount| from each side, so the
        # achievable total is capped by either side's remaining absolute sum.
        sum_l = sum_r = 0
        m = mask
        n_left, amounts = self.n_left, self.amounts
        while m:
            b = m & (-m)
            i = b.bit_length() - 1
            if i < n_left:
                sum_l += abs(amounts[i])
            else:
                sum_r += abs(amounts[i])
            m &= m - 1
        ub = min(sum_l, sum_r)
        if ub == 0:
            memo[mask] = 0
            return 0

        best = self._greedy_lower_bound(mask)
        if best < ub:
            t = self._choose_txn(mask)
            cands = self.candidates
            # branch: t belongs to one of the candidates
            for ci in self.cand_by_txn[t]:
                cm = cands[ci].mask
                if cm & mask == cm:
                    v = cands[ci].weight + self._max_value(mask ^ cm)
                    if v > best:
                        best = v
                        if best == ub:
                            break
            # branch: t stays unmatched
            if best < ub:
                v = self._max_value(mask & ~(1 << t))
                if v > best:
                    best = v
        memo[mask] = best
        return best

    def _greedy_lower_bound(self, mask: int) -> int:
        value = 0
        m = mask
        cands = self.candidates
        while m:
            b = m & (-m)
            t = b.bit_length() - 1
            placed = False
            for ci in self.cand_by_txn[t]:
                cm = cands[ci].mask
                if cm & m == cm:
                    value += cands[ci].weight
                    m ^= cm
                    placed = True
                    break
            if not placed:
                m &= m - 1
        return value

    def _choose_txn(self, mask: int) -> int:
        """Pick the transaction with the fewest currently usable candidates."""
        best_t = -1
        best_count: int | None = None
        m = mask
        cands = self.candidates
        while m:
            b = m & (-m)
            t = b.bit_length() - 1
            m &= m - 1
            cnt = 0
            for ci in self.cand_by_txn[t]:
                cm = cands[ci].mask
                if cm & mask == cm:
                    cnt += 1
                    if best_count is not None and cnt >= best_count:
                        break
            if best_count is None or cnt < best_count:
                best_count = cnt
                best_t = t
                if cnt == 0:
                    break
        return best_t

    # ------------------------------------------------------------------ #
    # optimal solution with lexicographically smallest sorted encodings
    # ------------------------------------------------------------------ #
    def solve(self, locked: Sequence[Candidate] | None = None) -> list[Candidate]:
        """Return the optimal packing, forcing every ``locked`` group in.

        Locked groups are removed from the available transaction pool; the
        global maximum and the lexicographic tie-break are computed over the
        remaining transactions only, then merged back with the locked groups.
        """
        if locked is not None:
            self.locked = list(locked)
        lock_mask = 0
        for cand in self.locked:
            if cand.mask & lock_mask:
                raise ValueError("locked groups overlap")  # pragma: no cover - guarded upstream
            lock_mask |= cand.mask
        if not self.candidates:
            return sorted(self.locked, key=lambda c: c.encoding)
        full = (1 << len(self.txns)) - 1
        free = full ^ lock_mask
        remaining_value = self._max_value(free)
        # A candidate can open an optimal residual solution iff the rest still
        # achieves the remaining optimum; taking the smallest such encoding at
        # every step yields the lexicographically smallest sorted encoding list.
        # Candidates touching a locked transaction never fit ``mask``, so they
        # are excluded automatically.
        ordered = sorted(range(len(self.candidates)), key=lambda ci: self.candidates[ci].encoding)
        solution: list[Candidate] = []
        mask = free
        while remaining_value > 0:
            for ci in ordered:
                cand = self.candidates[ci]
                if cand.weight > remaining_value or cand.mask & mask != cand.mask:
                    continue
                if self._max_value(mask ^ cand.mask) == remaining_value - cand.weight:
                    solution.append(cand)
                    mask ^= cand.mask
                    remaining_value -= cand.weight
                    break
            else:  # pragma: no cover - defensive, cannot happen if _max_value is exact
                raise RuntimeError("no feasible candidate for the remaining optimum")
        return sorted(self.locked + solution, key=lambda c: c.encoding)
