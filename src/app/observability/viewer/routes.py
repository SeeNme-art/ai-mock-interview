"""TraceLab Viewer：只读的链路浏览页面。

P0 只做两件事：列表 + 单条链路的 span 树与瀑布图。
不做写操作、不做鉴权（本地单用户），由 OBSERVE_UI 开关整体控制。
"""

from __future__ import annotations

import html
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Query, Response
from fastapi.responses import HTMLResponse

from app.core.tracing import get_store

router = APIRouter(tags=["observe"])

# span 类型的配色：填充色 + 描边色
TYPE_COLOR: dict[str, tuple[str, str]] = {
    "chain": ("#B5D4F4", "#185FA5"),
    "llm": ("#85B7EB", "#185FA5"),
    "tool": ("#9FE1CB", "#1D9E75"),
    "tts": ("#FAC775", "#BA7517"),
    "asr": ("#F4C0D1", "#993556"),
    "db": ("#D3D1C7", "#5F5E5A"),
    "guard": ("#F7C1C1", "#A32D2D"),
}

CSS = """
*{box-sizing:border-box}
body{margin:0;background:#F6F6F3;color:#1C1C1A;font-size:13.5px;line-height:1.65;
 font-family:"PingFang SC","Microsoft YaHei",system-ui,-apple-system,"Segoe UI",sans-serif;-webkit-font-smoothing:antialiased}
.bar-top{position:sticky;top:0;z-index:5;height:52px;display:flex;align-items:center;justify-content:space-between;
 padding:0 22px;background:rgba(246,246,243,.9);backdrop-filter:blur(10px);border-bottom:1px solid #E6E5DF}
.brand{display:flex;align-items:center;gap:9px;font-weight:500}
.dot-logo{width:24px;height:24px;border-radius:7px;background:#185FA5;color:#fff;display:grid;place-items:center;font-size:11px}
.wrap{max-width:1080px;margin:0 auto;padding:26px 22px 60px}
h1{font-size:19px;font-weight:500;margin:0 0 6px}
.sub{color:#5F5E5A;font-size:12.5px;margin:0 0 20px}
.stats{display:flex;gap:12px;margin-bottom:20px;flex-wrap:wrap}
.stat{background:#fff;border:1px solid #E6E5DF;border-radius:10px;padding:10px 16px;min-width:112px}
.stat .v{font-size:19px;font-weight:500}
.stat .k{font-size:11px;color:#8A8983}
table{width:100%;border-collapse:collapse;background:#fff;border:1px solid #E6E5DF;border-radius:12px;overflow:hidden}
th{text-align:left;font-weight:500;font-size:11.5px;color:#8A8983;padding:10px 14px;border-bottom:1px solid #E6E5DF;background:#FAFAF7}
td{padding:11px 14px;border-bottom:1px solid #EFEEE9;font-size:13px}
tr:last-child td{border-bottom:0}
tr:hover td{background:#FBFAF7}
a{color:#185FA5;text-decoration:none}
a:hover{text-decoration:underline}
.pill{display:inline-block;font-size:11px;padding:1px 8px;border-radius:999px;border:1px solid}
.ok{background:#E1F5EE;color:#085041;border-color:#9FE1CB}
.err{background:#FCEBEB;color:#A32D2D;border-color:#F7C1C1}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}
.muted{color:#8A8983}
.tree{background:#fff;border:1px solid #E6E5DF;border-radius:12px;padding:16px 18px;margin-bottom:18px}
.row{display:grid;grid-template-columns:210px 1fr 74px;gap:12px;align-items:center;margin-bottom:7px}
.row .name{font-size:12.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.row .track{position:relative;height:18px;background:#FAFAF7;border:1px solid #EFEEE9;border-radius:4px}
.row .bar2{position:absolute;top:0;height:100%;border-radius:3px}
.row .dur{font-size:11px;color:#8A8983;text-align:right}
details{margin:2px 0 8px}
summary{cursor:pointer;font-size:11.5px;color:#5F5E5A;padding:4px 0}
pre{background:#FAFAF7;border:1px solid #EFEEE9;border-radius:8px;padding:10px 12px;font-size:11.5px;
 white-space:pre-wrap;word-break:break-word;margin:6px 0 10px;max-height:220px;overflow:auto}
select{border:1px solid #D3D1C7;border-radius:8px;padding:5px 9px;font:inherit;font-size:12px;background:#fff}
.legend{display:flex;gap:14px;font-size:11px;color:#8A8983;margin-top:10px;flex-wrap:wrap}
.legend i{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:5px}
"""


def _fmt_ms(ms: int | None) -> str:
    if ms is None:
        return "-"
    return f"{ms} ms" if ms < 1000 else f"{ms / 1000:.2f} s"


def _fmt_time(ms: int | None) -> str:
    if not ms:
        return "-"
    return datetime.fromtimestamp(ms / 1000).strftime("%m-%d %H:%M:%S")


def _page(title: str, body: str, status_code: int = 200) -> HTMLResponse:
    doc = (
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{html.escape(title)}</title><style>{CSS}</style></head><body>"
        "<div class='bar-top'><div class='brand'>"
        "<div class='dot-logo'>TL</div><span>TraceLab · 本地链路观测</span></div>"
        "<a href='/observe' style='font-size:12px'>刷新列表</a></div>"
        f"<div class='wrap'>{body}</div></body></html>"
    )
    return HTMLResponse(doc, status_code=status_code)


@router.get("/observe", response_class=HTMLResponse)
def page_list(
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
) -> HTMLResponse:
    """链路列表。"""
    store = get_store()
    traces = store.list_traces(limit=limit, status=status)
    summary = store.summary()

    if traces:
        rows = "".join(
            "<tr>"
            f"<td class='mono muted'>{_fmt_time(t['started_ms'])}</td>"
            f"<td><a href='/observe/traces/{t['id']}'>{html.escape(t['name'])}</a>"
            f"<div class='mono muted' style='font-size:11px'>{t['id']}</div></td>"
            f"<td class='mono'>{html.escape(t['session_id'] or '-')}</td>"
            f"<td><span class='pill {'ok' if t['status'] == 'ok' else 'err'}'>{t['status']}</span></td>"
            f"<td>{_fmt_ms(t['duration_ms'])}</td>"
            f"<td class='mono'>{t['total_tokens'] or '-'}</td>"
            "</tr>"
            for t in traces
        )
    else:
        rows = (
            "<tr><td colspan='6' class='muted' style='text-align:center;padding:28px'>"
            "还没有链路数据，先跑一次 "
            "<span class='mono'>python scripts/demo_trace.py</span></td></tr>"
        )

    body = (
        "<h1>链路列表</h1>"
        "<p class='sub'>每一条 trace 对应一场面试中的一轮交互，点进去看 span 树与瀑布图。</p>"
        "<div class='stats'>"
        f"<div class='stat'><div class='v'>{summary['traces']}</div><div class='k'>链路数</div></div>"
        f"<div class='stat'><div class='v'>{summary['spans']}</div><div class='k'>span 数</div></div>"
        f"<div class='stat'><div class='v'>{summary['slow_spans']}</div><div class='k'>慢 span (&gt;3s)</div></div>"
        f"<div class='stat'><div class='v'>{summary['failed_spans']}</div><div class='k'>失败 span</div></div>"
        "</div>"
        "<form method='get' style='margin-bottom:12px;display:flex;gap:10px;align-items:center'>"
        "<select name='status' onchange='this.form.submit()'>"
        f"<option value='' {'selected' if not status else ''}>全部状态</option>"
        f"<option value='ok' {'selected' if status == 'ok' else ''}>ok</option>"
        f"<option value='error' {'selected' if status == 'error' else ''}>error</option>"
        "</select>"
        f"<span class='muted' style='font-size:11.5px'>共 {len(traces)} 条</span></form>"
        "<table><thead><tr>"
        "<th>时间</th><th>链路</th><th>会话</th><th>状态</th><th>耗时</th><th>token</th>"
        f"</tr></thead><tbody>{rows}</tbody></table>"
    )
    return _page("TraceLab · 链路列表", body)


@router.get("/observe/traces/{trace_id}", response_class=HTMLResponse)
def page_detail(trace_id: str) -> HTMLResponse:
    """单条链路详情：span 树 + 瀑布图 + 输入输出。"""
    store = get_store()
    trace = store.get_trace(trace_id)
    if trace is None:
        return _page(
            "TraceLab · 未找到",
            "<h1>未找到该链路</h1>"
            "<p class='sub'>可能还没写盘，返回列表刷新一次。</p>"
            "<p><a href='/observe'>← 返回列表</a></p>",
            status_code=404,
        )

    spans = store.list_spans(trace_id)
    t0 = min((s["started_ms"] for s in spans), default=trace["started_ms"])
    total = max((s["started_ms"] - t0 + s["duration_ms"] for s in spans), default=1) or 1

    # 计算每个 span 的层级深度（沿 parent_id 向上数）
    id_to_parent = {s["id"]: s["parent_id"] for s in spans}
    depth_of: dict[str, int] = {}
    for s in spans:
        depth, parent, seen = 0, s["parent_id"], set()
        while parent and parent in id_to_parent and parent not in seen:
            seen.add(parent)
            depth += 1
            parent = id_to_parent.get(parent)
        depth_of[s["id"]] = depth

    rows: list[str] = []
    for s in spans:
        fill, stroke = TYPE_COLOR.get(s["type"], ("#E6E5DF", "#8A8983"))
        left = (s["started_ms"] - t0) / total * 100
        width = max(s["duration_ms"] / total * 100, 0.6)
        depth = depth_of.get(s["id"], 0)
        indent = "&nbsp;" * (depth * 4)
        rows.append(
            "<div class='row'>"
            f"<div class='name'>{indent}<span class='mono'>{html.escape(s['name'])}</span></div>"
            "<div class='track'>"
            f"<div class='bar2' style='left:{left:.2f}%;width:{width:.2f}%;background:{fill};"
            f"border:1px solid {stroke}'></div></div>"
            f"<div class='dur'>{_fmt_ms(s['duration_ms'])}</div>"
            "</div>"
        )
        payload = ""
        if s["input"]:
            payload += f"<details><summary>input</summary><pre>{html.escape(s['input'])}</pre></details>"
        if s["output"]:
            payload += f"<details><summary>output</summary><pre>{html.escape(s['output'])}</pre></details>"
        if s["error"]:
            payload += (
                f"<details open><summary style='color:#A32D2D'>error</summary>"
                f"<pre>{html.escape(s['error'])}</pre></details>"
            )
        if payload:
            rows.append(f"<div style='margin-left:{depth * 18 + 4}px'>{payload}</div>")

    legend = "".join(
        f"<span><i style='background:{fill};border:1px solid {stroke}'></i>{name}</span>"
        for name, (fill, stroke) in TYPE_COLOR.items()
    )

    body = (
        f"<h1>{html.escape(trace['name'])}</h1>"
        f"<p class='sub mono'>{trace['id']} · {_fmt_time(trace['started_ms'])} · "
        f"总耗时 {_fmt_ms(trace['duration_ms'])} · token {trace['total_tokens'] or '-'}</p>"
        "<div class='tree'>"
        + "".join(rows)
        + f"<div class='legend'>{legend}</div></div>"
        "<p class='sub'>横向位置是相对时间，长度是耗时。默认仅记录元数据；"
        "不展示输入输出正文，异常仅显示安全错误码。</p>"
        "<p><a href='/observe'>← 返回列表</a></p>"
    )
    return _page(f"TraceLab · {trace['name']}", body)


@router.get("/api/v1/observe/traces")
def api_traces(limit: int = Query(default=50, ge=1, le=500), status: str | None = None) -> dict[str, Any]:
    """列表 JSON，供脚本与后续前端使用。"""
    store = get_store()
    return {
        "ok": True,
        "data": {"items": store.list_traces(limit=limit, status=status),
                 "summary": store.summary()},
        "error": None,
    }


@router.get("/api/v1/observe/traces/{trace_id}")
def api_trace(trace_id: str, response: Response) -> dict[str, Any]:
    store = get_store()
    trace = store.get_trace(trace_id)
    if trace is None:
        response.status_code = 404
        return {"ok": False, "data": None,
                "error": {"code": "RESOURCE_NOT_FOUND", "message": "trace not found", "retryable": False}}
    return {"ok": True, "data": {"trace": trace, "spans": store.list_spans(trace_id)},
            "error": None}
