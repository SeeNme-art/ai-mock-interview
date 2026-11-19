"""结构化日志：stdout 可读 + logs/app.jsonl 可查。

规范（docs/项目设计/03-系统架构与运行机制.md §7）：
- 必填字段：ts / level / event / trace_id / span_id / session_id
- 事件命名：点分小写下划线，如 llm.call.failed
- 默认仅保留明确列出的元数据，不接受正文和嵌套对象
- 不记录外部日志消息和原始 traceback，异常只保存安全错误码
- metadata 字段与事件名由代码定义，调用方不得填入用户文本

为什么自己写 formatter 而不用 structlog：
P0 使用标准库实现结构化输出与轮转，减少依赖；mask 仅是遗留工具，
不能据此承诺已清除自由文本中的个人信息。
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from app.core.context import current_session_id, current_span_id, current_trace_id
from app.core.telemetry import error_code, safe_event, safe_fields

# ---------- 脱敏 ----------
# 顺序有讲究：先手机号再身份证，避免长数字被短规则先截断
_PII_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"1[3-9]\d{9}"), "1**********"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "***@***"),
    (re.compile(r"\b\d{17}[\dXx]\b"), "******************"),
)


def mask(text: str) -> str:
    """对文本中的手机号 / 邮箱 / 身份证做掩码。"""
    for pattern, repl in _PII_PATTERNS:
        text = pattern.sub(repl, text)
    return text


class JsonFormatter(logging.Formatter):
    """把 LogRecord 序列化为单行 JSON，便于 grep 与后续入库。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "event": safe_event(getattr(record, "event", None)),
            "trace_id": getattr(record, "trace_id", None) or current_trace_id(),
            "span_id": getattr(record, "span_id", None) or current_span_id(),
            "session_id": getattr(record, "session_id", None) or current_session_id(),
        }

        # 业务字段统一放在 fields 里，避免与保留字（如 name/msg）冲突
        extras = safe_fields(getattr(record, "fields", None))
        if extras:
            payload["fields"] = extras

        if record.exc_info:
            exc = record.exc_info[1]
            payload["error_code"] = error_code(exc) if exc is not None else "UNEXPECTED_ERROR"

        return json.dumps(payload, ensure_ascii=False)


class ConsoleFormatter(logging.Formatter):
    """开发期可读格式：时间 级别 [trace] 事件 字段。"""

    LEVEL_COLOR = {
        "DEBUG": "\033[38;5;245m",
        "INFO": "\033[38;5;31m",
        "WARNING": "\033[38;5;178m",
        "ERROR": "\033[38;5;160m",
        "CRITICAL": "\033[38;5;125m",
    }
    RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        color = self.LEVEL_COLOR.get(record.levelname, "")
        ts = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        trace = current_trace_id() or "-"
        short_trace = trace[2:10] if trace.startswith("t_") else trace[:8]
        event = safe_event(getattr(record, "event", None))
        extras = safe_fields(getattr(record, "fields", None))
        tail = ""
        if extras:
            tail = " " + " ".join(f"{k}={v}" for k, v in extras.items())
        line = f"{ts} {color}{record.levelname:<7}{self.RESET} [{short_trace}] {event}{tail}"
        if record.exc_info:
            exc = record.exc_info[1]
            line += " error_code=" + (error_code(exc) if exc is not None else "UNEXPECTED_ERROR")
        return line


def setup_logging(level: str = "INFO", log_dir: Path | None = None) -> None:
    """初始化根 logger：控制台 + 轮转文件。重复调用安全（幂等）。"""
    root = logging.getLogger()
    if getattr(root, "_ai_interview_configured", False):
        return

    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.handlers.clear()

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(ConsoleFormatter())
    root.addHandler(console)

    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            filename=log_dir / "app.jsonl",
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setFormatter(JsonFormatter())
        root.addHandler(file_handler)

    # 第三方库降噪：httpx 每次请求都打一条 INFO，太吵
    for name in ("httpx", "httpcore", "urllib3", "uvicorn.access", "watchfiles"):
        logging.getLogger(name).setLevel(logging.WARNING)

    root._ai_interview_configured = True  # type: ignore[attr-defined]


def log_event(event: str, level: int = logging.INFO, **fields: Any) -> None:
    """记录一条业务事件。

    Args:
        event: 点分命名的事件名，如 ``rag.retrieve.empty``。
        level: 日志级别，默认 INFO。
        **fields: 仅允许 telemetry.py 定义的元数据；禁止传入真实正文。
    """
    logger = logging.getLogger("app")
    logger.log(level, safe_event(event), extra={"event": safe_event(event), "fields": safe_fields(fields)})
