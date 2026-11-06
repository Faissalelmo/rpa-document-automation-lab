import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("RPA_DATA_DIR", ROOT / "data"))  # runtime state, git-ignored
INBOX = DATA / "inbox"          # where scanned invoices land (the "shared folder")
DONE = DATA / "archive"
GROUND_TRUTH = DATA / "ground_truth.json"
QUEUE_DB = DATA / "orchestrator.db"
ERP_DB = DATA / "erp.db"
REPORTS = ROOT / "reports"
CONFIG = ROOT / "config" / "settings.json"
TESSDATA = ROOT / ".tessdata"
