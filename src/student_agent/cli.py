from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .cases import load_case_set
from .config import Settings
from .contracts import Contracts
from .mcp_gateway import EvidenceGateway, connect_gateway, is_transport_failure
from .submission import package_submission, validate_artifacts
from .trace import CaseTraceBuffer, TraceWriter
from .workflow import solve_case

CASE_CONCURRENCY = 4
MAX_SESSIONS = 6
RECONNECT_BACKOFF_SECONDS = 3.0


def _root(value: str) -> Path:
    return Path(value).resolve()


async def _show_tools(root: Path) -> None:
    settings = Settings.load(root)
    contracts = Contracts(root / "contracts" / "schemas")
    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        for tool in await gateway.list_tools():
            print(tool)


def _prune_trace(trace_path: Path, keep: set[str]) -> None:
    """Keep only events of completed cases (used by --resume)."""
    if not trace_path.exists():
        return
    lines = trace_path.read_text(encoding="utf-8").splitlines()
    kept = [line for line in lines if line.strip() and json.loads(line)["case_id"] in keep]
    trace_path.write_text("".join(line + "\n" for line in kept), encoding="utf-8")


async def _run(root: Path, resume: bool = False) -> None:
    settings = Settings.load(root)
    case_set = load_case_set(root)
    contracts = Contracts(root / "contracts" / "schemas")
    output_root = root / "outputs"
    trace_path = root / "traces" / "trace.jsonl"
    output_root.mkdir(parents=True, exist_ok=True)
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    for partial in output_root.glob("*.json.tmp"):
        partial.unlink()
    if resume:
        done = {p.stem for p in output_root.glob("*.json")} & set(case_set.case_ids)
        _prune_trace(trace_path, done)
    else:
        for stale in output_root.glob("*.json"):
            stale.unlink()
        trace_path.unlink(missing_ok=True)
        done = set()
    trace = TraceWriter(trace_path, contracts)

    async def run_one(gateway: EvidenceGateway, semaphore: asyncio.Semaphore, case_id: str) -> None:
        async with semaphore:
            case = case_set.cases[case_id]
            buffer = CaseTraceBuffer(trace)
            buffer.emit(case_id=case_id, event_type="case_received", actor="coordinator")
            output = await solve_case(case, gateway, buffer)
            contracts.validate_output(output, f"outputs/{case_id}.json")
            if output.get("case_id") != case_id:
                raise ValueError(f"solver returned a mismatched case_id for {case_id}")
            if not output.get("evidence_refs"):
                # An evidence-less output trips missing_required_evidence (score 0 for the whole
                # submission). Abort instead of writing it; usually MCP is rejecting calls.
                raise RuntimeError(
                    f"{case_id}: no MCP evidence collected (every tool call failed); "
                    "aborting run - check the MCP gateway, then rerun"
                )
            buffer.emit(case_id=case_id, event_type="case_finalized", actor="coordinator")
            target = output_root / f"{case_id}.json"
            temporary = target.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            buffer.flush()
            temporary.replace(target)
            done.add(case_id)

    for session in range(1, MAX_SESSIONS + 1):
        pending = [case_id for case_id in case_set.case_ids if case_id not in done]
        if not pending:
            break
        try:
            async with connect_gateway(
                settings.mcp_endpoint, settings.team_api_key, contracts
            ) as gateway:
                if not await gateway.list_tools():
                    raise RuntimeError("MCP Gateway returned no tools")
                semaphore = asyncio.Semaphore(CASE_CONCURRENCY)
                async with asyncio.TaskGroup() as group:
                    for case_id in pending:
                        group.create_task(run_one(gateway, semaphore, case_id))
        except BaseException as exc:
            if not is_transport_failure(exc) or session == MAX_SESSIONS:
                raise
            print(
                f"WARN: MCP session {session} lost ({type(exc).__name__}); "
                f"{len(done)}/{len(case_set.case_ids)} cases done, reconnecting...",
                file=sys.stderr,
            )
            await asyncio.sleep(RECONNECT_BACKOFF_SECONDS * session)

    missing = [case_id for case_id in case_set.case_ids if case_id not in done]
    if missing:
        raise RuntimeError(f"{len(missing)} cases not completed; rerun with --resume")
    print(f"OK: {len(done)} cases written to {output_root}")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Day09 L3B student workflow")
    result.add_argument("--root", default=".", help="repository root (default: current directory)")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("validate-inputs", help="validate case-set.json and all 100 inputs")
    commands.add_parser("mcp-tools", help="authenticate and list discovered MCP tools")
    run = commands.add_parser("run", help="run the implemented workflow for all cases")
    run.add_argument(
        "--resume",
        action="store_true",
        help="keep completed outputs/trace and only run the missing cases",
    )
    commands.add_parser("validate", help="validate outputs and observable trace")
    package = commands.add_parser("package", help="validate and build the submission ZIP")
    package.add_argument("--output", default="dist/submission.zip")
    return result


def main() -> None:
    args = parser().parse_args()
    root = _root(args.root)
    try:
        if args.command == "validate-inputs":
            case_set = load_case_set(root)
            print(
                f"OK: {case_set.variant_id} / {case_set.version} / {len(case_set.case_ids)} cases"
            )
        elif args.command == "mcp-tools":
            asyncio.run(_show_tools(root))
        elif args.command == "run":
            asyncio.run(_run(root, resume=args.resume))
        elif args.command == "validate":
            case_set = load_case_set(root)
            contracts = Contracts(root / "contracts" / "schemas")
            _, trace = validate_artifacts(root, case_set, contracts)
            print(f"OK: {len(case_set.case_ids)} outputs / {len(trace)} trace events")
        elif args.command == "package":
            destination = package_submission(root, root / args.output)
            print(f"OK: {destination}")
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
