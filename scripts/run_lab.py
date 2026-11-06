"""Run the whole lab end to end and write reports/<name>.json.

  1. reset the Orchestrator and ERP databases, seed the ERP master data
  2. start the Orchestrator API (:8090) and the ERP API (:8091, with chaos if asked)
  3. run the Dispatcher once over data/inbox
  4. start N unattended Performer robots in parallel and wait for the queue to drain
  5. score the run against data/ground_truth.json, build the Excel exception report

Usage:  python scripts/run_lab.py --name baseline --robots 2 --chaos
        python scripts/run_lab.py --name ai_fallback --robots 2 --chaos --ai-fallback
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from rpa_lab import erp  # noqa: E402
from rpa_lab.config import load_config  # noqa: E402
from rpa_lab.dispatcher import dispatch  # noqa: E402
from rpa_lab.evaluate import evaluate  # noqa: E402
from rpa_lab.exceptions_report import build as build_report  # noqa: E402
from rpa_lab.paths import DATA, ERP_DB, QUEUE_DB, REPORTS  # noqa: E402

CHAOS = {"RPA_ERP_FAILURE_RATE": "0.06", "RPA_ERP_SLOW_RATE": "0.03", "RPA_ERP_LOST_RESPONSE": "0.08"}


def wait_healthy(url: str, proc: subprocess.Popen) -> None:
    for _ in range(100):
        if proc.poll() is not None:
            raise SystemExit(f"server for {url} exited with code {proc.returncode}")
        try:
            if httpx.get(f"{url}/health", timeout=1).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    raise SystemExit(f"{url} did not become healthy")


def powershell(script: str, *args: str) -> tuple[int, str]:
    exe = shutil.which("powershell") or shutil.which("pwsh")
    if exe is None:
        raise SystemExit("--l2-recovery needs Windows PowerShell or pwsh (the L2 tooling is PowerShell)")
    p = subprocess.run([exe, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "ops" / script), *args],
                       cwd=ROOT, capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr


def l2_recovery(cfg, servers, serve, robot_cmd, wall, disp, summaries) -> dict:
    """The morning after: the ERP incident is fixed, L2 checks the queue, retries system failures, re-runs."""
    before = evaluate(cfg, wall, disp, summaries)
    code_before, _ = powershell("Get-QueueHealth.ps1")
    servers[1].terminate()
    servers[1].wait(timeout=10)
    servers[1] = serve("erp", 8091, {k: v for k, v in os.environ.items() if k not in CHAOS})  # incident fixed
    wait_healthy(cfg["ERP_URL"], servers[1])
    code_retry, retry_out = powershell("Invoke-RetryFailedItems.ps1")
    print(retry_out.strip())
    t0 = time.perf_counter()
    r = subprocess.run(robot_cmd + ["--name", "ROBOT-L2"], cwd=ROOT, capture_output=True, text=True)
    extra = json.loads(r.stdout.strip().splitlines()[-1])
    code_after, _ = powershell("Get-QueueHealth.ps1")
    after = evaluate(cfg, wall + time.perf_counter() - t0, disp, summaries + [extra])
    return {
        "health_check_exit_code_before": code_before,
        "items_retried": sum(1 for line in retry_out.splitlines() if line.startswith("Relance :")),
        "health_check_exit_code_after": code_after,
        "straight_through_after": after["straight_through"],
        "system_exceptions_after": after["system_exceptions"],
        "erp_after": after["erp"],
        "wrongly_booked_after": after["business_exceptions"]["wrongly_booked"],
        "outcome_accuracy_pct_after": after["outcome_accuracy_pct"],
        "straight_through_before": before["straight_through"],
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="run")
    ap.add_argument("--robots", type=int, default=2)
    ap.add_argument("--chaos", action="store_true", help="make the ERP fail, hang and lose responses")
    ap.add_argument("--ai-fallback", action="store_true")
    ap.add_argument("--l2-recovery", action="store_true",
                    help="after the run: fix the ERP, retry system failures with the PowerShell L2 script, re-run a robot")
    args = ap.parse_args()

    cfg = load_config()
    for p in (QUEUE_DB, ERP_DB, Path(str(QUEUE_DB) + "-wal"), Path(str(QUEUE_DB) + "-shm")):
        p.unlink(missing_ok=True)
    for p in (DATA / "logs").glob("*.jsonl"):
        p.unlink()
    erp.seed()

    env = {**os.environ, **(CHAOS if args.chaos else {})}
    py = sys.executable
    log_dir = DATA / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    def serve(module: str, port: int, server_env: dict) -> subprocess.Popen:
        return subprocess.Popen([py, "-m", "uvicorn", "--factory", f"rpa_lab.{module}:create_app", "--port", str(port),
                                 "--log-level", "warning"], cwd=ROOT, env=server_env,
                                stdout=open(log_dir / f"{module}.out", "a"), stderr=subprocess.STDOUT)

    servers = [serve("orchestrator", 8090, env), serve("erp", 8091, env)]
    try:
        wait_healthy(cfg["OrchestratorURL"], servers[0])
        wait_healthy(cfg["ERP_URL"], servers[1])
        t0 = time.perf_counter()
        disp = dispatch()
        robot_cmd = [py, "-m", "rpa_lab.performer"] + (["--ai-fallback"] if args.ai_fallback else [])
        robots = [subprocess.Popen(robot_cmd + ["--name", f"ROBOT-{i + 1:02d}"], cwd=ROOT, env=env,
                                   stdout=subprocess.PIPE, text=True) for i in range(args.robots)]
        summaries = [json.loads(r.communicate()[0].strip().splitlines()[-1]) for r in robots]
        wall = time.perf_counter() - t0
        result = evaluate(cfg, wall, disp, summaries)
        if args.l2_recovery:
            result["l2_recovery"] = l2_recovery(cfg, servers, serve, robot_cmd, wall, disp, summaries)
        result["run"] = {"name": args.name, "chaos": CHAOS if args.chaos else None, "ai_fallback": args.ai_fallback,
                         "ai_model": cfg["AiFallbackModel"] if args.ai_fallback else None}
        report = build_report(cfg)
    finally:
        for s in servers:
            s.terminate()
            s.wait(timeout=10)

    REPORTS.mkdir(exist_ok=True)
    out = REPORTS / f"{args.name}.json"
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    brief = {k: result[k] for k in ("outcome_accuracy_pct", "straight_through", "business_exceptions",
                                    "false_rejections", "system_exceptions", "erp", "dispatcher", "timing",
                                    "l2_recovery") if k in result}
    print(json.dumps(brief, indent=2, ensure_ascii=False))
    print(f"report: {out}\nexcel:  {report}")


if __name__ == "__main__":
    main()
