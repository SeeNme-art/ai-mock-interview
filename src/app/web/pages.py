"""Five server-routed screens for the local interview workspace."""
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.db.business import SessionError
from app.services.first_question import read_state
from app.web.api import local_request
from app.web.workspace import Workspace

ROOT = Path(__file__).parent


def pages(workspace: Workspace) -> APIRouter:
    from fastapi.templating import Jinja2Templates

    templates = Jinja2Templates(directory=str(ROOT / "templates"))
    router = APIRouter(dependencies=[Depends(local_request)])
    paths = {"PLANNING": "/planning", "ACTIVE": "/interview", "FINISHED": "/report"}
    headers = {
        "Cache-Control": "no-store",
        "Content-Security-Policy": "default-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
        "X-Content-Type-Options": "nosniff",
    }

    def render(request: Request, name: str, session_id: str = "", source: str = ""):
        return templates.TemplateResponse(request, "workspace.html",
                                          {"page": name, "session_id": session_id, "source": source,
                                           "cloud_enabled": workspace.cloud_enabled}, headers=headers)

    def session_page(request: Request, name: str, session_id: str):
        if not workspace.db.is_file():
            return RedirectResponse("/", status_code=303)
        try:
            state = read_state(workspace.db, session_id)
        except SessionError as exc:
            if exc.code != "RESOURCE_NOT_FOUND":
                raise
            return RedirectResponse("/", status_code=303)
        path = paths[state["phase"]]
        if path != f"/{name}":
            return RedirectResponse(f"{path}?s={session_id}", status_code=303)
        return render(request, name, session_id)

    @router.get("/", include_in_schema=False)
    def history(request: Request):
        return render(request, "history")

    @router.get("/new", include_in_schema=False)
    def new(request: Request, source: str = "text"):
        return render(request, "new", source="example" if source == "example" else "text")

    @router.get("/planning", include_in_schema=False)
    def planning(request: Request, s: str):
        return session_page(request, "planning", s)

    @router.get("/interview", include_in_schema=False)
    def interview(request: Request, s: str):
        return session_page(request, "interview", s)

    @router.get("/report", include_in_schema=False)
    def report(request: Request, s: str):
        return session_page(request, "report", s)

    return router


def assets() -> StaticFiles:
    return StaticFiles(directory=str(ROOT / "static"))
