"""观测只接受开发者定义的元数据；不序列化正文或异常消息。"""

from __future__ import annotations

import re
from typing import Any

ERROR_CODES = frozenset({
    "TIMEOUT", "VALUE_ERROR", "RUNTIME_ERROR", "CANCELLED", "UNEXPECTED_ERROR",
    "OBSERVE_WRITE_FAILED", "LEGACY_ERROR_REDACTED",
})
_NUMBER_FIELDS = frozenset({"turn", "chars", "duration_ms", "total_tokens", "tokens_in", "tokens_out"})
_LABEL_FIELDS = frozenset({"env", "python", "version", "mode", "status", "span", "span_type", "trace_name"})
_LABEL = re.compile(r"[a-zA-Z0-9_.-]{1,80}\Z")
_EVENT = re.compile(r"[a-z][a-z0-9_.]{0,79}\Z")


def error_code(exc: BaseException) -> str:
    if isinstance(exc, TimeoutError):
        return "TIMEOUT"
    if isinstance(exc, ValueError):
        return "VALUE_ERROR"
    if isinstance(exc, RuntimeError):
        return "RUNTIME_ERROR"
    if isinstance(exc, (KeyboardInterrupt, SystemExit)):
        return "CANCELLED"
    return "UNEXPECTED_ERROR"


def safe_error(value: str | None) -> str | None:
    return None if value is None else value if value in ERROR_CODES else "LEGACY_ERROR_REDACTED"


def safe_event(value: Any) -> str:
    return value if isinstance(value, str) and _EVENT.fullmatch(value) else "external.log"


def safe_fields(fields: Any) -> dict[str, Any]:
    """字段名为内部协议，不得将用户文本放进 label 字段或事件名。"""
    if not isinstance(fields, dict):
        return {}
    result: dict[str, Any] = {}
    for key, value in fields.items():
        if key in _NUMBER_FIELDS and type(value) is int and value >= 0:
            result[key] = value
        elif key in _LABEL_FIELDS and isinstance(value, str) and _LABEL.fullmatch(value):
            result[key] = value
        elif key == "error_code" and isinstance(value, str) and value in ERROR_CODES:
            result[key] = value
    return result
