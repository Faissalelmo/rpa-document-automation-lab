"""Score a finished run against the ground truth written by the generator before the robot ran.

Every number in reports/*.json comes from here; nothing in the README is typed by hand.
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict

import httpx

from .paths import DATA, GROUND_TRUTH

FIELDS = ("supplier_siret", "invoice_number", "invoice_date", "po_number", "total_ht", "tva", "total_ttc")


def final_outcome(item: dict) -> str:
    if item["Status"] == "Successful":
        return "OK"
    if item["Status"] == "Failed" and item["ProcessingExceptionType"] == "BusinessException":
        return item["ProcessingException"]["Reason"]
    if item["Status"] == "Failed":
        return "SYSTEM_FAILED"
    return item["Status"].upper()


def evaluate(cfg: dict, wall_seconds: float, dispatch: dict, robots: list[dict]) -> dict:
    gt = {d["file"]: d for d in json.loads(GROUND_TRUTH.read_text(encoding="utf-8"))}
    resent = json.loads((DATA / "resent.json").read_text(encoding="utf-8"))
    with httpx.Client(timeout=30) as c:
        items = c.get(f"{cfg['OrchestratorURL']}/api/queues/{cfg['QueueName']}/items").json()
        stats = c.get(f"{cfg['OrchestratorURL']}/api/queues/{cfg['QueueName']}/stats").json()
        erp = c.get(f"{cfg['ERP_URL']}/admin/stats").json()

    by_ref: dict[str, list[dict]] = defaultdict(list)
    for it in items:
        by_ref[it["Reference"]].append(it)
    finals = {}
    for ref, chain in by_ref.items():
        last = max(chain, key=lambda x: x["Id"])
        finals[last["SpecificContent"]["FileName"]] = (last, chain)

    confusion: dict[str, Counter] = defaultdict(Counter)
    per_doc = []
    field_hits, field_total = Counter(), Counter()
    by_variant = defaultdict(lambda: [0, 0])
    by_layout = defaultdict(lambda: [0, 0])
    app_exc_items, recovered_refs, unrecovered_refs, lost_recoveries = 0, 0, 0, 0
    for name, doc in gt.items():
        last, chain = finals[name]
        actual = final_outcome(last)
        expected = doc["expected_outcome"]
        confusion[expected][actual] += 1
        n_app = sum(1 for x in chain if x["ProcessingExceptionType"] == "ApplicationException")
        app_exc_items += n_app
        if n_app:
            recovered_refs += actual != "SYSTEM_FAILED"
            unrecovered_refs += actual == "SYSTEM_FAILED"
        out = last.get("Output") or {}
        lost_recoveries += bool(out.get("recovered_lost_response"))
        got = out.get("fields") or {}
        for k in FIELDS:
            hit = got.get(k) == doc["fields"][k]
            field_hits[k] += hit
            field_total[k] += 1
            by_variant[doc["variant"]][0] += hit
            by_variant[doc["variant"]][1] += 1
            by_layout[doc["layout"]][0] += hit
            by_layout[doc["layout"]][1] += 1
        per_doc.append({"file": name, "variant": doc["variant"], "layout": doc["layout"], "expected": expected,
                        "actual": actual, "attempts": len(chain), "method": out.get("method")})

    n = len(gt)
    correct = sum(confusion[e][e] for e in confusion)
    wrongly_booked = sum(c["OK"] for e, c in confusion.items() if e != "OK")
    ok_expected = sum(confusion["OK"].values())
    exceptions_expected = n - ok_expected
    exceptions_caught = sum(sum(v for a, v in c.items() if a != "OK") for e, c in confusion.items() if e != "OK")
    pct = lambda a, b: round(100 * a / b, 1) if b else None  # noqa: E731

    return {
        "documents": n,
        "robots": len(robots),
        "outcome_accuracy_pct": pct(correct, n),
        "straight_through": {"expected": ok_expected, "booked": confusion["OK"]["OK"],
                             "rate_pct": pct(confusion["OK"]["OK"], ok_expected)},
        "business_exceptions": {"injected": exceptions_expected, "stopped_before_booking": exceptions_caught,
                                "stopped_with_the_right_reason": sum(confusion[e][e] for e in confusion if e != "OK"),
                                "wrongly_booked": wrongly_booked},
        "false_rejections": {"count": ok_expected - confusion["OK"]["OK"],
                             "reasons": dict(Counter(a for a, v in confusion["OK"].items() for _ in range(v) if a != "OK"))},
        "system_exceptions": {"application_exception_items": app_exc_items,
                              "documents_affected": recovered_refs + unrecovered_refs,
                              "recovered_by_auto_retry": recovered_refs,
                              "still_failed_after_retries": unrecovered_refs,
                              "lost_responses_recovered_without_double_booking": lost_recoveries,
                              "erp_chaos_injected": erp["chaos"]},
        "erp": {"booked_by_robot": erp["booked_by_robot"], "double_bookings": erp["double_bookings"]},
        "dispatcher": {"queued": dispatch["added"], "resent_files": len(resent),
                       "rejected_as_duplicate_reference": len(dispatch["rejected_duplicates"])},
        "field_accuracy_pct": {k: pct(field_hits[k], field_total[k]) for k in FIELDS},
        "field_accuracy_by_variant_pct": {k: pct(*v) for k, v in sorted(by_variant.items())},
        "field_accuracy_by_layout_pct": {k: pct(*v) for k, v in sorted(by_layout.items())},
        "confusion": {e: dict(c) for e, c in sorted(confusion.items())},
        "timing": {"wall_seconds": round(wall_seconds, 1),
                   "avg_transaction_seconds": stats["avg_processing_seconds"],
                   "documents_per_minute": round(n / wall_seconds * 60, 1)},
        "per_robot": robots,
        "per_document": per_doc,
    }
