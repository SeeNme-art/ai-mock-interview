from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from app.schemas.materials import Confirmation, MaterialInput, ProjectInput
from app.services.materials import (
    ConfirmationConflict, DraftExpired, confirm_material, load_material, preview_text, start_draft,
)


class MaterialsTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db = Path(temp.name) / "interviews.db"
        self.initial = MaterialInput(projects=[ProjectInput(action="未确认的项目描述")])
        self.final = MaterialInput(jd_text="初级 AI 应用开发", skills=["Python"], projects=[
            ProjectInput(action="为课程检索练习实现分块", excerpt="通过固定查询比较两种分块大小")
        ])
        self.confirmation = Confirmation(mode="mixed", material=self.final)

    def test_project_with_only_action_can_have_an_edited_snapshot(self):
        draft = start_draft(self.initial, "text")
        self.assertFalse(self.db.exists())
        session = confirm_material(self.db, draft.id, "first", self.confirmation, draft=draft)
        saved = load_material(self.db, session)
        self.assertEqual((saved["phase"], saved["revision"], saved["input_mode"]), ("PLANNING", 1, "text"))
        self.assertEqual(saved["jd_text"], self.final.jd_text)
        candidate = saved["resume_profile"]["candidate"]
        self.assertEqual(candidate, {"display_name": "候选人", "skills": ["Python"], "years": None, "education": None})
        project = saved["resume_profile"]["projects"][0]
        self.assertEqual(project["action"], self.final.projects[0].action)
        self.assertIsNone(project["name"])
        self.assertIsNone(project["result"])
        self.assertEqual(project["field_provenance"]["action"], {"origin": "edited", "verification": "edited"})
        self.assertEqual(project["field_provenance"]["result"]["verification"], "missing")
        self.assertNotIn("未确认的项目描述".encode(), self.db.read_bytes())

    def test_no_project_requires_explicit_knowledge_only(self):
        material = MaterialInput(skills=["Python"])
        with self.assertRaises(ValidationError):
            Confirmation(mode="mixed", material=material)
        draft = start_draft(material, "form")
        session = confirm_material(self.db, draft.id, "knowledge", Confirmation(mode="knowledge_only", material=material),
                                   draft=draft)
        saved = load_material(self.db, session)
        self.assertEqual(saved["mode"], "knowledge_only")
        self.assertEqual(saved["resume_profile"]["projects"], [])

    def test_replay_after_preview_is_gone_and_conflicting_retry(self):
        draft = start_draft(self.initial, "form")
        first = confirm_material(self.db, draft.id, "stable-key", self.confirmation, draft=draft)
        self.assertEqual(confirm_material(self.db, draft.id, "stable-key", self.confirmation), first)
        changed = Confirmation(mode="mixed", material=self.initial)
        with self.assertRaises(ConfirmationConflict):
            confirm_material(self.db, draft.id, "stable-key", changed, draft=draft)
        with self.assertRaises(ConfirmationConflict):
            confirm_material(self.db, draft.id, "different-key", self.confirmation, draft=draft)
        new_draft = start_draft(self.initial, "form")
        second = confirm_material(self.db, new_draft.id, "new-session", changed, draft=new_draft)
        self.assertNotEqual(first, second)
        self.assertEqual(load_material(self.db, first)["resume_profile"]["projects"][0]["action"],
                         self.final.projects[0].action)

    def test_expired_and_missing_drafts_do_not_create_sessions(self):
        draft = start_draft(self.initial, "form")
        expired = replace(draft, expires_at=datetime.now(UTC))
        with self.assertRaises(DraftExpired):
            confirm_material(self.db, draft.id, "expired", self.confirmation, draft=expired)
        with self.assertRaises(DraftExpired):
            confirm_material(self.db, draft.id, "lost", self.confirmation)
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT name FROM sqlite_master").fetchall(), [])

    def test_concurrent_same_key_creates_only_one_session(self):
        draft = start_draft(self.initial, "form")
        with ThreadPoolExecutor(max_workers=2) as pool:
            tasks = [pool.submit(confirm_material, self.db, draft.id, "same-key", self.confirmation, draft=draft)
                     for _ in range(2)]
            self.assertEqual(tasks[0].result(), tasks[1].result())
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM interview_session").fetchone()[0], 1)

    def test_private_fields_and_clear_contact_patterns_never_save(self):
        with self.assertRaises(ValidationError):
            MaterialInput.model_validate({"display_name": "真实姓名", "raw_text": "原文"})
        test_id = "110105138123456789"
        self.assertNotIn(test_id, preview_text(f"身份证号{test_id}，邮箱 demo@example.invalid"))
        self.assertNotIn("123456789", preview_text(f"身份证号{test_id}"))
        self.assertNotIn("demo@example.invalid", preview_text("邮箱 demo@example.invalid"))
        self.assertIn("用量100次", preview_text("用量100次"))
        draft = start_draft(self.initial, "text")
        with self.assertRaisesRegex(ValueError, "请求标识"):
            confirm_material(self.db, draft.id, "", self.confirmation, draft=draft)
        with self.assertRaisesRegex(ValueError, "疑似手机号"):
            confirm_material(self.db, draft.id, "pii", Confirmation(mode="knowledge_only", material=MaterialInput(
                jd_text="联系１３８００１３８０００"
            )), draft=draft)
        self.assertFalse(self.db.exists())

    def test_business_migration_refuses_unknown_or_changed_database(self):
        other = self.db.with_name("other.db")
        draft = start_draft(self.initial, "form")
        with closing(sqlite3.connect(other)) as db, db:
            db.execute("CREATE TABLE unrelated (value TEXT)")
        with self.assertRaisesRegex(ValueError, "未登记数据库"):
            confirm_material(other, draft.id, "other", self.confirmation, draft=draft)
        session = confirm_material(self.db, draft.id, "valid", self.confirmation, draft=draft)
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("UPDATE schema_migration SET checksum='other'")
        with self.assertRaisesRegex(ValueError, "迁移校验值不匹配"):
            load_material(self.db, session)

    def test_cli_text_preview_edit_confirm_and_reload_in_new_process(self):
        script = ROOT / "scripts" / "material_profile.py"
        raw = self.db.with_name("synthetic.txt")
        raw.write_text("仅供预览的唯一正文标记\n邮箱 demo@example.invalid", encoding="utf-8")
        args = [sys.executable, "-X", "utf8", str(script), "--db", str(self.db)]
        initial = ["", "Python", "1", "", "初稿项目描述", "", "", ""]
        final = ["AI 应用开发", "Python", "1", "", "做了分块", "", "", ""]
        cancel = subprocess.run([*args, "create"], input="\n\n0\nq\n", encoding="utf-8", capture_output=True,
                                timeout=15, check=True)
        self.assertIn("已取消", cancel.stdout)
        self.assertFalse(self.db.exists())
        saved = subprocess.run([*args, "create", "--text-file", str(raw)], encoding="utf-8", capture_output=True,
                               input="\n".join([*initial, "e", *final, "m", ""]), timeout=15, check=True)
        self.assertIn("仅供预览的唯一正文标记", saved.stdout)
        self.assertNotIn("demo@example.invalid", saved.stdout + saved.stderr)
        # The last JSON is the create result. Earlier JSON blocks are local previews.
        session = json.loads(saved.stdout[saved.stdout.rindex("{\n"):])["session_id"]
        shown = subprocess.run([*args, "show", session], encoding="utf-8", capture_output=True, timeout=15, check=True)
        self.assertEqual(json.loads(shown.stdout)["resume_profile"]["projects"][0]["action"], "做了分块")
        for text in ("仅供预览的唯一正文标记", "初稿项目描述", "demo@example.invalid"):
            self.assertNotIn(text, shown.stdout + shown.stderr)
            self.assertNotIn(text.encode(), self.db.read_bytes())
