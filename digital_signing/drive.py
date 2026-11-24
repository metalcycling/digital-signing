import re
import hashlib
from io import BytesIO
from googleapiclient.http import MediaIoBaseUpload
from datetime import datetime
from googleapiclient.errors import HttpError
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

DOC = "application/vnd.google-apps.document"
FOLDER = "application/vnd.google-apps.folder"


def safe_id(value):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError("Configure valid Google Drive folder IDs in .env.")
    return value


class GoogleDrive:
    def __init__(self, settings):
        token = settings.data_dir / "google-token.json"
        if not token.exists():
            raise ValueError("Google is not connected. Follow the README authorization steps.")
        credentials = Credentials.from_authorized_user_file(str(token))
        if credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
            token.write_text(credentials.to_json())
        self.api = build("drive", "v3", credentials=credentials, cache_discovery=False)
        self.settings = settings

    def children(self, parent, mime):
        result, page = [], None
        while True:
            response = self.api.files().list(
                q=f"'{safe_id(parent)}' in parents and trashed = false and mimeType = '{mime}'",
                fields="nextPageToken,files(id,name,webViewLink)", pageSize=100,
                pageToken=page, orderBy="name", supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            ).execute()
            result.extend(response.get("files", []))
            page = response.get("nextPageToken")
            if not page:
                return result

    def templates(self):
        return self.children(self.settings.template_folder_id, DOC)

    def folder(self, parent, name):
        existing = next((f for f in self.children(parent, FOLDER) if f["name"] == name), None)
        if existing:
            return existing["id"]
        return self.api.files().create(body={"name": name, "mimeType": FOLDER, "parents": [parent]},
                                       fields="id", supportsAllDrives=True).execute()["id"]

    def export_contract(self, document_id):
        # Check for concurrent edits so the snapshot metadata describes its content.
        def metadata():
            return self.api.files().get(fileId=document_id,
                fields="id,name,mimeType,modifiedTime,version,trashed", supportsAllDrives=True).execute()
        before = metadata()
        if before.get("trashed") or before["mimeType"] != DOC:
            raise ValueError("The agreement is no longer an active Google Doc.")
        html = self.api.files().export(fileId=document_id, mimeType="application/zip").execute()
        pdf = self.api.files().export(fileId=document_id, mimeType="application/pdf").execute()
        content = {"html": html, "pdf": pdf}
        after = metadata()
        if before.get("version") != after.get("version"):
            raise ValueError("The document changed during export. Wait for Google Docs to save and render again.")
        return content, after

    def copy(self, template_id, title, contract_id, created):
        root = safe_id(self.settings.contracts_folder_id)
        if root == self.settings.template_folder_id:
            raise ValueError("Use a separate agreements folder, not the template folder.")
        meta = self.api.files().get(fileId=root, fields="id,mimeType,trashed", supportsAllDrives=True).execute()
        if meta.get("trashed") or meta["mimeType"] != FOLDER:
            raise ValueError("The agreements destination must be an active Google Drive folder.")
        year = self.folder(root, created.strftime("%Y"))
        month = self.folder(year, created.strftime("%m"))
        folder = self.folder(month, f"{contract_id} - {title}")
        # One copy request; do not automatically retry this non-idempotent mutation.
        document = self.api.files().copy(fileId=template_id,
            body={"name": title, "parents": [folder]}, fields="id,name,webViewLink",
            supportsAllDrives=True).execute()

        document["folder_id"] = folder
        return document

    def reserve_pdf_id(self):
        return self.api.files().generateIds(count=1, space='drive', type='files').execute()['ids'][0]

    def save_final_pdf(self, contract, version, file_id, content):
        # Persist a preallocated ID locally before upload so uncertain retries cannot duplicate files.
        expected_hash = hashlib.md5(content).hexdigest()
        try:
            existing = self.api.files().get(fileId=safe_id(file_id),
                fields='id,webViewLink,mimeType,md5Checksum,trashed', supportsAllDrives=True).execute()
        except HttpError as error:
            if error.resp.status != 404:
                raise
        else:
            if existing.get('trashed') or existing.get('mimeType') != 'application/pdf' or existing.get('md5Checksum') != expected_hash:
                raise ValueError('The archived PDF was changed or removed in Drive.')
            return existing
        document = self.api.files().get(fileId=safe_id(contract['document_id']),
            fields='id,mimeType,parents,trashed', supportsAllDrives=True).execute()
        if document.get('trashed') or document.get('mimeType') != DOC or len(document.get('parents', [])) != 1:
            raise ValueError('Cannot locate the copied agreement’s current folder.')
        return self.api.files().create(
            body={'id':file_id, 'name':f"{contract['name']} - v{version} - Fully executed.pdf",
                  'mimeType':'application/pdf', 'parents':document['parents']},
            media_body=MediaIoBaseUpload(BytesIO(content), mimetype='application/pdf', resumable=False),
            fields='id,webViewLink', supportsAllDrives=True).execute()

    def delete_archived_pdf(self, file_id):
        try:
            self.api.files().delete(fileId=safe_id(file_id), supportsAllDrives=True).execute()
        except HttpError as error:
            if error.resp.status != 404:
                raise

    def contract_folder(self, contract):
        """Find the original dedicated folder for agreements created before folder tracking."""
        created = datetime.fromisoformat(contract["created_at"])
        parent = self.settings.contracts_folder_id
        for name in (created.strftime("%Y"), created.strftime("%m"), f"{contract['id']} - {contract['name']}"):
            matches = [f for f in self.children(parent, FOLDER) if f["name"] == name]
            if len(matches) != 1:
                raise ValueError("Cannot uniquely locate the agreement folder.")
            parent = matches[0]["id"]
        return parent

    def delete_contract(self, contract, folder_id):
        protected = {contract["template_id"], self.settings.template_folder_id, self.settings.contracts_folder_id}
        targets = [contract["document_id"], folder_id]
        if any(not target or target in protected for target in targets):
            raise ValueError("Refusing to delete a template or shared storage folder.")
        # Delete the document explicitly too, in case it was moved out of its folder.
        for target in targets:
            try:
                self.api.files().delete(fileId=safe_id(target), supportsAllDrives=True).execute()
            except HttpError as error:
                if error.resp.status != 404:
                    raise
