import json

import pytest
from fastapi.testclient import TestClient

from rpa_lab import erp
from rpa_lab.document import OcrLine, extract, luhn_ok, parse_amount, parse_date


@pytest.fixture()
def erp_client(tmp_path, monkeypatch):
    monkeypatch.setattr(erp, "ERP_DB", tmp_path / "erp.db")
    ref = tmp_path / "reference.json"
    ref.write_text(json.dumps({"suppliers": [{"siret": "73282932000074", "name": "S", "address": "A"}],
                               "open_purchase_orders": [], "booked_invoices": []}), encoding="utf-8")
    erp.seed(ref)
    return TestClient(erp.create_app())


INVOICE = {"supplier_siret": "73282932000074", "invoice_number": "F1", "total_ht": 10.0, "tva": 2.0, "total_ttc": 12.0}


def test_idempotency_key_prevents_double_booking(erp_client):
    first = erp_client.post("/invoices", json=INVOICE, headers={"Idempotency-Key": "ref-1"})
    again = erp_client.post("/invoices", json=INVOICE, headers={"Idempotency-Key": "ref-1"})
    assert first.status_code == 201 and again.status_code == 200
    assert again.json()["id"] == first.json()["id"] and again.json()["replayed"] is True
    assert erp_client.get("/admin/stats").json()["double_bookings"] == 0


def test_without_key_a_replay_double_books(erp_client):
    erp_client.post("/invoices", json=INVOICE)
    erp_client.post("/invoices", json=INVOICE)
    assert erp_client.get("/admin/stats").json()["double_bookings"] == 1


def test_unknown_supplier_is_404(erp_client):
    assert erp_client.get("/suppliers/73282932000074").status_code == 200
    assert erp_client.get("/suppliers/00000000000000").status_code == 404


@pytest.mark.parametrize("text,expected", [
    ("Total TTC 1 864,68 €", 1864.68), ("Total HT 1553,90€", 1553.90), ("Montant HT 1.060,00", 1060.0),
    ("Qté Prix unit. HT Montant HT", None)])
def test_parse_amount(text, expected):
    got = parse_amount(text)
    assert (got[0] if got else None) == expected


@pytest.mark.parametrize("text,expected", [
    ("Date : 11/10/2026", "2026-10-11"), ("Date de facturation : 01.09.2026", "2026-09-01"),
    ("Émise le 2 octobre 2026", "2026-10-02"), ("Date : 31/02/2026", None)])
def test_parse_date(text, expected):
    got = parse_date(text)
    assert (got[0] if got else None) == expected


def test_siret_checksum():
    assert luhn_ok("73282932000074")
    assert not luhn_ok("73282932000075")


def line(text, conf=95.0, low_word=None):
    return OcrLine([(w, 10.0 if w == low_word else conf) for w in text.split()])


def test_extract_fields_and_value_level_confidence():
    ex = extract([
        line("SIRET : 732 829 320 00074"),
        line("Facture n° F2026-001"),
        line("Émise le 2 octobre 2026", low_word="Émise"),  # a badly read label must not lower the value's confidence
        line("Votre commande : PO-2026-12345"),
        line("Montant HT 100,00 €"), line("Montant TVA (20%) 20,00 €"), line("Net à payer TTC 120,00 €"),
    ])
    assert ex.fields == {"supplier_siret": "73282932000074", "invoice_number": "F2026-001", "invoice_date": "2026-10-02",
                         "po_number": "PO-2026-12345", "total_ht": 100.0, "tva": 20.0, "total_ttc": 120.0}
    assert ex.confidence["invoice_date"] == 95.0
    assert ex.missing() == []


def test_word_starting_with_n_is_not_an_invoice_number_label():
    assert "invoice_number" not in extract([line("Facture Nettoyage Pro Confluence")]).fields
