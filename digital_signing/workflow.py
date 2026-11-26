"""Local submission and countersignature workflow with immutable evidence and PDFs."""
import hashlib
import json
from datetime import datetime, timezone

from fastapi import HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from digital_signing.rendering import validate_values
from digital_signing.completed_pdf import create_completed_pdf
from digital_signing.signing_rules import normalize_signer, signing_day

CLIENT_CONSENT = 'I have reviewed this agreement, agree to its terms, and intend the signature I provided to be my electronic signature.'
VENDOR_CONSENT = 'I have reviewed the client submission and intend the signature I provided to countersign this agreement.'


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class SigningInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    signer_name: str | None = Field(default=None, min_length=1, max_length=5000)
    consent: bool = Field(strict=True)
    values: dict[str, str | bool | dict]

    @field_validator('signer_name')
    @classmethod
    def name(cls, value):
        if value is None: return None
        value = value.strip()
        if not value or any(ord(c) < 32 for c in value):
            raise ValueError('Enter your full name.')
        return value


def vendor_fields(snapshot):
    result = [dict(f) for f in snapshot['fields'] if f['role'] == 'vendor']
    if not any(f['type'] == 'signature' for f in result):
        key = 'vendor.completion_signature'
        keys = {f['key'] for f in snapshot['fields']}
        suffix = 1
        while key in keys:
            key = f'vendor.completion_signature_{suffix}'
            suffix += 1
        result.append({'key':key, 'type':'signature', 'label':'Your signature',
                       'required':True, 'role':'vendor', 'options':[]})
    return result


def install_workflow(app, db, lock, static):
    with db() as con:
        con.executescript('''
        CREATE TABLE IF NOT EXISTS submissions (
          contract_id TEXT NOT NULL, version INTEGER NOT NULL,
          client_record TEXT NOT NULL, client_hash TEXT NOT NULL, client_pdf BLOB NOT NULL,
          vendor_record TEXT, vendor_hash TEXT, final_pdf BLOB,
          PRIMARY KEY(contract_id, version));
        CREATE TABLE IF NOT EXISTS workflow_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT, contract_id TEXT NOT NULL,
          version INTEGER NOT NULL, kind TEXT NOT NULL, created_at TEXT NOT NULL, details TEXT NOT NULL);
        ''')

    def context(con, contract_id, version):
        row = con.execute('SELECT snapshot FROM renders WHERE contract_id=? AND version=?', (contract_id, version)).fetchone()
        if not row:
            raise HTTPException(404, 'Rendered agreement not found.')
        snapshot = json.loads(row['snapshot'])
        latest = con.execute('SELECT MAX(version) FROM renders WHERE contract_id=?', (contract_id,)).fetchone()[0]
        return snapshot, latest

    def record(con, contract_id, version):
        return con.execute('SELECT * FROM submissions WHERE contract_id=? AND version=?', (contract_id, version)).fetchone()

    def status(con, contract_id, version):
        snapshot, latest = context(con, contract_id, version)
        row = record(con, contract_id, version)
        state = ('completed' if row['final_pdf'] else 'awaiting_vendor') if row else 'editable'
        sent = bool(con.execute('SELECT 1 FROM invitations WHERE contract_id=? AND version=?', (contract_id,version)).fetchone())
        return {'state':state, 'sent':sent, 'version':version, 'superseded':version!=latest,
                'client':json.loads(row['client_record']) if row else None,
                'vendor':json.loads(row['vendor_record']) if row and row['vendor_record'] else None,
                'client_pdf_url': f'/api/contracts/{contract_id}/submissions/{version}/client.pdf' if row else None,
                'final_pdf_url': f'/api/contracts/{contract_id}/submissions/{version}/final.pdf' if row and row['final_pdf'] else None,
                'signing_date':signing_day(datetime.now(timezone.utc).isoformat(),app.state.signing_timezone),
                'vendor_fields':vendor_fields(snapshot), 'client_consent':CLIENT_CONSENT, 'vendor_consent':VENDOR_CONSENT}

    def validate(payload, fields, role):
        if not payload.consent:
            raise HTTPException(422, {'message':('Before submitting your countersignature and completing the agreement' if role == 'vendor' else 'Before submitting your answers and electronic signature to the vendor') + ', check the box to confirm that you have reviewed the agreement, agree to its terms, and intend to sign electronically.', 'errors':{}})
        selected = [dict(f, role='client') for f in fields if f['role'] == role]
        errors = validate_values(selected, payload.values, address_api_key=app.state.address_api_key)
        if errors:
            raise HTTPException(422, {'message':'Please correct the highlighted fields.', 'errors':errors})
        if not any(f['type']=='signature' and payload.values.get(f['key']) for f in selected):
            raise HTTPException(422, {'message':'At least one signature is required. Add a signature field before submitting.', 'errors':{}})

    def pdf(con, snapshot, values, client, vendor=None):
        rows = con.execute('SELECT name,content FROM render_assets WHERE contract_id=? AND version=?',
                           (snapshot['contract_id'], snapshot['version'])).fetchall()
        assets = {row['name']:bytes(row['content']) for row in rows}
        if not snapshot.get('pages'):
            raise HTTPException(409, 'Render this agreement again before submitting; this version has no PDF page layout.')
        try:
            return create_completed_pdf(snapshot, [assets[f"{p['number']}.png"] for p in snapshot['pages']], values, client, vendor)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    def signing_record(payload, consent, timestamp):
        return {'signer_name':payload.signer_name, 'signed_at':timestamp,
                'consent_text':consent, 'consent':True, 'values':payload.values}

    @app.get('/api/contracts/{contract_id}/renders/{version}/workflow')
    def workflow_state(contract_id: str, version: int):
        with db() as con:
            return status(con, contract_id, version)

    def submit_record(contract_id: str, version: int, payload: SigningInput):
        with lock, db() as con:
            con.execute('BEGIN IMMEDIATE')
            snapshot, latest = context(con, contract_id, version)
            existing = record(con, contract_id, version)
            timestamp = json.loads(existing['client_record'])['signed_at'] if existing else datetime.now(timezone.utc).isoformat()
            payload = normalize_signer(payload, snapshot['fields'], 'client', timestamp, app.state.signing_timezone)
            request_hash = digest(payload.model_dump())
            if existing:
                if existing['client_hash'] != request_hash:
                    raise HTTPException(409, 'This version is already submitted and cannot be changed. Create a revision instead.')
                return status(con, contract_id, version)
            if latest != version:
                raise HTTPException(409, 'A newer agreement version exists. Open the latest version before submitting.')
            validate(payload, snapshot['fields'], 'client')
            client = signing_record(payload, CLIENT_CONSENT, timestamp)
            content = pdf(con, snapshot, payload.values, client)
            con.execute('INSERT INTO submissions (contract_id,version,client_record,client_hash,client_pdf) VALUES (?,?,?,?,?)',
                        (contract_id, version, canonical(client), request_hash, content))
            con.execute("UPDATE contracts SET status='awaiting_vendor' WHERE id=?", (contract_id,))
            con.execute('INSERT INTO workflow_events (contract_id,version,kind,created_at,details) VALUES (?,?,?,?,?)',
                        (contract_id, version, 'client_submitted', client['signed_at'], canonical({'pdf_sha256':hashlib.sha256(content).hexdigest(), 'source_sha256':snapshot['source_sha256']})))
            app.state.queue_vendor_alert(con, snapshot, client)
            return status(con, contract_id, version)

    @app.post('/api/contracts/{contract_id}/renders/{version}/submit')
    def submit(contract_id: str, version: int, payload: SigningInput):
        result = submit_record(contract_id,version,payload)
        app.state.deliver_vendor_alert(contract_id,version)
        return result

    def countersign_record(contract_id: str, version: int, payload: SigningInput):
        with lock, db() as con:
            con.execute('BEGIN IMMEDIATE')
            snapshot, latest = context(con, contract_id, version)
            existing = record(con, contract_id, version)
            if not existing:
                raise HTTPException(409, 'The client must submit this agreement first.')
            timestamp = json.loads(existing['vendor_record'])['signed_at'] if existing['vendor_record'] else datetime.now(timezone.utc).isoformat()
            payload = normalize_signer(payload, vendor_fields(snapshot), 'vendor', timestamp, app.state.signing_timezone)
            request_hash = digest(payload.model_dump())
            if existing['vendor_record']:
                if existing['vendor_hash'] != request_hash:
                    raise HTTPException(409, 'This version is fully executed and cannot be changed.')
                return status(con, contract_id, version)
            if latest != version:
                raise HTTPException(409, 'This submission was superseded by a revision. Countersign the latest client submission.')
            validate(payload, vendor_fields(snapshot), 'vendor')
            client = json.loads(existing['client_record'])
            vendor = signing_record(payload, VENDOR_CONSENT, timestamp)
            content = pdf(con, snapshot, {**client['values'], **payload.values}, client, vendor)
            con.execute('UPDATE submissions SET vendor_record=?,vendor_hash=?,final_pdf=? WHERE contract_id=? AND version=?',
                        (canonical(vendor), request_hash, content, contract_id, version))
            con.execute("UPDATE contracts SET status='completed' WHERE id=?", (contract_id,))
            con.execute('INSERT INTO workflow_events (contract_id,version,kind,created_at,details) VALUES (?,?,?,?,?)',
                        (contract_id, version, 'vendor_signed', vendor['signed_at'], canonical({'pdf_sha256':hashlib.sha256(content).hexdigest()})))
            from digital_signing.email_messages import queue_completion
            queue_completion(con, contract_id, version, snapshot['name'], vendor['signed_at'], app.state.vendor_recipient())
            if app.state.email_mode == 'gmail':
                con.execute("UPDATE outbox SET delivery_status='pending' WHERE contract_id=? AND version=? AND kind IN ('completed','completed_vendor')", (contract_id,version))
            return status(con, contract_id, version)

    @app.post('/api/contracts/{contract_id}/renders/{version}/countersign')
    def countersign(contract_id: str, version: int, payload: SigningInput):
        result = countersign_record(contract_id,version,payload)
        # Archive only after the signatures and final PDF have committed.
        result['drive_archive'] = app.state.save_final_pdf_to_drive(contract_id,version)
        email = app.state.deliver_completion(contract_id,version)
        if email and email['delivery_status'] != 'local':
            result['email_delivery'] = {'status':email['delivery_status'], 'error':email['delivery_error']}
        return result

    @app.get('/api/contracts/{contract_id}/submissions/{version}/{kind}.pdf')
    def completion_pdf(contract_id: str, version: int, kind: str):
        if kind not in {'client','final'}:
            raise HTTPException(404, 'PDF not found.')
        with db() as con:
            row = record(con, contract_id, version)
            content = row[f'{kind}_pdf'] if row else None
        if not content:
            raise HTTPException(404, 'This PDF is not available yet.')
        return Response(bytes(content), media_type='application/pdf', headers={
            'Cache-Control':'private, no-store',
            'Content-Disposition':f'attachment; filename="{contract_id}-v{version}-{kind}.pdf"'})

    @app.get('/api/notifications')
    def notifications():
        with db() as con:
            return [dict(row) for row in con.execute('''SELECT e.id,e.contract_id,e.version,e.kind,e.created_at,c.name
                FROM workflow_events e JOIN contracts c ON c.id=e.contract_id ORDER BY e.id DESC''')]

    @app.get('/countersign/{contract_id}')
    def countersign_page(contract_id: str):
        with db() as con:
            if not con.execute('SELECT id FROM contracts WHERE id=?', (contract_id,)).fetchone():
                raise HTTPException(404, 'Agreement not found.')
        return FileResponse(static / 'preview.html')

    return {"state":workflow_state, "submit":submit, "pdf":completion_pdf}
