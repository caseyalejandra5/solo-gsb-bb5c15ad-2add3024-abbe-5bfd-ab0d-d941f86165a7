"""Pydantic schemas for the batch reconciliation API."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, StrictInt, StrictStr, field_validator

MAX_TRANSACTIONS_PER_SIDE = 12


class TransactionIn(BaseModel):
    """One fund-flow record.

    - ``id``: batch-unique identifier (non-empty string).
    - ``time``: ISO-8601 timestamp; naive values are treated as UTC.
    - ``amount``: non-zero signed integer amount in cents.
    """

    id: StrictStr = Field(min_length=1, max_length=128)
    time: datetime
    amount: StrictInt

    @field_validator("amount")
    @classmethod
    def amount_must_be_non_zero(cls, v: int) -> int:
        if v == 0:
            raise ValueError("amount must be a non-zero integer (cents)")
        return v

    def epoch_seconds(self) -> float:
        """Return the timestamp as UTC epoch seconds."""
        dt = self.time
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()


class LockedGroupIn(BaseModel):
    """An operator-confirmed group, referencing transactions of this batch.

    ``left_ids`` must reference ids from ``left`` and ``right_ids`` ids from
    ``right``; the referenced transactions must form a legal group. Semantic
    validation (unknown ids, reuse across groups, group legality) happens in
    the endpoint and returns 422 with the offending group's index.
    """

    left_ids: list[StrictStr] = Field(min_length=1, max_length=3)
    right_ids: list[StrictStr] = Field(min_length=1, max_length=3)


class ExcludedPairIn(BaseModel):
    """An operator-declared pair that must never share a reconciliation group.

    ``left_id`` must reference an id from ``left`` and ``right_id`` an id from
    ``right``.  Semantic validation (unknown ids, wrong side, duplicate pairs)
    happens in the endpoint and returns 422 with the offending pair's index.
    """

    left_id: StrictStr = Field(min_length=1, max_length=128)
    right_id: StrictStr = Field(min_length=1, max_length=128)


class ReconcileRequest(BaseModel):
    """Batch of fund flows from two sides (at most 12 each)."""

    left: list[TransactionIn] = Field(default_factory=list, max_length=MAX_TRANSACTIONS_PER_SIDE)
    right: list[TransactionIn] = Field(default_factory=list, max_length=MAX_TRANSACTIONS_PER_SIDE)
    locked_groups: list[LockedGroupIn] | None = Field(
        default=None,
        description="Optional operator-confirmed groups forced into the solution.",
    )
    excluded_pairs: list[ExcludedPairIn] | None = Field(
        default=None,
        description=(
            "Optional (left_id, right_id) pairs that must never end up in the "
            "same reconciliation group."
        ),
    )


class AuditRequest(ReconcileRequest):
    """Batch fields identical to ``ReconcileRequest`` plus an external plan.

    ``proposed_groups`` is the complete reconciliation plan to be audited;
    every group uses the same shape as ``locked_groups``.  Semantic
    validation (unknown ids, reuse, excluded pairs, group legality) runs per
    group and returns 422 with one error entry per offending group index.
    """

    proposed_groups: list[LockedGroupIn] = Field(
        description="The externally proposed complete reconciliation plan.",
    )


class GroupOut(BaseModel):
    """One reconciliation group: sorted id lists of both sides."""

    left_ids: list[str]
    right_ids: list[str]
    amount: int  # signed reconciled amount in cents (sum of either side)


class ReconcileResponse(BaseModel):
    groups: list[GroupOut]
    total_amount: int  # sum of absolute reconciled amounts across groups
    unmatched_left_ids: list[str]
    unmatched_right_ids: list[str]


SuboptimalReason = Literal["amount", "lexicographic"]


class AuditResponse(BaseModel):
    """Outcome of auditing an external complete reconciliation plan.

    - ``valid``: every proposed group passed all checks;
    - ``optimal``: the valid plan equals the canonical optimum (maximum total
      amount, then the lexicographically smallest sorted group-encoding list);
    - ``suboptimal_reason``: why a valid plan is not canonical -- ``"amount"``
      for a smaller reconciled total, ``"lexicographic"`` when the totals tie
      but another encoding list is the canonical tie-break winner;
    - ``proposed_total`` / ``optimal_total``: sums of absolute group amounts
      for the proposed plan and the canonical solution (locked groups included);
    - ``groups``: the canonical solution groups (sorted by group encoding) so
      the caller can correct a non-optimal plan.
    """

    valid: bool
    optimal: bool
    suboptimal_reason: SuboptimalReason | None = None
    proposed_total: int
    optimal_total: int
    groups: list[GroupOut]


class UnmatchedSummaryRequest(ReconcileRequest):
    """Reconcile batch plus the IANA timezone used to localise unmatched flows.

    The batch fields and their validation are identical to
    ``ReconcileRequest``; ``timezone`` must be a valid IANA key such as
    ``"Asia/Shanghai"`` — anything else is rejected with 422.
    """

    timezone: StrictStr = Field(min_length=1, max_length=64)

    @field_validator("timezone")
    @classmethod
    def timezone_must_be_iana_key(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown IANA timezone: {v!r}") from exc
        return v


class SignSummary(BaseModel):
    """Count and absolute amount (cents) of unmatched flows of one sign."""

    count: int = 0
    amount: int = 0  # sum of absolute amounts in cents


class SignBreakdown(BaseModel):
    """Unmatched flows split by amount sign."""

    positive: SignSummary = Field(default_factory=SignSummary)
    negative: SignSummary = Field(default_factory=SignSummary)


class DaySummary(SignBreakdown):
    """Unmatched flows attributed to one local calendar day (YYYY-MM-DD)."""

    date: date


class UnmatchedSummaryResponse(BaseModel):
    timezone: str  # validated IANA key the local dates were computed with
    days: list[DaySummary]  # ascending by local date; only days with unmatched flows
    total: SignBreakdown  # whole-batch totals across all days
