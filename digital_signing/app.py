import hashlib
import json
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator, ConfigDict
from starlette.middleware.trustedhost import TrustedHostMiddleware

from digital_signing.config import Settings
from digital_signing.drive import GoogleDrive
from digital_signing.workflow import install_workflow
from digital_signing.addresses import validate_address
from digital_signing.rendering import DEMO_HTML, RenderError, unpack_export, validate_values
from digital_signing.pdf_rendering import demo_pdf, render_pdf

STATIC = Path(__file__).parent / "static"


class ContractInput(BaseModel):
    name: str
    template_id: str

    @field_validator("name")
    @classmethod
    def valid_name(cls, value):
        value = value.strip()
        if not 3 <= len(value) <= 100 or re.search(r'[<>:"/\\|?*\x00-\x1f\x7f]', value):
            raise ValueError("Use 3–100 characters without control characters or <> : / \\ | ? * quotes.")
        return value


class DeleteContractInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmation: str


def create_app(settings=None, drive_factory=GoogleDrive):
    settings = settings or Settings()
    settings.prepare()
    database = settings.data_dir / f"contracts-{settings.mode}.sqlite3"
    lock = threading.RLock()

    def db():
        connection = sqlite3.connect(database)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA secure_delete=ON")
        return connection

    with db() as connection:
        connection.execute("""CREATE TABLE IF NOT EXISTS contracts (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, name_key TEXT UNIQUE NOT NULL,
            template_id TEXT NOT NULL, document_id TEXT, document_url TEXT,
            created_at TEXT NOT NULL, status TEXT NOT NULL)""")

        connection.execute("""CREATE TABLE IF NOT EXISTS renders (
            contract_id TEXT NOT NULL, version INTEGER NOT NULL, created_at TEXT NOT NULL,
            snapshot TEXT NOT NULL, PRIMARY KEY(contract_id, version),
            FOREIGN KEY(contract_id) REFERENCES contracts(id))""")

        connection.execute("""CREATE TABLE IF NOT EXISTS render_assets (
            contract_id TEXT NOT NULL, version INTEGER NOT NULL, name TEXT NOT NULL,
            content BLOB NOT NULL, PRIMARY KEY(contract_id, version, name))""")

        if "folder_id" not in {row[1] for row in connection.execute("PRAGMA table_info(contracts)")}:
            connection.execute("ALTER TABLE contracts ADD COLUMN folder_id TEXT")

    app = FastAPI(title="Bello · Agreement workspace")
    hostname = urlsplit(settings.app_base_url).hostname
    local_hosts = {'localhost', '127.0.0.1', '::1', 'testserver'}
    allowed_hosts = sorted(local_hosts) if hostname in local_hosts else [hostname]
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)
    from digital_signing.auth import install_auth
    install_auth(app, db, lock, settings, STATIC)
    allowed_origins = {settings.app_base_url}
    if hostname in local_hosts:
        allowed_origins.update({'http://localhost:8000','http://127.0.0.1:8000','http://testserver'})

    @app.middleware("http")
    async def local_guard(request: Request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if request.headers.get("origin") not in allowed_origins | {None} or request.headers.get("x-workspace-request") != "1":
                return JSONResponse({"detail": "Request must originate in this application."}, status_code=403)
        response = app.state.auth_guard(request)
        if response is None:
            response = await call_next(request)
        # Local development must revalidate UI files rather than reuse stale controls.
        if request.url.path.startswith("/static/") or response.headers.get("content-type", "").startswith("text/html"):
            response.headers["Cache-Control"] = "no-cache, must-revalidate"
        if request.url.path.startswith(("/api/", "/sign/", "/auth/", "/login")) or settings.auth_mode == "google":
            response.headers["Cache-Control"] = "no-store"
        if settings.app_base_url.startswith("https://"):
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self'; style-src-attr 'unsafe-inline'; img-src 'self' data: blob:; script-src 'self'; frame-ancestors 'none'"
        return response

    def provider():
        try:
            return drive_factory(settings)
        except Exception as error:
            raise HTTPException(503, "Google connection unavailable. Check the local authorization and folder settings in README.md.") from error

    def templates():
        if settings.mode == "demo":
            return [{"id": "demo-harp", "name": "Harp Agreement — sample template"}]
        try:
            return provider().templates()
        except HTTPException:
            raise
        except Exception as error:
            raise HTTPException(502, "Could not list templates. Check Google access and the template folder ID.") from error

    @app.get("/api/workspace")
    def workspace():
        with db() as connection:
            contracts = [dict(row) for row in connection.execute("SELECT id,name,template_id,document_id,document_url,created_at,status,(SELECT MAX(version) FROM renders WHERE contract_id=contracts.id) AS latest_version,(SELECT drive_file_url FROM submissions WHERE contract_id=contracts.id AND version=(SELECT MAX(version) FROM renders WHERE contract_id=contracts.id)) AS final_drive_url,(SELECT drive_save_error FROM submissions WHERE contract_id=contracts.id AND version=(SELECT MAX(version) FROM renders WHERE contract_id=contracts.id)) AS drive_save_error FROM contracts ORDER BY created_at DESC")]
        return {"mode": settings.mode, "contracts": contracts}

    @app.get("/api/templates")
    def list_templates():
        return templates()

    @app.post("/api/contracts", status_code=201)
    def create_contract(payload: ContractInput):
        with lock:
            with db() as connection:
                if connection.execute("SELECT id FROM contracts WHERE name_key=?", (payload.name.casefold(),)).fetchone():
                    raise HTTPException(409, "An agreement with this name already exists. Choose a different name.")
                if payload.template_id not in {t["id"] for t in templates()}:
                    raise HTTPException(400, "Select a template from the configured folder.")
                now = datetime.now(timezone.utc)
                contract_id = "CT-" + uuid4().hex[:12].upper()
                document = {}
                if settings.mode == "google":
                    try:
                        document = provider().copy(payload.template_id, payload.name, contract_id, now)
                    except HTTPException:
                        raise
                    except Exception as error:
                        raise HTTPException(502, "Google could not confirm the copy. Check the destination folder before retrying; a copy may exist.") from error
                record = {"id": contract_id, "name": payload.name, "template_id": payload.template_id,
                          "document_id": document.get("id"), "document_url": document.get("webViewLink"),
                          "created_at": now.isoformat(), "status": "draft"}
                connection.execute("INSERT INTO contracts (id,name,name_key,template_id,document_id,document_url,created_at,status,folder_id) VALUES (?,?,?,?,?,?,?,?,?)",
                    (contract_id, payload.name, payload.name.casefold(), payload.template_id,
                     record["document_id"], record["document_url"], record["created_at"], "draft", document.get("folder_id")))
                return record

    def contract_record(contract_id):
        with db() as connection:
            row = connection.execute("SELECT * FROM contracts WHERE id=?", (contract_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Agreement not found.")
        return dict(row)

    @app.delete("/api/contracts/{contract_id}")
    def delete_contract(contract_id: str, payload: DeleteContractInput):
        with lock:
            contract = contract_record(contract_id)
            if payload.confirmation != contract["name"]:
                raise HTTPException(400, "Type the exact agreement name to confirm permanent deletion.")
            if settings.mode == "google":
                try:
                    drive = provider()
                    folder_id = contract["folder_id"] or drive.contract_folder(contract)
                    # Persist the discovered legacy folder before deleting anything, for retries.
                    with db() as connection:
                        connection.execute("UPDATE contracts SET folder_id=? WHERE id=?", (folder_id, contract_id))
                    with db() as connection:
                        archived = connection.execute('SELECT drive_file_id FROM submissions WHERE contract_id=? AND drive_file_id IS NOT NULL', (contract_id,)).fetchall()
                    for row in archived:
                        drive.delete_archived_pdf(row['drive_file_id'])
                    drive.delete_contract(contract, folder_id)
                except Exception as error:
                    raise HTTPException(502, "Google Drive deletion could not be completed. Local records are retained; some Drive files may already be deleted. Check Google access and retry Delete.") from error
            with db() as connection:
                connection.execute("BEGIN IMMEDIATE")
                for table in ("outbox", "invitations", "workflow_events", "submissions", "render_assets", "renders"):
                    connection.execute(f"DELETE FROM {table} WHERE contract_id=?", (contract_id,))
                connection.execute("DELETE FROM contracts WHERE id=?", (contract_id,))
            return {"deleted": True}

    def saved_render(contract_id, version):
        contract_record(contract_id)
        with db() as connection:
            row = connection.execute("SELECT snapshot FROM renders WHERE contract_id=? AND version=?",
                                     (contract_id, version)).fetchone()
        if row is None:
            raise HTTPException(404, "Rendered version not found. Render this agreement first.")
        return json.loads(row["snapshot"])

    def render_contract(contract_id: str, revision=False):
        with lock:
            contract = contract_record(contract_id)
            if contract['status'] != 'draft' and not revision:
                raise HTTPException(409, 'Submitted agreements are fixed. Use Create revision to make changes.')
            if revision and contract['status'] == 'draft':
                raise HTTPException(409, 'This agreement already has an editable draft.')
            if settings.mode == "demo":
                source, images, metadata = DEMO_HTML, {}, {}
                pdf_content = demo_pdf(source)
            else:
                try:
                    content, metadata = provider().export_contract(contract["document_id"])
                    source, images = unpack_export(content["html"])
                    pdf_content = content["pdf"]
                except HTTPException:
                    raise
                except (RenderError, ValueError) as error:
                    raise HTTPException(422, str(error)) from error
                except Exception as error:
                    raise HTTPException(502, "Unable to export the agreement. Check Google access, wait for Docs to finish saving, and try again.") from error
            try:
                snapshot, page_images = render_pdf(pdf_content, source, images)
                for field in snapshot['fields']:
                    if field['type'] == 'address' and field.get('locked'):
                        error = validate_address(field['default'], settings.address_api_key)
                        if error:
                            raise RenderError(f"{field['label']}: {error}")
            except RenderError as error:
                raise HTTPException(422, str(error)) from error
            now = datetime.now(timezone.utc).isoformat()
            with db() as connection:
                version = connection.execute("SELECT COALESCE(MAX(version),0)+1 FROM renders WHERE contract_id=?", (contract_id,)).fetchone()[0]
                snapshot.update(contract_id=contract_id, name=contract["name"], version=version,
                                created_at=now, source_modified_at=metadata.get("modifiedTime"),
                                source_version=metadata.get("version"), document_url=contract["document_url"],
                                source_sha256=hashlib.sha256(pdf_content).hexdigest(), mode=settings.mode)
                for page in snapshot['pages']:
                    page['image_url'] = f"/api/contracts/{contract_id}/renders/{version}/pages/{page['number']}.png"
                connection.executemany("INSERT INTO render_assets VALUES (?,?,?,?)",
                    [(contract_id, version, f"{index+1}.png", image) for index, image in enumerate(page_images)]
                    + [(contract_id, version, "source.pdf", pdf_content)])
                connection.execute("INSERT INTO renders VALUES (?,?,?,?)", (contract_id, version, now, json.dumps(snapshot)))
                if revision:
                    connection.execute("UPDATE contracts SET status='draft' WHERE id=?", (contract_id,))
                    connection.execute("INSERT INTO workflow_events (contract_id,version,kind,created_at,details) VALUES (?,?,?,?,?)",
                                       (contract_id, version, 'revision_created', now, '{}'))
            return {"version": version, "field_count": len(snapshot["fields"]),
                    "preview_url": f"/preview/{contract_id}?version={version}"}

    @app.post("/api/contracts/{contract_id}/renders", status_code=201)
    def new_render(contract_id: str):
        return render_contract(contract_id)

    @app.post("/api/contracts/{contract_id}/revisions", status_code=201)
    def new_revision(contract_id: str):
        return render_contract(contract_id, revision=True)

    @app.get("/api/contracts/{contract_id}/renders")
    def render_versions(contract_id: str):
        contract_record(contract_id)
        with db() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT version,created_at FROM renders WHERE contract_id=? ORDER BY version DESC", (contract_id,))]

    @app.get("/api/contracts/{contract_id}/renders/{version}")
    def get_render(contract_id: str, version: int):
        return saved_render(contract_id, version)

    def render_asset(contract_id, version, name, mime):
        contract_record(contract_id)
        with db() as connection:
            row = connection.execute("SELECT content FROM render_assets WHERE contract_id=? AND version=? AND name=?",
                                     (contract_id, version, name)).fetchone()
        if row is None:
            raise HTTPException(404, "This render asset was not found. Older HTML previews need to be rendered again.")
        return Response(bytes(row["content"]), media_type=mime,
                        headers={"Cache-Control": "no-store", "Content-Disposition": 'inline'})

    @app.get("/api/contracts/{contract_id}/renders/{version}/pages/{page_number}.png")
    def page_image(contract_id: str, version: int, page_number: int):
        return render_asset(contract_id, version, f"{page_number}.png", "image/png")

    @app.get("/api/contracts/{contract_id}/renders/{version}/source.pdf")
    def source_pdf(contract_id: str, version: int):
        return render_asset(contract_id, version, "source.pdf", "application/pdf")

    @app.post("/api/contracts/{contract_id}/renders/{version}/validate")
    def check_preview(contract_id: str, version: int, values: dict[str, str | bool | dict]):
        snapshot = saved_render(contract_id, version)
        errors = validate_values(snapshot["fields"], values, address_api_key=settings.address_api_key)
        return {"valid": not errors, "errors": errors,
                "message": "Preview check only. Answers are not stored and no signature or submission is recorded."}

    @app.get("/preview/{contract_id}")
    def preview_page(contract_id: str):
        contract_record(contract_id)
        return FileResponse(STATIC / "preview.html")

    app.state.address_api_key = settings.address_api_key
    app.state.email_mode = settings.email_mode
    app.state.signing_timezone = settings.signing_timezone
    handlers = install_workflow(app, db, lock, STATIC)
    from digital_signing.delivery import install_delivery
    install_delivery(app, db, lock, STATIC, handlers, render_asset)

    from digital_signing.mail_transport import install_mail
    install_mail(app, db, lock, settings)
    from digital_signing.drive_archive import install_drive_archive
    install_drive_archive(app, db, lock, settings, provider)

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    return app


app = create_app()
