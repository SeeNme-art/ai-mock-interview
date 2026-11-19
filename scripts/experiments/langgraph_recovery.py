"""EXP-001：独立进程、真实 SQLite、合成数据；不接入正式应用。"""
from __future__ import annotations

import argparse
from contextlib import closing
from importlib.metadata import version
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
from typing import TypedDict
import uuid

# 即使用户机器配置过 LangSmith，也不外发本次实验；子进程禁止 socket 连接。
os.environ["LANGSMITH_TRACING"] = "false"
os.environ["LANGCHAIN_TRACING_V2"] = "false"

MARKER = "SYNTHETIC_PRIVATE_MATERIAL_EXP001_7a8b"
PACKAGES = ("langgraph", "langgraph-checkpoint", "langgraph-checkpoint-sqlite", "langchain-core")


class State(TypedDict, total=False):
    operation_id: str
    step_key: str
    result_id: int
    continued: bool


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def initialize(path: Path) -> None:
    with closing(connect(path)) as conn:
        conn.executescript('''
CREATE TABLE IF NOT EXISTS material(operation_id TEXT PRIMARY KEY, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS visits(id INTEGER PRIMARY KEY, operation_id TEXT, pid INTEGER);
CREATE TABLE IF NOT EXISTS effects(id INTEGER PRIMARY KEY, operation_id TEXT, step_key TEXT);
CREATE TABLE IF NOT EXISTS completed_steps(
 operation_id TEXT, step_key TEXT, result_id INTEGER REFERENCES effects(id),
 PRIMARY KEY(operation_id, step_key));
''')
        conn.execute("INSERT OR IGNORE INTO material VALUES(?,?)", ("op_demo", MARKER))
        conn.commit()


def write_effect(path: Path, state: State, guarded: bool) -> int:
    # 幂等结果与副作用是同一个 SQLite 事务；不模拟远端计费调用。
    with closing(connect(path)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO visits(operation_id,pid) VALUES(?,?)", (state["operation_id"], os.getpid()))
        body = conn.execute("SELECT body FROM material WHERE operation_id=?", (state["operation_id"],)).fetchone()
        require(body is not None and body[0] == MARKER, "synthetic business material missing")
        previous = conn.execute("SELECT result_id FROM completed_steps WHERE operation_id=? AND step_key=?",
                                (state["operation_id"], state["step_key"])).fetchone()
        if guarded and previous:
            conn.commit()
            return previous[0]
        cursor = conn.execute("INSERT INTO effects(operation_id,step_key) VALUES(?,?)",
                              (state["operation_id"], state["step_key"]))
        result_id = cursor.lastrowid
        require(result_id is not None, "missing result id")
        if guarded:
            conn.execute("INSERT INTO completed_steps VALUES(?,?,?)",
                         (state["operation_id"], state["step_key"], result_id))
        conn.commit()
        return result_id


def counts(path: Path) -> dict:
    with closing(connect(path)) as conn:
        return {
            "visits": conn.execute("SELECT COUNT(*) FROM visits").fetchone()[0],
            "effects": conn.execute("SELECT COUNT(*) FROM effects").fetchone()[0],
            "completed_steps": conn.execute("SELECT COUNT(*) FROM completed_steps").fetchone()[0],
        }


def child(args: argparse.Namespace) -> int:
    def deny_connect(*_args, **_kwargs):
        raise RuntimeError("network_disabled_for_experiment")
    socket.socket.connect = deny_connect
    from langgraph.checkpoint.sqlite import SqliteSaver
    from langgraph.graph import END, START, StateGraph
    from langgraph.types import Command, interrupt

    business = args.run_dir / "business.db"
    checkpoint = args.run_dir / ("graph-g2.db" if args.phase == "rebuild" else "graph-g1.db")
    initialize(business)

    def node(state: State) -> dict:
        result_id = write_effect(business, state, args.strategy == "guarded")
        if args.phase == "crash":
            # 事务已 commit；节点还没 return，因此其成功输出尚未进入 checkpoint。
            os._exit(73)
        if args.flow == "interrupt":
            decision = interrupt({"operation_id": state["operation_id"], "action": "continue"})
            require(decision == {"operation_id": state["operation_id"], "decision": "continue"},
                    "resume reference mismatch")
        return {"result_id": result_id, "continued": True}

    builder = StateGraph(State)
    builder.add_node("write_then_wait", node)
    builder.add_edge(START, "write_then_wait")
    builder.add_edge("write_then_wait", END)
    config = {"configurable": {"thread_id": args.thread_id}}
    with SqliteSaver.from_conn_string(str(checkpoint)) as saver:
        graph = builder.compile(checkpointer=saver)
        before = graph.get_state(config)
        if args.phase in {"resume", "recover"} and not before.next:
            print(json.dumps({"error": "NO_PENDING_EXECUTION", "pid": os.getpid()}))
            return 4
        if args.phase == "resume":
            graph.invoke(Command(resume={"operation_id": "op_demo", "decision": "continue"}),
                         config, durability="sync")
        elif args.phase == "recover":
            graph.invoke(None, config, durability="sync")
        elif args.phase != "status":
            graph.invoke({"operation_id": "op_demo", "step_key": "effect_v1"}, config, durability="sync")
        snapshot = graph.get_state(config)
        private_in_checkpoint = any(
            MARKER in repr((item.checkpoint, item.metadata, item.pending_writes))
            for item in saver.list(config)
        )
        require(not private_in_checkpoint, "private marker in decoded checkpoint")
        pending = bool(snapshot.next)
        state_keys = sorted(snapshot.values)
        require(set(state_keys) <= set(State.__annotations__), "unexpected graph state keys")
    # 额外检查保存后的数据库列，包括 blob；只扫描，不将内容打印出来。
    with closing(sqlite3.connect(checkpoint)) as conn:
        for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"):
            require(table.replace("_", "").isalnum(), "unexpected checkpoint table name")
            for row in conn.execute('SELECT * FROM "' + table + '"'):
                for value in row:
                    raw = value if isinstance(value, bytes) else str(value).encode()
                    require(MARKER.encode() not in raw, "private marker in checkpoint storage")
    print(json.dumps({"pid": os.getpid(), "pending": pending, "state_keys": state_keys,
                      "checkpoint_marker_absent": True, **counts(business)}))
    return 0


def run(args: argparse.Namespace) -> int:
    args.run_dir.mkdir(parents=True, exist_ok=False)
    results = []

    def invoke(folder: Path, phase: str, strategy: str, flow: str, thread: str = "s_demo:g1", code: int = 0):
        folder.mkdir(exist_ok=True)
        command = [sys.executable, str(Path(__file__).resolve()), "--child", "--run-dir", str(folder),
                   "--phase", phase, "--strategy", strategy, "--flow", flow, "--thread-id", thread]
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        process = subprocess.run(command, capture_output=True, text=True, env=env, timeout=30)
        require(MARKER not in process.stdout + process.stderr, "private marker in process output")
        require(process.returncode == code, "child exit mismatch: " + phase + ":" + str(process.returncode))
        return json.loads(process.stdout) if process.stdout.strip() else {}

    for strategy, expected in (("naive", 2), ("guarded", 1)):
        folder = args.run_dir / strategy
        first = invoke(folder, "start", strategy, "interrupt")
        require(first["pending"] and first["effects"] == 1, "start did not pause after first effect")
        wrong = invoke(folder, "resume", strategy, "interrupt", "wrong-thread", 4)
        after_wrong = invoke(folder, "status", strategy, "interrupt")
        require(wrong["error"] == "NO_PENDING_EXECUTION" and after_wrong["effects"] == 1,
                "wrong thread modified business state")
        resumed = invoke(folder, "resume", strategy, "interrupt")
        require(first["pid"] != resumed["pid"], "resume did not use a new process")
        require(not resumed["pending"] and resumed["effects"] == expected and resumed["visits"] == 2,
                "unexpected interrupt replay counts")
        results.append({"case": strategy + "_interrupt_restart", "status": "pass", "before": first,
                        "wrong_thread_rejected": True, "after": resumed})

    folder = args.run_dir / "crash"
    invoke(folder, "crash", "guarded", "direct", code=73)
    before = invoke(folder, "status", "guarded", "direct")
    require(before["pending"] and before["effects"] == 1 and before["visits"] == 1,
            "crash window did not leave a committed effect with pending graph work")
    recovered = invoke(folder, "recover", "guarded", "direct")
    require(not recovered["pending"] and recovered["effects"] == 1 and recovered["visits"] == 2,
            "recovery duplicated committed effect")
    results.append({"case": "hard_exit_after_business_commit", "status": "pass", "expected_exit": 73,
                    "before": before, "after": recovered})
    rebuilt = invoke(folder, "rebuild", "guarded", "direct", "s_demo:g2")
    require(not rebuilt["pending"] and rebuilt["effects"] == 1 and rebuilt["visits"] == 3,
            "fresh graph generation duplicated business effect")
    results.append({"case": "fresh_checkpoint_generation_reuses_business_result", "status": "pass",
                    "before": recovered, "after": rebuilt})
    report = {"experiment": "EXP-001", "synthetic_only": True, "network_disabled_in_children": True,
              "python": sys.version.split()[0], "versions": {p: version(p) for p in PACKAGES},
              "cases": results, "all_passed": True,
              "limits": ["single_process_per_command", "sqlite_effect_only", "no_real_model",
                         "no_remote_exactly_once_claim", "no_production_workflow"]}
    (args.run_dir / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, default=Path("data/experiments") / uuid.uuid4().hex)
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--phase", choices=("start", "resume", "crash", "recover", "status", "rebuild"), default="start")
    parser.add_argument("--strategy", choices=("naive", "guarded"), default="guarded")
    parser.add_argument("--flow", choices=("interrupt", "direct"), default="interrupt")
    parser.add_argument("--thread-id", default="s_demo:g1")
    args = parser.parse_args()
    if args.child:
        return child(args)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
