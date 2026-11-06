"""A fake accounts-payable ERP: the target system the robot books invoices into.

It deliberately misbehaves so the robot's system-exception handling is exercised, not assumed:
  RPA_ERP_FAILURE_RATE    share of requests answered 503 Service Unavailable
  RPA_ERP_SLOW_RATE       share of requests that take longer than the robot's client timeout
  RPA_ERP_LOST_RESPONSE   share of POST /invoices that COMMIT and then time out (the nasty case:
                          the robot believes it failed, but the invoice is booked)
Chaos uses a seeded RNG (RPA_ERP_CHAOS_SEED) so a run is reproducible.

POST /invoices honours an Idempotency-Key header: replaying the same key returns the original booking
instead of creating a second one. That is what makes an automatic retry after a lost response safe.
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import sqlite3
import threading
import time
from datetime import datetime, timezone

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from .paths import DATA, ERP_DB

SCHEMA = """
CREATE TABLE IF NOT EXISTS suppliers (siret TEXT PRIMARY KEY, name TEXT NOT NULL, address TEXT);
CREATE TABLE IF NOT EXISTS purchase_orders (po_number TEXT PRIMARY KEY, supplier_siret TEXT NOT NULL,
                                            amount_ht REAL NOT NULL);
CREATE TABLE IF NOT EXISTS invoices (
    id INTEGER PRIMARY KEY AUTOINCREMENT, supplier_siret TEXT NOT NULL, invoice_number TEXT NOT NULL,
    invoice_date TEXT, po_number TEXT, total_ht REAL, tva REAL, total_ttc REAL, booked_on TEXT NOT NULL,
    idempotency_key TEXT UNIQUE, source TEXT NOT NULL DEFAULT 'history'
);
CREATE TABLE IF NOT EXISTS chaos_log (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, kind TEXT, path TEXT);
"""


def connect() -> sqlite3.Connection:
    ERP_DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(ERP_DB, isolation_level=None, check_same_thread=False, timeout=10)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def seed(reference_path=None) -> None:
    """Load supplier master data, open POs and already-booked invoices produced by the generator."""
    ref = json.loads((reference_path or DATA / "reference.json").read_text(encoding="utf-8"))
    con = connect()
    con.executescript("DELETE FROM suppliers; DELETE FROM purchase_orders; DELETE FROM invoices; DELETE FROM chaos_log;")
    con.executemany("INSERT INTO suppliers VALUES (?,?,?)",
                    [(s["siret"], s["name"], s["address"]) for s in ref["suppliers"]])
    con.executemany("INSERT INTO purchase_orders VALUES (?,?,?)",
                    [(p["po_number"], p["supplier_siret"], p["amount_ht"]) for p in ref["open_purchase_orders"]])
    con.executemany("INSERT INTO invoices(supplier_siret, invoice_number, total_ttc, booked_on) VALUES (?,?,?,?)",
                    [(b["supplier_siret"], b["invoice_number"], b["total_ttc"], b["booked_on"])
                     for b in ref["booked_invoices"]])
    con.close()


def create_app() -> FastAPI:
    con = connect()
    lock = threading.Lock()
    rng = random.Random(int(os.environ.get("RPA_ERP_CHAOS_SEED", "42")))
    fail_rate = float(os.environ.get("RPA_ERP_FAILURE_RATE", "0"))
    slow_rate = float(os.environ.get("RPA_ERP_SLOW_RATE", "0"))
    lost_rate = float(os.environ.get("RPA_ERP_LOST_RESPONSE", "0"))
    slow_seconds = float(os.environ.get("RPA_ERP_SLOW_SECONDS", "3"))
    app = FastAPI(title="Fake AP ERP", version="1.0")

    def log_chaos(kind: str, path: str) -> None:
        con.execute("INSERT INTO chaos_log(at, kind, path) VALUES (?,?,?)",
                    (datetime.now(timezone.utc).isoformat(), kind, path))

    @app.middleware("http")
    async def chaos(request: Request, call_next):
        if request.url.path.startswith("/admin") or request.url.path == "/health":
            return await call_next(request)
        with lock:
            roll = rng.random()
        if roll < fail_rate:
            log_chaos("503", request.url.path)
            return JSONResponse({"error": "ERP temporarily unavailable"}, status_code=503)
        if roll < fail_rate + slow_rate:
            log_chaos("slow", request.url.path)
            await asyncio.sleep(slow_seconds)  # the ERP is "stuck" for this request
        return await call_next(request)

    @app.get("/suppliers/{siret}")
    def get_supplier(siret: str) -> dict:
        r = con.execute("SELECT * FROM suppliers WHERE siret=?", (siret,)).fetchone()
        if r is None:
            raise HTTPException(404, "Unknown supplier")
        return dict(r)

    @app.get("/purchase-orders/{po}")
    def get_po(po: str) -> dict:
        r = con.execute("SELECT * FROM purchase_orders WHERE po_number=?", (po,)).fetchone()
        if r is None:
            raise HTTPException(404, "Unknown purchase order")
        return dict(r)

    @app.get("/invoices")
    def find_invoices(supplier_siret: str, invoice_number: str) -> list[dict]:
        return [dict(r) for r in con.execute(
            "SELECT * FROM invoices WHERE supplier_siret=? AND invoice_number=?", (supplier_siret, invoice_number))]

    @app.post("/invoices", status_code=201)
    def book_invoice(body: dict, idempotency_key: str | None = Header(default=None)):
        with lock:
            if idempotency_key:
                prior = con.execute("SELECT * FROM invoices WHERE idempotency_key=?", (idempotency_key,)).fetchone()
                if prior is not None:
                    return JSONResponse({**dict(prior), "replayed": True}, status_code=200)
            cur = con.execute(
                "INSERT INTO invoices(supplier_siret, invoice_number, invoice_date, po_number, total_ht, tva, "
                "total_ttc, booked_on, idempotency_key, source) VALUES (?,?,?,?,?,?,?,?,?, 'robot')",
                (body["supplier_siret"], body["invoice_number"], body.get("invoice_date"), body.get("po_number"),
                 body.get("total_ht"), body.get("tva"), body.get("total_ttc"),
                 datetime.now(timezone.utc).date().isoformat(), idempotency_key))
            row = dict(con.execute("SELECT * FROM invoices WHERE id=?", (cur.lastrowid,)).fetchone())
            lost = rng.random() < lost_rate
        if lost:
            log_chaos("lost_response", "/invoices")
            time.sleep(slow_seconds)  # committed, but the caller times out before seeing it
        return row

    @app.get("/admin/stats")
    def admin_stats() -> dict:
        chaos_counts = {r[0]: r[1] for r in con.execute("SELECT kind, COUNT(*) FROM chaos_log GROUP BY kind")}
        dupes = con.execute("SELECT COUNT(*) FROM (SELECT supplier_siret, invoice_number FROM invoices "
                            "GROUP BY 1, 2 HAVING COUNT(*) > 1)").fetchone()[0]
        booked = con.execute("SELECT COUNT(*) FROM invoices WHERE source='robot'").fetchone()[0]
        return {"chaos": chaos_counts, "booked_by_robot": booked, "double_bookings": dupes}

    @app.get("/admin/invoices")
    def admin_invoices() -> list[dict]:
        return [dict(r) for r in con.execute("SELECT * FROM invoices WHERE source='robot'")]

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    return app
