"""Dev-only: print MCP tool input schemas and dump raw responses for a few cases.

Usage: python scripts/explore_mcp.py [--schemas-only] CASE_ID [CASE_ID ...]
Every call is audited by the server, so keep the case list small.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from student_agent.cases import load_case_set
from student_agent.config import Settings
from student_agent.contracts import Contracts
from student_agent.mcp_gateway import connect_gateway

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "debug" / "mcp"


async def main(case_ids: list[str], schemas_only: bool) -> None:
    settings = Settings.load(ROOT)
    contracts = Contracts(ROOT / "contracts" / "schemas")
    case_set = load_case_set(ROOT)
    OUT.mkdir(parents=True, exist_ok=True)
    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gw:
        tools = (await gw._session.list_tools()).tools
        schemas = {t.name: {"description": t.description, "input": t.input_schema} for t in tools}
        (OUT / "tool_schemas.json").write_text(json.dumps(schemas, indent=2), encoding="utf-8")
        for name, meta in schemas.items():
            props = meta["input"].get("properties", {})
            print(f"{name}: required={meta['input'].get('required')} props={list(props)}")
            print(f"    {meta['description']}")
        if schemas_only:
            return

        for case_id in case_ids:
            case = case_set.cases[case_id]
            order_id = case["customer_request"]["claimed_order_id"]
            hint = case["customer_unique_id_hint"]
            dump: dict[str, object] = {}

            async def call(label: str, tool: str, _dump=dump, _cid=case_id, **args: str) -> None:
                try:
                    _dump[label] = await gw.call(tool, case_id=_cid, **args)
                except Exception as exc:  # noqa: BLE001 - exploration only
                    _dump[label] = {"error": repr(exc)}

            await call("get_order", "get_order", order_id=order_id)
            for name, meta in schemas.items():
                if name == "get_order":
                    continue
                props = meta["input"].get("properties", {})
                args = {}
                if "order_id" in props:
                    args["order_id"] = order_id
                if "customer_unique_id" in props:
                    args["customer_unique_id"] = hint
                if "policy_version" in props:
                    args["policy_version"] = case["policy_version"]
                await call(name, name, **args)
            path = OUT / f"{case_id}.json"
            path.write_text(json.dumps(dump, indent=2, ensure_ascii=False), encoding="utf-8")
            print(f"wrote {path}")


if __name__ == "__main__":
    argv = sys.argv[1:]
    only = "--schemas-only" in argv
    asyncio.run(main([a for a in argv if not a.startswith("--")], only))
