"""Payment/refund agent: captures, reconciliation and refund lifecycle per lifecycle row."""

from __future__ import annotations

import json
from collections import Counter
from decimal import Decimal
from itertools import combinations
from typing import Any

from ..a2a import PAYMENT_AGENT, POLICY_AGENT, CaseContext, Envelope
from ..planner import PAYMENT, REFUND
from ..timeline import OrderScope, money

REFUND_DONE = {"succeeded", "completed", "processed", "refunded", "confirmed"}
TOLERANCE = Decimal("0.01")
MAX_SUBSET_CAPTURES = 12


async def run_payment_agent(ctx: CaseContext, envelope: Envelope) -> list[dict[str, Any]]:
    entity = ctx.findings["entity"]
    order_id = entity["order_id"]
    refs: list[str] = []
    refund_refs: list[str] = []

    plan = ctx.findings["plan"]
    timeline: dict[str, Any] = {}
    if PAYMENT in plan:
        ev = await ctx.evidence.fetch(PAYMENT_AGENT, "get_payment_timeline", order_id=order_id)
        if ev is not None and isinstance(ev.data, dict):
            refs.append(ev.ref)
            timeline = ev.data

    refund_events: list[dict[str, Any]] = []
    if REFUND in plan:
        refund_ev = await ctx.evidence.fetch(
            PAYMENT_AGENT, "get_refund_timeline", order_id=order_id
        )
        if refund_ev is not None and isinstance(refund_ev.data, dict):
            refund_refs.append(refund_ev.ref)
            refund_events = [e for e in refund_ev.data.get("events") or [] if isinstance(e, dict)]

    analyses = [
        {
            **analyze_payment(timeline, refund_events, scope, order.get("order_value")),
            "refs": refs,
            "refund_refs": refund_refs,
        }
        for scope, order in zip(entity["scopes"], ctx.findings["order_by_scope"], strict=True)
    ]
    ctx.findings["payment_by_scope"] = analyses
    ctx.findings["raw_payment"] = timeline
    ctx.findings["raw_refund_events"] = refund_events
    verdict = analyses[0]["verdict"]
    ctx.handoff(envelope, POLICY_AGENT, f"PAYMENT_{verdict.upper()}", refs + refund_refs)
    return analyses


def analyze_payment(
    timeline: dict[str, Any],
    refund_events: list[dict[str, Any]],
    scope: OrderScope,
    order_value: Decimal | None,
) -> dict[str, Any]:
    events = _distinct(
        e
        for e in timeline.get("events") or []
        if isinstance(e, dict) and scope.contains(e.get("event_at"))
    )
    captures = [
        money(e.get("amount_brl"))
        for e in events
        if e.get("event_type") == "captured" and e.get("status") in (None, "confirmed")
    ]
    captures = [c for c in captures if c is not None]
    mismatch = [
        e
        for e in events
        if e.get("event_type") in {"reconciliation_mismatch", "capture_mismatch"}
        and e.get("status") != "resolved"
    ]
    refunds = _distinct(e for e in refund_events if scope.contains(e.get("event_at")))
    refund_statuses = [str(e.get("status")) for e in refunds]
    refunded = sum(
        (money(e.get("amount_brl")) or Decimal("0"))
        for e in refunds
        if str(e.get("status")) in REFUND_DONE
    ) or Decimal("0")

    split_group = _split_group(captures, order_value)
    duplicate_amounts = [
        amount
        for amount, count in Counter(captures).items()
        if count >= 2 and not (order_value and abs(amount * count - order_value) <= TOLERANCE)
    ]

    if "failed" in refund_statuses:
        verdict = "refund_failed"
    elif "pending" in refund_statuses:
        verdict = "refund_pending"
    elif refunded > 0:
        verdict = "refunded"
    elif mismatch:
        verdict = "capture_mismatch"
    elif duplicate_amounts:
        verdict = "duplicate_capture"
    elif timeline:
        verdict = "reconciled"
    else:
        verdict = "insufficient_evidence"

    captured_total = sum(captures, Decimal("0")) if timeline else None
    refundable = None
    if captured_total is not None:
        refundable = max(captured_total - refunded, Decimal("0"))
    return {
        "verdict": verdict,
        "captured_total": captured_total,
        "refunded_total": refunded if timeline else None,
        "refundable_total": refundable,
        "captures": captures,
        "split_group": split_group,
        "duplicate_amounts": duplicate_amounts,
        "refund_statuses": refund_statuses,
    }


def _distinct(events: Any) -> list[dict[str, Any]]:
    """Drop byte-identical events: a lifecycle replicated across rows is one event, not two.

    A genuine duplicate charge is two captures at different instants, which is kept.
    """
    return list({json.dumps(e, sort_keys=True): e for e in events}.values())


def _split_group(captures: list[Decimal], order_value: Decimal | None) -> list[Decimal] | None:
    """Smallest set of >= 2 captures that exactly pays the order value (a valid split)."""
    if not order_value or len(captures) < 2 or len(captures) > MAX_SUBSET_CAPTURES:
        return None
    for size in range(2, len(captures) + 1):
        for group in combinations(captures, size):
            if abs(sum(group, Decimal("0")) - order_value) <= TOLERANCE:
                return list(group)
    return None
