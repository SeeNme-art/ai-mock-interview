"""演示脚本：生成三条模拟链路，用于验证 TraceLab 是否工作。

跑法::

    .venv\\Scripts\\python scripts\\demo_trace.py

它不调用任何模型，只用 time.sleep 模拟耗时，
目的是让 /observe 有数据可看，与 P0 的验收标准对应。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

# 允许直接以脚本方式运行（把 src 加进 sys.path）
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from app.config import get_settings                      # noqa: E402
from app.core.logging_setup import log_event, setup_logging  # noqa: E402
from app.core.tracing import init_tracing, shutdown_tracing, span, start_trace  # noqa: E402
from app.core.tracing import get_store                   # noqa: E402


def simulate_turn(index: int, fail: bool = False) -> str:
    """模拟一轮「出题 + 检索 + 语音合成」，返回 trace_id。"""
    with start_trace("interview.turn", session_id="s_demo_001") as tr:
        log_event("user.answer.received", turn=index, mode="voice", chars=86)

        with span("turn.ask", "chain"):
            with span("llm.invoke", "llm",
                      input={"model": "deepseek-flash",
                             "prompt": "你是 AI 应用工程师岗位的面试官，请围绕 RAG 出一道题…"}) as s_llm:
                time.sleep(0.42)
                s_llm.output = {"text": "如果一份简历问答经常召回不到正确片段，你会怎么排查？"}
                s_llm.tokens_in, s_llm.tokens_out = 1180, 96

            with span("tool.search_questions", "tool",
                      input={"query": "RAG 召回 排查", "k": 3}) as s_tool:
                time.sleep(0.11)
                if fail:
                    raise RuntimeError("vector index unavailable")   # 演示失败 span
                s_tool.output = {"hits": 3, "top": "c_000031"}

        with span("tts.stream", "tts", input={"sentences": 3}) as s_tts:
            time.sleep(0.23)
            s_tts.output = {"bytes": 48120, "first_byte_ms": 210}

        log_event("turn.ask.done", turn=index)
        return tr.id


def main() -> None:
    settings = get_settings()
    settings.ensure_dirs()
    setup_logging(settings.log_level, settings.log_dir)
    init_tracing(settings)

    print("=" * 62)
    print("生成模拟链路（不调用模型，仅用于验证观测台）")
    print("=" * 62)

    ids: list[str] = []
    for i in (1, 2):
        tid = simulate_turn(i)
        ids.append(tid)
        print(f"第 {i} 轮  trace_id = {tid}")

    # 第三条故意让工具调用失败，用来验证错误链路能否正确记录。
    # 注意：采集层不吞异常，异常会照常抛给业务，所以这里必须自己接住。
    try:
        tid = simulate_turn(3, fail=True)
        print(f"第 3 轮 trace_id = {tid}")
    except RuntimeError as exc:
        print(f"第 3 轮（预期失败）异常已被业务层捕获: {exc}")

    # 进程退出前把队列刷干净
    get_store().flush()
    store = get_store()
    print("-" * 62)
    print("落盘统计:", store.summary())
    for t in store.list_traces(limit=5):
        print(f"  [{t['status']:<5}] {t['id']}  {t['duration_ms']}ms  {t['name']}")
    shutdown_tracing()

    print("-" * 62)
    print("下一步：启动服务查看页面")
    print("  PYTHONPATH=src .venv/Scripts/python -m uvicorn app.main:app --port 8000")
    print("  浏览器打开 http://127.0.0.1:8000/observe")


if __name__ == "__main__":
    main()
