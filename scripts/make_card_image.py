"""Render docs/overview.png: one real scan from the test set with the fields the robot actually read,
next to the measured run results. Nothing in the image is typed by hand: boxes come from Tesseract,
values from the extractor, numbers from reports/*.json.

Usage: python scripts/make_card_image.py [facture_XXX.jpg] [--uipath | --du]
       --uipath  label it as the UiPath REFramework robot and take the numbers from reports/uipath_performer.json
                 (written to docs/overview_uipath.png)
       --du      the UiPath robot with Document Understanding: values are the ones DU returned for this scan
                 and the numbers come from reports/uipath_du_performer.json (written to docs/overview_uipath_du.png).
                 Tesseract is only used to find which printed lines to highlight.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from rpa_lab.document import read_invoice, tesseract_bin  # noqa: E402
from rpa_lab.paths import INBOX, TESSDATA  # noqa: E402

W, H = 1200, 680
BG, PANEL, INK, MUTED, BLUE, GREEN, AMBER = "#0b0f17", "#121826", "#e6edf7", "#8b98ad", "#3b82f6", "#22c55e", "#f59e0b"
FONT = "C:/Windows/Fonts/segoeui.ttf" if Path("C:/Windows/Fonts/segoeui.ttf").exists() else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_B = "C:/Windows/Fonts/segoeuib.ttf" if Path("C:/Windows/Fonts/segoeuib.ttf").exists() else "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
MONO = "C:/Windows/Fonts/consola.ttf" if Path("C:/Windows/Fonts/consola.ttf").exists() else "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
f = lambda s, b=False: ImageFont.truetype(FONT_B if b else FONT, s)  # noqa: E731
mono = lambda s: ImageFont.truetype(MONO, s)  # noqa: E731


def line_boxes(path: Path) -> list[tuple[str, tuple[int, int, int, int]]]:
    env = {**os.environ, "TESSDATA_PREFIX": str(TESSDATA)}
    out = subprocess.run([tesseract_bin(), str(path), "stdout", "-l", "fra", "--psm", "4", "-c", "tessedit_create_tsv=1"],
                         capture_output=True, env=env).stdout.decode("utf-8", "replace")
    lines: dict = {}
    for row in out.splitlines()[1:]:
        c = row.split("\t")
        if len(c) < 12 or c[0] != "5" or not c[11].strip():
            continue
        x, y, w, h = map(int, c[6:10])
        key = tuple(map(int, c[2:5]))
        words = lines.setdefault(key, [])
        words.append((c[11], (x, y, x + w, y + h)))
    return [(" ".join(w for w, _ in ws), (min(b[0] for _, b in ws), min(b[1] for _, b in ws),
                                           max(b[2] for _, b in ws), max(b[3] for _, b in ws))) for ws in lines.values()]


def main() -> None:
    du = "--du" in sys.argv
    uipath = "--uipath" in sys.argv or du
    args = [a for a in sys.argv[1:] if a not in ("--uipath", "--du")]
    name = args[0] if args else "facture_053.jpg"
    path = INBOX / name
    ex = read_invoice(str(path))
    if du:
        du_report = json.loads((ROOT / "reports" / "uipath_du_performer.json").read_text(encoding="utf-8"))
        doc = next(d for d in du_report["per_document"] if d["file"] == name)
        ex.fields, ex.confidence = {k: v for k, v in doc["extracted"].items() if v is not None}, {}
    report = json.loads((ROOT / "reports" / "ai_fallback.json").read_text(encoding="utf-8"))
    base = json.loads((ROOT / "reports" / "baseline.json").read_text(encoding="utf-8"))

    canvas = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(canvas)
    # window chrome
    d.rounded_rectangle((16, 16, W - 16, H - 16), 14, fill=PANEL)
    for i, col in enumerate(("#ef4444", "#f59e0b", "#22c55e")):
        d.ellipse((36 + i * 22, 34, 48 + i * 22, 46), fill=col)
    title = ("UiPath REFramework · Document Understanding · Action Center · queue InvoicesDU" if du
             else "UiPath Studio · REFramework Performer · Orchestrator queue Invoices" if uipath
             else "ROBOT-01 · REFramework · queue FACTURES_FOURNISSEURS")
    d.text((110, 30), title, font=mono(15), fill=MUTED)

    # left: the real scan, with the lines the robot read highlighted
    scan = Image.open(path).convert("RGB")
    crop_h = int(scan.width * 0.86)
    scan = scan.crop((0, 0, scan.width, crop_h))
    scale = 590 / crop_h
    boxes = line_boxes(path)
    hl = ImageDraw.Draw(scan, "RGBA")
    values = [str(v) for v in ex.fields.values()]
    needles = [v.replace(".", ",") if isinstance(v, float) else v for v in values]
    for text, (x0, y0, x1, y1) in boxes:
        flat = text.replace(" ", "")
        low = text.lower()
        is_header = "désignation" in low or "prix unit" in low
        if not is_header and (any(n.replace(" ", "") in flat for n in needles if n)
                              or any(k in low for k in ("siret", "émise", "date", "total", "montant", "tva", "ttc"))):
            hl.rectangle((x0 - 6, y0 - 4, x1 + 6, y1 + 4), outline=(59, 130, 246, 255), width=4, fill=(59, 130, 246, 40))
    scan = scan.resize((int(scan.width * scale), 590), Image.LANCZOS)
    canvas.paste(scan, (40, 66))

    # right: extracted fields + measured results
    x = 40 + scan.width + 36
    d.text((x, 70), "Read by DU · checked · booked" if du else "Extracted · checked · booked",
           font=f(26, True), fill=INK)
    labels = {"supplier_siret": "SIRET", "invoice_number": "Invoice n°", "invoice_date": "Date", "po_number": "PO",
              "total_ht": "Total HT", "tva": "TVA", "total_ttc": "Total TTC"}
    y = 118
    for k, lab in labels.items():
        v = ex.fields.get(k, "—")
        conf = ex.confidence.get(k)
        d.text((x, y), lab, font=f(17), fill=MUTED)
        d.text((x + 120, y), str(v), font=mono(18), fill=INK)
        if conf is not None:
            d.text((x + 360, y), f"{conf:.0f}%", font=mono(15), fill=GREEN if conf >= 50 else AMBER)
        y += 30
    y += 18
    d.line((x, y, W - 48, y), fill="#26324a", width=1)
    y += 18
    st, bst = report["straight_through"], base["straight_through"]
    rows = [
        (f"{report['business_exceptions']['wrongly_booked']}", "invalid invoices booked", GREEN),
        (f"{report['business_exceptions']['stopped_before_booking']}/{report['business_exceptions']['injected']}",
         "injected errors stopped", GREEN),
        (f"{bst['rate_pct']:.0f}→{st['rate_pct']:.0f}%", "no-touch rate, rules → +LLM", BLUE),
        (f"{report['erp']['double_bookings']}", "double bookings, ERP chaos on", GREEN),
    ]
    if du:
        report = du_report
        be, st, se = report["business_exceptions"], report["straight_through"], report["system_exceptions"]
        rows = [
            (f"{be['wrongly_booked']}", "invalid invoices booked", GREEN),
            (f"{be['stopped_before_booking']}/{be['injected']}", "injected errors stopped", GREEN),
            (f"{st['rate_pct']:.0f}%", f"no-touch ({st['booked']}/{st['expected']} valid)", BLUE),
            (f"{report['action_center']['validation_tasks_created']}", "sent to Action Center for review", AMBER),
            (f"{report['erp']['double_bookings']}", "double bookings, ERP chaos on", GREEN),
        ]
    elif uipath:
        report = json.loads((ROOT / "reports" / "uipath_performer.json").read_text(encoding="utf-8"))
        be, st, se = report["business_exceptions"], report["straight_through"], report["system_exceptions"]
        rows = [
            (f"{be['wrongly_booked']}", "invalid invoices booked", GREEN),
            (f"{be['stopped_before_booking']}/{be['injected']}", "injected errors stopped", GREEN),
            (f"{st['rate_pct']:.0f}%", f"no-touch ({st['booked']}/{st['expected']} valid)", BLUE),
            (f"{se['recovered_by_auto_retry']}/{se['documents_affected']}", "ERP failures recovered by retry", BLUE),
            (f"{report['erp']['double_bookings']}", "double bookings, ERP chaos on", GREEN),
        ]
    for big, small, col in rows:
        d.text((x, y), big, font=f(28, True), fill=col)
        d.text((x + 168, y + 9), small, font=f(16), fill=MUTED)
        y += 46
    d.text((x, H - 58), f"Measured on {report['documents']} invoices vs ground truth", font=f(14), fill=MUTED)

    out = ROOT / "docs" / ("overview_uipath_du.png" if du else "overview_uipath.png" if uipath else "overview.png")
    canvas.save(out)
    print(out, ex.fields)


if __name__ == "__main__":
    main()
