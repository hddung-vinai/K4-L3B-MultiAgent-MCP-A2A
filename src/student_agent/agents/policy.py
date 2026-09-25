"""Policy agent: classify the primary issue from specialist findings and apply the policy."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from ..a2a import POLICY_AGENT, VERIFIER, CaseContext, Envelope
from ..timeline import money

# Issue -> evidence groups that justify it (used for claim linkage and output evidence).
ISSUE_EVIDENCE: dict[str, tuple[str, ...]] = {
    "canceled_order_paid": ("entity", "payment"),
    "unavailable_order_paid": ("entity", "payment", "order"),
    "late_delivery_seller": ("entity", "shipment", "order"),
    "late_delivery_logistics": ("entity", "shipment", "order"),
    "valid_split_payment": ("entity", "payment", "order"),
    "payment_mismatch": ("entity", "payment"),
    "duplicate_charge": ("entity", "payment", "order"),
    "refund_pending": ("entity", "payment", "refund"),
    "refund_failed": ("entity", "payment", "refund"),
    "unsupported_claim": ("entity", "shipment", "payment", "order"),
    "insufficient_evidence": ("entity",),
}

FALLBACK_RULES: dict[str, dict[str, Any]] = {
    "insufficient_evidence": {
        "case_status": "needs_investigation",
        "recommended_action": "escalate_manual_review",
        "refund_brl": 0.0,
        "responsible_parties": [{"party_type": "unknown", "party_id": None}],
    },
}


def detect_issues(
    scope_row: dict[str, Any], shipment: dict[str, Any], payment: dict[str, Any]
) -> list[str]:
    """Every issue the evidence of one lifecycle row supports, in policy priority order."""
    issues: list[str] = []
    status = scope_row.get("order_status")
    paid = (payment.get("captured_total") or Decimal("0")) > 0
    if status == "canceled" and paid:
        issues.append("canceled_order_paid")
    if status == "unavailable" and paid:
        issues.append("unavailable_order_paid")
    statuses = payment.get("refund_statuses") or []
    if "failed" in statuses:
        issues.append("refund_failed")
    if "pending" in statuses:
        issues.append("refund_pending")
    if payment.get("verdict") == "capture_mismatch":
        issues.append("payment_mismatch")
    if payment.get("duplicate_amounts"):
        issues.append("duplicate_charge")
    if shipment.get("verdict") == "seller_delay":
        issues.append("late_delivery_seller")
    if shipment.get("verdict") == "logistics_delay":
        issues.append("late_delivery_logistics")
    if payment.get("split_group"):
        issues.append("valid_split_payment")
    return issues


def select_issue(ctx: CaseContext) -> tuple[int, str, list[list[str]]]:
    """Pick (scope index, primary issue).

    Default: the highest-priority issue of the most recent row at/before opened_at.
    If the customer's claim is supported by that row, it wins; if only an older eligible
    row supports the claim, that row is selected instead (claim-guided verification).
    """
    f = ctx.findings
    scopes = (f.get("entity") or {}).get("scopes") or []
    if not scopes or not f.get("payment_by_scope"):
        return 0, "insufficient_evidence", []
    detected = [
        detect_issues(scope.row, ship, pay)
        for scope, ship, pay in zip(
            scopes, f["shipment_by_scope"], f["payment_by_scope"], strict=True
        )
    ]
    claimed = [t for t in _claim_topics(ctx.case) if t in ISSUE_EVIDENCE]
    for topic in claimed:
        if topic in detected[0]:
            return 0, topic, detected
    for topic in claimed:
        for index, issues in enumerate(detected[1:], start=1):
            if topic in issues:
                return index, topic, detected
    if detected[0]:
        return 0, detected[0][0], detected
    ship, pay = f["shipment_by_scope"][0], f["payment_by_scope"][0]
    if ship.get("verdict") == "insufficient_evidence" and pay.get("verdict") == (
        "insufficient_evidence"
    ):
        return 0, "insufficient_evidence", detected
    return 0, "unsupported_claim", detected


def _claim_topics(case: dict[str, Any]) -> list[str]:
    claims = (case.get("customer_request") or {}).get("claims") or []
    return [c.get("topic") for c in claims if isinstance(c, dict) and c.get("topic")]


async def run_policy_agent(ctx: CaseContext, envelope: Envelope) -> dict[str, Any]:
    findings = ctx.findings
    refs: list[str] = []
    rules: dict[str, Any] = {}
    currency = "BRL"
    policy_ev = await ctx.evidence.fetch(
        POLICY_AGENT, "get_policy", policy_version=ctx.case.get("policy_version") or ""
    )
    if policy_ev is not None and isinstance(policy_ev.data, dict):
        refs.append(policy_ev.ref)
        rules = policy_ev.data.get("rules") or {}
        currency = policy_ev.data.get("currency") or "BRL"

    index, issue, detected = select_issue(ctx)
    entity = findings["entity"]
    if entity.get("scopes"):
        # Bind the selected lifecycle row's analyses as the case findings.
        entity["scope"] = entity["scopes"][index]
        findings["order"] = findings["order_by_scope"][index]
        findings["shipment"] = findings["shipment_by_scope"][index]
        findings["payment"] = findings["payment_by_scope"][index]
    findings["scope_switched"] = index > 0
    findings["competing_issues"] = [i for i in (detected[index] if detected else []) if i != issue]
    rule = rules.get(issue) or FALLBACK_RULES.get(issue)
    if rule is None:
        issue, rule = "insufficient_evidence", FALLBACK_RULES["insufficient_evidence"]

    order_id = findings["entity"].get("order_id")
    sellers = _responsible_sellers(issue, findings)
    parties = []
    for party in rule.get("responsible_parties") or []:
        party_type = party.get("party_type", "unknown")
        if party_type == "seller":
            # Policy templates may carry another order's seller; bind to this case's sellers.
            for seller in sellers or [None]:
                parties.append({"party_type": "seller", "party_id": seller})
        else:
            parties.append({"party_type": party_type, "party_id": party.get("party_id")})

    refund = money(rule.get("refund_brl")) or Decimal("0")
    action = rule.get("recommended_action")
    refund_lines = []
    if refund > 0:
        refund_lines.append(
            {"reason_code": issue, "amount_brl": float(refund), "entity_id": order_id}
        )
    decision = {
        "primary_issue": issue,
        "case_status": rule.get("case_status", "needs_investigation"),
        "recommended_action": action,
        "resolution_actions": [action] if action else [],
        "responsible_parties": _dedupe(parties)[:5],
        "ranked_causes": [{"cause_code": issue.upper(), "rank": 1}],
        "currency": currency,
        "recommended_refund": refund,
        "refund_lines": refund_lines,
        "refs": refs,
    }
    findings["policy"] = decision
    ctx.trace.emit(
        case_id=ctx.case_id,
        event_type="policy_decided",
        actor=POLICY_AGENT,
        decision_code=issue,
        evidence_refs=refs or None,
        attributes={
            "case_status": decision["case_status"],
            "recommended_action": action,
            "refund_brl": float(refund),
        },
    )
    ctx.handoff(envelope, VERIFIER, "POLICY_DECIDED", refs)
    return decision


def _responsible_sellers(issue: str, findings: dict[str, Any]) -> list[str]:
    late = (findings.get("shipment") or {}).get("late_seller_ids") or []
    if issue == "late_delivery_seller" and late:
        return list(late)
    return list((findings.get("order") or {}).get("seller_ids") or [])


def _dedupe(parties: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen, out = set(), []
    for party in parties:
        key = (party["party_type"], party["party_id"])
        if key not in seen:
            seen.add(key)
            out.append(party)
    return out
