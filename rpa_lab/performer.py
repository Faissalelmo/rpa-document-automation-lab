"""Performer: the REFramework state machine, written out in Python.

    Init ──> Get Transaction Data ──> Process Transaction ──> (Set Transaction Status) ──┐
     ^                 │ no more items                                                    │
     │                 v                                                                  │
     │               End  <── too many consecutive system exceptions                      │
     └──── system exception: close and re-open the applications ◄─────────────────────────┘

Each state maps 1:1 to the REFramework file of the same name (InitAllApplications.xaml,
GetTransactionData.xaml, Process.xaml, SetTransactionStatus.xaml, CloseAllApplications.xaml).
"""
from __future__ import annotations

import argparse
import json
import socket
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from .ai_fallback import llm_extract
from .config import load_config
from .document import luhn_ok, read_invoice
from .exceptions import ApplicationException, BusinessRuleException
from .paths import DATA


class Robot:
    def __init__(self, name: str, ai_fallback: bool = False):
        self.cfg = load_config()
        self.name = name
        self.ai_fallback = ai_fallback
        self.log_path = DATA / "logs" / f"{name}.jsonl"
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.orch: httpx.Client | None = None
        self.erp: httpx.Client | None = None
        self.consecutive_system_exceptions = 0
        self.counts = {"Successful": 0, "BusinessException": 0, "ApplicationException": 0}
        self.current_output: dict = {}

    # ------------------------------------------------------------------ logging (like Log Message + Add Log Fields)
    def log(self, level: str, message: str, **fields) -> None:
        rec = {"ts": datetime.now(timezone.utc).isoformat(), "level": level, "robot": self.name,
               "message": message, **fields}
        with self.log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    # ------------------------------------------------------------------ Init
    def init_all_applications(self) -> None:
        self.close_all_applications()
        self.orch = httpx.Client(base_url=self.cfg["OrchestratorURL"], timeout=10)
        self.erp = httpx.Client(base_url=self.cfg["ERP_URL"], timeout=self.cfg["ErpTimeoutSeconds"])
        self.orch.get("/health").raise_for_status()
        self.log("Info", "Applications initialised")

    def close_all_applications(self) -> None:
        for c in (self.orch, self.erp):
            if c is not None:
                c.close()
        self.orch = self.erp = None

    # ------------------------------------------------------------------ Get Transaction Data
    def get_transaction(self) -> dict | None:
        r = self.orch.post("/odata/Queues/UiPathODataSvc.StartTransaction", json={"transactionData": {
            "Name": self.cfg["QueueName"], "RobotIdentifier": self.name}})
        if r.status_code == 204:
            return None
        r.raise_for_status()
        return r.json()

    # ------------------------------------------------------------------ ERP calls: every transport problem is a system exception
    def erp_call(self, method: str, url: str, **kw) -> httpx.Response:
        try:
            r = self.erp.request(method, url, **kw)
        except httpx.TimeoutException as e:
            raise ApplicationException(f"ERP timeout on {method} {url}") from e
        except httpx.TransportError as e:
            raise ApplicationException(f"ERP unreachable on {method} {url}: {e}") from e
        if r.status_code >= 500:
            raise ApplicationException(f"ERP {r.status_code} on {method} {url}")
        return r

    # ------------------------------------------------------------------ Process
    def process(self, item: dict) -> dict:
        cfg = self.cfg
        path = item["SpecificContent"]["FilePath"]
        if not Path(path).exists():
            raise ApplicationException(f"Input file not found: {path}")
        self.set_progress(item, "OCR")
        ex = read_invoice(path, cfg["OcrLanguage"])
        f = ex.fields

        invalid = [k for k in ex.missing()]
        if f.get("supplier_siret") and not luhn_ok(f["supplier_siret"]):
            invalid.append("supplier_siret")
        if invalid and self.ai_fallback:
            self.set_progress(item, "AI fallback")
            try:
                ai = llm_extract(ex.ocr_text, cfg["AiFallbackModel"], cfg["OllamaURL"])
            except (httpx.HTTPError, ValueError) as e:
                raise ApplicationException(f"AI fallback unavailable: {e}") from e
            for k in set(invalid):
                if k in ai:
                    f[k] = ai[k]
                    ex.confidence[k] = None  # no OCR confidence for an LLM-read value
            ex.method = "rules+llm"
            invalid = [k for k in ex.missing()]
            if f.get("supplier_siret") and not luhn_ok(f["supplier_siret"]):
                invalid.append("supplier_siret")

        out = {"fields": f, "confidence": ex.confidence, "method": ex.method}
        self.current_output = out  # kept so a business exception still hands the reader's values to the reviewer
        if invalid:
            raise BusinessRuleException("BE_EXTRACTION_REVIEW", f"Unreadable or invalid fields: {sorted(set(invalid))}")
        low = [k for k in ("invoice_number", "invoice_date")
               if ex.confidence.get(k) is not None and ex.confidence[k] < cfg["MinFieldConfidence"]]
        if low:
            raise BusinessRuleException("BE_EXTRACTION_REVIEW", f"Low OCR confidence on {low}")

        # Arithmetic cross-check: catches both supplier errors and OCR misreads of amounts.
        ht, tva, ttc = f["total_ht"], f["tva"], f["total_ttc"]
        if abs(ht + tva - ttc) > 0.011 or abs(round(ht * cfg["VatRate"], 2) - tva) > 0.011:
            amount_confs = [ex.confidence.get(k) for k in ("total_ht", "tva", "total_ttc")]
            if any(c is not None and c < cfg["MinFieldConfidence"] for c in amount_confs):
                raise BusinessRuleException("BE_EXTRACTION_REVIEW", "Totals inconsistent and an amount was read with low confidence")
            raise BusinessRuleException("BE_TOTAL_MISMATCH", f"HT {ht} + TVA {tva} != TTC {ttc}")

        self.set_progress(item, "ERP checks")
        siret = f["supplier_siret"]
        if self.erp_call("GET", f"/suppliers/{siret}").status_code == 404:
            raise BusinessRuleException("BE_UNKNOWN_SUPPLIER", f"SIRET {siret} not in the supplier register")

        existing = self.erp_call("GET", "/invoices", params={"supplier_siret": siret,
                                                             "invoice_number": f["invoice_number"]}).json()
        mine = [e for e in existing if e.get("idempotency_key") == item["Reference"]]
        if mine:  # booked by a previous attempt whose response was lost: finish, do not book again
            self.log("Warn", "Invoice already booked by an earlier attempt of this item", erp_id=mine[0]["id"],
                     reference=item["Reference"])
            return {**out, "erp_invoice_id": mine[0]["id"], "recovered_lost_response": True}
        if existing:
            raise BusinessRuleException("BE_DUPLICATE_INVOICE",
                                        f"Invoice {f['invoice_number']} already booked on {existing[0]['booked_on']}")

        r = self.erp_call("GET", f"/purchase-orders/{f['po_number']}")
        if r.status_code == 404 or r.json()["supplier_siret"] != siret:
            raise BusinessRuleException("BE_PO_NOT_FOUND", f"No open purchase order {f['po_number']} for this supplier")
        po_amount = r.json()["amount_ht"]
        if abs(po_amount - ht) > po_amount * cfg["PoAmountTolerancePct"] / 100:
            raise BusinessRuleException("BE_PO_AMOUNT_MISMATCH", f"Invoice HT {ht} vs PO {po_amount}")

        self.set_progress(item, "Booking")
        r = self.erp_call("POST", "/invoices", json=f, headers={"Idempotency-Key": item["Reference"]})
        r.raise_for_status()
        return {**out, "erp_invoice_id": r.json()["id"], "recovered_lost_response": bool(r.json().get("replayed"))}

    def set_progress(self, item: dict, text: str) -> None:
        self.orch.post(f"/odata/QueueItems({item['Id']})/UiPathODataSvc.SetTransactionProgress", json={"progress": text})

    # ------------------------------------------------------------------ Set Transaction Status
    def set_status(self, item: dict, ok: bool, exc: Exception | None, output: dict | None) -> None:
        result: dict = {"IsSuccessful": ok, "Output": output or {}}
        if exc is not None:
            is_business = isinstance(exc, BusinessRuleException)
            result["ProcessingException"] = {
                "Type": "BusinessException" if is_business else "ApplicationException",
                "Reason": exc.code if is_business else type(exc).__name__,
                "Details": str(exc)[:1000]}
        self.orch.post(f"/odata/Queues({item['Id']})/UiPathODataSvc.SetTransactionResult",
                       json={"transactionResult": result}).raise_for_status()

    # ------------------------------------------------------------------ main loop
    def run(self) -> dict:
        t0 = time.perf_counter()
        self.init_all_applications()
        while True:
            item = self.get_transaction()
            if item is None:
                self.log("Info", "No more transactions: End Process")
                break
            ctx = {"transaction_id": item["Id"], "reference": item["Reference"], "file": item["SpecificContent"]["FileName"],
                   "retry": item["RetryNumber"]}
            self.log("Info", "Processing Transaction", **ctx)
            output = None
            self.current_output = {}
            try:
                output = self.process(item)
                self.set_status(item, True, None, output)
                self.counts["Successful"] += 1
                self.consecutive_system_exceptions = 0
                self.log("Info", "Transaction Successful", **ctx)
            except BusinessRuleException as e:
                self.set_status(item, False, e, {"reason": e.code, **self.current_output})
                self.counts["BusinessException"] += 1
                self.consecutive_system_exceptions = 0
                self.log("Warn", f"Business exception: {e}", **ctx, reason=e.code)
            except Exception as e:  # noqa: BLE001  - REFramework catches every system exception here
                self.counts["ApplicationException"] += 1
                self.consecutive_system_exceptions += 1
                self.log("Error", f"System exception: {e}", **ctx, exception=type(e).__name__)
                self.set_status(item, False, e, self.current_output)
                if self.consecutive_system_exceptions >= self.cfg["MaxConsecutiveSystemExceptions"]:
                    self.log("Fatal", "Too many consecutive system exceptions: stopping the robot")
                    break
                self.init_all_applications()  # kill and restart the applications, then carry on
        self.close_all_applications()
        summary = {"robot": self.name, **self.counts, "seconds": round(time.perf_counter() - t0, 2)}
        self.log("Info", "Robot finished", **summary)
        return summary


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default=f"{socket.gethostname()}-robot")
    ap.add_argument("--ai-fallback", action="store_true", help="ask a local LLM when the extraction rules miss fields")
    args = ap.parse_args()
    print(json.dumps(Robot(args.name, args.ai_fallback).run()))


if __name__ == "__main__":
    main()
