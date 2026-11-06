"""Generate a deterministic batch of fictional French supplier invoices.

Outputs (all under data/):
  inbox/*.png|*.jpg     the documents the robot will read (clean PDFs-as-images and degraded scans)
  ground_truth.json     what is really printed on each document + the outcome a correct robot must reach
  reference.json        the ERP master data: supplier register, open purchase orders, already-booked invoices

Every supplier, SIRET, PO and amount is invented. The defects are injected on purpose so that each
business rule of the robot is exercised; the expected outcome of every document is recorded before
the robot ever sees it.
"""
from __future__ import annotations

import argparse
import json
import random
import shutil
import sys
from datetime import date, timedelta
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from rpa_lab.paths import DATA, GROUND_TRUTH, INBOX  # noqa: E402

W, H = 1240, 1754  # A4 at 150 dpi

MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
        "septembre", "octobre", "novembre", "décembre"]

PRODUCTS = [
    ("Ramettes papier A4 80 g", 4.90), ("Cartouche toner noir", 89.00), ("Licence logicielle annuelle", 420.00),
    ("Maintenance imprimante", 150.00), ("Câble réseau Cat6 5 m", 7.50), ("Écran 24 pouces", 189.00),
    ("Prestation de nettoyage", 310.00), ("Location benne 10 m3", 265.00), ("Transport palette", 95.00),
    ("Heure de conseil", 110.00), ("Casque audio USB", 39.90), ("Fournitures de bureau (lot)", 58.30),
]


def luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def make_siret(rng: random.Random) -> str:
    while True:
        s = "".join(str(rng.randint(0, 9)) for _ in range(14))
        if s[0] != "0" and luhn_ok(s):
            return s


def fmt_eur(x: float) -> str:
    """1234.5 -> '1 234,50' (French format, narrow spacing rendered as a normal space)."""
    s = f"{x:,.2f}"
    return s.replace(",", " ").replace(".", ",")


def fmt_siret(s: str) -> str:
    return f"{s[:3]} {s[3:6]} {s[6:9]} {s[9:]}"


def find_font(bold: bool = False) -> str:
    candidates = (
        ["C:/Windows/Fonts/arialbd.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]
        if bold else
        ["C:/Windows/Fonts/arial.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]
    )
    for c in candidates:
        if Path(c).exists():
            return c
    raise SystemExit("No TrueType font found (install fonts-dejavu-core).")


FONT = find_font()
FONT_B = find_font(bold=True)


def f(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(FONT_B if bold else FONT, size)


# --------------------------------------------------------------------------- layouts
# Three supplier layouts with different labels, positions and date formats, so the extractor
# has to rely on patterns rather than on fixed coordinates.

def layout_a(d: ImageDraw.ImageDraw, inv: dict) -> None:
    d.text((80, 70), inv["supplier_name"], font=f(40, True), fill="black")
    d.text((80, 125), inv["supplier_address"], font=f(22), fill="black")
    d.text((80, 158), f"SIRET : {fmt_siret(inv['supplier_siret'])}", font=f(22), fill="black")
    d.text((820, 70), "FACTURE", font=f(48, True), fill="black")
    d.text((780, 150), f"N° facture : {inv['invoice_number']}", font=f(24), fill="black")
    dd = date.fromisoformat(inv["invoice_date"])
    d.text((780, 185), f"Date : {dd.strftime('%d/%m/%Y')}", font=f(24), fill="black")
    d.text((780, 220), f"Bon de commande : {inv['po_number']}", font=f(24), fill="black")
    d.text((80, 300), "Facturé à : Société Cliente Démo SAS, 12 rue des Lilas, 69003 Lyon", font=f(22), fill="black")
    table(d, inv, top=380)
    totals(d, inv, x_label=760, x_val=1160, top=380 + 60 + 50 * len(inv["lines"]) + 40,
           labels=("Total HT", "TVA 20 %", "Total TTC"))


def layout_b(d: ImageDraw.ImageDraw, inv: dict) -> None:
    d.rectangle((0, 0, W, 190), fill=(225, 232, 240))
    d.text((80, 50), inv["supplier_name"].upper(), font=f(36, True), fill=(20, 40, 80))
    d.text((80, 105), inv["supplier_address"], font=f(21), fill="black")
    d.text((80, 140), f"N° SIRET {inv['supplier_siret']}", font=f(21), fill="black")
    dd = date.fromisoformat(inv["invoice_date"])
    d.text((80, 240), f"Facture n° {inv['invoice_number']}", font=f(30, True), fill="black")
    d.text((80, 290), f"Émise le {dd.day} {MOIS[dd.month - 1]} {dd.year}", font=f(23), fill="black")
    d.text((80, 325), f"Votre commande : {inv['po_number']}", font=f(23), fill="black")
    d.text((700, 240), "Client :", font=f(22, True), fill="black")
    d.text((700, 275), "Société Cliente Démo SAS", font=f(22), fill="black")
    d.text((700, 305), "12 rue des Lilas, 69003 Lyon", font=f(22), fill="black")
    table(d, inv, top=420)
    totals(d, inv, x_label=720, x_val=1160, top=420 + 60 + 50 * len(inv["lines"]) + 40,
           labels=("Montant HT", "Montant TVA (20%)", "Net à payer TTC"))


def layout_c(d: ImageDraw.ImageDraw, inv: dict) -> None:
    d.text((80, 70), "Facture", font=f(44, True), fill="black")
    d.text((80, 140), f"Référence : {inv['invoice_number']}", font=f(23), fill="black")
    dd = date.fromisoformat(inv["invoice_date"])
    d.text((80, 175), f"Date de facturation : {dd.strftime('%d.%m.%Y')}", font=f(23), fill="black")
    d.text((80, 210), f"Commande client : {inv['po_number']}", font=f(23), fill="black")
    d.text((720, 70), inv["supplier_name"], font=f(30, True), fill="black")
    d.text((720, 115), inv["supplier_address"], font=f(19), fill="black")
    d.text((720, 145), f"SIRET: {fmt_siret(inv['supplier_siret'])}", font=f(21), fill="black")
    table(d, inv, top=320)
    top = 320 + 60 + 50 * len(inv["lines"]) + 40
    totals(d, inv, x_label=740, x_val=1160, top=top, labels=("Sous-total HT", "TVA", "TOTAL TTC"))
    d.text((80, H - 160), "Paiement à 30 jours. Pénalités de retard : 3 fois le taux d'intérêt légal.",
           font=f(18), fill="black")


def table(d: ImageDraw.ImageDraw, inv: dict, top: int) -> None:
    d.line((80, top, 1160, top), fill="black", width=2)
    for x, label in ((90, "Désignation"), (690, "Qté"), (820, "Prix unit. HT"), (1020, "Montant HT")):
        d.text((x, top + 12), label, font=f(21, True), fill="black")
    d.line((80, top + 50, 1160, top + 50), fill="black", width=1)
    y = top + 65
    for desc, qty, pu in inv["lines"]:
        d.text((90, y), desc, font=f(21), fill="black")
        d.text((700, y), str(qty), font=f(21), fill="black")
        d.text((830, y), fmt_eur(pu), font=f(21), fill="black")
        amt = fmt_eur(round(qty * pu, 2))
        d.text((1160 - d.textlength(amt, font=f(21)), y), amt, font=f(21), fill="black")
        y += 50
    d.line((80, y, 1160, y), fill="black", width=1)


def totals(d, inv, x_label, x_val, top, labels) -> None:
    vals = (inv["printed"]["total_ht"], inv["printed"]["tva"], inv["printed"]["total_ttc"])
    for i, (lab, v) in enumerate(zip(labels, vals)):
        bold = i == 2
        txt = f"{fmt_eur(v)} €"
        d.text((x_label, top + i * 45), lab, font=f(24, bold), fill="black")
        d.text((x_val - d.textlength(txt, font=f(24, bold)), top + i * 45), txt, font=f(24, bold), fill="black")


def layout_d(d: ImageDraw.ImageDraw, inv: dict) -> None:
    """A supplier template the extraction rules were NOT written for (different vocabulary everywhere).
    It stands for the new supplier whose invoices arrive one morning in a format nobody has seen yet."""
    d.text((80, 60), inv["supplier_name"], font=f(34, True), fill="black")
    d.text((80, 110), inv["supplier_address"], font=f(20), fill="black")
    d.text((80, 140), f"Siret {inv['supplier_siret']}  -  TVA intracom. FR{inv['supplier_siret'][:11]}", font=f(20),
           fill="black")
    dd = date.fromisoformat(inv["invoice_date"])
    d.text((80, 230), "NOTE D'HONORAIRES / FACTURATION", font=f(30, True), fill="black")
    d.text((80, 285), f"Pièce comptable : {inv['invoice_number']}", font=f(23), fill="black")
    d.text((80, 320), f"En date du {dd.day} {MOIS[dd.month - 1]} {dd.year}", font=f(23), fill="black")
    d.text((80, 355), f"Réf. achat client : {inv['po_number']}", font=f(23), fill="black")
    table(d, inv, top=430)
    totals(d, inv, x_label=720, x_val=1160, top=430 + 60 + 50 * len(inv["lines"]) + 40,
           labels=("Base hors taxes", "T.V.A. à 20,0", "Total à régler"))


LAYOUTS = {"A": layout_a, "B": layout_b, "C": layout_c, "D": layout_d}


def render(inv: dict) -> Image.Image:
    img = Image.new("RGB", (W, H), "white")
    LAYOUTS[inv["layout"]](ImageDraw.Draw(img), inv)
    return img


def degrade(img: Image.Image, rng: random.Random, strength: str) -> Image.Image:
    """Make a document look like an office-scanner capture: skew, blur, grain, JPEG artefacts."""
    angle = rng.uniform(-1.5, 1.5) if strength == "scan" else rng.uniform(-3.0, 3.0)
    img = img.convert("L").rotate(angle, resample=Image.BICUBIC, expand=True, fillcolor=255)
    img = img.filter(ImageFilter.GaussianBlur(rng.uniform(0.4, 0.8) if strength == "scan" else 1.1))
    noise = Image.effect_noise(img.size, 18 if strength == "scan" else 40)
    img = Image.blend(img, noise, 0.06 if strength == "scan" else 0.12)
    if strength == "hard":
        img = img.resize((int(img.width * 0.72), int(img.height * 0.72)), Image.BILINEAR)
    return img


# --------------------------------------------------------------------------- batch

def build(n: int, seed: int) -> tuple[list[dict], dict]:
    rng = random.Random(seed)
    names = ["Bureautique Rhône SARL", "Atelier Numérique Dupuis", "Nettoyage Pro Confluence",
             "Transports Vallée Verte", "Conseil & Données SAS", "Imprimerie du Parc",
             "Matériaux Saône Distribution"]
    cities = ["14 avenue Jean Jaurès, 69007 Lyon", "3 rue de la Gare, 38000 Grenoble",
              "88 cours Lafayette, 69003 Lyon", "Zone Artisanale Nord, 01000 Bourg-en-Bresse",
              "27 quai Saint-Antoine, 69002 Lyon", "5 place Bellecour, 69002 Lyon",
              "112 route de Chalon, 71100 Mâcon"]
    suppliers = [{"siret": make_siret(rng), "name": n_, "address": a, "layout": "ABCABCD"[i]}
                 for i, (n_, a) in enumerate(zip(names, cities))]
    unknown = {"siret": make_siret(rng), "name": "Fournitures Express Nouveau", "address":
               "1 impasse des Tilleuls, 42000 Saint-Étienne", "layout": "B"}

    # Outcome plan: mostly clean invoices, plus every business-rule violation the robot must catch.
    plan = (["OK"] * int(n * 0.6)
            + ["BE_TOTAL_MISMATCH"] * int(n * 0.08)
            + ["BE_UNKNOWN_SUPPLIER"] * int(n * 0.08)
            + ["BE_PO_NOT_FOUND"] * int(n * 0.08)
            + ["BE_PO_AMOUNT_MISMATCH"] * int(n * 0.08)
            + ["BE_DUPLICATE_INVOICE"] * int(n * 0.08))
    plan += ["OK"] * (n - len(plan))
    rng.shuffle(plan)

    open_pos, booked = [], []
    docs = []
    base = date(2026, 9, 1)
    for i, outcome in enumerate(plan):
        sup = unknown if outcome == "BE_UNKNOWN_SUPPLIER" else rng.choice(suppliers)
        lines = []
        for _ in range(rng.randint(1, 4)):
            desc, pu = rng.choice(PRODUCTS)
            lines.append((desc, rng.randint(1, 12), pu))
        ht = round(sum(q * p for _, q, p in lines), 2)
        tva = round(ht * 0.20, 2)
        ttc = round(ht + tva, 2)
        inv_no = f"F{rng.randint(2026000, 2026999)}-{i:03d}"
        po = f"PO-2026-{rng.randint(10000, 99999)}"
        printed = {"total_ht": ht, "tva": tva, "total_ttc": ttc}

        if outcome == "BE_TOTAL_MISMATCH":
            printed["total_ttc"] = round(ttc + rng.choice([-1, 1]) * rng.choice([10, 25.5, 100, 0.9]), 2)
        if outcome != "BE_PO_NOT_FOUND":
            po_amount = ht if outcome != "BE_PO_AMOUNT_MISMATCH" else round(ht * rng.uniform(0.7, 0.9), 2)
            open_pos.append({"po_number": po, "supplier_siret": sup["siret"], "amount_ht": po_amount})
        if outcome == "BE_DUPLICATE_INVOICE":
            booked.append({"supplier_siret": sup["siret"], "invoice_number": inv_no, "total_ttc": ttc,
                           "booked_on": "2026-09-30"})

        variant = rng.choices(["digital", "scan", "hard"], weights=[45, 45, 10])[0]
        docs.append({
            "file": f"facture_{i:03d}." + ("png" if variant == "digital" else "jpg"),
            "variant": variant,
            "layout": sup["layout"],
            "expected_outcome": outcome,
            "fields": {
                "supplier_siret": sup["siret"], "invoice_number": inv_no,
                "invoice_date": (base + timedelta(days=rng.randint(0, 45))).isoformat(),
                "po_number": po, **printed,
            },
            "_render": {"supplier_name": sup["name"], "supplier_address": sup["address"], "lines": lines},
        })

    reference = {"suppliers": suppliers, "open_purchase_orders": open_pos, "booked_invoices": booked}
    return docs, reference


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=100)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    if DATA.exists():
        shutil.rmtree(DATA)
    INBOX.mkdir(parents=True)
    docs, reference = build(args.n, args.seed)
    rng = random.Random(args.seed + 1)
    for doc in docs:
        r = doc.pop("_render")
        inv = {**doc["fields"], **r, "layout": doc["layout"],
               "printed": {k: doc["fields"][k] for k in ("total_ht", "tva", "total_ttc")}}
        img = render(inv)
        if doc["variant"] == "digital":
            img.save(INBOX / doc["file"])
        else:
            degrade(img, rng, doc["variant"]).save(INBOX / doc["file"], quality=rng.randint(45, 70)
                                                   if doc["variant"] == "scan" else 35)
    # The same scans dropped a second time (a re-sent batch): byte-identical, different file name.
    resent = []
    for doc in rng.sample(docs, 4):
        src = INBOX / doc["file"]
        dst = src.with_name(src.stem + "_renvoi" + src.suffix)
        shutil.copyfile(src, dst)
        resent.append({"copy": dst.name, "original": doc["file"]})
    (DATA / "resent.json").write_text(json.dumps(resent, indent=2), encoding="utf-8")
    GROUND_TRUTH.write_text(json.dumps(docs, indent=2, ensure_ascii=False), encoding="utf-8")
    (DATA / "reference.json").write_text(json.dumps(reference, indent=2, ensure_ascii=False), encoding="utf-8")
    counts: dict[str, int] = {}
    for d_ in docs:
        counts[d_["expected_outcome"]] = counts.get(d_["expected_outcome"], 0) + 1
    print(f"generated {len(docs)} invoices in {INBOX}")
    print(json.dumps(counts, indent=2))


if __name__ == "__main__":
    main()
