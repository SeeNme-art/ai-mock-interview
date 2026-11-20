"""Run the six approved synthetic cases once, with a durable local budget ledger."""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import http.client
import json
from pathlib import Path
import sqlite3
import sys
import time

from model_preflight import MAX_ATTEMPTS, MAX_OUTPUT, PRICE, build_plan, validate

ROOT = Path(__file__).resolve().parents[2]
RUN_DIR = ROOT / "data" / "experiments" / "EXP-002-live"
BUDGET = 20_000_000  # USD 0.02 in billionths of a dollar
MAX_REQUESTS = 12
TIMEOUT_SECONDS = 30
RETRYABLE_HTTP = {429, 500, 503}
MAX_BODY_BYTES = 65536


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def usd(nanos: int) -> str:
    return format(Decimal(nanos) / 10**9, "f")


def upper_cost(prompt_tokens: int, completion_tokens: int) -> int:
    # All prompt tokens use the peak cache-miss rate, including cache hits.
    input_rate = int(Decimal(PRICE["input_cache_miss_per_million"]) * 1000)
    output_rate = int(Decimal(PRICE["output_per_million"]) * 1000)
    return prompt_tokens * input_rate + completion_tokens * output_rate


def send_once(request_bytes: bytes, key: str) -> tuple[int, bytes]:
    # Direct HTTPS to the provider. Each invocation issues one POST, with no redirect
    # handling, SDK retries or response body included in HTTP error logs.
    with closing(http.client.HTTPSConnection("api.deepseek.com", timeout=TIMEOUT_SECONDS)) as conn:
        conn.request("POST", "/chat/completions", body=request_bytes, headers={
            "Authorization": "Bearer " + key, "Content-Type": "application/json",
        })
        with closing(conn.getresponse()) as response:
            return response.status, response.read(MAX_BODY_BYTES + 1) if response.status == 200 else b""


def assess(case_id: str, status: int, body: bytes, input_allowance: int) -> tuple[str, int | None, int | None]:
    if status != 200:
        return ("retryable_http" if status in RETRYABLE_HTTP else "http_stop", None, None)
    try:
        if len(body) > MAX_BODY_BYTES:
            return "usage_unknown", None, None
        payload = json.loads(body)
        usage = payload["usage"]
        prompt, completion = usage["prompt_tokens"], usage["completion_tokens"]
        total = usage["total_tokens"]
        if any(type(x) is not int or x < 0 for x in (prompt, completion, total)) or total != prompt + completion:
            return "usage_unknown", None, None
        if max(prompt, completion) > 10_000_000:
            return "usage_unknown", None, None
    except (ValueError, TypeError, KeyError, RecursionError):
        return "usage_unknown", None, None
    if prompt > input_allowance or completion > MAX_OUTPUT:
        return "usage_over_plan", prompt, completion
    if payload.get("model") != PRICE["model"]:
        return "unexpected_model", prompt, completion
    try:
        choice = payload["choices"][0]
        content = choice["message"]["content"]
        if not isinstance(content, str):
            return "invalid_output", prompt, completion
        validate(case_id, content, choice["finish_reason"])
    except (ValueError, TypeError, KeyError, IndexError, RecursionError):
        return "invalid_output", prompt, completion
    return "passed", prompt, completion


def start_run(run_dir: Path, key: str, transport=send_once, *, budget: int = BUDGET) -> dict:
    """Claim the run directory once. After interruption the ledger is inspectable only."""
    cases = build_plan()["cases"]
    if len(cases) * MAX_ATTEMPTS > MAX_REQUESTS or not 0 < budget <= BUDGET:
        raise ValueError("invalid trial limits")
    run_dir.mkdir(parents=True, exist_ok=False)
    with closing(sqlite3.connect(run_dir / "ledger.db")) as db:
        db.executescript("""
            CREATE TABLE attempts (
                case_id TEXT NOT NULL, attempt INTEGER NOT NULL, reserved_nanos INTEGER NOT NULL,
                charge_nanos INTEGER, outcome TEXT NOT NULL, http_status INTEGER,
                prompt_tokens INTEGER, completion_tokens INTEGER, request_sha256 TEXT NOT NULL,
                started_utc TEXT NOT NULL, PRIMARY KEY (case_id, attempt)
            );
            CREATE TABLE run (stop_reason TEXT NOT NULL, budget_nanos INTEGER NOT NULL);
        """)
        with db:
            db.execute("INSERT INTO run VALUES('incomplete', ?)", (budget,))
        reason = execute_cases(db, cases, key, transport, budget)
        with db:
            db.execute("UPDATE run SET stop_reason=?", (reason,))
        return summarize(db)


def execute_cases(db, cases, key, transport, budget) -> str:
    used = 0
    attempts = 0
    for case in cases:
        case_id = case["case_id"]
        allowance = case["planning_input_tokens"]
        reserve = upper_cost(allowance, MAX_OUTPUT)
        request = json.dumps(case["request"], ensure_ascii=False, sort_keys=True).encode("utf-8")
        request_hash = hashlib.sha256(request).hexdigest()
        for number in range(1, MAX_ATTEMPTS + 1):
            if attempts >= MAX_REQUESTS or used + reserve > budget:
                return "budget_or_attempt_limit"
            # A failed or interrupted commit cannot be followed by a network request.
            with db:
                db.execute("INSERT INTO attempts VALUES(?,?,?,NULL,'reserved',NULL,NULL,NULL,?,?)",
                           (case_id, number, reserve, request_hash, utc_now()))
            attempts += 1
            status = None
            try:
                status, body = transport(request, key)
                outcome, prompt, completion = assess(case_id, status, body, allowance)
            except (OSError, http.client.HTTPException):
                outcome, prompt, completion = "network_usage_unknown", None, None
            charge = upper_cost(prompt, completion) if prompt is not None and completion is not None else None
            used += charge if charge is not None else reserve
            with db:
                db.execute("""UPDATE attempts SET charge_nanos=?, outcome=?, http_status=?,
                           prompt_tokens=?, completion_tokens=? WHERE case_id=? AND attempt=?""",
                           (charge, outcome, status, prompt, completion, case_id, number))
            if used > budget:
                return "usage_over_budget"
            if outcome == "passed":
                break
            if outcome not in {"invalid_output", "retryable_http"}:
                return outcome
            if number < MAX_ATTEMPTS and outcome == "retryable_http":
                time.sleep(1)
        else:
            if case_id == "connection_json" or outcome == "retryable_http":
                return "attempts_exhausted"
    return "cases_finished"


def summarize(db: sqlite3.Connection) -> dict:
    db.row_factory = sqlite3.Row
    rows = []
    known = held = 0
    for rec in db.execute("SELECT * FROM attempts ORDER BY rowid"):
        row = dict(rec)
        reserved, charge = row.pop("reserved_nanos"), row.pop("charge_nanos")
        row["reserved_usd"] = usd(reserved)
        row["peak_cost_from_usage_usd"] = usd(charge) if charge is not None else None
        rows.append(row)
        if charge is None:
            held += reserved
        else:
            known += charge
    run = db.execute("SELECT * FROM run").fetchone()
    return {"experiment": "EXP-002", "model": PRICE["model"], "price_snapshot": PRICE,
            "budget_usd": usd(run["budget_nanos"]), "stop_reason": run["stop_reason"],
            "attempts_recorded": len(rows), "cases_passed": sum(row["outcome"] == "passed" for row in rows),
            "prompt_tokens_known": sum(row["prompt_tokens"] or 0 for row in rows),
            "completion_tokens_known": sum(row["completion_tokens"] or 0 for row in rows),
            "peak_cost_from_known_usage_usd": usd(known), "unknown_cost_reserved_usd": usd(held),
            "attempts": rows}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("run", "status"))
    args = parser.parse_args()
    try:
        if args.action == "status":
            ledger = (RUN_DIR / "ledger.db").as_uri() + "?mode=ro"
            with closing(sqlite3.connect(ledger, uri=True)) as db:
                report = summarize(db)
        else:
            if RUN_DIR.exists():
                print("TRIAL_ALREADY_STARTED: use status to inspect the existing ledger", file=sys.stderr)
                return 1
            sys.path.insert(0, str(ROOT / "src"))
            from app.config import get_settings

            settings = get_settings()
            key = settings.deepseek_api_key.strip()
            if not key or not key.isascii() or not key.isprintable() or " " in key:
                print("LOCAL_KEY_MISSING_OR_INVALID", file=sys.stderr)
                return 1
            if settings.llm_model_main != PRICE["model"]:
                print("LOCAL_MODEL_DOES_NOT_MATCH_APPROVED_MODEL", file=sys.stderr)
                return 1
            report = start_run(RUN_DIR, key)
    except (OSError, sqlite3.Error, ValueError, RuntimeError) as exc:
        # Configuration errors and remote exceptions can contain sensitive text.
        print("TRIAL_ERROR: " + type(exc).__name__, file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if args.action == "status" or report["cases_passed"] == 6 else 1


if __name__ == "__main__":
    raise SystemExit(main())
