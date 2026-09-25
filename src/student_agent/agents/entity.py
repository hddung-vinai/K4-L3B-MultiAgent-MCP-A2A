"""Entity/customer agent: resolve the complained-about order among candidates."""

from __future__ import annotations

import re
from typing import Any

from ..a2a import ENTITY_AGENT, CaseContext, Envelope
from ..timeline import candidate_scopes, parse_ts

ORDER_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


async def run_entity_agent(ctx: CaseContext, envelope: Envelope) -> dict[str, Any]:
    case = ctx.case
    request = case.get("customer_request") or {}
    claimed = request.get("claimed_order_id")
    candidates = list(dict.fromkeys(case.get("candidate_order_ids") or []))
    if claimed and claimed not in candidates:
        candidates.insert(0, claimed)
    hint = case.get("customer_unique_id_hint")
    refs: list[str] = []

    history_rows: list[dict[str, Any]] = []
    customer_id = None
    if hint:
        history = await ctx.evidence.fetch(
            ENTITY_AGENT, "get_customer_history", customer_unique_id=hint
        )
        if history is not None and isinstance(history.data, dict):
            refs.append(history.ref)
            customer_id = history.data.get("customer_unique_id") or hint
            history_rows = [r for r in history.data.get("orders") or [] if isinstance(r, dict)]
    history_ids = list(dict.fromkeys(r.get("order_id") for r in history_rows if r.get("order_id")))

    matches = [c for c in candidates if c in history_ids]
    status = "resolved"
    if len(matches) == 1:
        resolved = matches[0]
        confidence = 0.95 if resolved == claimed else 0.85
        method = "CUSTOMER_HISTORY_MATCH"
    elif len(matches) > 1:
        if claimed in matches:
            resolved, confidence, method = claimed, 0.7, "CLAIMED_AMONG_MULTIPLE"
        else:
            resolved = _latest_before(history_rows, matches, case.get("opened_at"))
            confidence, method = 0.5, "NEAREST_PURCHASE_AMONG_MULTIPLE"
        status = "ambiguous"
    else:
        resolved, confidence, method = None, 0.0, "NO_HISTORY_MATCH"

    order_row = None
    if resolved is None:
        # No customer link: probe only syntactically valid order IDs, claimed first.
        for candidate in [c for c in candidates if ORDER_ID_PATTERN.fullmatch(c)]:
            order = await ctx.evidence.fetch(ENTITY_AGENT, "get_order", order_id=candidate)
            if order is not None and isinstance(order.data, dict):
                refs.append(order.ref)
                resolved, order_row = candidate, order.data
                confidence, method = 0.6, "ORDER_LOOKUP_ONLY"
                break
    else:
        order = await ctx.evidence.fetch(ENTITY_AGENT, "get_order", order_id=resolved)
        if order is not None and isinstance(order.data, dict):
            refs.append(order.ref)
            order_row = order.data

    if resolved is None:
        status = "not_found"

    scopes = (
        candidate_scopes(history_rows, resolved, case.get("opened_at"), order_row)
        if resolved
        else []
    )
    related = [oid for oid in history_ids if oid]
    result = {
        "status": status,
        "order_id": resolved,
        "resolved_order_ids": [resolved] if resolved else [],
        "rejected_candidates": [c for c in candidates if c != resolved],
        "confidence": confidence,
        "method": method,
        "customer_unique_id": customer_id,
        "related_order_ids": related,
        "order_row": order_row,
        "scopes": scopes,
        "scope": scopes[0] if scopes else None,
        "refs": refs,
    }
    ctx.findings["entity"] = result
    ctx.handoff(envelope, "coordinator", f"ENTITY_{status.upper()}", refs)
    return result


def _latest_before(rows: list[dict[str, Any]], ids: list[str], opened_at: str | None) -> str:
    opened = parse_ts(opened_at)
    best, best_ts = ids[0], None
    for row in rows:
        ts = parse_ts(row.get("order_purchase_timestamp"))
        eligible = row.get("order_id") in ids and ts and (opened is None or ts <= opened)
        if eligible and (best_ts is None or ts > best_ts):
            best, best_ts = row["order_id"], ts
    return best
