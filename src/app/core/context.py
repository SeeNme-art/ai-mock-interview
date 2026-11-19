"""请求级上下文变量。

日志与 trace 靠这里传递「当前在哪条链路、哪个 span」，
避免把 trace_id 当作参数在每一层函数里手工透传。

为什么用 ContextVar 而不是全局变量：
1. 并发安全——每个请求/协程有自己的一份
2. 不会在请求结束后残留污染下一个请求
3. asyncio 原生支持，FastAPI 下直接可用
"""

from __future__ import annotations

from contextvars import ContextVar

# 当前操作的 trace_id（一个会话可有多条 trace）
trace_id_var: ContextVar[str | None] = ContextVar("trace_id", default=None)

# 当前 span 的 id（用于建立父子关系）
span_id_var: ContextVar[str | None] = ContextVar("span_id", default=None)

# 当前会话 id（业务维度，和 trace 不是一对一）
session_id_var: ContextVar[str | None] = ContextVar("session_id", default=None)


def current_trace_id() -> str | None:
    return trace_id_var.get()


def current_span_id() -> str | None:
    return span_id_var.get()


def current_session_id() -> str | None:
    return session_id_var.get()
