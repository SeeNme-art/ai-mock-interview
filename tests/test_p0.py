"""P0 合成数据回归；不依赖 httpx、pytest、模型密钥或真实材料。"""
from __future__ import annotations

import json
from contextlib import closing
import logging
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import build_opener, ProxyHandler

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from app.config import Settings
from app.core.logging_setup import ConsoleFormatter, JsonFormatter
from app.core.tracing import span, start_trace
from app.observability.store import ObserveStore, SpanRecord, TraceRecord

MARKER = "SYNTHETIC_PRIVATE_BODY_9f3d"


class TelemetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.store = ObserveStore(self.path / "observe.db", flush_size=1)

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def test_default_mode_and_legacy_full_mode_rejected(self) -> None:
        self.assertFalse(Settings(_env_file=None, observe_store_io=False).observe_store_io)
        with self.assertRaises(ValueError):
            Settings(_env_file=None, observe_store_io=True)

    def test_direct_store_drops_bodies_without_serializing(self) -> None:
        class CannotSerialize:
            def __str__(self) -> str:
                raise AssertionError("body must never be serialized")

        self.store.submit_span(SpanRecord("sp_1", "t_1", None, "test.node", "llm", 1, 2,
                                        input=CannotSerialize(), output=MARKER, error=MARKER))
        self.store.flush()
        with closing(sqlite3.connect(self.path / "observe.db")) as conn:
            row = conn.execute("SELECT input,output,error FROM span").fetchone()
        self.assertEqual(row, (None, None, "LEGACY_ERROR_REDACTED"))

    def test_legacy_body_is_hidden_but_not_claimed_erased(self) -> None:
        with closing(sqlite3.connect(self.path / "observe.db")) as conn:
            conn.execute("INSERT INTO span VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                         ("old", "t_old", None, "old.node", "llm", 1, 2, MARKER, MARKER, "error", MARKER))
            conn.commit()
        for row in (self.store.get_span("old"), self.store.list_spans("t_old")[0]):
            self.assertIsNone(row["input"])
            self.assertIsNone(row["output"])
            self.assertEqual(row["error"], "LEGACY_ERROR_REDACTED")
        with closing(sqlite3.connect(self.path / "observe.db")) as conn:
            self.assertEqual(conn.execute("SELECT input FROM span").fetchone()[0], MARKER)

    def test_trace_exception_preserves_behavior_and_metadata(self) -> None:
        with patch("app.core.tracing.get_store", return_value=self.store):
            with self.assertRaisesRegex(RuntimeError, MARKER):
                with start_trace("test.operation", session_id="s_test"):
                    with span("llm.invoke", "llm", input=MARKER) as handle:
                        handle.output = MARKER
                        handle.tokens_in, handle.tokens_out = 12, 7
                        raise RuntimeError(MARKER)
        self.store.flush()
        trace = self.store.list_traces()[0]
        node = self.store.list_spans(trace["id"])[0]
        self.assertEqual((trace["status"], trace["total_tokens"], node["error"]),
                         ("error", 19, "RUNTIME_ERROR"))
        with closing(sqlite3.connect(self.path / "observe.db")) as conn:
            self.assertNotIn(MARKER, "\n".join(conn.iterdump()))

    def test_formatters_discard_nested_body_and_exception_text(self) -> None:
        record = logging.LogRecord("app", logging.ERROR, "test.py", 1, MARKER, (),
                                   (ValueError, ValueError(MARKER), None))
        record.event = "test.failed"
        record.fields = {"answer": MARKER, "nested": {"prompt": MARKER}, "error_code": {},
                         "chars": 23, "status": "error"}
        for formatter in (JsonFormatter(), ConsoleFormatter()):
            rendered = formatter.format(record)
            self.assertNotIn(MARKER, rendered)
            self.assertIn("VALUE_ERROR", rendered)
            self.assertIn("23", rendered)

    def test_write_failure_rolls_back_entire_batch(self) -> None:
        with closing(sqlite3.connect(self.path / "observe.db")) as conn:
            conn.execute("DROP TABLE span")
            self.store._write(conn, [
                ("trace", TraceRecord("failed_batch", "test.operation", None, "ok", 1, 2)),
                ("span", SpanRecord("missing_table", "failed_batch", None, "test.node", "llm", 1, 2)),
            ])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM trace").fetchone()[0], 0)


class ServerTests(unittest.TestCase):
    def test_demo_script_writes_three_synthetic_traces(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env = os.environ.copy()
            env.update(PYTHONPATH=str(ROOT / "src"), PYTHONIOENCODING="utf-8")
            program = '''
import runpy,sys
from pathlib import Path
from app import config
root=Path(sys.argv[1])
config.get_settings=lambda: config.Settings(_env_file=None, observe_store_io=False,
    log_dir=root/'logs',data_dir=root/'data',observe_db=root/'data/observe.db')
runpy.run_path('scripts/demo_trace.py',run_name='__main__')
'''
            result = subprocess.run([sys.executable, "-c", program, directory], env=env, cwd=ROOT,
                                    capture_output=True, timeout=20)
            self.assertEqual(result.returncode, 0, "synthetic demo failed")
            with closing(sqlite3.connect(Path(directory) / "data/observe.db")) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM trace").fetchone()[0], 3)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM span").fetchone()[0], 11)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM trace WHERE status='error'").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM span WHERE input IS NOT NULL OR output IS NOT NULL").fetchone()[0], 0)

    def test_import_has_no_database_or_log_side_effect(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env = os.environ.copy()
            env.update(PYTHONPATH=str(ROOT / "src"), LOG_DIR=directory + "/logs",
                       DATA_DIR=directory + "/data", OBSERVE_DB=directory + "/data/observe.db",
                       OBSERVE_STORE_IO="false")
            program = ("from app import config; config.get_settings=lambda: config.Settings(_env_file=None); "
                       "import app.main")
            result = subprocess.run([sys.executable, "-c", program], env=env, cwd=ROOT,
                                    capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_marker_absent_from_disk_and_console(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT / "src")
            program = '''
import logging, sqlite3, sys
from pathlib import Path
from app.config import Settings
from app.core.logging_setup import setup_logging, log_event
from app.core.tracing import init_tracing, shutdown_tracing, start_trace, span
root=Path(sys.argv[1])
settings=Settings(_env_file=None, observe_store_io=False, log_dir=root/'logs',
                  data_dir=root/'data', observe_db=root/'data/observe.db')
setup_logging('INFO',settings.log_dir)
init_tracing(settings)
marker=sys.argv[2]
try:
    with start_trace('test.operation'):
        with span('llm.invoke','llm',input=marker) as handle:
            handle.output=marker
            log_event('user.answer.received',answer=marker,nested={'prompt':marker},chars=23)
            raise ValueError(marker)
except ValueError:
    logging.getLogger('external').exception(marker)
shutdown_tracing()
with sqlite3.connect(settings.observe_db) as conn:
    assert marker not in '\\n'.join(conn.iterdump())
'''
            result = subprocess.run([sys.executable, "-c", program, directory, MARKER], env=env,
                                    cwd=ROOT, capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, "synthetic telemetry subprocess failed")
            self.assertNotIn(MARKER.encode(), result.stdout + result.stderr)
            self.assertNotIn(MARKER, (Path(directory) / "logs/app.jsonl").read_text(encoding="utf-8"))

    def run_server(self, observe_ui: bool, env_name: str = "test") -> None:
        with tempfile.TemporaryDirectory() as directory:
            location = Path(directory)
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            env = os.environ.copy()
            env.update(PYTHONPATH=str(ROOT / "src"), ENV=env_name,
                       LOG_DIR=str(location / "logs"), DATA_DIR=str(location / "data"),
                       OBSERVE_DB=str(location / "data" / "observe.db"),
                       OBSERVE_UI=str(observe_ui).lower(), OBSERVE_STORE_IO="false")
            # 不读取开发者 .env；P0 测试仅使用临时目录和合成内容。
            program = (
                "from app import config; config.get_settings=lambda: config.Settings(_env_file=None); "
                "import uvicorn; uvicorn.run('app.main:app',host='127.0.0.1',port=" + str(port) +
                ",access_log=False)"
            )
            opener = build_opener(ProxyHandler({}))
            with (location / "server.log").open("wb") as output:
                process = subprocess.Popen([sys.executable, "-c", program], cwd=ROOT, env=env,
                                           stdout=output, stderr=subprocess.STDOUT)
                try:
                    base = f"http://127.0.0.1:{port}"
                    deadline = time.monotonic() + 15
                    while True:
                        try:
                            with opener.open(base + "/healthz", timeout=1) as response:
                                health = json.load(response)
                            break
                        except (URLError, TimeoutError):
                            if process.poll() is not None or time.monotonic() >= deadline:
                                self.fail("P0 server failed to start; inspect synthetic test logs")
                            time.sleep(0.1)
                    enabled = observe_ui and env_name in {"dev", "local", "test"}
                    self.assertEqual(health["data"]["observe_ui"], enabled)
                    self.assertEqual(health["data"]["status"], "healthy")
                    if enabled:
                        for route in ("/observe", "/api/v1/observe/traces"):
                            with opener.open(base + route, timeout=2) as response:
                                self.assertEqual(response.status, 200)
                        with self.assertRaises(HTTPError) as invalid:
                            opener.open(base + "/api/v1/observe/traces?limit=-1", timeout=2)
                        self.assertEqual(invalid.exception.code, 422)
                        for route in ("/observe/traces/not_found", "/api/v1/observe/traces/not_found"):
                            with self.assertRaises(HTTPError) as missing:
                                opener.open(base + route, timeout=2)
                            self.assertEqual(missing.exception.code, 404)
                    else:
                        with self.assertRaises(HTTPError) as caught:
                            opener.open(base + "/observe", timeout=2)
                        self.assertEqual(caught.exception.code, 404)
                finally:
                    process.terminate()
                    process.wait(timeout=10)

    def test_real_http_health_and_observe(self) -> None:
        self.run_server(True)

    def test_observe_switch(self) -> None:
        self.run_server(False)

    def test_non_dev_does_not_mount_observe(self) -> None:
        self.run_server(True, "production")


if __name__ == "__main__":
    unittest.main()
