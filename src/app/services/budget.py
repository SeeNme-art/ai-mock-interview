"""A durable per-session budget around a single provider attempt."""
from __future__ import annotations

from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
import re
import sqlite3
from typing import Callable, TypeVar
from uuid import uuid4

from app.db.business import SessionError, check_business_schema, find_session, upgrade_business
from app.db.sqlite import canonical_json, connect

BUDGET_SCHEMA = 6
MAX_TOKENS = 10_000_000
MAX_RATE_NANOS = 1_000_000_000
MAX_CAP_NANOS = 10**15
CLOSING_ROLES = {"evaluate", "report"}
ACTIVE_ROLES = {"question", "question_check", "probe", "evaluate"}
Result = TypeVar("Result")


def _number(value: int, lower: int, upper: int, label: str) -> None:
    if type(value) is not int or not lower <= value <= upper:
        raise SessionError("INVALID_INPUT", f"{label}必须是 {lower} 至 {upper} 的整数")


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int = 0

    def __post_init__(self) -> None:
        _number(self.input_tokens, 0, MAX_TOKENS, "输入 token 数")
        _number(self.output_tokens, 0, MAX_TOKENS, "输出 token 数")
        _number(self.cached_input_tokens, 0, self.input_tokens, "缓存命中 token 数")


@dataclass(frozen=True)
class PriceSnapshot:
    model: str
    version: str
    input_nanos_per_token: int
    output_nanos_per_token: int
    cached_input_nanos_per_token: int | None = None

    def __post_init__(self) -> None:
        for identifier in (self.model, self.version):
            if not isinstance(identifier, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}", identifier) is None:
                raise SessionError("INVALID_INPUT", "模型或价格版本标识无效")
        _number(self.input_nanos_per_token, 0, MAX_RATE_NANOS, "输入单价")
        _number(self.output_nanos_per_token, 0, MAX_RATE_NANOS, "输出单价")
        if self.cached_input_nanos_per_token is not None:
            _number(self.cached_input_nanos_per_token, 0, self.input_nanos_per_token, "缓存命中单价")

    def cost(self, usage: Usage) -> int:
        cached_rate = self.cached_input_nanos_per_token
        if cached_rate is None:
            cached_rate = self.input_nanos_per_token
        return ((usage.input_tokens - usage.cached_input_tokens) * self.input_nanos_per_token
                + usage.cached_input_tokens * cached_rate + usage.output_tokens * self.output_nanos_per_token)


def _account(db: sqlite3.Connection, session_id: str) -> sqlite3.Row | None:
    return db.execute("SELECT * FROM budget_account WHERE session_id=?", (session_id,)).fetchone()


def _view_budget(db: sqlite3.Connection, session_id: str, account: sqlite3.Row | None) -> dict:
    if account is None:
        return {"session_id": session_id, "configured": False, "attempts": []}
    available = account["hard_limit_nanos"] - account["spent_nanos"] - account["reserved_nanos"]
    attempts = []
    for row in db.execute("SELECT * FROM call_attempt WHERE session_id=? ORDER BY started_at,id", (session_id,)):
        attempt = dict(row)
        attempt["pricing_snapshot"] = json.loads(attempt.pop("pricing_snapshot_json"))
        attempts.append(attempt)
    return {**dict(account), "configured": True, "available_nanos": max(0, available),
            "over_limit_nanos": max(0, -available), "attempts": attempts}


def read_budget(path: Path, session_id: str) -> dict:
    if not path.is_file():
        raise SessionError("RESOURCE_NOT_FOUND", "找不到可访问的会话")
    with closing(connect(path, readonly=True)) as db, db:
        db.execute("BEGIN")
        version = check_business_schema(db)
        find_session(db, session_id)
        account = _account(db, session_id) if version >= BUDGET_SCHEMA else None
        return _view_budget(db, session_id, account)


def configure_budget(path: Path, session_id: str, *, hard_limit_nanos: int, closing_reserve_nanos: int = 0) -> dict:
    _number(hard_limit_nanos, 1, MAX_CAP_NANOS, "会话额度")
    _number(closing_reserve_nanos, 0, hard_limit_nanos, "收尾预留")
    if not path.is_file():
        raise SessionError("RESOURCE_NOT_FOUND", "找不到可访问的会话")
    with closing(connect(path)) as db:
        while True:
            with db:
                db.execute("BEGIN IMMEDIATE")
                version = check_business_schema(db)
                find_session(db, session_id)
                if version < BUDGET_SCHEMA:
                    # Each next backup must observe the last committed schema.
                    upgrade_business(db, path, version + 1)
                    if version + 1 < BUDGET_SCHEMA:
                        continue
                account = _account(db, session_id)
                if account is None:
                    db.execute("""INSERT INTO budget_account(session_id,currency,hard_limit_nanos,closing_reserve_nanos,updated_at)
                                  VALUES (?,'USD',?,?,?)""",
                               (session_id, hard_limit_nanos, closing_reserve_nanos, datetime.now(UTC).isoformat()))
                    account = _account(db, session_id)
                elif (account["hard_limit_nanos"], account["closing_reserve_nanos"]) != (hard_limit_nanos, closing_reserve_nanos):
                    raise SessionError("BUDGET_ALREADY_CONFIGURED", "该会话预算已冻结")
                return _view_budget(db, session_id, account)


def _reserve_call(path: Path, session_id: str, operation_id: str, step_key: str, attempt_no: int, *,
                  role: str, price: PriceSnapshot, estimated_input_tokens: int, max_output_tokens: int) -> str:
    _number(attempt_no, 1, 2, "尝试编号")
    _number(estimated_input_tokens, 1, MAX_TOKENS, "输入 token 估算")
    _number(max_output_tokens, 1, MAX_TOKENS, "输出 token 上限")
    if not isinstance(step_key, str) or re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,79}", step_key) is None:
        raise SessionError("INVALID_INPUT", "调用步骤标识无效")
    if not path.is_file():
        raise SessionError("RESOURCE_NOT_FOUND", "找不到可访问的会话")
    reserve = price.cost(Usage(estimated_input_tokens, max_output_tokens))
    with closing(connect(path)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        version = check_business_schema(db)
        session = find_session(db, session_id)
        account = _account(db, session_id) if version >= BUDGET_SCHEMA else None
        if account is None:
            raise SessionError("BUDGET_NOT_CONFIGURED", "会话尚未配置模型调用预算")
        operation = db.execute("""SELECT expected_epoch,status FROM operation
                                   WHERE id=? AND session_id=? AND owner_id=?""",
                               (operation_id, session_id, session["owner_id"])).fetchone()
        roles = {"PLANNING": {"plan"}, "ACTIVE": ACTIVE_ROLES, "FINISHED": CLOSING_ROLES}
        if (operation is None or operation["expected_epoch"] != session["control_epoch"]
                or operation["status"] not in ("queued", "running") or role not in roles[session["phase"]]):
            raise SessionError("ILLEGAL_ACTION", "当前操作或阶段不能发出模型调用")
        previous = db.execute("""SELECT attempt_no,status,role FROM call_attempt WHERE operation_id=? AND step_key=?
                                 ORDER BY attempt_no DESC LIMIT 1""", (operation_id, step_key)).fetchone()
        if previous is not None and attempt_no <= previous["attempt_no"]:
            raise SessionError("CALL_ALREADY_RECORDED", "该次模型调用已登记")
        if attempt_no == 2 and (previous is None or previous["status"] == "reserved" or previous["role"] != role):
            raise SessionError("ILLEGAL_ACTION", "只有同职责的上一次尝试已结束才可再次调用")
        ceiling = account["hard_limit_nanos"]
        if role not in CLOSING_ROLES:
            ceiling -= account["closing_reserve_nanos"]
        if account["spent_nanos"] + account["reserved_nanos"] + reserve > ceiling:
            raise SessionError("BUDGET_EXHAUSTED", "模型调用超出当前会话可用额度")
        now, call_id = datetime.now(UTC).isoformat(), uuid4().hex
        db.execute("""INSERT INTO call_attempt(id,session_id,operation_id,step_key,attempt_no,role,status,
                          estimated_input_tokens,max_output_tokens,reserved_nanos,pricing_snapshot_json,started_at)
                      VALUES (?,?,?,?,?,?,'reserved',?,?,?,?,?)""",
                   (call_id, session_id, operation_id, step_key, attempt_no, role, estimated_input_tokens,
                    max_output_tokens, reserve, canonical_json(asdict(price)), now))
        db.execute("UPDATE budget_account SET reserved_nanos=reserved_nanos+?,updated_at=? WHERE session_id=?",
                   (reserve, now, session_id))
        return call_id


def settle_call(path: Path, session_id: str, call_id: str, usage: Usage | None) -> None:
    if not path.is_file():
        raise SessionError("RESOURCE_NOT_FOUND", "找不到可访问的会话")
    with closing(connect(path)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        version = check_business_schema(db)
        find_session(db, session_id)
        row = db.execute("SELECT * FROM call_attempt WHERE id=? AND session_id=?",
                         (call_id, session_id)).fetchone() if version >= BUDGET_SCHEMA else None
        if row is None:
            raise SessionError("RESOURCE_NOT_FOUND", "找不到该会话的模型调用")
        if row["status"] == "settled":
            saved = Usage(row["actual_input_tokens"], row["actual_output_tokens"], row["actual_cached_input_tokens"])
            if usage != saved:
                raise SessionError("IDEMPOTENCY_CONFLICT", "该次调用已按其他用量结算")
            return
        now = datetime.now(UTC).isoformat()
        if usage is None:
            db.execute("UPDATE call_attempt SET status='usage_unknown',ended_at=? WHERE id=? AND status='reserved'",
                       (now, call_id))
            return
        charge = PriceSnapshot(**json.loads(row["pricing_snapshot_json"])).cost(usage)
        db.execute("""UPDATE call_attempt SET status='settled',actual_input_tokens=?,actual_output_tokens=?,
                      actual_cached_input_tokens=?,actual_nanos=?,ended_at=? WHERE id=?""",
                   (usage.input_tokens, usage.output_tokens, usage.cached_input_tokens, charge, now, call_id))
        db.execute("""UPDATE budget_account SET spent_nanos=spent_nanos+?,reserved_nanos=reserved_nanos-?,updated_at=?
                      WHERE session_id=?""", (charge, row["reserved_nanos"], now, session_id))


def invoke_budgeted(path: Path, session_id: str, operation_id: str, step_key: str, *, role: str, attempt_no: int,
                    price: PriceSnapshot, estimated_input_tokens: int, max_output_tokens: int,
                    send_once: Callable[[int], tuple[Result, Usage | None]]) -> Result:
    """Reserve before sending, then settle from metered usage or hold funds when usage is unknown."""
    call_id = _reserve_call(path, session_id, operation_id, step_key, attempt_no, role=role, price=price,
                            estimated_input_tokens=estimated_input_tokens, max_output_tokens=max_output_tokens)
    try:
        result, usage = send_once(max_output_tokens)
        if usage is not None and not isinstance(usage, Usage):
            raise ValueError("invalid provider usage")
    except Exception:
        settle_call(path, session_id, call_id, None)
        raise SessionError("MODEL_UNAVAILABLE", "模型调用失败，用量待核对") from None
    settle_call(path, session_id, call_id, usage)
    return result
