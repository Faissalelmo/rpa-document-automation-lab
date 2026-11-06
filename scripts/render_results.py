"""Render the README results block from reports/*.json, so no figure in the README is typed by hand."""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
START, END = "<!-- results:start -->", "<!-- results:end -->"


def load(name: str) -> dict:
    return json.loads((ROOT / "reports" / f"{name}.json").read_text(encoding="utf-8"))


def row(label: str, b, a) -> str:
    return f"| {label} | {b} | {a} |"


def main() -> None:
    b, a = load("baseline"), load("ai_fallback")
    st = lambda r: f"{r['straight_through']['booked']}/{r['straight_through']['expected']} ({r['straight_through']['rate_pct']} %)"  # noqa: E731
    sx = lambda r: r["system_exceptions"]  # noqa: E731
    lines = [
        START,
        f"Measured on {b['documents']} generated invoices ({b['documents'] - b['straight_through']['expected']} with an injected "
        f"business-rule violation), {b['robots']} robots in parallel, ERP chaos on "
        f"(`{json.dumps(b['run']['chaos'])}`). Same documents, same chaos seed for both runs; "
        "robot scheduling makes the exact chaos hits differ slightly between runs.",
        "",
        "| | Rules only | Rules + local LLM fallback |",
        "|---|---|---|",
        row("Valid invoices booked with no human touch (straight-through)", st(b), st(a)),
        row("Invalid invoices **wrongly booked**", b["business_exceptions"]["wrongly_booked"],
            a["business_exceptions"]["wrongly_booked"]),
        row("Invalid invoices stopped before booking",
            f"{b['business_exceptions']['stopped_before_booking']}/{b['business_exceptions']['injected']}",
            f"{a['business_exceptions']['stopped_before_booking']}/{a['business_exceptions']['injected']}"),
        row("…stopped with the exact reason", b["business_exceptions"]["stopped_with_the_right_reason"],
            a["business_exceptions"]["stopped_with_the_right_reason"]),
        row("Final outcome = expected outcome", f"{b['outcome_accuracy_pct']} %", f"{a['outcome_accuracy_pct']} %"),
        row("Field accuracy, unseen supplier layout (D)", f"{b['field_accuracy_by_layout_pct']['D']} %",
            f"{a['field_accuracy_by_layout_pct']['D']} %"),
        row("Field accuracy, known layouts A / B / C",
            " / ".join(f"{b['field_accuracy_by_layout_pct'][k]} %" for k in "ABC"),
            " / ".join(f"{a['field_accuracy_by_layout_pct'][k]} %" for k in "ABC")),
        row("System exceptions (ERP 503 / timeout / lost response)", sx(b)["application_exception_items"],
            sx(a)["application_exception_items"]),
        row("…documents recovered by Orchestrator auto-retry",
            f"{sx(b)['recovered_by_auto_retry']}/{sx(b)['documents_affected']}",
            f"{sx(a)['recovered_by_auto_retry']}/{sx(a)['documents_affected']}"),
        row("…lost ERP responses resolved without re-booking", sx(b)["lost_responses_recovered_without_double_booking"],
            sx(a)["lost_responses_recovered_without_double_booking"]),
        row("Double bookings in the ERP", b["erp"]["double_bookings"], a["erp"]["double_bookings"]),
        row("Re-sent scans rejected by the queue's unique reference",
            f"{b['dispatcher']['rejected_as_duplicate_reference']}/{b['dispatcher']['resent_files']}",
            f"{a['dispatcher']['rejected_as_duplicate_reference']}/{a['dispatcher']['resent_files']}"),
        row("Mean time per transaction", f"{b['timing']['avg_transaction_seconds']} s", f"{a['timing']['avg_transaction_seconds']} s"),
    ]
    for name, r in (("Rules only", b), ("Rules + LLM", a)):
        l2 = r.get("l2_recovery")
        if l2:
            lines.append("")
            lines.append(
                f"**L2 recovery ({name}).** After the run, `Get-QueueHealth.ps1` exited `{l2['health_check_exit_code_before']}` "
                f"({sx(r)['still_failed_after_retries']} item(s) still failed after automatic retries). The ERP was restarted "
                f"without chaos, `Invoke-RetryFailedItems.ps1` re-queued {l2['items_retried']} item(s), one robot re-ran, and the "
                f"health check exited `{l2['health_check_exit_code_after']}`. Straight-through went from "
                f"{l2['straight_through_before']['booked']} to {l2['straight_through_after']['booked']}/"
                f"{l2['straight_through_after']['expected']}, with {l2['erp_after']['double_bookings']} double booking(s) and "
                f"{l2['wrongly_booked_after']} invalid invoice(s) booked.")
    lines += ["", "Full numbers, the confusion matrix and the per-document trace: "
              "[`reports/baseline.json`](reports/baseline.json), [`reports/ai_fallback.json`](reports/ai_fallback.json).", END]
    text = README.read_text(encoding="utf-8")
    text = re.sub(re.escape(START) + ".*?" + re.escape(END), lambda _: "\n".join(lines), text, flags=re.S)
    README.write_text(text, encoding="utf-8", newline="\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
