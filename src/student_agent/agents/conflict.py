"""Conflict agent: record source disagreements and which source was selected.

The order's evidence carries rows from several lifecycles. For every evidence family where
rows fall outside the complained-about lifecycle window, one conflict is recorded naming the
kept row as the selected source (EXCLUDED_OUTSIDE_CASE_WINDOW). Byte-identical replicated
rows are recorded as a `.duplicate` conflict instead.
"""

from __future__ import annotations

import json
from typing import Any

from ..a2a import CaseContext, Envelope

WINDOW_CODE = "EXCLUDED_OUTSIDE_CASE_WINDOW"
DUPLICATE_CODE = "DEDUPLICATED_IDENTICAL_ROWS"


async def run_conflict_agent(ctx: CaseContext, envelope: Envelope) -> list[dict[str, Any]]:
    f = ctx.findings
    entity = f.get("entity") or {}
    scope = entity.get("scope")
    conflicts: list[dict[str, Any]] = []

    if scope is not None:
        conflicts += _family(
            "order_items.shipping_limit_date",
            "get_order_items",
            f.get("raw_items") or [],
            lambda r: r.get("shipping_limit_date"),
            scope,
        )
        conflicts += _family(
            "payment_events.event_at",
            "get_payment_timeline",
            [e for e in (f.get("raw_payment") or {}).get("events") or [] if isinstance(e, dict)],
            lambda e: e.get("event_at"),
            scope,
        )
        excluded_ship = [
            i
            for i, e in enumerate(f.get("raw_shipment_events") or [])
            if not scope.contains(e.get("event_at"))
        ]
        if excluded_ship:
            conflicts.append(
                {
                    "field": "shipment_events.event_at",
                    "sources": ["get_order", f"get_shipment_summary[{excluded_ship[0]}]"],
                    "selected_source": "get_order",
                    "resolution_code": WINDOW_CODE,
                }
            )
        conflicts += _family(
            "refund_events.event_at",
            "get_refund_timeline",
            f.get("raw_refund_events") or [],
            lambda e: e.get("event_at"),
            scope,
        )

    shipment = f.get("shipment") or {}
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
    f["conflicts"] = conflicts
    ctx.handoff(envelope, "coordinator", "CONFLICTS_RECORDED" if conflicts else "NO_CONFLICT")
    return conflicts


def _family(field, tool, rows, timestamp, scope) -> list[dict[str, Any]]:
    """One conflict per family: kept row vs first row excluded (or duplicated)."""
    if len(rows) < 2:
        return []
    kept = [i for i, r in enumerate(rows) if scope.contains(timestamp(r))]
    excluded = [i for i, r in enumerate(rows) if i not in kept]
    if kept and excluded:
        return [
            {
                "field": field,
                "sources": [f"{tool}[{kept[0]}]", f"{tool}[{excluded[0]}]"],
                "selected_source": f"{tool}[{kept[0]}]",
                "resolution_code": WINDOW_CODE,
            }
        ]
    seen: dict[str, int] = {}
    for i, row in enumerate(rows):
        key = json.dumps(row, sort_keys=True)
        if key in seen:
            return [
                {
                    "field": f"{field}.duplicate",
                    "sources": [f"{tool}[{seen[key]}]", f"{tool}[{i}]"],
                    "selected_source": f"{tool}[{seen[key]}]",
                    "resolution_code": DUPLICATE_CODE,
                }
            ]
        seen[key] = i
    return []
