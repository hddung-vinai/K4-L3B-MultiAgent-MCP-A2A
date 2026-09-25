"""Coordinator investigation plan: which evidence families each claim actually needs.

Every MCP call is audited and counts against the per-case budget, so specialists only
fetch the families the claimed issues require. Entity (customer history + order) and
policy evidence are always collected. Unknown topics fall back to the full plan.
"""

from __future__ import annotations

from typing import Any

ITEMS = "items"  # get_order_items: prices/freight (order value), shipping limits
PRODUCT = "product"  # get_product_context: item/product/seller ids
SHIPMENT = "shipment"  # get_shipment_summary: delivery events and seller limits
PAYMENT = "payment"  # get_payment_timeline: captures and reconciliation events
REFUND = "refund"  # get_refund_timeline: refund lifecycle

FULL_PLAN = frozenset({ITEMS, PRODUCT, SHIPMENT, PAYMENT, REFUND})

TOPIC_PLAN: dict[str, frozenset[str]] = {
    "late_delivery_logistics": frozenset({SHIPMENT}),
    "late_delivery_seller": frozenset({SHIPMENT}),
    "payment_mismatch": frozenset({PAYMENT}),
    "canceled_order_paid": frozenset({PAYMENT}),
    "unavailable_order_paid": frozenset({PAYMENT}),
    # Split vs duplicate is decided by comparing captures with the order value.
    "valid_split_payment": frozenset({ITEMS, PAYMENT}),
    "duplicate_charge": frozenset({ITEMS, PAYMENT}),
    "refund_pending": frozenset({PAYMENT, REFUND}),
    "refund_failed": frozenset({PAYMENT, REFUND}),
    # Rejecting a claim needs both delivery and payment evidence showing no fault.
    "unsupported_claim": frozenset({SHIPMENT, PAYMENT}),
    # Money request only; the concrete issue topic drives the plan.
    "requested_full_refund": frozenset(),
}


def investigation_plan(case: dict[str, Any]) -> frozenset[str]:
    claims = (case.get("customer_request") or {}).get("claims") or []
    topics = [c.get("topic") for c in claims if isinstance(c, dict)]
    if not topics or any(t not in TOPIC_PLAN for t in topics):
        plan = set(FULL_PLAN)
    else:
        plan = set().union(*(TOPIC_PLAN[t] for t in topics))
        if not plan:
            plan = set(FULL_PLAN)
    scope = case.get("investigation_scope") or {}
    if scope.get("include_product_context", True):
        plan.add(PRODUCT)
    elif ITEMS not in plan:
        plan.add(ITEMS)  # still need item/seller ids from somewhere
    return frozenset(plan)
