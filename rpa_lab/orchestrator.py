"""A small Orchestrator: work queues with the same contract as UiPath Orchestrator queues.

Endpoint names follow the Orchestrator OData API so a robot written against this lab would talk to the
real thing with only the base URL and authentication changed:

  POST /odata/Queues/UiPathODataSvc.AddQueueItem
  POST /odata/Queues/UiPathODataSvc.StartTransaction
  POST /odata/Queues({id})/UiPathODataSvc.SetTransactionResult
  POST /odata/QueueItems({id})/UiPathODataSvc.SetTransactionProgress

Semantics reproduced (and covered by tests/test_orchestrator.py):
  * unique Reference per queue -> AddQueueItem answers 409 for a duplicate
  * StartTransaction hands each New item to exactly one robot (High > Normal > Low, then FIFO)
  * Business exception -> item Failed, never retried
  * Application exception -> item Failed + status Retried, and a clone is queued while RetryNumber < MaxRetryNumber
  * an item left InProgress longer than the timeout becomes Abandoned (robot crashed)
Plus a few read endpoints for supervision (/api/...), used by the PowerShell L2 scripts in ops/.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import Body, FastAPI, HTTPException, Response

from .config import load_config
from .paths import QUEUE_DB

PRIORITY = {"High": 3, "Normal": 2, "Low": 1}
SCHEMA = """
CREATE TABLE IF NOT EXISTS queue_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL,
    queue_name TEXT NOT NULL,
    reference TEXT,
    priority INTEGER NOT NULL DEFAULT 2,
    status TEXT NOT NULL DEFAULT 'New',
    specific_content TEXT NOT NULL,
    output TEXT,
    progress TEXT,
    processing_exception_type TEXT,
    processing_exception_reason TEXT,
    processing_exception_details TEXT,
    retry_number INTEGER NOT NULL DEFAULT 0,
    ancestor_id INTEGER,
    robot TEXT,
    creation_time TEXT NOT NULL,
    start_processing TEXT,
    end_processing TEXT
);
CREATE INDEX IF NOT EXISTS ix_status ON queue_items(queue_name, status, priority, id);
CREATE INDEX IF NOT EXISTS ix_ref ON queue_items(queue_name, reference);
"""

_lock = threading.Lock()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def connect() -> sqlite3.Connection:
    QUEUE_DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(QUEUE_DB, isolation_level=None, timeout=10, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    return con


def to_dto(r: sqlite3.Row) -> dict:
    """Shape a row like an Orchestrator QueueItemDto."""
    return {
        "Id": r["id"], "Key": r["key"], "QueueName": r["queue_name"], "Reference": r["reference"],
        "Priority": {v: k for k, v in PRIORITY.items()}[r["priority"]], "Status": r["status"],
        "SpecificContent": json.loads(r["specific_content"]),
        "Output": json.loads(r["output"]) if r["output"] else None,
        "Progress": r["progress"],
        "ProcessingExceptionType": r["processing_exception_type"],
        "ProcessingException": None if not r["processing_exception_type"] else {
            "Reason": r["processing_exception_reason"], "Details": r["processing_exception_details"],
            "Type": r["processing_exception_type"]},
        "RetryNumber": r["retry_number"], "AncestorId": r["ancestor_id"], "Robot": r["robot"],
        "CreationTime": r["creation_time"], "StartProcessing": r["start_processing"],
        "EndProcessing": r["end_processing"],
    }


def create_app() -> FastAPI:
    cfg = load_config()
    max_retry = int(cfg["QueueMaxRetryNumber"])
    in_progress_timeout = int(cfg["InProgressTimeoutSeconds"])
    con = connect()
    app = FastAPI(title="Mini Orchestrator", version="1.0")

    def reap_abandoned(queue: str) -> None:
        limit = (datetime.now(timezone.utc) - timedelta(seconds=in_progress_timeout)).isoformat()
        con.execute("UPDATE queue_items SET status='Abandoned', end_processing=? WHERE queue_name=? "
                    "AND status='InProgress' AND start_processing < ?", (now(), queue, limit))

    @app.post("/odata/Queues/UiPathODataSvc.AddQueueItem", status_code=201)
    def add_queue_item(body: dict = Body(...)) -> dict:
        item = body["itemData"]
        queue, ref = item["Name"], item.get("Reference")
        with _lock:
            if ref and con.execute("SELECT 1 FROM queue_items WHERE queue_name=? AND reference=? AND "
                                   "status NOT IN ('Deleted')", (queue, ref)).fetchone():
                raise HTTPException(409, f"Error creating Transaction. Duplicate Reference: {ref}")
            cur = con.execute(
                "INSERT INTO queue_items(key, queue_name, reference, priority, specific_content, creation_time) "
                "VALUES (?,?,?,?,?,?)",
                (str(uuid.uuid4()), queue, ref, PRIORITY[item.get("Priority", "Normal")],
                 json.dumps(item.get("SpecificContent", {}), ensure_ascii=False), now()))
            return to_dto(con.execute("SELECT * FROM queue_items WHERE id=?", (cur.lastrowid,)).fetchone())

    @app.post("/odata/Queues/UiPathODataSvc.StartTransaction")
    def start_transaction(body: dict = Body(...)):
        data = body["transactionData"]
        queue, robot = data["Name"], data.get("RobotIdentifier", "robot")
        with _lock:
            con.execute("BEGIN IMMEDIATE")
            try:
                reap_abandoned(queue)
                row = con.execute("SELECT id FROM queue_items WHERE queue_name=? AND status='New' "
                                  "ORDER BY priority DESC, id ASC LIMIT 1", (queue,)).fetchone()
                if row is None:
                    con.execute("COMMIT")
                    return Response(status_code=204)
                con.execute("UPDATE queue_items SET status='InProgress', robot=?, start_processing=? WHERE id=?",
                            (robot, now(), row["id"]))
                con.execute("COMMIT")
            except Exception:
                con.execute("ROLLBACK")
                raise
            return to_dto(con.execute("SELECT * FROM queue_items WHERE id=?", (row["id"],)).fetchone())

    @app.post("/odata/QueueItems({item_id})/UiPathODataSvc.SetTransactionProgress")
    def set_progress(item_id: int, body: dict = Body(...)) -> Response:
        con.execute("UPDATE queue_items SET progress=? WHERE id=?", (body.get("progress"), item_id))
        return Response(status_code=204)

    @app.post("/odata/Queues({item_id})/UiPathODataSvc.SetTransactionResult")
    def set_transaction_result(item_id: int, body: dict = Body(...)) -> Response:
        res = body["transactionResult"]
        with _lock:
            r = con.execute("SELECT * FROM queue_items WHERE id=?", (item_id,)).fetchone()
            if r is None:
                raise HTTPException(404, "Queue item not found")
            if r["status"] != "InProgress":
                raise HTTPException(400, f"Queue item is {r['status']}, not InProgress")
            output = json.dumps(res.get("Output") or {}, ensure_ascii=False)
            if res.get("IsSuccessful"):
                con.execute("UPDATE queue_items SET status='Successful', output=?, end_processing=? WHERE id=?",
                            (output, now(), item_id))
                return Response(status_code=204)
            exc = res.get("ProcessingException") or {}
            etype = exc.get("Type", "ApplicationException")
            retry = etype == "ApplicationException" and r["retry_number"] < max_retry
            con.execute(
                "UPDATE queue_items SET status=?, output=?, processing_exception_type=?, "
                "processing_exception_reason=?, processing_exception_details=?, end_processing=? WHERE id=?",
                ("Retried" if retry else "Failed", output, etype, exc.get("Reason"), exc.get("Details"),
                 now(), item_id))
            if retry:  # Orchestrator auto-retry: a fresh clone, same reference, RetryNumber + 1
                con.execute(
                    "INSERT INTO queue_items(key, queue_name, reference, priority, specific_content, retry_number, "
                    "ancestor_id, creation_time) VALUES (?,?,?,?,?,?,?,?)",
                    (str(uuid.uuid4()), r["queue_name"], r["reference"], r["priority"], r["specific_content"],
                     r["retry_number"] + 1, r["ancestor_id"] or r["id"], now()))
        return Response(status_code=204)

    # ------------------------------------------------------------- supervision / L2 support
    @app.get("/api/queues/{queue}/items")
    def list_items(queue: str, status: str | None = None, exception_type: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM queue_items WHERE queue_name=?", [queue]
        if status:
            sql, args = sql + " AND status=?", args + [status]
        if exception_type:
            sql, args = sql + " AND processing_exception_type=?", args + [exception_type]
        return [to_dto(r) for r in con.execute(sql + " ORDER BY id", args)]

    @app.get("/api/queues/{queue}/stats")
    def stats(queue: str) -> dict:
        by_status = {r[0]: r[1] for r in con.execute(
            "SELECT status, COUNT(*) FROM queue_items WHERE queue_name=? GROUP BY status", (queue,))}
        reasons = {r[0]: r[1] for r in con.execute(
            "SELECT processing_exception_reason, COUNT(*) FROM queue_items WHERE queue_name=? AND status='Failed' "
            "AND processing_exception_type='BusinessException' GROUP BY 1", (queue,))}
        avg = con.execute(
            "SELECT AVG((julianday(end_processing) - julianday(start_processing)) * 86400) FROM queue_items "
            "WHERE queue_name=? AND end_processing IS NOT NULL AND start_processing IS NOT NULL", (queue,)).fetchone()[0]
        return {"queue": queue, "by_status": by_status, "business_exceptions_by_reason": reasons,
                "avg_processing_seconds": round(avg, 3) if avg else None}

    @app.post("/api/queues/{queue}/items/{item_id}/retry", status_code=201)
    def manual_retry(queue: str, item_id: int) -> dict:
        """What an L2 engineer does in Orchestrator after fixing the root cause: Retry a Failed item."""
        with _lock:
            r = con.execute("SELECT * FROM queue_items WHERE id=? AND queue_name=?", (item_id, queue)).fetchone()
            if r is None:
                raise HTTPException(404, "Queue item not found")
            if r["status"] not in ("Failed", "Abandoned"):
                raise HTTPException(400, f"Only Failed or Abandoned items can be retried (item is {r['status']})")
            con.execute("UPDATE queue_items SET status='Retried' WHERE id=?", (item_id,))
            cur = con.execute(
                "INSERT INTO queue_items(key, queue_name, reference, priority, specific_content, retry_number, "
                "ancestor_id, creation_time) VALUES (?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), queue, r["reference"], r["priority"], r["specific_content"],
                 r["retry_number"] + 1, r["ancestor_id"] or r["id"], now()))
            return to_dto(con.execute("SELECT * FROM queue_items WHERE id=?", (cur.lastrowid,)).fetchone())

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    return app

