"""链路采集：trace / span 的上下文管理器。

用法::

    with start_trace("interview.turn", session_id="s_1") as tr:
        with span("turn.ask", "chain"):
            with span("llm.invoke", "llm", input={"prompt": "..."}) as llm:
                llm.output = {"text": "..."}
                llm.tokens_in, llm.tokens_out = 1200, 180

三条设计要点：
1. 业务代码只加两行（with），不感知存储细节
2. span 失败不吞异常，只标记 status=error，异常继续往上抛给业务处理
3. 慢 span（>1000ms）自动记 WARNING，不打开 UI 也能发现性能问题
"""

from __future__ import annotations

import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

from app.config import Settings, get_settings
from app.core.context import session_id_var, span_id_var, trace_id_var
from app.core.logging_setup import log_event
from app.core.telemetry import error_code
from app.observability.store import ObserveStore, SpanRecord, TraceRecord

SLOW_SPAN_MS = 1000

_store: ObserveStore | None = None
_current_trace: ContextVar["TraceHandle | None"] = ContextVar("trace_handle", default=None)


# ---------------- 生命周期 ----------------
def init_tracing(settings: Settings | None = None) -> ObserveStore:
    """创建全局 store。幂等，重复调用返回同一个。"""
    global _store
    if _store is not None:
        return _store
    s = settings or get_settings()
    s.ensure_dirs()
    _store = ObserveStore(
        s.observe_db,
        flush_size=s.observe_flush_size,
        flush_interval_ms=s.observe_flush_interval_ms,
    )
    return _store


def get_store() -> ObserveStore:
    if _store is None:
        return init_tracing()
    return _store


def shutdown_tracing() -> None:
    """进程退出前把队列写空。"""
    global _store
    if _store is not None:
        _store.flush()
        _store.close()
        _store = None


def _now_ms() -> int:
    return int(time.time() * 1000)


def _new_id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:16]}"


# ---------------- 句柄 ----------------
class SpanHandle:
    """一个进行中的 span。允许在 with 体内写 output / tokens。"""

    __slots__ = ("id", "name", "type", "parent_id", "trace_id",
                 "input", "output", "tokens_in", "tokens_out", "started_ms", "t0")

    def __init__(self, name: str, type_: str, parent_id: str | None,
                 trace_id: str, input_: Any) -> None:
        self.id = _new_id("sp_")
        self.name = name
        self.type = type_
        self.parent_id = parent_id
        self.trace_id = trace_id
        self.input = None  # 正文不进入观测句柄；参数保留以兼容旧调用方
        self.output: Any = None
        self.tokens_in: int | None = None
        self.tokens_out: int | None = None
        self.started_ms = _now_ms()
        self.t0 = time.perf_counter()


class TraceHandle:
    """一条链路。t0 用 perf_counter 保证时长精确，started_ms 用于跨进程排序。"""

    __slots__ = ("id", "name", "session_id", "started_ms", "t0", "total_tokens")

    def __init__(self, name: str, session_id: str | None) -> None:
        self.id = _new_id("t_")
        self.name = name
        self.session_id = session_id
        self.started_ms = _now_ms()
        self.t0 = time.perf_counter()
        self.total_tokens = 0


# ---------------- 上下文管理器 ----------------
@contextmanager
def start_trace(name: str, session_id: str | None = None) -> Iterator[TraceHandle]:
    """开启一条链路。异常时标记 error，但仍然落盘。"""
    handle = TraceHandle(name, session_id or session_id_var.get())
    token_trace = trace_id_var.set(handle.id)
    token_session = session_id_var.set(handle.session_id)
    token_handle = _current_trace.set(handle)

    status, error = "ok", None
    try:
        yield handle
    except BaseException as exc:  # noqa: BLE001 —— 记录后原样抛出，不改变业务行为
        status, error = "error", error_code(exc)
        log_event("trace.failed", trace_name=name, error_code=error)
        raise
    finally:
        duration_ms = int((time.perf_counter() - handle.t0) * 1000)
        get_store().submit_trace(
            TraceRecord(
                id=handle.id,
                name=handle.name,
                session_id=handle.session_id,
                status=status,
                started_ms=handle.started_ms,
                duration_ms=duration_ms,
                total_tokens=handle.total_tokens or None,
                error=error,
            )
        )
        log_event("trace.end", trace_name=handle.name, duration_ms=duration_ms,
                  status=status, total_tokens=handle.total_tokens)
        _current_trace.reset(token_handle)
        session_id_var.reset(token_session)
        trace_id_var.reset(token_trace)


@contextmanager
def span(name: str, type_: str, input: Any = None) -> Iterator[SpanHandle]:
    """开启一个 span，自动挂到当前 span 之下（形成树）。"""
    trace_id = trace_id_var.get()

    # 容错：没有 trace 就补一条，避免因为忘写 start_trace 而丢数据
    if trace_id is None:
        with start_trace(name):
            with span(name, type_, input) as inner:
                yield inner
        return

    handle = SpanHandle(name, type_, span_id_var.get(), trace_id, input)
    token_span = span_id_var.set(handle.id)

    status, error = "ok", None
    try:
        yield handle
    except BaseException as exc:  # noqa: BLE001
        status, error = "error", error_code(exc)
        raise
    finally:
        duration_ms = int((time.perf_counter() - handle.t0) * 1000)
        get_store().submit_span(
            SpanRecord(
                id=handle.id,
                trace_id=handle.trace_id,
                parent_id=handle.parent_id,
                name=handle.name,
                type=handle.type,
                started_ms=handle.started_ms,
                duration_ms=duration_ms,
                status=status,
                input=None,
                output=None,
                error=error,
            )
        )

        parent = _current_trace.get()
        if parent is not None and (handle.tokens_in or handle.tokens_out):
            parent.total_tokens += (handle.tokens_in or 0) + (handle.tokens_out or 0)

        if status != "ok":
            log_event("span.error", level=40, span=name, span_type=type_,
                      duration_ms=duration_ms, error_code=error)
        elif duration_ms >= SLOW_SPAN_MS:
            log_event("span.slow", level=30, span=name, span_type=type_,
                      duration_ms=duration_ms)

        span_id_var.reset(token_span)
