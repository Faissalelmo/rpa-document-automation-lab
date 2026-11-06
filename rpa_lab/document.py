"""Document understanding: OCR (Tesseract) + rule-based field extraction with per-field confidence.

Equivalent in UiPath: Digitize Document (OCR engine) -> Extract Document Data (regex / form extractor)
-> Validation Station when confidence is too low. Here the "validation station" is the business
exception BE_EXTRACTION_REVIEW, which routes the document to a person.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import date

from .paths import TESSDATA

MOIS = {m: i + 1 for i, m in enumerate(["janvier", "fevrier", "mars", "avril", "mai", "juin", "juillet", "aout",
                                         "septembre", "octobre", "novembre", "decembre"])}
REQUIRED = ("supplier_siret", "invoice_number", "invoice_date", "po_number", "total_ht", "tva", "total_ttc")


def tesseract_bin() -> str:
    for c in (os.environ.get("TESSERACT_CMD"), shutil.which("tesseract"),
              r"C:\Program Files\Tesseract-OCR\tesseract.exe"):
        if c and os.path.exists(c):
            return c
    raise FileNotFoundError("Tesseract not found: install it or set TESSERACT_CMD")


@dataclass
class OcrLine:
    words: list[tuple[str, float]]  # (word, Tesseract confidence 0-100)

    @property
    def text(self) -> str:
        return " ".join(w for w, _ in self.words)

    def conf(self, start: int, end: int) -> float:
        """Lowest confidence among the words that overlap text[start:end] - the value, not its label."""
        pos, confs = 0, []
        for w, c in self.words:
            if pos < end and pos + len(w) > start:
                confs.append(c)
            pos += len(w) + 1
        return min(confs) if confs else 0.0


@dataclass
class Extraction:
    fields: dict = field(default_factory=dict)
    confidence: dict = field(default_factory=dict)
    ocr_text: str = ""
    method: str = "rules"

    def missing(self) -> list[str]:
        return [k for k in REQUIRED if self.fields.get(k) in (None, "")]


def ocr(path: str, lang: str = "fra") -> list[OcrLine]:
    """Run Tesseract once in TSV mode and rebuild lines with a confidence per line."""
    env = dict(os.environ)
    if TESSDATA.exists():
        env["TESSDATA_PREFIX"] = str(TESSDATA)
    out = subprocess.run([tesseract_bin(), str(path), "stdout", "-l", lang, "--psm", "4", "-c", "tessedit_create_tsv=1"],
                         capture_output=True, env=env, timeout=120)
    if out.returncode != 0:
        raise RuntimeError(f"tesseract failed: {out.stderr.decode(errors='replace')[:300]}")
    lines: dict[tuple, list[tuple[str, float]]] = {}
    for row in out.stdout.decode("utf-8", errors="replace").splitlines()[1:]:
        cols = row.split("\t")
        if len(cols) < 12 or cols[0] != "5" or not cols[11].strip():
            continue
        key = (int(cols[2]), int(cols[3]), int(cols[4]))  # block, paragraph, line
        lines.setdefault(key, []).append((cols[11], float(cols[10])))
    return [OcrLine(words) for _, words in sorted(lines.items())]


# ------------------------------------------------------------------------------------------- parsing helpers

AMOUNT_END = re.compile(r"(\d{1,3}(?:[ .]?\d{3})*,\s?\d{2})\s*[€eE]?\s*$")


def parse_amount(s: str) -> tuple[float, tuple[int, int]] | None:
    m = AMOUNT_END.search(s.rstrip())
    if not m:
        return None
    return float(re.sub(r"[ .]", "", m.group(1)).replace(",", ".")), m.span(1)


def strip_accents(s: str) -> str:
    return (s.lower().replace("é", "e").replace("è", "e").replace("ê", "e").replace("û", "u")
            .replace("à", "a").replace("ô", "o"))


def parse_date(text: str) -> tuple[str, tuple[int, int]] | None:
    m = re.search(r"\b(\d{1,2})[/.\-](\d{1,2})[/.\-](20\d{2})\b", text)
    if m:
        d, mo, y = map(int, m.groups())
    else:
        m = re.search(r"\b(\d{1,2})\s+([a-zéû]+)\s+(20\d{2})\b", text, re.I)
        if not m or strip_accents(m.group(2)) not in MOIS:
            return None
        d, mo, y = int(m.group(1)), MOIS[strip_accents(m.group(2))], int(m.group(3))
    try:
        return date(y, mo, d).isoformat(), m.span()
    except ValueError:
        return None


def luhn_ok(digits: str) -> bool:
    if not digits.isdigit():
        return False
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch) * (2 if i % 2 else 1)
        total += n - 9 if n > 9 else n
    return total % 10 == 0


def extract(lines: list[OcrLine]) -> Extraction:
    ex = Extraction(ocr_text="\n".join(line.text for line in lines))

    def put(name: str, value, conf: float) -> None:
        if value not in (None, "") and name not in ex.fields:
            ex.fields[name], ex.confidence[name] = value, round(conf, 1)

    for line in lines:
        t = line.text
        low = strip_accents(t)

        m = re.search(r"siret\s*:?\s*([\d ]{14,20})", low)
        if m:
            digits = re.sub(r"\D", "", m.group(1))[:14]
            put("supplier_siret", digits if len(digits) == 14 else None, line.conf(*m.span(1)))

        m = re.search(r"(?:n[°ºo]?\s*facture|facture\s+n[°ºo]?(?=[\s:])|reference)\s*:?\s*([a-z0-9][a-z0-9\-/]{4,})", low)
        if m:  # match on the accent-free lower-case copy, keep the original characters
            put("invoice_number", t[m.start(1):m.end(1)], line.conf(*m.span(1)))

        m = re.search(r"P[O0]\s?-\s?(20\d{2})\s?-\s?(\d{5})", t, re.I)
        if m:
            put("po_number", f"PO-{m.group(1)}-{m.group(2)}", line.conf(*m.span()))

        if "date" in low or "emise le" in low:
            parsed = parse_date(t)
            if parsed:
                put("invoice_date", parsed[0], line.conf(*parsed[1]))

        # Blank out "20 %" (same length, so offsets still line up) before reading the trailing amount.
        parsed = parse_amount(re.sub(r"\(?\d{1,2}(?:[.,]\d+)?\s*%\)?", lambda m_: " " * len(m_.group()), t))
        if parsed is not None:
            amount, conf = parsed[0], line.conf(*parsed[1])
            if re.search(r"\bttc\b|net a payer", low):
                ex.fields["total_ttc"], ex.confidence["total_ttc"] = amount, round(conf, 1)
            elif re.search(r"(total|montant|sous-total)\s*ht\b", low):
                ex.fields["total_ht"], ex.confidence["total_ht"] = amount, round(conf, 1)
            elif re.match(r"^\s*(montant\s+)?tva\b", low):
                ex.fields["tva"], ex.confidence["tva"] = amount, round(conf, 1)

    return ex


def read_invoice(path: str, lang: str = "fra") -> Extraction:
    return extract(ocr(path, lang))
