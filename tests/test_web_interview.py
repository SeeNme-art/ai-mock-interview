from pathlib import Path
from contextlib import closing
from functools import partial
from io import BytesIO
import json
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from fastapi.testclient import TestClient
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from app.config import Settings
from app.db.business import MIGRATIONS, SessionError
from app.db.sqlite import connect
from app.kb.seed import read_seed
from app.kb.store import import_seed, list_questions
from app.main import create_app
from app.services.answers import accept_answer
from app.services.budget import CloudLimits, PriceSnapshot, Usage, configure_budget, invoke_budgeted
from app.services.document_text import MAX_DOCUMENT_BYTES
from app.services.model_gateway import DeepSeekGateway
from app.schemas.scoring import DIMENSIONS


def synthetic_docx(paragraphs, *, preamble=""):
    from xml.sax.saxutils import escape

    xml = "".join(f"<w:p><w:r><w:t>{escape(line)}</w:t></w:r></w:p>" for line in paragraphs)
    output = BytesIO()
    with ZipFile(output, "w") as package:
        package.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        package.writestr("word/document.xml", preamble + '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                         f"<w:body>{xml}</w:body></w:document>")
    return output.getvalue()


def synthetic_pdf(*, text=None, pages=1, encrypted=False):
    output = BytesIO()
    writer = PdfWriter()
    for _ in range(pages):
        page = writer.add_blank_page(width=595, height=842)
        if text:
            page[NameObject("/Resources")] = DictionaryObject({
                NameObject("/Font"): DictionaryObject({
                    NameObject("/F1"): DictionaryObject({
                        NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
                        NameObject("/BaseFont"): NameObject("/Helvetica"),
                    }),
                }),
            })
            stream = DecodedStreamObject()
            stream.set_data(f"BT /F1 12 Tf 40 780 Td ({text}) Tj ET".encode("ascii"))
            page.replace_contents(stream)
    if encrypted:
        writer.encrypt("fixture-password")
    writer.write(output)
    return output.getvalue()


class WebInterviewTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.settings = Settings(_env_file=None, env="test", data_dir=self.root, log_dir=self.root / "logs",
                                 observe_db=self.root / "observe.db", observe_ui=False)
        self.client = self.new_client()

    def new_client(self):
        with patch("app.main.get_settings", return_value=self.settings):
            return TestClient(create_app(), base_url="http://127.0.0.1", client=("127.0.0.1", 37100))

    def request(self, method, path, *, key=None, body=None, status=200):
        headers = {"Idempotency-Key": key} if key else {}
        response = self.client.request(method, "/api/v1" + path, headers=headers, json=body)
        self.assertEqual(response.status_code, status, response.text)
        return response.json()["data"] if status < 300 else response.json()["error"]

    def upload(self, content, format="docx", *, key="upload"):
        return self.client.post("/api/v1/drafts/document", params={"format": format}, content=content,
                                headers={"Idempotency-Key": key, "Content-Type": "application/octet-stream"})

    def test_docx_preview_confirmation_keeps_only_selected_fields(self):
        document = synthetic_docx(["岗位描述：维护检索", "技能：Python、SQLite", "项目做法：比较索引性能",
                                   "项目结果：覆盖 37 条样本", "未确认句 RAW_PRIVATE_MARKER contact@example.test"])
        response = self.upload(document)
        self.assertEqual(response.status_code, 200, response.text)
        preview = response.json()["data"]
        self.assertEqual(self.upload(document).json()["data"], preview)
        self.assertEqual(self.upload(synthetic_docx(["其他材料"])).status_code, 409)
        self.assertIn("RAW_PRIVATE_MARKER", preview["source_text"])
        self.assertNotIn("contact@example.test", response.text)
        material = preview["material"]
        self.assertEqual(material["projects"][0]["result"], "覆盖 37 条样本")
        material["projects"][0]["result"] = "人工核对后确认覆盖 37 条样本"
        session = self.request("POST", "/sessions", key="confirm", status=201,
                               body={"draft_id": preview["draft_id"], "material": material, "mode": "mixed"})
        state = self.request("GET", f"/sessions/{session['session_id']}/state")
        self.assertEqual(state["source"], "docx")
        self.assertNotIn(b"RAW_PRIVATE_MARKER", (self.root / "interviews.db").read_bytes())
        ambiguous = self.upload(synthetic_docx(["项目名称：A", "项目做法：搭索引", "项目名称：B", "项目结果：50 次"]),
                                key="ambiguous")
        self.assertEqual(ambiguous.json()["data"]["material"]["projects"], [])

    def test_pdf_text_and_manual_entry(self):
        response = self.upload(synthetic_pdf(text="Checked 37 rows."), "pdf")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["data"]["source_text"], "Checked 37 rows.")
        start = self.request("POST", "/drafts/preview", key="manual", body={"source": "form"})
        self.assertEqual(start["material"]["skills"], [])
        self.request("POST", "/sessions", key="confirm", status=201, body={
            "draft_id": start["draft_id"], "material": {"skills": ["SQLite"]}, "mode": "knowledge_only"})

    def test_document_failures_are_safe_and_stream_size_is_bounded(self):
        cases = [
            (b"RAW_PRIVATE_MARKER", "pdf", 422, "DOCUMENT_INVALID"),
            (b"RAW_PRIVATE_MARKER", "docx", 422, "DOCUMENT_INVALID"),
            (synthetic_pdf(), "pdf", 422, "DOCUMENT_NO_TEXT"),
            (synthetic_pdf(encrypted=True), "pdf", 422, "DOCUMENT_ENCRYPTED"),
            (synthetic_pdf(pages=21), "pdf", 422, "DOCUMENT_LIMIT"),
            (synthetic_docx(["A" * 16001]), "docx", 422, "DOCUMENT_LIMIT"),
            (synthetic_docx(["A"], preamble="<!DOCTYPE document>"), "docx", 422, "DOCUMENT_INVALID"),
            (b"A" * (MAX_DOCUMENT_BYTES + 1), "docx", 413, "DOCUMENT_TOO_LARGE"),
            (iter([b"A" * MAX_DOCUMENT_BYTES, b"A"]), "docx", 413, "DOCUMENT_TOO_LARGE"),
        ]
        for content, format, status, code in cases:
            with self.subTest(format=format, code=code):
                response = self.upload(content, format)
                self.assertEqual(response.status_code, status, response.text)
                self.assertEqual(response.json()["error"]["code"], code)
                self.assertNotIn("RAW_PRIVATE_MARKER", response.text)

    def configured(self, *, source="example"):
        preview = self.request("POST", "/drafts/preview", key="preview", body={"source": source})
        confirmed = {"draft_id": preview["draft_id"], "material": preview["material"],
                     "mode": "mixed" if source == "example" else "knowledge_only"}
        new = self.request("POST", "/sessions", key="confirm", body=confirmed, status=201)
        session = new["session_id"]
        plan = self.request("PATCH", f"/sessions/{session}/config", key="plan",
                            body={"expected_revision": new["revision"], "choices": {}}, status=202)
        return session, plan["revision"], confirmed

    def test_existing_bank_upgrades_and_respects_five_and_eight_question_capacity(self):
        path = self.root / "question_bank.db"
        bank = read_seed(ROOT / "data/seeds/knowledge_questions.json")
        original_ids = {101, 102, 103, 104, 201, 202, 203, 301, 302, 303}
        legacy = [question for question in bank.questions if question.stable_faiss_id in original_ids]
        source_ids = {reference for question in legacy for reference in question.source_refs}
        legacy_sources = [source for source in bank.sources if source.id in source_ids]
        self.assertEqual(len(legacy), 10)
        import_seed(path, bank.model_copy(update={"questions": legacy, "sources": legacy_sources}))
        session, revision, _ = self.configured(source="form")
        self.assertEqual(len(list_questions(path)), 30)
        route = f"/sessions/{session}/config"
        plan = self.request("PATCH", route, key="single-five", status=202,
                            body={"expected_revision": revision, "choices": {"directions": ["rag"], "difficulty": "applied"}})
        self.assertEqual((plan["counts"]["knowledge"], plan["question_count"]), (5, 5))
        error = self.request("PATCH", route, key="single-eight", status=409, body={
            "expected_revision": plan["revision"], "choices": {"directions": ["rag"], "question_count": 8}})
        self.assertEqual(error["code"], "QUESTION_UNAVAILABLE")
        wider = self.request("PATCH", route, key="two-eight", status=202, body={
            "expected_revision": plan["revision"], "choices": {"directions": ["rag", "agent"], "question_count": 8}})
        self.assertEqual((wider["counts"]["knowledge"], len(wider["items"])), (8, 8))

    def test_cloud_mode_is_explicit_and_uses_validated_feedback(self):
        contexts = []

        def transport(body, _key, _timeout):
            request = json.loads(body)
            context = json.loads(request["messages"][1]["content"])
            contexts.append(context)
            exchanges = context["exchanges"]
            ref = {"exchange_id": exchanges[-1]["id"], "quote": exchanges[-1]["answer"]}
            if "rubric" in context:
                rubric, task = context["rubric"], context["task_type"]
                active = set(rubric["applicable_dimensions"])
                result = {"task_type": task, "rubric_version": rubric["rubric_version"], "dimensions": {
                    name: {"applicable": name in active, "value": 3 if name in active else None,
                           "reason": "有具体核对步骤", "evidence": [ref] if name in active else []}
                    for name in DIMENSIONS[task]
                }}
            elif len(exchanges) == 1:
                result = {"should_probe": True, "purpose": "deepen", "reason": "核对具体步骤",
                          "evidence_refs": [ref], "probe_question": "你会记录哪两项检查结果？"}
            else:
                result = {"should_probe": False, "reason": "已给出检查步骤"}
            payload = {"model": request["model"], "usage": {"prompt_tokens": 180, "completion_tokens": 20, "total_tokens": 200},
                       "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps(result)}}]}
            return 200, json.dumps(payload).encode()

        self.settings = self.settings.model_copy(update={"deepseek_api_key": "fake-credential", "llm_web_enabled": True,
                                                         "llm_web_total_nanos": 50_000_000,
                                                         "llm_web_session_nanos": 20_000_000, "llm_web_turn_nanos": 5_000_000})
        with patch("app.web.workspace.DeepSeekGateway", partial(DeepSeekGateway, transport=transport)):
            self.client = self.new_client()
        self.assertIn("我同意本场使用 DeepSeek", self.client.get("/new").text)
        example = self.request("POST", "/drafts/preview", key="preview-example", body={"source": "example"})
        self.request("POST", "/sessions", key="refuse-example", status=503, body={
            "draft_id": example["draft_id"], "material": example["material"], "mode": "mixed", "cloud_assistance": True})
        preview = self.request("POST", "/drafts/preview", key="preview", body={"source": "text",
                               "text": "未确认原文 RAW_PRIVATE_MARKER"})
        new = self.request("POST", "/sessions", key="cloud-confirm", status=201, body={
            "draft_id": preview["draft_id"], "material": preview["material"],
            "mode": "knowledge_only", "cloud_assistance": True})
        base = f"/sessions/{new['session_id']}"
        plan = self.request("PATCH", base + "/config", key="plan", status=202,
                            body={"expected_revision": new["revision"], "choices": {}})
        self.request("POST", base + "/start", key="start", status=202, body={"expected_revision": plan["revision"]})
        self.assertEqual(contexts, [])
        for index, answer in enumerate(("先核对审核题版本", "记录命中的题目和修订号")):
            state = self.request("GET", base + "/state")
            self.assertTrue(state["cloud_assistance"])
            self.assertEqual(state["current"]["round_no"], index)
            turn = state["current"]
            body = {"exchange_id": turn["exchange_id"], "expected_revision": state["revision"], "answer": answer}
            route = f"/turns/{turn['turn_id']}/answer"
            receipt = self.request("POST", route, key=f"cloud-answer-{index}", body=body, status=202)
            self.assertEqual(receipt, self.request("POST", route, key=f"cloud-answer-{index}", body=body, status=202))
        state = self.request("GET", base + "/state")
        self.assertEqual(state["progress"]["scored"], 1)
        self.assertEqual(len(contexts), 3)
        self.assertNotIn("RAW_PRIVATE_MARKER", str(contexts))
        self.request("POST", base + "/abort", key="abort", status=202,
                     body={"expected_revision": state["revision"], "reason": "user_cancel"})
        report = self.request("GET", base + "/report")
        self.assertTrue(report["cloud_assistance"])
        self.assertEqual(report["knowledge_score"], 75.0)
        clone = self.request("POST", base + "/clone", key="clone", status=201)
        self.assertFalse(self.request("GET", f"/sessions/{clone['session_id']}/state")["cloud_assistance"])

    def test_cloud_budget_spans_sessions_and_turn_limits(self):
        path = self.root / "interviews.db"
        limits = CloudLimits(100, 60)

        def ready(index):
            preview = self.request("POST", "/drafts/preview", key=f"draft-{index}", body={"source": "form", "text": str(index)})
            new = self.request("POST", "/sessions", key=f"new-{index}", status=201, body={
                "draft_id": preview["draft_id"], "material": preview["material"], "mode": "knowledge_only"})
            session_id = new["session_id"]
            base = f"/sessions/{session_id}"
            configure_budget(path, session_id, hard_limit_nanos=100, cloud_limits=limits)
            plan = self.request("PATCH", base + "/config", key=f"plan-{index}", status=202,
                                body={"expected_revision": new["revision"], "choices": {}})
            self.request("POST", base + "/start", key=f"start-{index}", status=202, body={"expected_revision": plan["revision"]})
            state = self.request("GET", base + "/state")
            turn = state["current"]
            accepted = accept_answer(path, session_id, turn["turn_id"], turn["exchange_id"], state["revision"],
                                     f"saved-{index}", "合成回答")
            return session_id, accepted["operation_id"], turn["turn_id"]

        price = PriceSnapshot("deepseek-flash", "synthetic", 1, 1)

        def bill(context, step):
            session_id, operation, turn = context
            return invoke_budgeted(path, session_id, operation, step, role="probe", attempt_no=1, price=price,
                                   estimated_input_tokens=20, max_output_tokens=25, send_once=lambda _: ("sent", Usage(20, 25)),
                                   cloud_limits=limits, turn_id=turn)

        first, second, third = (ready(index) for index in range(3))
        self.assertEqual(bill(first, "first"), "sent")
        with self.assertRaises(SessionError) as blocked:
            bill(first, "same-turn")
        self.assertEqual(blocked.exception.code, "TURN_BUDGET_EXHAUSTED")
        self.assertEqual(bill(second, "second"), "sent")
        with self.assertRaises(SessionError) as exhausted:
            bill(third, "third")
        self.assertEqual(exhausted.exception.code, "BUDGET_EXHAUSTED")
        base = f"/sessions/{first[0]}"
        state = self.request("GET", base + "/state")
        self.request("POST", base + "/abort", key="abort-budget", status=202,
                     body={"expected_revision": state["revision"], "reason": "user_cancel"})
        state = self.request("GET", base + "/state")
        headers = {"Idempotency-Key": "remove-budget"}
        response = self.client.request("DELETE", "/api/v1" + base, headers=headers,
                                       json={"expected_revision": state["revision"]})
        self.assertIn(response.status_code, (202, 204), response.text)
        with closing(connect(path, readonly=True)) as db:
            self.assertEqual(tuple(db.execute("SELECT spent_nanos,reserved_nanos FROM cloud_budget WHERE id=1").fetchone()), (90, 0))
            self.assertIsNone(db.execute("SELECT 1 FROM call_attempt WHERE session_id=?", (first[0],)).fetchone())

    def test_complete_mixed_http_flow_replay_and_report_after_app_restart(self):
        session, revision, confirmed = self.configured()
        base = f"/sessions/{session}"
        self.assertEqual(self.request("POST", "/sessions", key="confirm", body=confirmed, status=201)["session_id"], session)
        self.assertEqual(self.request("GET", base + "/report", status=409)["code"], "ILLEGAL_ACTION")
        self.request("POST", base + "/start", key="start", body={"expected_revision": revision}, status=202)
        for number in range(1, 6):
            state = self.request("GET", base + "/state")
            turn = state["current"]
            self.assertEqual(turn["seq"], number)
            body = {"exchange_id": turn["exchange_id"], "expected_revision": state["revision"], "answer": "本机合成答复"}
            route = f"/turns/{turn['turn_id']}/answer"
            receipt = self.request("POST", route, key=f"answer-{number}", body=body, status=202)
            self.assertEqual(self.request("POST", route, key=f"answer-{number}", body=body, status=202), receipt)
            state = self.request("GET", base + "/state")
            if number == 1:
                self.assertEqual(state["current"]["round_no"], 1)
                self.request("POST", route, key="follow-up",
                             body={"exchange_id": state["current"]["exchange_id"], "expected_revision": state["revision"],
                                   "answer": "固定对照输入"}, status=202)
                state = self.request("GET", base + "/state")
            if state["phase"] != "FINISHED":
                self.request("POST", base + "/advance", key=f"advance-{number}",
                             body={"expected_revision": state["revision"]}, status=202)
        self.client = self.new_client()
        report = self.request("GET", base + "/report")
        self.assertEqual(report["category_counts"], {"knowledge": 3, "project": 2})
        self.assertEqual(report["source"], "example")
        self.assertEqual(report["counts"]["answered"], 5)

    def test_saved_answer_survives_process_restart_and_text_mode_has_no_score(self):
        session, revision, _ = self.configured(source="text")
        base = f"/sessions/{session}"
        self.request("POST", base + "/start", key="start", body={"expected_revision": revision}, status=202)
        state = self.request("GET", base + "/state")
        turn = state["current"]
        from app.web.workspace import Workspace
        with patch.object(Workspace, "process"):
            saved = self.request("POST", f"/turns/{turn['turn_id']}/answer", key="answer",
                                 body={"exchange_id": turn["exchange_id"], "expected_revision": state["revision"],
                                       "answer": "只有一份持久化回答"}, status=202)
        self.client = self.new_client()
        state = self.request("GET", base + "/state")
        self.assertEqual(state["current"]["answer"], "只有一份持久化回答")
        self.request("POST", base + "/resume", key="resume", body={"expected_revision": state["revision"],
                     "operation_id": saved["operation_id"]}, status=202)
        state = self.request("GET", base + "/state")
        self.request("POST", base + "/abort", key="abort", body={"expected_revision": state["revision"],
                     "reason": "user_cancel"}, status=202)
        report = self.request("GET", base + "/report")
        self.assertEqual((report["kind"], report["knowledge_score"], report["counts"]["answered"]), ("summary", None, 1))

    def test_error_responses_hide_invalid_body_and_reject_other_origins(self):
        response = self.client.post("/api/v1/drafts/preview", json={"source": "PRIVATE_MARKER"},
                                    headers={"Idempotency-Key": "key"})
        self.assertEqual(response.status_code, 422)
        self.assertNotIn("PRIVATE_MARKER", response.text)
        response = self.client.post("/api/v1/drafts/preview", json={"source": "example"},
                                    headers={"Idempotency-Key": "key", "Origin": "https://other.invalid"})
        self.assertEqual(response.status_code, 403)

    def test_page_guards_history_clone_and_markdown(self):
        self.assertIn("练习记录", self.client.get("/").text)
        self.assertIn("准备材料", self.client.get("/new").text)
        session, revision, _ = self.configured()
        base = f"/sessions/{session}"
        guard = self.client.get("/interview", params={"s": session}, follow_redirects=False)
        self.assertEqual((guard.status_code, guard.headers["location"]), (303, f"/planning?s={session}"))
        self.request("POST", base + "/start", key="start", body={"expected_revision": revision}, status=202)
        state = self.request("GET", base + "/state")
        turn = state["current"]
        self.request("POST", f"/turns/{turn['turn_id']}/answer", key="answer",
                     body={"exchange_id": turn["exchange_id"], "expected_revision": state["revision"], "answer": "<b>记录证据</b>"},
                     status=202)
        state = self.request("GET", base + "/state")
        self.request("POST", base + "/abort", key="abort", body={"expected_revision": state["revision"],
                     "reason": "user_cancel"}, status=202)
        saved = self.request("GET", base + "/report")
        exported = self.client.get(f"/api/v1/reports/{saved['id']}/export")
        self.assertEqual(exported.status_code, 200)
        self.assertIn("attachment", exported.headers["content-disposition"])
        self.assertNotIn("<b>记录证据</b>", exported.text)
        self.assertIn("记录证据", exported.text)
        first = self.request("POST", base + "/clone", key="clone", status=201)
        same = self.request("POST", base + "/clone", key="clone", status=201)
        self.assertEqual(first, same)
        self.assertNotEqual(first["session_id"], session)
        clone = self.request("GET", f"/sessions/{first['session_id']}/state")
        self.assertEqual((clone["phase"], clone["plan_version"], clone["progress"]["answered"]), ("PLANNING", 0, 0))
        first_page = self.request("GET", "/sessions?limit=1")
        older = self.request("GET", f"/sessions?limit=1&cursor={first_page['next_cursor']}")
        self.assertNotEqual(first_page["sessions"][0]["id"], older["sessions"][0]["id"])
        self.assertNotIn("记录证据", str(first_page) + str(older))

    def test_failed_deletion_can_resume_across_app_restart_and_purges_backups(self):
        with patch("app.db.business.MIGRATIONS", MIGRATIONS[:6]):
            session, revision, _ = self.configured()
        base = f"/sessions/{session}"
        self.assertEqual(self.request("DELETE", base, key="premature", body={"expected_revision": revision},
                                      status=409)["code"], "ILLEGAL_ACTION")
        self.request("POST", base + "/abort", key="abort", body={"expected_revision": revision, "reason": "user_cancel"}, status=202)
        state = self.request("GET", base + "/state")
        report = self.request("GET", base + "/report")
        with closing(connect(self.settings.observe_db)) as store, store:
            store.execute("CREATE TABLE trace (id TEXT, session_id TEXT)")
            store.execute("CREATE TABLE span (trace_id TEXT)")
            store.executemany("INSERT INTO trace VALUES (?,?)", [("ours", session), ("other", "other-session")])
            store.executemany("INSERT INTO span VALUES (?)", [("ours",), ("other",)])
        with patch("app.web.library._purge_backups", side_effect=PermissionError("synthetic failure")):
            job = self.request("DELETE", base, key="delete", body={"expected_revision": state["revision"]}, status=202)
        self.assertEqual(self.request("GET", f"/deletions/{job['job_id']}")["status"], "failed")
        self.assertEqual(self.request("GET", base + "/state", status=404)["code"], "RESOURCE_NOT_FOUND")
        self.assertEqual(self.request("GET", f"/reports/{report['id']}/export", status=404)["code"], "RESOURCE_NOT_FOUND")
        self.assertTrue(list(self.root.glob("interviews.before-007-*.db")))
        self.client = self.new_client()
        self.request("POST", f"/deletions/{job['job_id']}/retry", key="retry", status=202)
        self.assertEqual(self.request("GET", f"/deletions/{job['job_id']}")["status"], "completed")
        self.assertFalse(list(self.root.glob("interviews.before-*.db")))
        with closing(connect(self.settings.observe_db, readonly=True)) as store:
            self.assertEqual([row[0] for row in store.execute("SELECT id FROM trace")], ["other"])
        with closing(connect(self.root / "interviews.db", readonly=True)) as store:
            self.assertEqual(store.execute("SELECT count(*) FROM interview_session").fetchone()[0], 0)
            self.assertEqual(store.execute("SELECT count(*) FROM operation").fetchone()[0], 0)
        self.assertEqual(self.client.delete("/api/v1" + base, headers={"Idempotency-Key": "repeat"}).status_code, 204)
