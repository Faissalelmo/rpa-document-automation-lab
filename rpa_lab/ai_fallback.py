"""Optional AI fallback: ask a local LLM (Ollama) to read the OCR text when the extraction rules miss fields.

The LLM is only trusted for *reading*. Whatever it returns goes through exactly the same deterministic
checks as rule-extracted values (SIRET checksum, HT + TVA = TTC, supplier and PO lookups in the ERP).
A hallucinated amount therefore cannot be booked: it fails the arithmetic or the PO match and the
document goes to a person, as it would without AI.
"""
from __future__ import annotations

import json

import httpx

PROMPT = """Tu lis le texte OCR d'une facture fournisseur française. Renvoie UNIQUEMENT un objet JSON avec ces clés :
supplier_siret (14 chiffres, sans espaces), invoice_number (numéro de facture ou de pièce, tel qu'imprimé),
invoice_date (AAAA-MM-JJ), po_number (référence de commande du client, forme PO-AAAA-NNNNN),
total_ht (nombre, montant hors taxes), tva (nombre, montant de TVA), total_ttc (nombre, total toutes taxes / à payer).
Utilise null si une valeur est absente. Les montants français utilisent la virgule décimale : "1 234,50" -> 1234.5.

Texte OCR :
---
{text}
---"""

KEYS = ("supplier_siret", "invoice_number", "invoice_date", "po_number", "total_ht", "tva", "total_ttc")


def llm_extract(ocr_text: str, model: str, base_url: str, timeout: float = 120.0) -> dict:
    r = httpx.post(f"{base_url}/api/generate", timeout=timeout, json={
        "model": model, "prompt": PROMPT.format(text=ocr_text), "format": "json", "stream": False,
        "options": {"temperature": 0, "seed": 0}})
    r.raise_for_status()
    raw = json.loads(r.json()["response"])
    out: dict = {}
    for k in KEYS:
        v = raw.get(k)
        if v in (None, "", "null"):
            continue
        if k in ("total_ht", "tva", "total_ttc"):
            try:
                v = round(float(str(v).replace(" ", "").replace(",", ".")), 2)
            except ValueError:
                continue
        elif k == "supplier_siret":
            v = "".join(ch for ch in str(v) if ch.isdigit())
        else:
            v = str(v).strip()
        out[k] = v
    return out
