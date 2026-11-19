# AI 模拟面试系统

面向 AI 应用 / Agent 初级岗位求职者的模拟面试项目，目标是根据岗位与个人材料提问、针对回答追问，并生成有证据的分类反馈。首版采用本地浏览器文字界面。
这是个人学习与求职展示项目。当前已有 P0 配置、日志与链路观测代码；面试业务仍待实现，以下目标不代表已经上线或通过验收。

> 当前设计：[设计总入口](docs/项目设计/README.md) · [当前交接](docs/项目设计/记录/交接文档.md)
> 过程证据：[开发日志](docs/项目设计/记录/开发日志.md) · [问题记录](docs/项目设计/记录/问题记录.md) · [实验记录](docs/项目设计/记录/实验记录.md)
> 后续方向：[演进路线](docs/项目设计/06-后续演进路线.md)；历史问题：[旧问题记录](docs/问题记录.md)
> 原型：`demo/overview.html` 与 `demo/index.html`，含模拟数据与旧业务规则，不能当作当前功能已实现的证明。

---

## 当前进度

| 阶段 | 内容 | 状态 |
|---|---|---|
| 历史 P0 | 骨架 / 配置 / 日志 / 链路采集 / TraceLab Viewer | 已有代码，本轮未重新运行验证 |
| v2.0 设计 | 五批决策、业务与接口规范、评测计划、记录体系 | 文档已整合；不是产品发布 |
| M0–M5 | 现状核对 → 小型 CLI 闭环 → 本地 Web → 材料与题库 → 验收交付 | 待按新版计划实施 |

阶段顺序和验收条件以[评测与交付计划](docs/项目设计/05-评测与交付计划.md)为准。下方启动说明保留自 P0，M0 需复核实际环境；语音依赖属于后续方向。

## 环境

- 项目目前按 Python **3.12** 配置；实际依赖与兼容性在 M0 核对。
- 虚拟环境目录为 `.venv/`；历史 P0 只安装了基础依赖，当前安装情况尚未复核。

```bash
# 首次
source .venv/Scripts/activate          # Git Bash
.venv\Scripts\activate                 # CMD / PowerShell

pip install -r requirements.txt        # 或：uv pip install -r requirements.txt
```

仓库现有依赖文件分四份，按实际需要安装：

| 文件 | 用途 | 何时需要 |
|---|---|---|
| `requirements.txt` | fastapi / uvicorn / pydantic | P0 基础与 M0 复核 |
| `requirements-llm.txt` | langchain / faiss | 模型与检索实验，先核对内容及版本 |
| `requirements-embed.txt` | sentence-transformers | 启用本地 embedding 时 |
| `requirements-voice.txt` | edge-tts / faster-whisper | 后续语音方向，非首版必需 |

## 跑起来

```bash
# 1) 生成三条模拟链路（不调用模型）
.venv/Scripts/python scripts/demo_trace.py

# 2) 启动服务
PYTHONPATH=src .venv/Scripts/python -m uvicorn app.main:app --reload --port 8000

# 3) 打开观测台
#    http://127.0.0.1:8000/observe
```

## 目录

```
src/app/
├── main.py                     # FastAPI 入口（配置→日志→采集→路由）
├── config.py                   # pydantic-settings 读 .env
├── core/
│   ├── context.py              # trace_id / span_id / session_id 上下文变量
│   ├── logging_setup.py        # JSON 日志 + 控制台彩色 + PII 打码
│   └── tracing.py              # start_trace() / span() 上下文管理器
└── observability/
    ├── store.py                # SQLite 两表 + 后台线程批量写盘
    └── viewer/routes.py        # /observe 列表页与详情页
scripts/demo_trace.py           # 生成模拟链路
```

## TraceLab 是什么

自建的本地链路观测台，用于查看 trace 和 span。它只描述本地观测存储；业务使用云端模型时，仍可能外发材料并产生调用费用。

- 每条 trace 对应一轮交互，子节点是 span（模型调用、检索、语音合成…）
- 页面提供 span 树 + 瀑布图，横向位置是相对时间，长度是耗时
- 当前代码默认保存输入输出的前 120 字预览，与新设计的默认不保存正文约定存在差异；见 [ISSUE-001](docs/项目设计/记录/问题记录.md)。接入真实材料前须修复并验证。

## 日志

- 控制台：`21:19:04 INFO [facd9402] trace.end duration_ms=761`
- 落盘：`logs/app.jsonl`，单行 JSON，可 grep、可入库
- 铁律：INFO 不落 Prompt 全文；落盘前过 PII 掩码；`except` 必须先 log 再处理
