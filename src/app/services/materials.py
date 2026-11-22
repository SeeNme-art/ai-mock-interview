"""Turn a local preview into one immutable PLANNING session snapshot."""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import unicodedata
from uuid import uuid4

from app.core.logging_setup import mask
from app.db.business import OWNER_ID, check_business_schema, initialize_business, validate_request_key
from app.db.sqlite import canonical_json, connect, sha256
from app.schemas.materials import Confirmation, InputMode, MaterialInput

DRAFT_TTL = timedelta(minutes=30)
TEXT_LIMIT = 16000


@dataclass(frozen=True)
class MaterialDraft:
    id: str
    input_mode: InputMode
    expires_at: datetime
    initial_material_json: str = field(repr=False)
    web_source: str | None = None


class DraftExpired(ValueError):
    pass


class ConfirmationConflict(ValueError):
    pass


def preview_text(text: str) -> str:
    if len(text) > TEXT_LIMIT:
        raise ValueError(f"材料原文最多 {TEXT_LIMIT} 个字符")
    normalized = unicodedata.normalize("NFKC", text.replace("\r\n", "\n").replace("\r", "\n"))
    printable = "".join(char if char.isprintable() or char in "\n\t" else " " for char in normalized)
    return mask(printable).strip()


def validate_material(material: MaterialInput) -> None:
    text = [material.jd_text, material.education, *material.skills]
    for project in material.projects:
        text.extend(project.model_dump().values())
    for value in text:
        if value is None:
            continue
        normalized = unicodedata.normalize("NFKC", value)
        if mask(normalized) != normalized:
            raise ValueError("材料字段含疑似手机号、邮箱或身份证号，请删除后再确认")
        if any(not char.isprintable() and char not in "\n\r\t" for char in value):
            raise ValueError("材料字段含不可显示字符，请清理后再确认")


def start_draft(material: MaterialInput, input_mode: InputMode, *, web_source: str | None = None) -> MaterialDraft:
    validate_material(material)
    return MaterialDraft(uuid4().hex, input_mode, datetime.now(UTC) + DRAFT_TTL,
                         material.model_dump_json(), web_source)


def _profile_snapshot(material: MaterialInput, draft: MaterialDraft) -> dict:
    initial_projects = json.loads(draft.initial_material_json)["projects"]
    projects = []
    for position, project in enumerate(material.projects):
        values = project.model_dump()
        original = initial_projects[position] if position < len(initial_projects) else None
        provenance = {}
        for name, value in values.items():
            origin = "edited" if original is not None and value != original[name] else "provided"
            provenance[name] = {"origin": origin, "verification": "missing" if value is None else origin}
        projects.append({"id": f"p_{position + 1}", **values, "field_provenance": provenance})
    return {"candidate": {"display_name": "候选人", "years": material.years, "education": material.education,
                          "skills": material.skills}, "projects": projects}


def confirm_material(path: Path, draft_id: str, key: str, confirmation: Confirmation,
                     *, draft: MaterialDraft | None = None, cloud_assistance: bool = False) -> str:
    validate_request_key(key)
    validate_material(confirmation.material)
    cloud_policy = {"cloud_assistance": True} if cloud_assistance else {}
    digest = sha256(canonical_json({"draft_id": draft_id, **confirmation.model_dump(mode="json"), **cloud_policy}))
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(connect(path)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        initialize_business(db)
        previous = db.execute("""SELECT id, confirmation_hash FROM interview_session
                                 WHERE owner_id=? AND confirmation_key=?""", (OWNER_ID, key)).fetchone()
        if previous:
            if previous["confirmation_hash"] != digest:
                raise ConfirmationConflict("确认请求已使用该标识，不能用同一标识提交不同材料")
            return previous["id"]
        used = db.execute("SELECT 1 FROM interview_session WHERE owner_id=? AND draft_id=?",
                          (OWNER_ID, draft_id)).fetchone()
        if used:
            raise ConfirmationConflict("草稿已确认，更新材料请新建草稿和会话")
        now = datetime.now(UTC)
        if draft is None or draft.id != draft_id or draft.expires_at <= now:
            raise DraftExpired("材料预览已过期或已丢失，请重新提供材料")
        material = confirmation.material
        session_id = uuid4().hex
        policy = {"web_source": draft.web_source} if draft.web_source else {}
        db.execute("""INSERT INTO interview_session (
                          id, owner_id, draft_id, confirmation_key, confirmation_hash, input_mode,
                          mode, jd_text, resume_profile_json, policy_snapshot_json, created_at
                      ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                   (session_id, OWNER_ID, draft_id, key, digest, draft.input_mode, confirmation.mode,
                    material.jd_text, canonical_json(_profile_snapshot(material, draft)),
                    canonical_json({**policy, **cloud_policy}), now.isoformat()))
        return session_id


def load_material(path: Path, session_id: str) -> dict:
    with closing(connect(path, readonly=True)) as db, db:
        db.execute("BEGIN")
        check_business_schema(db)
        row = db.execute("""SELECT id AS session_id, phase, revision, mode, input_mode, jd_text,
                                   resume_profile_json, created_at
                            FROM interview_session WHERE id=? AND owner_id=? AND delete_state='none'""",
                         (session_id, OWNER_ID)).fetchone()
        if row is None:
            raise ValueError("找不到该会话的已确认材料")
    snapshot = dict(row)
    snapshot["resume_profile"] = json.loads(snapshot.pop("resume_profile_json"))
    return snapshot
