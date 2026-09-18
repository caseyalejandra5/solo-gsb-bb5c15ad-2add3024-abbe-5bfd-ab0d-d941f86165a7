"""Batch fund-flow reconciliation API."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Header, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import Response

from .idempotency import FingerprintConflict, canonical_fingerprint, store
from .models import (
    AuditRequest,
    AuditResponse,
    DaySummary,
    DiagnosticsResponse,
    DiagnosticEntry,
    GroupOut,
    ReconcileRequest,
    ReconcileResponse,
    SignBreakdown,
    UnmatchedSummaryRequest,
    UnmatchedSummaryResponse,
)
from .solver import (
    ExcludedPair,
    ExcludedPairError,
    LockedGroup,
    LockedGroupError,
    Reconciler,
    Txn,
    resolve_excluded_pairs,
    resolve_locked_groups,
    resolve_proposed_groups,
)

IDEMPOTENCY_KEY_MIN = 1
IDEMPOTENCY_KEY_MAX = 128


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    store.close()


app = FastAPI(
    title="Batch Reconciliation API",
    version="1.0.0",
    summary="Match fund flows from two sides into optimal reconciliation groups.",
    lifespan=lifespan,
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def prepare_batch(payload: ReconcileRequest) -> tuple[Reconciler, list]:
    """Validate batch-level fields and build the reconciler with locked groups.

    Shared by ``/reconcile``, ``/reconcile/unmatched-summary`` and
    ``/reconcile/audit`` so the three endpoints share identical 400/422
    behaviour for duplicate ids, excluded pairs and locked groups.  Returns
    the reconciler (with locked groups resolved) and the locked candidates.
    """
    ids = [t.id for t in payload.left] + [t.id for t in payload.right]
    if len(set(ids)) != len(ids):
        raise HTTPException(status_code=400, detail="duplicate transaction id in batch")

    left = [Txn(id=t.id, ts=t.epoch_seconds(), amount=t.amount) for t in payload.left]
    right = [Txn(id=t.id, ts=t.epoch_seconds(), amount=t.amount) for t in payload.right]

    excluded: set[tuple[str, str]] = set()
    if payload.excluded_pairs:
        pairs_in = [
            ExcludedPair(left_id=p.left_id, right_id=p.right_id)
            for p in payload.excluded_pairs
        ]
        try:
            excluded = resolve_excluded_pairs(
                {t.id for t in left}, {t.id for t in right}, pairs_in
            )
        except ExcludedPairError as exc:
            raise RequestValidationError(
                [
                    {
                        "type": "excluded_pair.invalid",
                        "loc": ["body", "excluded_pairs", exc.index],
                        "msg": str(exc),
                        "input": payload.excluded_pairs[exc.index].model_dump(),
                    }
                ]
            )

    reconciler = Reconciler(left, right, excluded_pairs=excluded)

    locked_cands = []
    if payload.locked_groups:
        locked_in = [
            LockedGroup(left_ids=tuple(g.left_ids), right_ids=tuple(g.right_ids))
            for g in payload.locked_groups
        ]
        try:
            locked_cands = resolve_locked_groups(reconciler, locked_in)
        except LockedGroupError as exc:
            raise RequestValidationError(
                [
                    {
                        "type": "locked_group.invalid",
                        "loc": ["body", "locked_groups", exc.index],
                        "msg": str(exc),
                        "input": payload.locked_groups[exc.index].model_dump(),
                    }
                ]
            )

    reconciler.locked = locked_cands
    return reconciler, locked_cands


def _solve_groups(payload: ReconcileRequest) -> tuple[Reconciler, list]:
    """Validate the batch and return ``(reconciler, sorted optimal groups)``."""
    reconciler, _ = prepare_batch(payload)
    groups = reconciler.solve()
    groups.sort(key=lambda c: c.encoding)
    return reconciler, groups


def _result_fields(
    payload: ReconcileRequest, groups: list
) -> tuple[list[GroupOut], int, list[str], list[str]]:
    """Build the standard response fields from a solved group list."""
    matched_left = {i for c in groups for i in c.left_ids}
    matched_right = {i for c in groups for i in c.right_ids}
    return (
        [
            GroupOut(left_ids=list(c.left_ids), right_ids=list(c.right_ids), amount=c.amount)
            for c in groups
        ],
        sum(c.weight for c in groups),
        sorted(t.id for t in payload.left if t.id not in matched_left),
        sorted(t.id for t in payload.right if t.id not in matched_right),
    )


def solve(payload: ReconcileRequest) -> ReconcileResponse:
    """Validate the batch and compute the reconciliation result."""
    _, groups = _solve_groups(payload)
    out_groups, total, unmatched_left, unmatched_right = _result_fields(payload, groups)
    return ReconcileResponse(
        groups=out_groups,
        total_amount=total,
        unmatched_left_ids=unmatched_left,
        unmatched_right_ids=unmatched_right,
    )


def _stored_response(body: str, replayed: bool) -> Response:
    # Return the stored bytes verbatim; ``Response`` instances bypass the
    # ``response_model`` serialization, so replays are byte-for-byte identical
    # to the first response.
    headers = {"Idempotency-Replayed": "true"} if replayed else None
    return Response(status_code=200, content=body, media_type="application/json", headers=headers)


@app.post(
    "/reconcile",
    response_model=ReconcileResponse,
    responses={
        409: {"description": "Idempotency-Key reused with a different request body."},
        200: {
            "headers": {
                "Idempotency-Replayed": {
                    "description": "'true' when a stored response was replayed for the key.",
                    "schema": {"type": "string", "enum": ["true"]},
                }
            }
        },
    },
)
def reconcile(
    payload: ReconcileRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Any:
    # The endpoint behaves exactly as before when no key is supplied.
    if idempotency_key is None:
        return solve(payload)

    if not (IDEMPOTENCY_KEY_MIN <= len(idempotency_key) <= IDEMPOTENCY_KEY_MAX):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Idempotency-Key must be {IDEMPOTENCY_KEY_MIN}-"
                f"{IDEMPOTENCY_KEY_MAX} characters long"
            ),
        )

    fingerprint = canonical_fingerprint(payload)

    def producer() -> str:
        # Serialised with FastAPI's default JSON encoder so first-time and
        # replayed responses are byte-for-byte identical.
        return json.dumps(solve(payload).model_dump(), separators=(",", ":"), ensure_ascii=False)

    try:
        result = store.handle(idempotency_key, fingerprint, producer)
    except FingerprintConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    return _stored_response(result.body, result.replayed)


@app.post("/reconcile/diagnostics", response_model=DiagnosticsResponse)
def diagnostics(payload: ReconcileRequest) -> DiagnosticsResponse:
    """Explain why each transaction left unmatched by the optimum is unmatched.

    Runs the exact same validation and optimal reconciliation as
    ``POST /reconcile`` (same 400/422 behaviour), then for every unmatched
    transaction enumerates all 1-1 / 1-2 / 1-3 / 2-1 / 3-1 combinations that
    contain it without using any locked transaction (transactions matched by
    the optimum are allowed as counterparties), and applies the counterparty,
    sign, equal-amount, 48-hour and excluded-pair filters in order.  The first
    filter that empties the set is reported as ``no_counterparty``,
    ``sign_mismatch``, ``amount_mismatch``, ``time_window`` or
    ``excluded_pair``; a transaction with fully legal candidates that is still
    unmatched gets ``optimization_conflict``.

    The response carries the standard ``groups`` / ``total_amount`` /
    ``unmatched_*`` fields plus a ``diagnostics`` list covering exactly the
    unmatched transactions, ordered left side first then right side, each side
    in request input order.  This endpoint does not participate in
    ``Idempotency-Key`` replay.
    """
    reconciler, groups = _solve_groups(payload)
    out_groups, total, unmatched_left, unmatched_right = _result_fields(payload, groups)
    diag_rows = reconciler.diagnose(groups)
    return DiagnosticsResponse(
        groups=out_groups,
        total_amount=total,
        unmatched_left_ids=unmatched_left,
        unmatched_right_ids=unmatched_right,
        diagnostics=[
            DiagnosticEntry(side=side, transaction_id=tid, reason=reason)
            for side, tid, reason in diag_rows
        ],
    )


@app.post("/reconcile/unmatched-summary", response_model=UnmatchedSummaryResponse)
def unmatched_summary(payload: UnmatchedSummaryRequest) -> UnmatchedSummaryResponse:
    """Aggregate unmatched flows by local day and amount sign.

    Runs the exact same validation and reconciliation as ``POST /reconcile``
    (same 400/422 behaviour), then attributes every unmatched transaction to
    its local calendar day in the requested IANA timezone -- DST transitions
    included -- and aggregates count and absolute amount per sign, plus
    whole-batch totals.
    """
    result = solve(payload)
    tz = ZoneInfo(payload.timezone)
    by_id = {t.id: t for t in payload.left + payload.right}

    per_day: dict[date, SignBreakdown] = {}
    total = SignBreakdown()
    for tid in result.unmatched_left_ids + result.unmatched_right_ids:
        txn = by_id[tid]
        day = datetime.fromtimestamp(txn.epoch_seconds(), tz=tz).date()
        bucket = per_day.setdefault(day, SignBreakdown())
        for acc in (bucket, total):
            sign = acc.positive if txn.amount > 0 else acc.negative
            sign.count += 1
            sign.amount += abs(txn.amount)

    return UnmatchedSummaryResponse(
        timezone=payload.timezone,
        days=[
            DaySummary(date=day, positive=b.positive, negative=b.negative)
            for day, b in sorted(per_day.items())
        ],
        total=total,
    )


@app.post("/reconcile/audit", response_model=AuditResponse)
def audit(payload: AuditRequest) -> AuditResponse:
    """Audit an externally supplied complete reconciliation plan.

    The batch fields (``left`` / ``right`` / ``locked_groups`` /
    ``excluded_pairs``) are validated exactly as for ``POST /reconcile``.
    Every group in ``proposed_groups`` is then checked independently against
    the reference, shape, common-sign, equal-sum, 48-hour, excluded-pair and
    cross-group mutual-exclusion rules; one bad group never aborts the checks
    for the others, and errors are returned stably keyed by the group's
    0-based index (HTTP 422).

    When the whole plan is legal, the canonical optimum (maximum total
    absolute amount, then lexicographically smallest sorted group-encoding
    list) is computed and compared with the proposal: a strictly smaller
    total is reported as ``"amount"`` sub-optimality, while equal totals with
    a different encoding list are reported as ``"lexicographic"``.  The
    canonical ``groups`` are always returned so the caller can correct the
    plan.  This endpoint does not participate in ``Idempotency-Key`` replay.
    """
    reconciler, locked_cands = prepare_batch(payload)

    proposed_in = [
        LockedGroup(left_ids=tuple(g.left_ids), right_ids=tuple(g.right_ids))
        for g in payload.proposed_groups
    ]
    resolved, errors = resolve_proposed_groups(reconciler, proposed_in)
    if errors:
        raise RequestValidationError(
            [
                {
                    "type": err.code,
                    "loc": ["body", "proposed_groups", err.index],
                    "msg": str(err),
                    "input": payload.proposed_groups[err.index].model_dump(),
                }
                for err in errors
            ]
        )

    proposed_cands = [c for c in resolved if c is not None]
    canonical = sorted(reconciler.solve(), key=lambda c: c.encoding)

    proposed_total = sum(c.weight for c in proposed_cands)
    optimal_total = sum(c.weight for c in canonical)
    proposed_encoding = sorted(c.encoding for c in proposed_cands)
    canonical_encoding = [c.encoding for c in canonical]

    if proposed_encoding == canonical_encoding:
        optimal = True
        reason = None
    else:
        optimal = False
        reason = "amount" if proposed_total < optimal_total else "lexicographic"

    return AuditResponse(
        valid=True,
        optimal=optimal,
        suboptimal_reason=reason,
        proposed_total=proposed_total,
        optimal_total=optimal_total,
        groups=[
            GroupOut(left_ids=list(c.left_ids), right_ids=list(c.right_ids), amount=c.amount)
            for c in canonical
        ],
    )
