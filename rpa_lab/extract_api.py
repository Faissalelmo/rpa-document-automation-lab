"""Extraction service for the UiPath Performer: the same Tesseract reader, behind HTTP.

The UiPath robot (uipath/Solution/InvoicePerformer) calls POST /extract with the file path of the
queue item and gets the fields, a per-field OCR confidence, and the SIRET checksum result. Every
business rule (missing fields, low confidence, totals, supplier, PO, duplicates) is applied in the
UiPath workflow, not here: this service only reads the document, like a Document Understanding step.

Run:  python -m uvicorn --factory rpa_lab.extract_api:create_app --port 8092
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from .config import load_config
from .document import luhn_ok, read_invoice


class ExtractRequest(BaseModel):
    path: str


def create_app() -> FastAPI:
    cfg = load_config()
    app = FastAPI(title="Invoice extraction service")

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.post("/extract")
    def extract(req: ExtractRequest) -> dict:
        if not Path(req.path).is_file():
            raise HTTPException(404, f"file not found: {req.path}")
        ex = read_invoice(req.path, cfg["OcrLanguage"])
        siret = ex.fields.get("supplier_siret")
        return {
            "fields": ex.fields,
            # null = no confidence available for that field; the robot treats it as "not low"
            "confidence": {k: ex.confidence.get(k) for k in ex.fields},
            "missing": ex.missing(),
            "siret_checksum_ok": bool(siret) and luhn_ok(siret),
            "method": ex.method,
        }

    return app
