from pathlib import Path
from contextlib import closing
from io import BytesIO
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
from app.db.business import MIGRATIONS
from app.db.sqlite import connect
from app.main import create_app
from app.services.document_text import MAX_DOCUMENT_BYTES


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
