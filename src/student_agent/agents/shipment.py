"""Shipment agent: delivery verdict for each candidate lifecycle row."""

from __future__ import annotations

from typing import Any

from ..a2a import POLICY_AGENT, SHIPMENT_AGENT, CaseContext, Envelope
from ..planner import SHIPMENT
from ..timeline import ROW_TIMESTAMP_FIELDS, OrderScope, parse_ts

NOT_DELIVERED_STATUSES = {"canceled", "unavailable"}


async def run_shipment_agent(ctx: CaseContext, envelope: Envelope) -> list[dict[str, Any]]:
    entity = ctx.findings["entity"]
    refs: list[str] = []
    summary: dict[str, Any] = {}

    ev = None
    if SHIPMENT in ctx.findings["plan"]:
        ev = await ctx.evidence.fetch(
            SHIPMENT_AGENT, "get_shipment_summary", order_id=entity["order_id"]
        )
    if ev is not None and isinstance(ev.data, dict):
        refs.append(ev.ref)
        summary = ev.data

    analyses = [
        {**analyze_shipment(summary, scope, order), "refs": refs}
        for scope, order in zip(entity["scopes"], ctx.findings["order_by_scope"], strict=True)
    ]
    ctx.findings["shipment_by_scope"] = analyses
    ctx.handoff(envelope, POLICY_AGENT, f"SHIPMENT_{analyses[0]['verdict'].upper()}", refs)
    return analyses


def analyze_shipment(
    summary: dict[str, Any], scope: OrderScope, order: dict[str, Any]
) -> dict[str, Any]:
    row = scope.row
    status = row.get("order_status") or summary.get("order_status")
    delivered = parse_ts(row.get("order_delivered_customer_date"))
    estimated = parse_ts(row.get("order_estimated_delivery_date"))
    carrier = parse_ts(row.get("order_delivered_carrier_date"))

    limits = [
        (item.get("seller_id"), parse_ts(item.get("shipping_limit_date")))
        for item in order.get("items") or []
        if item.get("shipping_limit_date")
    ]
    if not limits:
        limits = [
            (lim.get("seller_id"), parse_ts(lim.get("shipping_limit_at")))
            for lim in summary.get("shipping_limits") or []
            if scope.contains(lim.get("shipping_limit_at"))
        ]
    late_sellers = [
        seller for seller, limit in limits if seller and carrier and limit and carrier > limit
    ]

    events = [
        e
        for e in summary.get("events") or []
        if isinstance(e, dict) and scope.contains(e.get("event_at"))
    ]
    late_actors = {
        e.get("actor")
        for e in events
        if e.get("event_type") == "delivered_late" and e.get("status") == "confirmed"
    }
    lost = any(e.get("event_type") in {"lost", "shipment_lost"} for e in events)
    returned = any(e.get("event_type") in {"returned", "return_delivered"} for e in events)

    conflict = None
    if status in NOT_DELIVERED_STATUSES:
        verdict = "insufficient_evidence"
    elif lost:
        verdict = "lost"
    elif returned:
        verdict = "returned"
    elif delivered is None or estimated is None:
        verdict = "insufficient_evidence"
    elif delivered > estimated:
        # Confirmed lifecycle events are authoritative; timestamps are the fallback signal.
        timestamp_says_seller = bool(late_sellers)
        if late_actors == {"seller"}:
            verdict = "seller_delay"
        elif late_actors == {"logistics_provider"}:
            verdict = "logistics_delay"
        else:
            verdict = "seller_delay" if timestamp_says_seller else "logistics_delay"
        if late_actors and (("seller" in late_actors) != timestamp_says_seller):
            conflict = "late_delivery_actor"
    else:
        verdict = "conflicting" if late_actors else "on_time"
        if late_actors:
            conflict = "delivered_late_event_vs_timestamps"

    if verdict == "seller_delay" and not late_sellers:
        late_sellers = list(order.get("seller_ids") or [])

    return {
        "verdict": verdict,
        "status": status,
        "late_seller_ids": list(dict.fromkeys(late_sellers)) if verdict == "seller_delay" else [],
        "timeline_complete": all(row.get(k) for k in ROW_TIMESTAMP_FIELDS),
        "late_actors": sorted(a for a in late_actors if a),
        "conflict": conflict,
    }
