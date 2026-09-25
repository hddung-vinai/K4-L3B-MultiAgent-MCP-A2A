"""Order/item agent: scoped items, sellers, order value and product context."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from ..a2a import ORDER_AGENT, POLICY_AGENT, CaseContext, Envelope
from ..planner import ITEMS, PRODUCT
from ..timeline import OrderScope, money


async def run_order_agent(ctx: CaseContext, envelope: Envelope) -> list[dict[str, Any]]:
    entity = ctx.findings["entity"]
    order_id = entity["order_id"]
    refs: list[str] = []
    product_refs: list[str] = []

    plan = ctx.findings["plan"]
    rows: list[dict[str, Any]] = []
    if ITEMS in plan:
        items_ev = await ctx.evidence.fetch(ORDER_AGENT, "get_order_items", order_id=order_id)
        if items_ev is not None and isinstance(items_ev.data, list):
            refs.append(items_ev.ref)
            rows = [r for r in items_ev.data if isinstance(r, dict)]

    if PRODUCT in plan:
        product = await ctx.evidence.fetch(ORDER_AGENT, "get_product_context", order_id=order_id)
        if product is not None:
            product_refs.append(product.ref)
            if not rows and isinstance(product.data, list):
                # Product context carries item/product/seller ids (no prices or limits).
                rows = [
                    {k: r.get(k) for k in ("order_item_id", "product_id", "seller_id")}
                    for r in product.data
                    if isinstance(r, dict)
                ]

    analyses = [
        {**analyze_items(rows, scope), "refs": refs, "product_refs": product_refs}
        for scope in entity["scopes"]
    ]
    ctx.findings["order_by_scope"] = analyses
    ctx.findings["raw_items"] = rows if refs else []
    ctx.handoff(envelope, POLICY_AGENT, "ORDER_ITEMS_SCOPED", refs + product_refs)
    return analyses


def analyze_items(rows: list[dict[str, Any]], scope: OrderScope) -> dict[str, Any]:
    scoped = [
        r
        for r in rows
        if "shipping_limit_date" not in r or scope.contains(r.get("shipping_limit_date"))
    ] or rows
    # Identical rows (same lifecycle replicated across sources) describe one item, not two.
    scoped = list({json.dumps(r, sort_keys=True): r for r in scoped}.values())
    priced = [r for r in scoped if "price" in r]
    order_value = Decimal("0")
    for row in priced:
        order_value += (money(row.get("price")) or Decimal("0")) + (
            money(row.get("freight_value")) or Decimal("0")
        )
    return {
        "items": scoped,
        "item_ids": _unique(r.get("order_item_id") for r in scoped),
        "seller_ids": _unique(r.get("seller_id") for r in scoped),
        "product_ids": _unique(r.get("product_id") for r in scoped),
        "order_value": order_value if priced else None,
    }


def _unique(values: Any) -> list[str]:
    return list(dict.fromkeys(v for v in values if v))
