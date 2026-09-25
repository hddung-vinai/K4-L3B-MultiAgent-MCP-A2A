"""Dev-only: audit a submission ZIP against every known hard gate before uploading.

Usage: python scripts/check_submission.py dist/submission.zip [--dumps debug/mcp_v3]
                                          [--baseline dist/submission-v1.zip]
Exit code 0 only if every check passes.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import zipfile
from collections import Counter, defaultdict
from pathlib import Path

from student_agent.cases import load_case_set
from student_agent.contracts import Contracts

ROOT = Path(__file__).resolve().parents[1]
REF = re.compile(r"^ev_[A-Za-z0-9_-]{20,96}$")
SECRET = re.compile(r"sk-team-[A-Za-z0-9_-]{8,}")
REQUIRED_EVENTS = [
    "case_received",
    "task_assigned",
    "tool_result_consumed",
    "handoff",
    "policy_decided",
    "verification_completed",
    "case_finalized",
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("zip")
    ap.add_argument("--dumps", default=None, help="raw MCP dump dir of the run")
    ap.add_argument("--baseline", default=None, help="accepted submission to compare evidence")
    ap.add_argument(
        "--allow-dropped",
        action="append",
        default=[],
        help="optional tool: ignored on both sides of the baseline comparison",
    )
    args = ap.parse_args()

    problems: list[str] = []
    ok: list[str] = []
    contracts = Contracts(ROOT / "contracts" / "schemas")
    case_set = load_case_set(ROOT)
    expected = set(case_set.case_ids)

    z = zipfile.ZipFile(args.zip)
    names = z.namelist()
    # 1. ZIP layout
    tops = {n.split("/")[0] for n in names}
    outputs = {n for n in names if n.startswith("outputs/") and n.endswith(".json")}
    extra = set(names) - outputs - {"manifest.json", "trace.jsonl"}
    if tops != {"manifest.json", "trace.jsonl", "outputs"} or extra:
        problems.append(f"zip layout: top={sorted(tops)} extra={sorted(extra)}")
    else:
        ok.append("zip layout: manifest.json, trace.jsonl, outputs/ at root, nothing else")

    # 2. manifest
    manifest = json.loads(z.read("manifest.json"))
    try:
        contracts.validate_manifest(manifest)
        assert manifest["variant_id"] == "l3b", manifest["variant_id"]
        assert manifest["case_set_version"] == case_set.version, manifest["case_set_version"]
        ok.append(f"manifest valid (l3b / {manifest['case_set_version']})")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"manifest: {exc}")

    # 3. outputs: exact case set, schema, case_id
    ids = {Path(n).stem for n in outputs}
    if ids != expected or len(outputs) != 100:
        problems.append(f"outputs: missing={sorted(expected - ids)} extra={sorted(ids - expected)}")
    docs: dict[str, dict] = {}
    for n in sorted(outputs):
        doc = json.loads(z.read(n))
        cid = Path(n).stem
        docs[cid] = doc
        try:
            contracts.validate_output(doc, n)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"schema {n}: {exc}")
        if doc.get("case_id") != cid:
            problems.append(f"case_id mismatch in {n}: {doc.get('case_id')}")
    if not any(p.startswith(("schema", "case_id", "outputs")) for p in problems):
        ok.append("100 outputs: exact case-set, schema-valid, case_id matches file name")

    # 4. trace
    events = []
    for i, line in enumerate(z.read("trace.jsonl").decode("utf-8").splitlines(), 1):
        if not line.strip():
            continue
        e = json.loads(line)
        try:
            contracts.validate_trace(e, f"trace:{i}")
        except Exception as exc:  # noqa: BLE001
            problems.append(str(exc))
        events.append(e)
    dup_ids = [k for k, v in Counter(e["event_id"] for e in events).items() if v > 1]
    if dup_ids:
        problems.append(f"duplicate event ids: {dup_ids[:5]}")
    per: dict[str, list] = defaultdict(list)
    for e in events:
        per[e["case_id"]].append(e)
    if set(per) != expected:
        problems.append(f"trace cases missing={sorted(expected - set(per))}")
    for cid, es in per.items():
        types = [e["event_type"] for e in es]
        if types[0] != "case_received" or types[-1] != "case_finalized":
            problems.append(f"{cid}: trace not received..finalized")
        if types.count("case_received") != 1 or types.count("case_finalized") != 1:
            problems.append(f"{cid}: received/finalized count")
        missing = [t for t in REQUIRED_EVENTS if t not in types]
        if missing:
            problems.append(f"{cid}: missing events {missing}")
    if not any("trace" in p or "events" in p or "received" in p for p in problems):
        ok.append(f"trace: {len(events)} schema-valid events, unique ids, all 7 types per case")

    # 5. evidence refs: format, traced in same case, never shared across cases
    consumed: dict[str, dict[str, str]] = defaultdict(dict)  # case -> ref -> tool
    ref_owner: dict[str, set] = defaultdict(set)
    for e in events:
        for r in e.get("evidence_refs") or []:
            ref_owner[r].add(e["case_id"])
        if e["event_type"] == "tool_result_consumed":
            consumed[e["case_id"]][e["evidence_refs"][0]] = e["tool_name"]
    cross = [r for r, owners in ref_owner.items() if len(owners) > 1]
    if cross:
        problems.append(f"refs appear in several cases: {cross[:5]}")
    for cid, doc in docs.items():
        refs = list(doc["evidence_refs"])
        for c in doc.get("claim_assessments") or []:
            refs += c["evidence_refs"]
        for r in refs:
            if not REF.fullmatch(r):
                problems.append(f"{cid}: bad ref format {r}")
            elif r not in consumed[cid]:
                problems.append(f"{cid}: ref {r} not consumed in this case's trace")
        if not doc["evidence_refs"]:
            problems.append(f"{cid}: no evidence refs")
    if not any("ref" in p for p in problems):
        ok.append("evidence refs: valid format, each consumed in its own case, no cross-case")

    # 6. refs really came from MCP responses of this case (raw dumps of the run)
    if args.dumps:
        real: dict[str, set] = defaultdict(set)
        for path in Path(args.dumps).glob("*.json"):
            dump = json.loads(path.read_text(encoding="utf-8"))
            for value in dump.values():
                if isinstance(value, dict) and value.get("evidence_ref"):
                    real[path.stem].add(value["evidence_ref"])
        unknown = [
            (cid, r)
            for cid, doc in docs.items()
            for r in doc["evidence_refs"]
            if r not in real[cid]
        ]
        if unknown:
            problems.append(f"refs not returned by MCP for that case: {unknown[:5]}")
        else:
            ok.append("every output ref was returned by the MCP server for that same case")

    # 7. required evidence: cited tools per case identical to the accepted baseline
    if args.baseline:
        b = zipfile.ZipFile(args.baseline)
        btool: dict[str, str] = {}
        for line in b.read("trace.jsonl").decode("utf-8").splitlines():
            e = json.loads(line)
            if e["event_type"] == "tool_result_consumed":
                btool[e["evidence_refs"][0]] = e["tool_name"]
        diffs = []
        for n in b.namelist():
            if n.startswith("outputs/"):
                bdoc = json.loads(b.read(n))
                cid = bdoc["case_id"]
                want = Counter(
                    btool[r] for r in bdoc["evidence_refs"] if btool[r] not in args.allow_dropped
                )
                got = Counter(
                    tool
                    for tool in (consumed[cid].get(r, "?") for r in docs[cid]["evidence_refs"])
                    if tool not in args.allow_dropped
                )
                if want != got:
                    diffs.append((cid, dict(want - got), dict(got - want)))
        if diffs:
            problems.append(f"cited tools differ from baseline: {diffs[:5]}")
        else:
            ok.append("cited evidence tools identical to accepted baseline for all 100 cases")

    # 8. cross-field consistency invariants
    inconsistent = []
    for cid, doc in docs.items():
        a, fin = doc["assessment"], doc["financial_resolution"]
        pay = doc["payment_analysis"]
        refund = fin["recommended_refund_brl"]
        lines = sum(line["amount_brl"] for line in fin["refund_lines"])
        if a["case_status"] == "no_action" and (refund or fin["refund_lines"]):
            inconsistent.append((cid, "no_action with refund"))
        if abs(lines - refund) > 0.005:
            inconsistent.append((cid, "refund_lines sum != recommended"))
        if pay["refundable_total_brl"] is not None and refund > pay["refundable_total_brl"] + 0.005:
            inconsistent.append((cid, "refund > refundable"))
        if len(set(doc["resolution_actions"])) != len(doc["resolution_actions"]):
            inconsistent.append((cid, "duplicate actions"))
        parties = doc["root_cause_analysis"]["responsible_parties"]
        if a["primary_issue"] == "late_delivery_seller":
            sellers = {p["party_id"] for p in parties if p["party_type"] == "seller"}
            if not sellers or not sellers <= set(doc["shipment_analysis"]["late_seller_ids"]):
                inconsistent.append((cid, "seller responsibility vs late sellers"))
        if not 0 <= a["confidence"] <= 1:
            inconsistent.append((cid, "confidence bounds"))
    if inconsistent:
        problems.append(f"consistency: {inconsistent[:5]}")
    else:
        ok.append("consistency invariants hold (status/refund/actions/seller/confidence)")

    # 9. secrets
    blob = b"".join(z.read(n) for n in names).decode("utf-8", "replace")
    if SECRET.search(blob):
        problems.append("Team API key found inside the ZIP")
    else:
        ok.append("no Team API key inside the ZIP")

    for line in ok:
        print(f"PASS  {line}")
    for line in problems:
        print(f"FAIL  {line}")
    print("RESULT:", "ALL CHECKS PASSED" if not problems else f"{len(problems)} PROBLEM(S)")
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
