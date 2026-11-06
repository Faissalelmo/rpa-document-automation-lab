"""Business-facing exception report (Excel): what the accounts-payable team must handle by hand this morning.

In a UiPath deployment this is the file the End state attaches to the run-report e-mail.
"""
from __future__ import annotations

from datetime import date

import httpx
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from .paths import DATA

LIBELLES = {
    "BE_EXTRACTION_REVIEW": ("Lecture à vérifier", "Un champ obligatoire est illisible ou incertain : vérifier la facture et saisir les valeurs."),
    "BE_TOTAL_MISMATCH": ("Totaux incohérents", "HT + TVA ≠ TTC sur la facture : contacter le fournisseur pour un avoir ou une facture rectificative."),
    "BE_UNKNOWN_SUPPLIER": ("Fournisseur inconnu", "SIRET absent du référentiel : faire créer le fournisseur (contrôle KYC) avant paiement."),
    "BE_DUPLICATE_INVOICE": ("Doublon", "Facture déjà comptabilisée : ne pas payer, classer sans suite."),
    "BE_PO_NOT_FOUND": ("Commande introuvable", "Aucun bon de commande ouvert pour ce fournisseur : vérifier avec l'acheteur."),
    "BE_PO_AMOUNT_MISMATCH": ("Écart avec la commande", "Le montant HT ne correspond pas au bon de commande : validation de l'acheteur requise."),
}


def build(cfg: dict, path=None) -> str:
    path = path or DATA / "out" / f"rapport_exceptions_{date.today():%Y%m%d}.xlsx"
    path.parent.mkdir(parents=True, exist_ok=True)
    items = httpx.get(f"{cfg['OrchestratorURL']}/api/queues/{cfg['QueueName']}/items", params={"status": "Failed"},
                      timeout=30).json()
    wb = Workbook()
    ws = wb.active
    ws.title = "À traiter"
    head = ["Fichier", "Motif", "Action attendue", "SIRET", "N° facture", "Bon de commande", "Total HT", "TVA",
            "Total TTC", "Détail technique", "Tentatives"]
    ws.append(head)
    for c in ws[1]:
        c.font, c.fill = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor="1F4E78")
    counts: dict[str, int] = {}
    for it in items:
        exc = it["ProcessingException"] or {}
        code = exc.get("Reason") if it["ProcessingExceptionType"] == "BusinessException" else "SYSTEME"
        label, action = LIBELLES.get(code, ("Erreur système", "Escalader au support RPA N2 (voir runbook)."))
        counts[label] = counts.get(label, 0) + 1
        f = (it.get("Output") or {}).get("fields") or {}
        ws.append([it["SpecificContent"]["FileName"], label, action, f.get("supplier_siret"), f.get("invoice_number"),
                   f.get("po_number"), f.get("total_ht"), f.get("tva"), f.get("total_ttc"), exc.get("Details"),
                   it["RetryNumber"] + 1])
    widths = [20, 22, 70, 16, 16, 16, 11, 10, 11, 60, 10]
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    s = wb.create_sheet("Synthèse")
    s.append(["Motif", "Nombre"])
    for k, v in sorted(counts.items(), key=lambda kv: -kv[1]):
        s.append([k, v])
    s.column_dimensions["A"].width = 28
    wb.save(path)
    return str(path)
