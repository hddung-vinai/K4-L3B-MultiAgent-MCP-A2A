"""MCP evidence collector: least-privilege permissions, per-case cache and bounded retry."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .mcp_gateway import TRANSPORT_ERRORS, EvidenceGateway, GatewayUnavailable
from .trace import TraceWriter

TOOL_PERMISSIONS: dict[str, frozenset[str]] = {
    "entity-agent": frozenset({"get_customer_history", "get_order"}),
    "order-agent": frozenset({"get_order_items", "get_product_context", "get_sellers"}),
    "shipment-agent": frozenset({"get_shipment_summary"}),
    "payment-agent": frozenset(
        {"get_payment_timeline", "get_order_payments", "get_refund_timeline"}
    ),
    "policy-agent": frozenset({"get_policy"}),
}

# Optional dev aid: dump raw MCP responses per case for offline replay (never packaged).
DUMP_DIR_ENV = "DAY09_DUMP_DIR"
MAX_TRANSIENT_RETRIES = 1
RETRY_BACKOFF_SECONDS = 1.0


@dataclass(frozen=True)
class Evidence:
    tool: str
    ref: str
    domain: str
    data: Any
    warnings: tuple[str, ...]


class EvidenceCollector:
    """Fetches evidence for one case. Each (tool, args) pair is called at most once."""

    def __init__(
        self,
        gateway: EvidenceGateway,
        trace: TraceWriter,
        case_id: str,
        available_tools: frozenset[str] | None = None,
        cache: dict[tuple[str, tuple[tuple[str, str], ...]], Evidence | None] | None = None,
    ) -> None:
        self._gateway = gateway
        self._trace = trace
        self._case_id = case_id
        self._available = available_tools
        # The cache may outlive this collector: when an MCP session drops mid-case, the
        # re-run of that case reuses results already returned (same case, real refs) instead
        # of calling the tools again.
        self._cache = {} if cache is None else cache
        self.calls = 0
        self.failures: list[str] = []
        self.consumed_refs: set[str] = set()

    async def fetch(self, actor: str, tool: str, **arguments: str) -> Evidence | None:
        if tool not in TOOL_PERMISSIONS.get(actor, frozenset()):
            raise PermissionError(f"{actor} is not allowed to call {tool}")
        if self._available is not None and tool not in self._available:
            self.failures.append(f"{tool}:not_discovered")
            return None
        key = (tool, tuple(sorted(arguments.items())))
        if key in self._cache:
            result = self._cache[key]
        else:
            result = await self._call_with_retry(tool, arguments)
            self._cache[key] = result
            self._dump(tool, arguments, result)
        if result is not None and result.ref not in self.consumed_refs:
            self.consumed_refs.add(result.ref)
            self._trace.emit(
                case_id=self._case_id,
                event_type="tool_result_consumed",
                actor=actor,
                tool_name=tool,
                evidence_refs=[result.ref],
                attributes={"domain": result.domain, "warnings": len(result.warnings)},
            )
        return result

    def _dump(self, tool: str, arguments: dict[str, str], result: Evidence | None) -> None:
        directory = os.getenv(DUMP_DIR_ENV)
        if not directory:
            return
        path = Path(directory) / f"{self._case_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        dump = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        label = f"{tool}:{arguments['order_id']}" if tool == "get_order" else tool
        dump[label] = (
            {"error": self.failures[-1] if self.failures else "unknown"}
            if result is None
            else {
                "evidence_ref": result.ref,
                "domain": result.domain,
                "data": result.data,
                "warnings": list(result.warnings),
            }
        )
        path.write_text(json.dumps(dump, ensure_ascii=False, indent=1), encoding="utf-8")

    async def _call_with_retry(self, tool: str, arguments: dict[str, str]) -> Evidence | None:
        for attempt in range(MAX_TRANSIENT_RETRIES + 1):
            self.calls += 1
            try:
                raw = await self._gateway.call(tool, case_id=self._case_id, **arguments)
            except TRANSPORT_ERRORS as exc:
                if attempt >= MAX_TRANSIENT_RETRIES:
                    # Never degrade to guessed data: fail the case so the runner retries it.
                    raise GatewayUnavailable(f"{tool}: {type(exc).__name__}") from exc
                self.failures.append(f"{tool}:transient:{type(exc).__name__}")
                await asyncio.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
                continue
            except RuntimeError:
                # Deterministic tool error (e.g. no rows for this scope): never retry.
                self.failures.append(f"{tool}:tool_error")
                return None
            return Evidence(
                tool=tool,
                ref=raw["evidence_ref"],
                domain=raw["domain"],
                data=raw["data"],
                warnings=tuple(raw.get("warnings") or ()),
            )
        return None
