"""Order/item agent: scoped items, sellers, order value and product context."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from ..a2a import ORDER_AGENT, POLICY_AGENT, CaseContext, Envelope
from ..timeline import OrderScope, money


async def run_order_agent(ctx: CaseContext, envelope: Envelope) -> list[dict[str, Any]]:
    entity = ctx.findings["entity"]
    order_id = entity["order_id"]
    refs: list[str] = []
    product_refs: list[str] = []

    items_ev = await ctx.evidence.fetch(ORDER_AGENT, "get_order_items", order_id=order_id)
    rows: list[dict[str, Any]] = []
    if items_ev is not None and isinstance(items_ev.data, list):
        refs.append(items_ev.ref)
        rows = [r for r in items_ev.data if isinstance(r, dict)]

    scope_flags = ctx.case.get("investigation_scope") or {}
    if scope_flags.get("include_product_context"):
        product = await ctx.evidence.fetch(ORDER_AGENT, "get_product_context", order_id=order_id)
        if product is not None:
            product_refs.append(product.ref)

    analyses = [
        {**analyze_items(rows, scope), "refs": refs, "product_refs": product_refs}
        for scope in entity["scopes"]
    ]
    ctx.findings["order_by_scope"] = analyses
    ctx.handoff(envelope, POLICY_AGENT, "ORDER_ITEMS_SCOPED", refs + product_refs)
    return analyses


def analyze_items(rows: list[dict[str, Any]], scope: OrderScope) -> dict[str, Any]:
    scoped = [r for r in rows if scope.contains(r.get("shipping_limit_date"))] or rows
    # Identical rows (same lifecycle replicated across sources) describe one item, not two.
    scoped = list({json.dumps(r, sort_keys=True): r for r in scoped}.values())
    order_value = Decimal("0")
    for row in scoped:
        order_value += (money(row.get("price")) or Decimal("0")) + (
            money(row.get("freight_value")) or Decimal("0")
        )
    return {
        "items": scoped,
        "item_ids": _unique(r.get("order_item_id") for r in scoped),
        "seller_ids": _unique(r.get("seller_id") for r in scoped),
        "product_ids": _unique(r.get("product_id") for r in scoped),
        "order_value": order_value if scoped else None,
    }


def _unique(values: Any) -> list[str]:
    return list(dict.fromkeys(v for v in values if v))
