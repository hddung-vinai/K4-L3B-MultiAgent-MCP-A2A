"""Verifier: assemble the output, enforce cross-field invariants, calibrate confidence."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from ..a2a import VERIFIER, CaseContext, Envelope
from ..output import empty_output
from ..timeline import as_brl
from .policy import ISSUE_EVIDENCE

NO_ACTION_STATUSES = {"no_action"}
SHIPMENT_ISSUES = {"late_delivery_seller", "late_delivery_logistics"}


async def run_verifier(ctx: CaseContext, envelope: Envelope) -> dict[str, Any]:
    f = ctx.findings
    entity, order = f.get("entity") or {}, f.get("order") or {}
    shipment, payment = f.get("shipment") or {}, f.get("payment") or {}
    policy, conflicts = f.get("policy") or {}, f.get("conflicts") or []
    checks: list[str] = []

    payment = _align_payment(issue := policy.get("primary_issue", "insufficient_evidence"), payment)
    out = empty_output(ctx.case_id)
    refund = policy.get("recommended_refund") or Decimal("0")
    refundable = payment.get("refundable_total")

    # Invariant: never recommend more than what is refundable.
    if refundable is not None and refund > refundable:
        refund = refundable
        checks.append("REFUND_CLAMPED")
    # Invariant: no_action cases carry no refund.
    if policy.get("case_status") in NO_ACTION_STATUSES and refund > 0:
        refund = Decimal("0")
        checks.append("NO_ACTION_REFUND_ZEROED")
    refund_lines = (
        [{**line, "amount_brl": float(refund)} for line in policy.get("refund_lines") or []]
        if refund > 0
        else []
    )

    evidence = _evidence_by_group(f)
    refs = _unique(r for group in ISSUE_EVIDENCE.get(issue, ("entity",)) for r in evidence[group])
    refs = _unique([*refs, *evidence["policy"], *evidence["product"]])
    if not refs:
        checks.append("NO_EVIDENCE")
    consumed = ctx.evidence.consumed_refs
    missing_trace = [r for r in refs if r not in consumed]
    if missing_trace:
        refs = [r for r in refs if r in consumed]
        checks.append("DROPPED_UNTRACED_REFS")

    late_sellers = shipment.get("late_seller_ids") or []

    out["assessment"].update(
        primary_issue=issue,
        case_status=policy.get("case_status", "needs_investigation"),
        confidence=_confidence(ctx, issue, conflicts),
    )
    out["affected_entities"].update(
        order_ids=list(entity.get("resolved_order_ids") or []),
        item_ids=list(order.get("item_ids") or []),
        seller_ids=list(order.get("seller_ids") or []),
    )
    out["entity_resolution"].update(
        status=entity.get("status", "not_found"),
        resolved_order_ids=list(entity.get("resolved_order_ids") or []),
        rejected_candidates=list(entity.get("rejected_candidates") or [])[:20],
        confidence=round(float(entity.get("confidence") or 0.0), 3),
    )
    out["customer_context"].update(
        customer_unique_id=entity.get("customer_unique_id"),
        related_order_ids=list(entity.get("related_order_ids") or [])[:20],
    )
    out["shipment_analysis"].update(
        verdict=shipment.get("verdict", "insufficient_evidence"),
        late_seller_ids=list(late_sellers),
        timeline_complete=bool(shipment.get("timeline_complete")),
    )
    out["payment_analysis"].update(
        verdict=payment.get("verdict", "insufficient_evidence"),
        captured_total_brl=as_brl(payment.get("captured_total")),
        refunded_total_brl=as_brl(payment.get("refunded_total")),
        refundable_total_brl=as_brl(payment.get("refundable_total")),
    )
    out["root_cause_analysis"] = {
        "ranked_causes": policy.get("ranked_causes") or [],
        "responsible_parties": policy.get("responsible_parties") or [],
    }
    out["evidence_refs"] = refs[:30]
    out["data_conflicts"] = conflicts
    out["financial_resolution"] = {
        "currency": "BRL",
        "recommended_refund_brl": as_brl(refund) or 0.0,
        "refund_lines": refund_lines,
    }
    out["resolution_actions"] = list(policy.get("resolution_actions") or [])[:8]
    out["claim_assessments"] = _claims(ctx, issue, refund, payment, refs, evidence)

    # Invariant: seller responsibility must match the late-seller finding.
    if issue == "late_delivery_seller" and not out["shipment_analysis"]["late_seller_ids"]:
        out["shipment_analysis"]["late_seller_ids"] = [
            p["party_id"]
            for p in out["root_cause_analysis"]["responsible_parties"]
            if p["party_type"] == "seller" and p["party_id"]
        ]
        checks.append("LATE_SELLER_BACKFILLED")

    ctx.trace.emit(
        case_id=ctx.case_id,
        event_type="verification_completed",
        actor=VERIFIER,
        decision_code="PASS" if not checks else "ADJUSTED",
        evidence_refs=refs[:20] or None,
        attributes={
            "checks": ",".join(checks) or "none",
            "primary_issue": issue,
            "mcp_calls": ctx.evidence.calls,
            "conflicts": len(conflicts),
        },
    )
    ctx.handoff(envelope, "coordinator", "VERIFIED_OUTPUT")
    return out


PAYMENT_VERDICT_BY_ISSUE = {
    "valid_split_payment": "reconciled",
    "payment_mismatch": "capture_mismatch",
    "duplicate_charge": "duplicate_capture",
    "refund_pending": "refund_pending",
    "refund_failed": "refund_failed",
}


def _align_payment(issue: str, payment: dict[str, Any]) -> dict[str, Any]:
    """Keep payment_analysis consistent with the decided issue (no competing verdicts)."""
    payment = dict(payment)
    if issue in PAYMENT_VERDICT_BY_ISSUE:
        payment["verdict"] = PAYMENT_VERDICT_BY_ISSUE[issue]
    group = payment.get("split_group")
    if issue == "valid_split_payment" and group:
        captured = sum(group, Decimal("0"))
        payment["captured_total"] = captured
        refunded = payment.get("refunded_total") or Decimal("0")
        payment["refundable_total"] = max(captured - refunded, Decimal("0"))
    return payment


def _evidence_by_group(f: dict[str, Any]) -> dict[str, list[str]]:
    return {
        "entity": list((f.get("entity") or {}).get("refs") or []),
        "order": list((f.get("order") or {}).get("refs") or []),
        "product": list((f.get("order") or {}).get("product_refs") or []),
        "shipment": list((f.get("shipment") or {}).get("refs") or []),
        "payment": list((f.get("payment") or {}).get("refs") or []),
        "refund": list((f.get("payment") or {}).get("refund_refs") or []),
        "policy": list((f.get("policy") or {}).get("refs") or []),
    }


def _confidence(ctx: CaseContext, issue: str, conflicts: list[dict[str, Any]]) -> float:
    entity = ctx.findings.get("entity") or {}
    if issue == "insufficient_evidence":
        return 0.3
    topics = _claim_topics(ctx.case)
    score = 0.92 if issue in topics else 0.7
    if ctx.findings.get("scope_switched"):
        score -= 0.07
    if ctx.findings.get("competing_issues"):
        score -= 0.05
    if entity.get("status") != "resolved":
        score -= 0.25
    if any(c["field"] == "late_delivery_responsibility" for c in conflicts):
        score -= 0.15
    if ctx.evidence.failures and any("transient" in x for x in ctx.evidence.failures):
        score -= 0.1
    return round(min(max(score, 0.05), 0.97), 3)


def _claim_topics(case: dict[str, Any]) -> list[str]:
    claims = (case.get("customer_request") or {}).get("claims") or []
    return [c.get("topic") for c in claims if isinstance(c, dict)]


def _claims(
    ctx: CaseContext,
    issue: str,
    refund: Decimal,
    payment: dict[str, Any],
    refs: list[str],
    evidence: dict[str, list[str]],
) -> list[dict[str, Any]]:
    claims = (ctx.case.get("customer_request") or {}).get("claims") or []
    full_amount = (ctx.findings.get("order") or {}).get("order_value") or payment.get(
        "captured_total"
    )
    result = []
    for claim in claims[:5]:
        if not isinstance(claim, dict) or not claim.get("claim_id"):
            continue
        topic = claim.get("topic")
        if topic == "requested_full_refund":
            if refund > 0 and full_amount is not None and refund >= full_amount:
                verdict = "supported"
            elif refund > 0:
                verdict = "partially_supported"
            else:
                verdict = "unsupported"
            claim_refs = _unique([*evidence["payment"], *evidence["policy"]]) or refs
        elif issue == "insufficient_evidence":
            verdict, claim_refs = "insufficient_evidence", refs
        elif topic == issue:
            verdict = "unsupported" if issue == "unsupported_claim" else "supported"
            claim_refs = refs
        else:
            verdict, claim_refs = "unsupported", refs
        claim_refs = [r for r in claim_refs if r in refs] or refs
        result.append(
            {
                "claim_id": str(claim["claim_id"])[:64],
                "verdict": verdict,
                "confidence": 0.85 if verdict != "insufficient_evidence" else 0.3,
                "evidence_refs": claim_refs[:30],
            }
        )
    return result


def _unique(values: Any) -> list[str]:
    return list(dict.fromkeys(v for v in values if v))
