"""Conflict agent: record source disagreements and which source was selected."""

from __future__ import annotations

from typing import Any

from ..a2a import CaseContext, Envelope

COMPARED_FIELDS = (
    "order_status",
    "order_purchase_timestamp",
    "order_delivered_customer_date",
)


async def run_conflict_agent(ctx: CaseContext, envelope: Envelope) -> list[dict[str, Any]]:
    entity = ctx.findings.get("entity") or {}
    shipment = ctx.findings.get("shipment") or {}
    scope = entity.get("scope")
    order_row = entity.get("order_row") or {}
    conflicts: list[dict[str, Any]] = []

    if scope is not None and scope.source == "get_customer_history" and order_row:
        for field in COMPARED_FIELDS:
            if order_row.get(field) != scope.row.get(field):
                conflicts.append(
                    {
                        "field": field,
                        "sources": ["get_order", "get_customer_history"],
                        "selected_source": "get_customer_history",
                        "resolution_code": "SCOPED_TO_COMPLAINT_TIMELINE",
                    }
                )

    if shipment.get("conflict"):
        conflicts.append(
            {
                "field": "late_delivery_responsibility",
                "sources": ["get_shipment_summary.events", "get_order_items.shipping_limit_date"],
                "selected_source": "get_shipment_summary.events",
                "resolution_code": "CONFIRMED_LIFECYCLE_EVENT_PRECEDENCE",
            }
        )

    conflicts = conflicts[:5]
    ctx.findings["conflicts"] = conflicts
    ctx.handoff(
        envelope,
        "coordinator",
        "CONFLICTS_RECORDED" if conflicts else "NO_CONFLICT",
    )
    return conflicts
