"""Dev-only: run solve_case offline against MCP responses saved by explore_mcp.py.

Usage: python scripts/replay.py [CASE_ID ...]   (default: every dump in debug/mcp)
No MCP calls are made; outputs go to debug/replay/ and are schema-validated.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from student_agent.cases import load_case_set
from student_agent.contracts import Contracts
from student_agent.trace import TraceWriter
from student_agent.workflow import solve_case

ROOT = Path(__file__).resolve().parents[1]
DUMPS = ROOT / "debug" / "mcp"
OUT = ROOT / "debug" / "replay"


class ReplayGateway:
    def __init__(self, dumps: dict[str, dict[str, Any]]) -> None:
        self.dumps = dumps
        self.calls: Counter[str] = Counter()

    async def list_tools(self) -> list[str]:
        schemas = json.loads((DUMPS / "tool_schemas.json").read_text(encoding="utf-8"))
        return sorted(schemas)

    async def call(self, tool: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        self.calls[case_id] += 1
        dump = self.dumps[case_id]
        entry = dump.get(f"{tool}:{arguments.get('order_id')}") or dump.get(tool)
        if tool == "get_order" and entry is not None:
            data = entry.get("data") or {}
            if data.get("order_id") != arguments.get("order_id"):
                entry = None
        if entry is None or "error" in entry:
            raise RuntimeError(f"MCP tool {tool} failed: replay has no data")
        return entry


async def main(case_ids: list[str]) -> None:
    contracts = Contracts(ROOT / "contracts" / "schemas")
    case_set = load_case_set(ROOT)
    if not case_ids:
        case_ids = sorted(p.stem for p in DUMPS.glob("*.json") if p.stem in case_set.cases)
    dumps = {
        cid: json.loads((DUMPS / f"{cid}.json").read_text(encoding="utf-8")) for cid in case_ids
    }
    OUT.mkdir(parents=True, exist_ok=True)
    trace_path = OUT / "trace.jsonl"
    trace_path.unlink(missing_ok=True)
    trace = TraceWriter(trace_path, contracts)
    gateway = ReplayGateway(dumps)
    for cid in case_ids:
        case = case_set.cases[cid]
        trace.emit(case_id=cid, event_type="case_received", actor="coordinator")
        output = await solve_case(case, gateway, trace)  # type: ignore[arg-type]
        contracts.validate_output(output, cid)
        trace.emit(case_id=cid, event_type="case_finalized", actor="coordinator")
        (OUT / f"{cid}.json").write_text(
            json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        topic = case["customer_request"]["claims"][0]["topic"]
        a, fin = output["assessment"], output["financial_resolution"]
        mark = "OK " if a["primary_issue"] == topic else "XX "
        print(
            f"{mark}{cid} claim={topic:<24} issue={a['primary_issue']:<24} "
            f"status={a['case_status']:<19} conf={a['confidence']:<5} "
            f"refund={fin['recommended_refund_brl']:<5} "
            f"ship={output['shipment_analysis']['verdict']:<16} "
            f"pay={output['payment_analysis']['verdict']:<17} calls={gateway.calls[cid]} "
            f"refs={len(output['evidence_refs'])} conflicts={len(output['data_conflicts'])}"
        )
    events = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    print(f"trace events: {len(events)}  types: {dict(Counter(e['event_type'] for e in events))}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
