"""In-process A2A protocol: message envelopes and the per-case shared context.

Every hop between agents is an Envelope and is mirrored as an observable trace event
(task_assigned / handoff). Only decisions and evidence refs are traced, never reasoning.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import Any

from .evidence import EvidenceCollector
from .trace import TraceWriter

COORDINATOR = "coordinator"
ENTITY_AGENT = "entity-agent"
ORDER_AGENT = "order-agent"
SHIPMENT_AGENT = "shipment-agent"
PAYMENT_AGENT = "payment-agent"
POLICY_AGENT = "policy-agent"
CONFLICT_AGENT = "conflict-agent"
VERIFIER = "verifier"


@dataclass(frozen=True)
class Envelope:
    case_id: str
    sender: str
    recipient: str
    task: str
    payload: dict[str, Any] = field(default_factory=dict)
    message_id: str = field(default_factory=lambda: f"msg_{secrets.token_hex(8)}")


@dataclass
class CaseContext:
    """Blackboard shared by the agents of exactly one case (no cross-case state)."""

    case: dict[str, Any]
    trace: TraceWriter
    evidence: EvidenceCollector
    findings: dict[str, Any] = field(default_factory=dict)
    hops: int = 0
    max_hops: int = 32

    @property
    def case_id(self) -> str:
        return self.case["case_id"]

    def assign(self, recipient: str, task: str, **payload: Any) -> Envelope:
        self._count_hop()
        envelope = Envelope(self.case_id, COORDINATOR, recipient, task, payload)
        self.trace.emit(
            case_id=self.case_id,
            event_type="task_assigned",
            actor=COORDINATOR,
            target=recipient,
            decision_code=task,
            attributes={"message_id": envelope.message_id},
        )
        return envelope

    def handoff(
        self,
        envelope: Envelope,
        recipient: str,
        decision_code: str,
        evidence_refs: list[str] | None = None,
    ) -> Envelope:
        self._count_hop()
        reply = Envelope(self.case_id, envelope.recipient, recipient, decision_code)
        self.trace.emit(
            case_id=self.case_id,
            event_type="handoff",
            actor=envelope.recipient,
            target=recipient,
            decision_code=decision_code,
            evidence_refs=_unique(evidence_refs or [])[:20] or None,
            attributes={"message_id": reply.message_id, "in_reply_to": envelope.message_id},
        )
        return reply

    def _count_hop(self) -> None:
        self.hops += 1
        if self.hops > self.max_hops:
            raise RuntimeError(f"{self.case_id}: A2A hop budget exceeded")


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))
