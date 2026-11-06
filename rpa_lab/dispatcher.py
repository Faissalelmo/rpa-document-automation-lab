"""Dispatcher: scan the shared inbox and add one queue item per document.

The queue Reference is the SHA-256 of the file content, so the same scan dropped twice (a very common
real-world event: someone re-sends the batch) is rejected by Orchestrator with 409 instead of being
processed twice.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone

import httpx

from .config import load_config
from .paths import INBOX


def sha256(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def dispatch(inbox=INBOX) -> dict:
    cfg = load_config()
    added, duplicates = 0, []
    with httpx.Client(base_url=cfg["OrchestratorURL"], timeout=10) as orch:
        for path in sorted(p for p in inbox.iterdir() if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".tif", ".tiff")):
            ref = sha256(path)[:32]
            r = orch.post("/odata/Queues/UiPathODataSvc.AddQueueItem", json={"itemData": {
                "Name": cfg["QueueName"], "Priority": "Normal", "Reference": ref,
                "SpecificContent": {"FilePath": str(path), "FileName": path.name,
                                    "ReceivedAt": datetime.now(timezone.utc).isoformat()}}})
            if r.status_code == 409:
                duplicates.append(path.name)
                continue
            r.raise_for_status()
            added += 1
    result = {"added": added, "rejected_duplicates": duplicates}
    print(json.dumps(result, ensure_ascii=False))
    return result


if __name__ == "__main__":
    argparse.ArgumentParser(description=__doc__).parse_args()
    dispatch()
