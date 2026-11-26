"""Local-only delivery rehearsal and version-scoped bearer signing links."""
import html
import hashlib
import json
import re
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator

from digital_signing.workflow import SigningInput
from digital_signing.email_messages import agreement_link, EmailOptions, defaults, message, html_message
from digital_signing.rendering import validate_values
from digital_signing.signing_rules import is_signing_date


class SendInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    recipient_name: str = Field(min_length=1, max_length=200)
    recipient_email: str = Field(max_length=254)
    reviewed: StrictBool
    email_options: EmailOptions | None = None

    @field_validator('recipient_name')
    @classmethod
    def name(cls, value):
        if not value.strip():
            raise ValueError('Enter the recipient name.')
        return value.strip()

    @field_validator('recipient_email')
    @classmethod
    def email(cls, value):
        value = value.strip()
        if not re.fullmatch(r'[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+', value):
            raise ValueError('Enter a valid email address.')
        return value


class CorrectionInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    note: str = Field(min_length=1, max_length=5000)

    @field_validator('note')
    @classmethod
    def nonempty(cls, value):
        if not value.strip():
            raise ValueError('Explain what the client needs to correct.')
        return value.strip()


def install_delivery(app, db, lock, static, handlers, asset):
    with db() as con:
        con.executescript('''
        CREATE TABLE IF NOT EXISTS invitations (
            contract_id TEXT NOT NULL, version INTEGER NOT NULL, token_hash TEXT UNIQUE NOT NULL,
            recipient_name TEXT NOT NULL, recipient_email TEXT NOT NULL,
            created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
            PRIMARY KEY(contract_id, version));
        CREATE TABLE IF NOT EXISTS outbox (
            id INTEGER PRIMARY KEY, contract_id TEXT NOT NULL, version INTEGER NOT NULL,
            kind TEXT NOT NULL, recipient_email TEXT NOT NULL, subject TEXT NOT NULL,
            body TEXT NOT NULL, link TEXT NOT NULL, created_at TEXT NOT NULL,
            UNIQUE(contract_id,version,kind));
        ''')

        if 'html_body' not in {row[1] for row in con.execute('PRAGMA table_info(outbox)')}:
            con.execute('ALTER TABLE outbox ADD COLUMN html_body TEXT')
        if 'attach_final_pdf' not in {row[1] for row in con.execute('PRAGMA table_info(outbox)')}:
            con.execute('ALTER TABLE outbox ADD COLUMN attach_final_pdf INTEGER NOT NULL DEFAULT 0')
        columns = {row[1] for row in con.execute('PRAGMA table_info(invitations)')}
        for column in ('email_options', 'completion_token_hash', 'completion_expires_at'):
            if column not in columns:
                con.execute(f'ALTER TABLE invitations ADD COLUMN {column} TEXT')
        con.execute('CREATE UNIQUE INDEX IF NOT EXISTS completion_tokens ON invitations(completion_token_hash)')
        con.execute('CREATE TABLE IF NOT EXISTS email_defaults (id INTEGER PRIMARY KEY CHECK(id=1), options TEXT NOT NULL)')

    @app.get('/api/email-defaults')
    def get_defaults():
        with db() as con:
            return defaults(con).model_dump()

    @app.put('/api/email-defaults')
    def save_defaults(payload: EmailOptions):
        with lock, db() as con:
            con.execute('INSERT INTO email_defaults VALUES (1,?) ON CONFLICT(id) DO UPDATE SET options=excluded.options', (payload.model_dump_json(),))
        return payload.model_dump()

    def prepare_email(contract_id: str, version: int, payload: SendInput):
        if not payload.reviewed:
            raise HTTPException(422, 'Review the rendered document and confirm it is ready to send.')
        with lock, db() as con:
            con.execute('BEGIN IMMEDIATE')
            contract = con.execute('SELECT * FROM contracts WHERE id=?', (contract_id,)).fetchone()
            if not contract:
                raise HTTPException(404, 'Agreement not found.')
            latest = con.execute('SELECT MAX(version) FROM renders WHERE contract_id=?', (contract_id,)).fetchone()[0]
            if version != latest:
                raise HTTPException(409, 'Review the latest rendered version before sending.')
            existing = con.execute('SELECT * FROM invitations WHERE contract_id=? AND version=?', (contract_id, version)).fetchone()
            if existing:
                if existing['recipient_email'] != payload.recipient_email or existing['recipient_name'] != payload.recipient_name:
                    raise HTTPException(409, 'This version already has a recipient. Create a revision to send to someone else.')
                if payload.email_options is not None and EmailOptions.model_validate_json(existing['email_options'] or '{}') != payload.email_options:
                    raise HTTPException(409, 'This email has already been created. Its message and expiration cannot be changed; create a revision instead.')
                row = con.execute("SELECT * FROM outbox WHERE contract_id=? AND version=? AND kind='invitation'", (contract_id, version)).fetchone()
                return dict(row)
            if contract['status'] != 'draft':
                raise HTTPException(409, 'Create a revision before sending this agreement again.')
            snapshot = json.loads(con.execute('SELECT snapshot FROM renders WHERE contract_id=? AND version=?', (contract_id, version)).fetchone()[0])
            if not snapshot.get('pages') or not any(f['role']=='client' and f['type']=='signature' for f in snapshot['fields']):
                raise HTTPException(422, 'Render a document with a client signature field before sending.')
            return queue_invitation(con,contract,version,payload)

    def queue_invitation(con, contract, version, payload, note=None):
        contract_id = contract['id']
        token = secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)
        link = f'/sign/{token}'
        options = payload.email_options or defaults(con)
        con.execute('INSERT INTO invitations (contract_id,version,token_hash,recipient_name,recipient_email,created_at,expires_at,email_options) VALUES (?,?,?,?,?,?,?,?)', (contract_id,version,hashlib.sha256(token.encode()).hexdigest(),payload.recipient_name,payload.recipient_email,now.isoformat(),(now+timedelta(days=options.signing_days)).isoformat(),options.model_dump_json()))
        title = re.sub(r'\{(recipient_name|contract_name)\}', lambda m: {'recipient_name':payload.recipient_name,'contract_name':contract['name']}[m[1]], options.invitation_title)
        title = title.replace('\r',' ').replace('\n',' ')
        subject = ('Corrections requested for ' if note else 'Signature request for ') + title
        body = message(options.invitation_body,payload.recipient_name,contract['name'],link,options.signing_days,subject=title)
        formatted = html_message(options.invitation_body,payload.recipient_name,contract['name'],link,options.signing_days,subject=title)
        if note:
            intro = 'Please correct the answers below, review the agreement, and sign again. Your previous answers are already filled in.'
            body = intro + '\n\nRequested corrections:\n' + note + message('',payload.recipient_name,contract['name'],link,options.signing_days)
            formatted = ('<p>' + intro + '</p><p><strong>Requested corrections</strong><br>'
                         + html.escape(note).replace('\n','<br>') + '</p>'
                         + agreement_link(link)
                         + f'<p>This private link expires in <strong>{options.signing_days} days</strong>. Keep it private; anyone with the link can access this agreement.</p>')
        con.execute('INSERT INTO outbox (contract_id,version,kind,recipient_email,subject,body,link,created_at,html_body) VALUES (?,?,?,?,?,?,?,?,?)', (contract_id,version,'invitation',payload.recipient_email,subject,body,link,now.isoformat(),formatted))
        if app.state.email_mode == 'gmail':
            con.execute("UPDATE outbox SET delivery_status='pending' WHERE contract_id=? AND version=? AND kind='invitation'", (contract_id,version))
        con.execute("UPDATE contracts SET status='sent' WHERE id=?", (contract_id,))
        return dict(con.execute("SELECT * FROM outbox WHERE contract_id=? AND version=? AND kind='invitation'", (contract_id,version)).fetchone())

    @app.post('/api/contracts/{contract_id}/renders/{version}/send')
    def send(contract_id: str, version: int, payload: SendInput):
        email = prepare_email(contract_id,version,payload)
        return app.state.deliver_email(email['id'])

    @app.post('/api/contracts/{contract_id}/renders/{version}/request-corrections')
    def request_corrections(contract_id: str, version: int, payload: CorrectionInput):
        with lock, db() as con:
            con.execute('BEGIN IMMEDIATE')
            contract = con.execute('SELECT * FROM contracts WHERE id=?', (contract_id,)).fetchone()
            if not contract:
                raise HTTPException(404, 'Agreement not found.')
            latest = con.execute('SELECT MAX(version) FROM renders WHERE contract_id=?', (contract_id,)).fetchone()[0]
            submission = con.execute('SELECT * FROM submissions WHERE contract_id=? AND version=?', (contract_id,version)).fetchone()
            if version != latest or contract['status'] != 'awaiting_vendor' or not submission or submission['vendor_record']:
                raise HTTPException(409, 'Corrections can only be requested for the latest submission awaiting your signature.')
            invite = con.execute('SELECT * FROM invitations WHERE contract_id=? AND version=?', (contract_id,version)).fetchone()
            if not invite:
                raise HTTPException(409, 'This submission has no client email invitation. Create a revision and send it from the workspace.')
            snapshot = json.loads(con.execute('SELECT snapshot FROM renders WHERE contract_id=? AND version=?', (contract_id,version)).fetchone()[0])
            answers = json.loads(submission['client_record'])['values']
            # Copy the immutable document, not a potentially edited Google Doc.
            new_version = version + 1
            now = datetime.now(timezone.utc).isoformat()
            snapshot.update(version=new_version, created_at=now, correction_from=version, correction_note=payload.note,
                            prefill_values={f['key']:answers[f['key']] for f in snapshot['fields']
                                            if f['role']=='client' and f['type'] not in {'signature','initials'}
                                            and not is_signing_date(f) and not f.get('calc') and f['key'] in answers})
            for page in snapshot['pages']:
                page['image_url'] = f"/api/contracts/{contract_id}/renders/{new_version}/pages/{page['number']}.png"
            con.execute('INSERT INTO renders VALUES (?,?,?,?)', (contract_id,new_version,now,json.dumps(snapshot)))
            con.execute('INSERT INTO render_assets SELECT contract_id, ?, name, content FROM render_assets WHERE contract_id=? AND version=?',
                        (new_version,contract_id,version))
            options = EmailOptions.model_validate_json(invite['email_options'] or '{}')
            email = queue_invitation(con,contract,new_version,SendInput(recipient_name=invite['recipient_name'],
                                     recipient_email=invite['recipient_email'],reviewed=True,email_options=options),payload.note)
            con.execute('INSERT INTO workflow_events (contract_id,version,kind,created_at,details) VALUES (?,?,?,?,?)',
                        (contract_id,new_version,'corrections_requested',now,json.dumps({'previous_version':version,'note':payload.note})))
        # Commit the revision and email together before attempting delivery.
        return {'version':new_version, 'email':app.state.deliver_email(email['id'])}

    @app.get('/api/outbox')
    def outbox():
        with db() as con:
            return [dict(row) for row in con.execute('SELECT o.*, c.name FROM outbox o JOIN contracts c ON c.id=o.contract_id ORDER BY o.id DESC')]

    def resolve(token, version=None):
        with db() as con:
            invite = con.execute('SELECT * FROM invitations WHERE token_hash=? OR completion_token_hash=?', (hashlib.sha256(token.encode()).hexdigest(),)*2).fetchone()
            if not invite:
                raise HTTPException(404, 'This signing link is not available.')
            latest = con.execute('SELECT MAX(version) FROM renders WHERE contract_id=?', (invite['contract_id'],)).fetchone()[0]
            completed_link = invite['completion_token_hash'] == hashlib.sha256(token.encode()).hexdigest()
            expiry = invite['completion_expires_at'] if completed_link else invite['expires_at']
            if invite['version'] != latest or datetime.fromisoformat(expiry) <= datetime.now(timezone.utc):
                raise HTTPException(410, 'This signing link has expired or been replaced. Please request a new link.')
            if version is not None and version != invite['version']:
                raise HTTPException(404, 'Version not available through this link.')
            return {**dict(invite), 'completion_link':completed_link}

    def client_state(token, invite, state):
        state = dict(state)
        prefix = f'/api/sign/{token}/{invite["version"]}'
        for kind in ('client', 'final'):
            if state[f'{kind}_pdf_url']:
                state[f'{kind}_pdf_url'] = f'{prefix}/{kind}.pdf'
        return state

    @app.get('/sign/{token}')
    def page(token: str):
        resolve(token)
        return FileResponse(static / 'preview.html', headers={'Cache-Control':'no-store','Referrer-Policy':'no-referrer'})

    @app.get('/api/sign/{token}')
    def versions(token: str):
        invite = resolve(token)
        return [{'version':invite['version'], 'created_at':invite['created_at']}]

    @app.get('/api/sign/{token}/{version}')
    def snapshot(token: str, version: int):
        invite = resolve(token, version)
        with db() as con:
            result = json.loads(con.execute('SELECT snapshot FROM renders WHERE contract_id=? AND version=?', (invite['contract_id'],version)).fetchone()[0])
        result.pop('document_url', None)
        for page in result['pages']:
            page['image_url'] = f'/api/sign/{token}/{version}/pages/{page["number"]}.png'
        return result

    @app.get('/api/sign/{token}/{version}/workflow')
    def state(token: str, version: int):
        invite = resolve(token, version)
        return client_state(token, invite, handlers['state'](invite['contract_id'],version))

    @app.get('/api/sign/{token}/{version}/pages/{page_number}.png')
    def image(token: str, version: int, page_number: int):
        invite = resolve(token, version)
        return asset(invite['contract_id'],version,f'{page_number}.png','image/png')

    @app.post('/api/sign/{token}/{version}/validate')
    def validate(token: str, version: int, values: dict[str,str|bool|dict]):
        errors = validate_values(snapshot(token,version)['fields'], values, address_api_key=app.state.address_api_key)
        return {'valid':not errors,'errors':errors}

    @app.post('/api/sign/{token}/{version}/submit')
    def submit(token: str, version: int, payload: SigningInput):
        # Same reentrant lock covers resolution and commit, so revisions/deletion cannot race it.
        with lock:
            invite = resolve(token,version)
            if invite['completion_link']:
                raise HTTPException(403, 'This link is for viewing the completed agreement only.')
            return client_state(token, invite, handlers['submit'](invite['contract_id'],version,payload))

    @app.get('/api/sign/{token}/{version}/{kind}.pdf')
    def pdf(token: str, version: int, kind: str):
        invite = resolve(token,version)
        return handlers['pdf'](invite['contract_id'],version,kind)
