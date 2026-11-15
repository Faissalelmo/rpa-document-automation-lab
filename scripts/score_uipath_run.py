"""Score a run of the UiPath Performer (uipath/InvoicePerformer) against the ground truth.

Same definitions as rpa_lab/evaluate.py, but the queue items live in UiPath Orchestrator (cloud), so the
per-transaction outcome is rebuilt from the robot's own execution log: the REFramework writes one line per
transaction with logF_TransactionID (the file name) and logF_TransactionStatus (Success / BusinessException /
ApplicationException). The last attempt of each document is its final outcome. ERP bookings and chaos
counts come from the lab ERP (:8091).

Usage:  python scripts/score_uipath_run.py <Execution.log> <jobId> [--name uipath_performer]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from rpa_lab.config import load_config  # noqa: E402
from rpa_lab.paths import DATA, GROUND_TRUTH, REPORTS  # noqa: E402

REASON = re.compile(r"\b(BE_[A-Z_]+)\b")


def read_transactions(log_path: Path, job_id: str) -> list[dict]:
    out = []
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if f'"jobId":"{job_id}"' not in line:
            continue
        rec = json.loads(line[line.index("{"):])
        if rec.get("logF_TransactionStatus") and rec.get("logF_TransactionID"):
            out.append(rec)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("log", type=Path)
    ap.add_argument("job_id")
    ap.add_argument("--name", default="uipath_performer")
    ap.add_argument("--queue", default="Invoices")
    ap.add_argument("--extractor", default="lab OCR service (Tesseract, rpa_lab/extract_api.py)")
    args = ap.parse_args()

    cfg = load_config()
    gt = {d["file"]: d for d in json.loads(GROUND_TRUTH.read_text(encoding="utf-8"))}
    copy_of = {r["copy"]: r["original"] for r in json.loads((DATA / "resent.json").read_text(encoding="utf-8"))}
    txs = read_transactions(args.log, args.job_id)
    if not txs:
        raise SystemExit("no transaction status lines for that job id")

    chains: dict[str, list[dict]] = defaultdict(list)
    for t in sorted(txs, key=lambda r: r["timeStamp"]):
        chains[copy_of.get(t["logF_TransactionID"], t["logF_TransactionID"])].append(t)

    def outcome(t: dict) -> str:
        s = t["logF_TransactionStatus"]
        if s == "Success":
            return "OK"
        if s == "BusinessException":
            m = REASON.search(t.get("message", ""))
            return m.group(1) if m else "BE_UNKNOWN_REASON"
        return "SYSTEM_FAILED"

    with httpx.Client(timeout=30) as c:
        erp = c.get(f"{cfg['ERP_URL']}/admin/stats").json()
        booked_numbers = {i["invoice_number"] for i in c.get(f"{cfg['ERP_URL']}/admin/invoices").json()}
    # Process.xaml logs this Warn when a retry finds the booking its earlier attempt committed (lost response)
    job_lines = [line for line in args.log.read_text(encoding="utf-8", errors="replace").splitlines()
                 if f'"jobId":"{args.job_id}"' in line]
    lost_recovered = sum(1 for line in job_lines if "lost response recovered" in line)
    # Process.xaml logs one of these per document sent to human review (DU validation task in Action Center)
    ac_created = sum(1 for line in job_lines if "Action Center validation task created" in line)
    ac_failed = sum(1 for line in job_lines if "Action Center task NOT created" in line)

    # Field accuracy of the extraction (DU Performer): Process.xaml logs "DU extracted: {json}" before the
    # transaction's status line, which is the first line that names the file, so pair each with the next one.
    fields_ok: dict[str, Counter] = defaultdict(Counter)
    extracted: dict[str, dict] = {}  # last extraction per original document
    pending = None
    for line in job_lines:
        rec = json.loads(line[line.index("{"):])
        msg = rec.get("message", "")
        if msg.startswith("DU extracted: "):
            pending = json.loads(msg[len("DU extracted: "):])
        elif pending is not None and rec.get("logF_TransactionStatus") and rec.get("logF_TransactionID"):
            doc = gt.get(copy_of.get(rec["logF_TransactionID"], rec["logF_TransactionID"]))
            if doc:
                extracted[doc["file"]] = pending
                for k, v in doc["fields"].items():
                    fields_ok[doc["layout"]]["total"] += 1
                    fields_ok[doc["layout"]]["ok"] += pending.get(k) == v
                    if pending.get(k) is None:
                        fields_ok[doc["layout"]][f"missing:{k}"] += 1
            pending = None

    confusion: dict[str, Counter] = defaultdict(Counter)
    app_exc, recovered, unrecovered, per_doc = 0, 0, 0, []
    for name, doc in gt.items():
        chain = chains.get(name)
        actual = outcome(chain[-1]) if chain else "NOT_PROCESSED"
        expected = doc["expected_outcome"]
        confusion[expected][actual] += 1
        n_app = sum(1 for t in chain or [] if t["logF_TransactionStatus"] == "ApplicationException")
        app_exc += n_app
        if n_app:
            recovered += actual != "SYSTEM_FAILED"
            unrecovered += actual == "SYSTEM_FAILED"
        per_doc.append({"file": name, "expected": expected, "actual": actual, "attempts": len(chain or []),
                        "in_erp": doc["fields"]["invoice_number"] in booked_numbers,
                        **({"extracted": extracted[name]} if name in extracted else {})})

    n = len(gt)
    pct = lambda a, b: round(100 * a / b, 1) if b else None  # noqa: E731
    ok_expected = sum(confusion["OK"].values())
    times = sorted(t["timeStamp"] for t in txs)
    result = {
        "run": {"name": args.name, "engine": "UiPath REFramework (Studio 26.10, VB) on a local attended robot",
                "queue": f"UiPath Orchestrator queue '{args.queue}' (cloud), filled by uipath/Solution/InvoiceDispatcher",
                "extraction": args.extractor,
                "job_id": args.job_id, "robots": 1,
                "chaos": {"RPA_ERP_FAILURE_RATE": "0.06", "RPA_ERP_SLOW_RATE": "0.03", "RPA_ERP_LOST_RESPONSE": "0.08"}},
        "documents": n,
        "transactions_logged": len(txs),
        "outcome_accuracy_pct": pct(sum(confusion[e][e] for e in confusion), n),
        "straight_through": {"expected": ok_expected, "booked": confusion["OK"]["OK"],
                             "rate_pct": pct(confusion["OK"]["OK"], ok_expected)},
        "business_exceptions": {
            "injected": n - ok_expected,
            "stopped_before_booking": sum(sum(v for a, v in c.items() if a != "OK") for e, c in confusion.items() if e != "OK"),
            "stopped_with_the_right_reason": sum(confusion[e][e] for e in confusion if e != "OK"),
            "wrongly_booked": sum(c["OK"] for e, c in confusion.items() if e != "OK")},
        "false_rejections": {"count": ok_expected - confusion["OK"]["OK"],
                             "reasons": dict(Counter(a for a, v in confusion["OK"].items() for _ in range(v) if a != "OK"))},
        "system_exceptions": {"application_exception_items": app_exc, "documents_affected": recovered + unrecovered,
                              "recovered_by_auto_retry": recovered, "still_failed_after_retries": unrecovered,
                              "lost_responses_recovered_without_double_booking": lost_recovered,
                              # failed in Orchestrator after the last retry, but the ERP holds the booking (once):
                              # the final attempt's POST committed and its response was lost
                              "failed_but_booked_once": [d["file"] for d in per_doc
                                                         if d["actual"] == "SYSTEM_FAILED" and d["in_erp"]],
                              "erp_chaos_injected": erp["chaos"]},
        "erp": {"booked_by_robot": erp["booked_by_robot"], "double_bookings": erp["double_bookings"]},
        "action_center": {"validation_tasks_created": ac_created, "task_creation_failed": ac_failed},
        # every extraction attempt (retries included), all 7 fields, against ground truth
        "field_accuracy_by_layout_pct": {lay: pct(c["ok"], c["total"]) for lay, c in sorted(fields_ok.items())},
        "fields_missing_by_layout": {lay: {k[8:]: v for k, v in c.items() if k.startswith("missing:")}
                                     for lay, c in sorted(fields_ok.items())},
        "extractions_scored": sum(c["total"] for c in fields_ok.values()) // 7,
        "timing": {"first_transaction": times[0], "last_transaction": times[-1]},
        "confusion": {e: dict(c) for e, c in sorted(confusion.items())},
        "per_document": per_doc,
    }
    REPORTS.mkdir(exist_ok=True)
    out = REPORTS / f"{args.name}.json"
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    brief = {k: result[k] for k in ("outcome_accuracy_pct", "straight_through", "business_exceptions",
                                    "false_rejections", "system_exceptions", "erp", "action_center",
                                    "field_accuracy_by_layout_pct", "fields_missing_by_layout")}
    print(json.dumps(brief, indent=2, ensure_ascii=False))
    print(f"report: {out}")


if __name__ == "__main__":
    main()
