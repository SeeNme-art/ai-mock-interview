"""TraceLab 存储层：SQLite + 后台线程批量写盘。

为什么用 sqlite3 而不是 SQLAlchemy：
这里是独立的观测存储；业务表关系与迁移按新版设计单独实施。

核心约束（docs/项目设计/03-系统架构与运行机制.md §7）：
- 绝不逐 span commit —— 语音场景一场 40+ span，逐条写会把面试拖慢
- 采集失败不能影响业务 —— 所有写入异常只记日志，绝不向外抛
"""

from __future__ import annotations

import logging
import queue
import sqlite3
import threading
import time
from contextlib import closing
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from app.core.telemetry import safe_error

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trace (
  id           TEXT PRIMARY KEY,
  session_id   TEXT,
  name         TEXT NOT NULL,
  status       TEXT NOT NULL,
  started_ms   INTEGER NOT NULL,
  duration_ms  INTEGER NOT NULL,
  total_tokens INTEGER,
  error        TEXT
);
CREATE TABLE IF NOT EXISTS span (
  id          TEXT PRIMARY KEY,
  trace_id    TEXT NOT NULL,
  parent_id   TEXT,
  name        TEXT NOT NULL,
  type        TEXT NOT NULL,
  started_ms  INTEGER NOT NULL,
  duration_ms INTEGER NOT NULL,
  input       TEXT,
  output      TEXT,
  status      TEXT NOT NULL,
  error       TEXT
);
CREATE INDEX IF NOT EXISTS idx_span_trace ON span(trace_id);
CREATE INDEX IF NOT EXISTS idx_trace_started ON trace(started_ms DESC);
"""


@dataclass(slots=True)
class TraceRecord:
    id: str
    name: str
    session_id: str | None
    status: str
    started_ms: int
    duration_ms: int
    total_tokens: int | None = None
    error: str | None = None


@dataclass(slots=True)
class SpanRecord:
    id: str
    trace_id: str
    parent_id: str | None
    name: str
    type: str
    started_ms: int
    duration_ms: int
    status: str = "ok"
    input: Any = None
    output: Any = None
    error: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def _metadata(row: sqlite3.Row) -> dict[str, Any]:
    """历史库读取同样不向 API/页面暴露已有正文；不删除旧库。"""
    result = dict(row)
    for key in ("input", "output"):
        if key in result:
            result[key] = None
    result["error"] = safe_error(result.get("error"))
    return result


class ObserveStore:
    """TraceLab 的读写入口。写入走队列，读取各开短连接。"""

    def __init__(
        self,
        path: Path,
        *,
        flush_size: int = 50,
        flush_interval_ms: int = 200,
    ) -> None:
        self._path = path
        self._flush_size = flush_size
        self._interval = flush_interval_ms / 1000.0

        self._queue: queue.Queue[Any] = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        self._path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            conn.executescript(_SCHEMA)

        self._start_worker()

    # ---------------- 连接 ----------------
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        # WAL：读写不互相阻塞，页面刷新时不会卡住写入
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    # ---------------- 写入 ----------------
    def submit_trace(self, rec: TraceRecord) -> None:
        self._queue.put(("trace", replace(rec, error=safe_error(rec.error))))

    def submit_span(self, rec: SpanRecord) -> None:
        self._queue.put(("span", replace(rec, input=None, output=None, extra={}, error=safe_error(rec.error))))

    def _start_worker(self) -> None:
        self._thread = threading.Thread(target=self._run, name="observe-writer", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        """后台写线程：攒够 flush_size 或等够 interval 就落盘一次。"""
        conn = self._connect()
        try:
            while True:
                item = self._queue.get()
                if item is None:          # 关闭信号
                    self._queue.task_done()
                    break

                batch = [item]
                deadline = time.monotonic() + self._interval
                while len(batch) < self._flush_size and time.monotonic() < deadline:
                    try:
                        nxt = self._queue.get(timeout=0.01)
                    except queue.Empty:
                        break
                    if nxt is None:
                        self._queue.task_done()
                        self._stop.set()
                        break
                    batch.append(nxt)

                self._write(conn, batch)
                for _ in batch:
                    self._queue.task_done()

                if self._stop.is_set():
                    break
        finally:
            conn.close()

    def _write(self, conn: sqlite3.Connection, batch: list[tuple[str, Any]]) -> None:
        traces = [r for kind, r in batch if kind == "trace"]
        spans = [r for kind, r in batch if kind == "span"]
        try:
            if traces:
                conn.executemany(
                    "INSERT OR REPLACE INTO trace"
                    "(id,session_id,name,status,started_ms,duration_ms,total_tokens,error)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    [
                        (t.id, t.session_id, t.name, t.status, t.started_ms,
                         t.duration_ms, t.total_tokens, safe_error(t.error))
                        for t in traces
                    ],
                )
            if spans:
                conn.executemany(
                    "INSERT OR REPLACE INTO span"
                    "(id,trace_id,parent_id,name,type,started_ms,duration_ms,input,output,status,error)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    [
                        (s.id, s.trace_id, s.parent_id, s.name, s.type, s.started_ms,
                         s.duration_ms,
                         None, None, s.status, safe_error(s.error))
                        for s in spans
                    ],
                )
            conn.commit()
        except Exception:  # noqa: BLE001 —— 采集失败绝不影响业务
            conn.rollback()
            logger.error("observe.write.failed", extra={"event": "observe.write.failed",
                         "fields": {"error_code": "OBSERVE_WRITE_FAILED"}})

    def flush(self, timeout: float = 5.0) -> None:
        """等待队列写空。测试与进程退出前调用。"""
        done = threading.Event()

        def _wait() -> None:
            self._queue.join()
            done.set()

        threading.Thread(target=_wait, daemon=True).start()
        done.wait(timeout)

    def close(self) -> None:
        """停止写线程（会把剩余条目写完）。"""
        self._queue.put(None)
        if self._thread is not None:
            self._thread.join(timeout=3.0)

    # ---------------- 读取 ----------------
    def list_traces(self, limit: int = 50, status: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM trace"
        args: list[Any] = []
        if status:
            sql += " WHERE status = ?"
            args.append(status)
        sql += " ORDER BY started_ms DESC LIMIT ?"
        args.append(limit)
        with closing(self._connect()) as conn:
            return [_metadata(r) for r in conn.execute(sql, args)]

    def get_trace(self, trace_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM trace WHERE id = ?", (trace_id,)).fetchone()
            return _metadata(row) if row else None

    def list_spans(self, trace_id: str) -> list[dict[str, Any]]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM span WHERE trace_id = ? ORDER BY started_ms ASC, duration_ms DESC",
                (trace_id,),
            )
            return [_metadata(r) for r in rows]

    def get_span(self, span_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM span WHERE id = ?", (span_id,)).fetchone()
            return _metadata(row) if row else None

    def summary(self) -> dict[str, int]:
        with closing(self._connect()) as conn:
            trace_count = conn.execute("SELECT COUNT(*) c FROM trace").fetchone()["c"]
            span_count = conn.execute("SELECT COUNT(*) c FROM span").fetchone()["c"]
            slow = conn.execute(
                "SELECT COUNT(*) c FROM span WHERE duration_ms >= 3000"
            ).fetchone()["c"]
            failed = conn.execute(
                "SELECT COUNT(*) c FROM span WHERE status != 'ok'"
            ).fetchone()["c"]
        return {"traces": trace_count, "spans": span_count,
                "slow_spans": slow, "failed_spans": failed}
