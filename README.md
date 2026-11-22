# AI 模拟面试系统

面向 AI 应用 / Agent 初级岗位求职者的模拟面试项目，目标是根据岗位与个人材料提问、针对回答追问，并生成有证据的分类反馈。首版采用本地浏览器文字界面。
这是个人学习与求职展示项目。当前已在本机用合成数据验证知识专项从规划、逐题问答和评分，到结场模板报告的命令行链路。完整首版进度见[当前交接](docs/项目设计/记录/交接文档.md)。

> 当前设计：[设计总入口](docs/项目设计/README.md) · [当前交接](docs/项目设计/记录/交接文档.md)
> 过程证据：[开发日志](docs/项目设计/记录/开发日志.md) · [问题记录](docs/项目设计/记录/问题记录.md) · [实验记录](docs/项目设计/记录/实验记录.md)
> 后续方向：[演进路线](docs/项目设计/06-后续演进路线.md)；历史问题：[旧问题记录](docs/问题记录.md)
> 原型：`demo/overview.html` 与 `demo/index.html`，含模拟数据与旧业务规则，不能当作当前功能已实现的证明。

---

## 当前进度

| 阶段 | 内容 | 状态 |
|---|---|---|
| P0 基础 | 配置 / 元数据日志 / 链路采集 / TraceLab Viewer | 本机合成回归已验证，本批版本记录见 Git 历史 |
| M0 / X02 | LangGraph 暂停、跨进程恢复与业务写入去重 | [独立实验](docs/项目设计/记录/阶段审阅-LangGraph恢复实验.md)已完成并获审阅通过；版本记录见 Git 历史，未接入正式业务 |
| M0 / X03–X04 试测 | DeepSeek Flash 合成样本、响应校验与预算账本 | [真实试测](docs/项目设计/记录/阶段审阅-模型试测.md)六项一次通过，已获用户审批 |
| M1 第一批 | 10 道可溯源种子题、SQLite 导入和候选筛选 | [首批题库](docs/项目设计/记录/阶段审阅-M1题库基础.md)已获审批并启用 |
| M1 第二批 | SQLite 筛选后用 FAISS 排序、索引版本校验 | [本地检索](docs/项目设计/记录/阶段审阅-M1向量检索.md)已完成命令行验证并通过审阅 |
| M1 第三批 | 纯文本预览、人工确认画像、会话材料快照 | [材料确认](docs/项目设计/记录/阶段审阅-M1材料确认.md)已完成本地验证并通过审阅 |
| M1 第四批 | 维度与证据校验、确定性评分聚合 | [评分汇总](docs/项目设计/记录/阶段审阅-M1评分聚合.md)已通过合成样本验收和作者审阅 |
| M2 第一批 | 会话计划生成、查看和开始前重建 | [会话规划](docs/项目设计/记录/阶段审阅-M2会话规划.md)已完成本地验证并通过审阅 |
| M2 第二批 | 开始面试、保存首题和评分依据、按 ID 读取进度 | [首题准备](docs/项目设计/记录/阶段审阅-M2首题准备.md)已完成本地验证并通过审阅 |
| M2 第三批 | 保存首答、同轮防重、跨进程读取 | [首答保存](docs/项目设计/记录/阶段审阅-M2首答保存.md)已完成本地验证并通过审阅 |
| M2 第四批 | 校验追问建议、发布追问、最多两轮后留待评分 | [追问决策](docs/项目设计/记录/阶段审阅-M2追问决策.md)已用本地样例验证并通过审阅 |
| M2 第五批 | 校验单题评分、保存分数与证据、收尾本题 | [单题评分](docs/项目设计/记录/阶段审阅-M2单题评分.md)已用本地样例验证并通过审阅 |
| M2 第六批 | 按冻结计划推进下一题、结场与进度计数 | [会话推进](docs/项目设计/记录/阶段审阅-M2会话推进.md)已完成本地验证并通过审阅 |
| M2 第七批 | 结场模板报告、分类均分、证据复盘和版本读取 | [整场报告](docs/项目设计/记录/阶段审阅-M2整场报告.md)已完成合成本地验证，待阶段审阅 |
| v2.0 设计 | 五批决策、业务与接口规范、评测计划、记录体系 | 文档已整合；不是产品发布 |
| M0–M5 | 现状核对 → 小型 CLI 闭环 → 本地 Web → 材料与题库 → 验收交付 | 待按新版计划实施 |

阶段顺序和验收条件见[评测与交付计划](docs/项目设计/05-评测与交付计划.md)。独立的 LangGraph、真实模型和题库检索验证已完成，真实模型接入和 Web 流程仍待开发。

## 环境

- 本轮实际环境：Windows x64、Python **3.12.14**；虚拟环境目录 `.venv/`。
- P0 版本快照：[requirements-p0.lock.txt](requirements-p0.lock.txt)。M1 第二批在全新环境中与检索依赖一起安装并通过回归。

```powershell
# 新电脑：先安装 Python 3.12，在项目根目录创建环境。
# 已有 .venv 时直接使用，不重复创建。
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-p0.lock.txt
```

仓库现有依赖文件分四份，按实际需要安装：

| 文件 | 用途 | 何时需要 |
|---|---|---|
| `requirements.txt` | fastapi / uvicorn / pydantic | P0 基础与 M0 复核 |
| `requirements-llm.txt` | LangChain 等前期候选依赖 | 模型实验按对应阶段文档安装 |
| `requirements-embed.txt` | FAISS / FastEmbed / ONNX Runtime | 运行本地题库检索，与 P0 依赖一同安装 |
| `requirements-voice.txt` | edge-tts / faster-whisper | 后续语音方向，非首版必需 |

## 跑起来

```powershell
# 在项目根目录执行；使用独立演示库，避免混入旧观测数据。
$env:PYTHONPATH = "src"
$env:OBSERVE_DB = "$PWD/data/p0-demo/observe.db"
$env:LOG_DIR = "$PWD/logs/p0-demo"
$env:OBSERVE_STORE_IO = "false"

# 生成三条模拟链路（不调用模型；重复运行会追加三条）
.\.venv\Scripts\python.exe scripts/demo_trace.py

# 在同一终端启动本机服务，Ctrl+C 停止。
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000

# 3) 打开观测台
#    http://127.0.0.1:8000/observe
```

回归检查：`.\.venv\Scripts\python.exe -m unittest discover -s tests -v`。使用临时数据库、合成内容和随机本机端口，不需要密钥；其中一次工具失败是演示脚本故意注入的案例。

本地题库的模型下载、索引重建、查询和检索测试见[检索复现步骤](docs/项目设计/记录/阶段审阅-M1向量检索.md#怎样复现)。

材料确认可使用 `scripts/material_profile.py`，从本地表单或 UTF-8 文本进入预览，确认后可重新读取会话快照。操作示例和合成验证见[材料确认复现步骤](docs/项目设计/记录/阶段审阅-M1材料确认.md#怎样复现)。

已确认材料的会话可用 `scripts/interview_plan.py` 生成和查看计划。复现方法见[会话规划审阅单](docs/项目设计/记录/阶段审阅-M2会话规划.md#怎样复现)。

计划生成后可用 `scripts/interview.py start` 准备首道审核原题，用 `state` 读取已保存进度。环境和命令见[首题准备复现步骤](docs/项目设计/记录/阶段审阅-M2首题准备.md#怎样复现)。

拿到正式题面后，可用 `scripts/interview.py answer` 提交 UTF-8 文本，再用 `state` 读回已提交答案和待处理操作。[首答复现步骤](docs/项目设计/记录/阶段审阅-M2首答保存.md#怎样复现)

已保存的回答可用 `scripts/interview.py process-answer` 加本地样例验收追问分支和两轮上限。[追问复现步骤](docs/项目设计/记录/阶段审阅-M2追问决策.md#怎样复现)

待评分的题可用 `scripts/interview.py score-answer` 验证评分样例与结果落库。面试中 `state` 展示评分状态。[单题评分复现步骤](docs/项目设计/记录/阶段审阅-M2单题评分.md#怎样复现)

已答题收尾后可用 `scripts/interview.py advance` 按原计划准备下一题，用 `state` 查看题号与整场进度。[会话推进复现步骤](docs/项目设计/记录/阶段审阅-M2会话推进.md#怎样复现)

会话结束后用 `scripts/interview.py generate-report <session_id>` 保存整场模板报告，再用 `report <session_id> --revision 1` 读回同一版本。[整场报告复现步骤](docs/项目设计/记录/阶段审阅-M2整场报告.md#怎样复现)

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
- 当前只写入元数据，输入输出为空，原始异常转为安全错误码；`OBSERVE_STORE_IO=true` 会拒绝启动。字段白名单见 `src/app/core/telemetry.py`，调用方不得把用户内容填入事件名或元数据标签。
- 旧库中的正文读取时被隐藏，但本轮没有删除历史数据。未来 checkpoint、模型 callback 等路径尚未实现，需要另行验证。

## 日志

- 控制台：`21:19:04 INFO [facd9402] trace.end duration_ms=761`
- 落盘：`logs/app.jsonl`，单行 JSON，可 grep、可入库
- 普通日志只接受白名单元数据；原始异常消息和 traceback 不落盘。外部日志消息被替换为 `external.log`，调试信息减少是当前最小化策略的代价。
