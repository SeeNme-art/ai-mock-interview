"""Metadata-safe HTTP entry points for a local single-user workspace."""
from __future__ import annotations

from contextlib import closing
from hashlib import sha256
import json
import sqlite3
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from app.db.business import OWNER_ID, SessionError, check_business_schema, find_session, validate_request_key
from app.db.sqlite import connect
from app.schemas.materials import Confirmation
from app.schemas.planning import PlanChanges
from app.services.advancement import accept_advance, accept_skip
from app.services.answers import accept_answer
from app.services.document_text import DocumentError, DocumentFormat, MAX_DOCUMENT_BYTES, document_too_large, extract_document
from app.services.finalization import accept_abort
from app.services.first_question import accept_start, read_state
from app.services.materials import ConfirmationConflict, DraftExpired, TEXT_LIMIT, confirm_material
from app.services.planning import rebuild_plan, show_plan
from app.services.reports import generate_report, read_report
from app.web.workspace import Workspace
from app.web import library

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
Revision = Annotated[int, Field(ge=1)]


def local_request(request: Request) -> None:
    if request.url.hostname not in LOCAL_HOSTS or request.client is None or request.client.host not in LOCAL_HOSTS:
        raise SessionError("FORBIDDEN", "面试工作台仅供本机访问")
    origin = request.headers.get("origin")
    if request.method not in ("GET", "HEAD") and origin and origin != f"{request.url.scheme}://{request.url.netloc}":
        raise SessionError("FORBIDDEN", "请求来源与当前工作台不一致")


def request_key(value: Annotated[str, Header(alias="Idempotency-Key")]) -> str:
    validate_request_key(value)
    return value


Key = Annotated[str, Depends(request_key)]


class Input(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", hide_input_in_errors=True)


class Preview(Input):
    source: Literal["text", "example", "form"]
    text: str = Field(default="", max_length=TEXT_LIMIT)


class Confirm(Confirmation):
    draft_id: str
    cloud_assistance: bool = False


class Expected(Input):
    expected_revision: Revision


class Configuration(Expected):
    choices: PlanChanges
    vector: bool = False


class Answer(Expected):
    exchange_id: str
    answer: str = Field(min_length=1, max_length=8000)
    answer_mode: Literal["text"] = "text"


class Abort(Expected):
    reason: Literal["user_cancel", "too_hard", "no_time"]


class Resume(Expected):
    operation_id: str


def _ok(data: object) -> dict:
    return {"ok": True, "data": data, "error": None}


def _error(status: int, code: str, message: str, revision: int | None = None) -> JSONResponse:
    return JSONResponse({"ok": False, "data": {"current_revision": revision} if revision is not None else None,
                         "error": {"code": code, "message": message}}, status_code=status,
                        headers={"Cache-Control": "no-store"})


def register_errors(app: FastAPI) -> None:
    @app.exception_handler(DocumentError)
    async def invalid_document(_request: Request, exc: DocumentError):
        status = 413 if exc.code == "DOCUMENT_TOO_LARGE" else 422
        return _error(status, exc.code, str(exc))

    @app.exception_handler(SessionError)
    async def session_error(_request: Request, exc: SessionError):
        status = {"FORBIDDEN": 403, "INVALID_INPUT": 422, "DRAFT_EXPIRED": 410, "RESOURCE_NOT_FOUND": 404,
                  "CONFIGURATION_ERROR": 503}.get(exc.code, 409)
        return _error(status, exc.code, str(exc), exc.current_revision)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request: Request, _exc: RequestValidationError):
        return _error(422, "INVALID_INPUT", "请核对必填字段、长度或选项")

    @app.exception_handler(sqlite3.Error)
    async def storage_error(_request: Request, _exc: sqlite3.Error):
        return _error(503, "STORAGE_ERROR", "数据库暂不可用，请稍后重新读取进度")

    @app.exception_handler(ValueError)
    async def invalid_value(_request: Request, _exc: ValueError):
        return _error(422, "INVALID_INPUT", "请核对材料字段与请求标识")


def business_api(workspace: Workspace, observe: Path) -> APIRouter:
    router = APIRouter(prefix="/api/v1", dependencies=[Depends(local_request)])

    def turn_session(turn_id: str) -> str:
        if not workspace.db.is_file():
            raise SessionError("RESOURCE_NOT_FOUND", "找不到可访问的题目")
        with closing(connect(workspace.db, readonly=True)) as db, db:
            db.execute("BEGIN")
            version = check_business_schema(db)
            row = db.execute("""SELECT t.session_id FROM turn t JOIN interview_session s ON s.id=t.session_id
                                WHERE t.id=? AND s.owner_id=? AND s.delete_state='none'""",
                             (turn_id, OWNER_ID)).fetchone() if version >= 3 else None
        if row is None:
            raise SessionError("RESOURCE_NOT_FOUND", "找不到可访问的题目")
        return row["session_id"]

    def schedule(tasks: BackgroundTasks, session_id: str, operation_id: str | None) -> dict:
        if operation_id is not None:
            tasks.add_task(workspace.process, session_id, operation_id)
        return _ok({"session_id": session_id, "operation_id": operation_id})

    def preview_data(draft):
        return {"draft_id": draft.id, "expires_at": draft.expires_at.isoformat(),
                "material": json.loads(draft.initial_material_json)}

    @router.post("/drafts/preview")
    def preview(body: Preview, key: Key):
        return _ok(preview_data(workspace.preview(key, body.source, body.text)))

    @router.post("/drafts/document")
    async def preview_document(request: Request, key: Key, format: DocumentFormat):
        if request.headers.get("content-type") != "application/octet-stream":
            raise DocumentError("DOCUMENT_INVALID", "请选择 PDF 或 DOCX 文件后重新上传。")
        length = request.headers.get("content-length")
        if length and length.isdecimal() and int(length) > MAX_DOCUMENT_BYTES:
            raise document_too_large()
        content = bytearray()
        async for chunk in request.stream():
            if len(content) + len(chunk) > MAX_DOCUMENT_BYTES:
                raise document_too_large()
            content.extend(chunk)
        text = await run_in_threadpool(extract_document, bytes(content), format)
        draft = workspace.preview(key, format, text, file_digest=sha256(content).hexdigest())
        return _ok({**preview_data(draft), "source_text": text})

    @router.post("/sessions", status_code=201)
    def create_session(body: Confirm, key: Key):
        draft = workspace.draft(body.draft_id)
        if body.cloud_assistance and (not workspace.cloud_enabled or (draft and draft.web_source == "example")):
            raise SessionError("CONFIGURATION_ERROR", "当前材料模式无法使用云端点评")
        try:
            session_id = confirm_material(workspace.db, body.draft_id, key, Confirmation(material=body.material, mode=body.mode),
                                          draft=draft, cloud_assistance=body.cloud_assistance)
        except DraftExpired as exc:
            raise SessionError("DRAFT_EXPIRED", str(exc)) from None
        except ConfirmationConflict as exc:
            raise SessionError("IDEMPOTENCY_CONFLICT", str(exc)) from None
        workspace.forget(body.draft_id)
        return _ok({"session_id": session_id, "revision": 1})

    @router.get("/sessions")
    def list_sessions(limit: Annotated[int, Query(ge=1, le=50)] = 20, cursor: str | None = None,
                      status: Literal["PLANNING", "ACTIVE", "FINISHED"] | None = None):
        return _ok(library.history(workspace.db, limit, cursor, status))

    @router.post("/sessions/{session_id}/clone", status_code=201)
    def clone(session_id: str, key: Key):
        return _ok({"session_id": library.clone(workspace.db, session_id, key), "revision": 1})

    @router.get("/sessions/{session_id}/state")
    def state(session_id: str):
        if not workspace.db.is_file():
            raise SessionError("RESOURCE_NOT_FOUND", "找不到可访问的会话")
        return _ok(workspace.state(session_id))

    @router.patch("/sessions/{session_id}/config", status_code=202)
    def plan_config(session_id: str, body: Configuration, key: Key):
        workspace.prepare_bank()
        assets = workspace.bank.parent / "index"
        vector = (assets / "bge-small-zh-v1.5", assets / "questions") if body.vector else None
        return _ok(rebuild_plan(workspace.db, workspace.bank, session_id, body.expected_revision, key, body.choices, vector=vector))

    @router.get("/sessions/{session_id}/plan")
    def plan(session_id: str):
        return _ok(show_plan(workspace.db, session_id))

    @router.post("/sessions/{session_id}/start", status_code=202)
    def start(session_id: str, body: Expected, key: Key, tasks: BackgroundTasks):
        workspace.prepare_cloud(session_id)
        operation, _ = accept_start(workspace.db, session_id, body.expected_revision, key)
        return schedule(tasks, session_id, operation)

    @router.post("/turns/{turn_id}/answer", status_code=202)
    def answer(turn_id: str, body: Answer, key: Key, tasks: BackgroundTasks):
        session_id = turn_session(turn_id)
        receipt = accept_answer(workspace.db, session_id, turn_id, body.exchange_id, body.expected_revision, key, body.answer)
        tasks.add_task(workspace.process, session_id, receipt["operation_id"])
        return _ok(receipt)

    @router.post("/turns/{turn_id}/skip", status_code=202)
    def skip(turn_id: str, body: Expected, key: Key, tasks: BackgroundTasks):
        session_id = turn_session(turn_id)
        operation, _ = accept_skip(workspace.db, session_id, turn_id, body.expected_revision, key)
        return schedule(tasks, session_id, operation)

    @router.post("/sessions/{session_id}/advance", status_code=202)
    def advance(session_id: str, body: Expected, key: Key, tasks: BackgroundTasks):
        operation, _ = accept_advance(workspace.db, session_id, body.expected_revision, key)
        return schedule(tasks, session_id, operation)

    @router.post("/sessions/{session_id}/abort", status_code=202)
    def abort(session_id: str, body: Abort, key: Key, tasks: BackgroundTasks):
        operation, _ = accept_abort(workspace.db, session_id, body.expected_revision, key, body.reason)
        return schedule(tasks, session_id, operation)

    @router.post("/sessions/{session_id}/resume", status_code=202)
    def resume(session_id: str, body: Resume, _key: Key, tasks: BackgroundTasks):
        current = read_state(workspace.db, session_id)
        if current["revision"] != body.expected_revision:
            raise SessionError("STALE_REVISION", "进度已更新，请刷新后继续", current_revision=current["revision"])
        operation = current["operation"]
        if operation is None or operation["operation_id"] != body.operation_id:
            raise SessionError("ILLEGAL_ACTION", "操作已变化，请读取当前进度")
        return schedule(tasks, session_id, body.operation_id)

    @router.get("/operations/{operation_id}")
    def operation_status(operation_id: str):
        with closing(connect(workspace.db, readonly=True)) as db, db:
            db.execute("BEGIN")
            version = check_business_schema(db)
            row = db.execute("""SELECT o.id AS operation_id,o.session_id,o.kind,o.status,o.error_code
                                FROM operation o JOIN interview_session s ON s.id=o.session_id
                                WHERE o.id=? AND o.owner_id=? AND s.delete_state='none'""",
                             (operation_id, OWNER_ID)).fetchone() if version >= 2 else None
        if row is None:
            raise SessionError("RESOURCE_NOT_FOUND", "找不到可访问的操作")
        return _ok(dict(row))

    @router.get("/sessions/{session_id}/report")
    def report(session_id: str, revision: int | None = None):
        preferences = workspace.preferences(session_id)
        return _ok({**read_report(workspace.db, session_id, revision), "source": preferences["source"],
                    "cloud_assistance": preferences["cloud_assistance"]})

    @router.post("/sessions/{session_id}/report/generate")
    def build_report(session_id: str, _key: Key):
        return _ok(generate_report(workspace.db, session_id))

    @router.get("/reports/{report_id}/export")
    def export(report_id: str):
        markdown = library.export_report(workspace.db, report_id)
        return Response(markdown, media_type="text/markdown",
                        headers={"Content-Disposition": 'attachment; filename="interview-report.md"',
                                 "X-Content-Type-Options": "nosniff"})

    @router.delete("/sessions/{session_id}", status_code=202)
    def delete(session_id: str, _key: Key, tasks: BackgroundTasks, body: Expected | None = None):
        job = library.request_deletion(workspace.db, session_id, body.expected_revision if body else None)
        if job is None or library.deletion_status(workspace.db, job)["status"] == "completed":
            return Response(status_code=204)
        tasks.add_task(library.run_deletion, workspace.db, observe, job)
        return _ok({"job_id": job})

    @router.get("/deletions/{job_id}")
    def deletion(job_id: str):
        return _ok(library.deletion_status(workspace.db, job_id))

    @router.post("/deletions/{job_id}/retry", status_code=202)
    def retry_deletion(job_id: str, _key: Key, tasks: BackgroundTasks):
        if library.deletion_status(workspace.db, job_id)["status"] == "completed":
            return Response(status_code=204)
        tasks.add_task(library.run_deletion, workspace.db, observe, job_id)
        return _ok({"job_id": job_id})

    return router
