"""Coordinator: routes one case through the specialist agents (A2A over an in-process bus).

Flow: entity -> order/item -> (shipment || payment) -> policy -> conflict -> verifier.
`case_received` / `case_finalized` are emitted by the CLI around this function.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .a2a import (
    CONFLICT_AGENT,
    ENTITY_AGENT,
    ORDER_AGENT,
    PAYMENT_AGENT,
    POLICY_AGENT,
    SHIPMENT_AGENT,
    VERIFIER,
    CaseContext,
)
from .agents import (
    run_conflict_agent,
    run_entity_agent,
    run_order_agent,
    run_payment_agent,
    run_policy_agent,
    run_shipment_agent,
    run_verifier,
)
from .evidence import EvidenceCollector
from .mcp_gateway import EvidenceGateway, is_transport_failure
from .output import empty_output
from .planner import investigation_plan
from .trace import TraceWriter

log = logging.getLogger(__name__)
_discovered: dict[int, frozenset[str]] = {}


async def _available_tools(gateway: EvidenceGateway) -> frozenset[str] | None:
    key = id(gateway)
    if key not in _discovered:
        try:
            _discovered[key] = frozenset(await gateway.list_tools())
        except Exception:  # noqa: BLE001 - discovery is advisory only
            return None
    return _discovered[key]


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    case_id = case["case_id"]
    collector = EvidenceCollector(gateway, trace, case_id, await _available_tools(gateway))
    ctx = CaseContext(case=case, trace=trace, evidence=collector)
    try:
        return await _coordinate(ctx)
    except Exception as exc:  # noqa: BLE001 - one bad case must not sink the batch
        if is_transport_failure(exc):
            raise  # the runner reconnects and re-runs this case from scratch
        log.exception("case %s failed", case_id)
        output = empty_output(case_id)
        output["evidence_refs"] = sorted(collector.consumed_refs)[:30]
        trace.emit(
            case_id=case_id,
            event_type="verification_completed",
            actor=VERIFIER,
            decision_code="FAILED_SAFE_DEFAULT",
            attributes={"error": type(exc).__name__},
        )
        return output


async def _coordinate(ctx: CaseContext) -> dict[str, Any]:
    ctx.findings["plan"] = investigation_plan(ctx.case)
    entity = await run_entity_agent(ctx, ctx.assign(ENTITY_AGENT, "RESOLVE_ENTITY"))

    if entity.get("scopes"):
        await run_order_agent(ctx, ctx.assign(ORDER_AGENT, "COLLECT_ORDER_ITEMS"))
        await asyncio.gather(
            run_shipment_agent(ctx, ctx.assign(SHIPMENT_AGENT, "ANALYZE_SHIPMENT")),
            run_payment_agent(ctx, ctx.assign(PAYMENT_AGENT, "ANALYZE_PAYMENT")),
        )

    await run_policy_agent(ctx, ctx.assign(POLICY_AGENT, "DECIDE_POLICY"))
    if entity.get("scopes"):
        await run_conflict_agent(ctx, ctx.assign(CONFLICT_AGENT, "RESOLVE_CONFLICTS"))
    return await run_verifier(ctx, ctx.assign(VERIFIER, "VERIFY_OUTPUT"))
