"""Gmail SMTP delivery of committed outbox messages; never sends during setup."""
import html
import json
import smtplib
import ssl
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formataddr, formatdate
from urllib.parse import urlsplit

from fastapi import HTTPException

from digital_signing.email_messages import agreement_link


def credentials(settings):
    path = settings.data_dir / 'gmail-credentials.json'
    data = json.loads(path.read_text())
    if not data.get('email') or not data.get('app_password'):
        raise ValueError('Missing Gmail credentials.')
    return data


def build_message(row, settings, sender, final_pdf=None):
    base = settings.email_base_url.rstrip('/')
    parsed = urlsplit(base)
    if parsed.scheme not in ('http','https') or not parsed.netloc or parsed.query or parsed.fragment or parsed.username:
        raise ValueError('Invalid email base URL.')
    link = base + row['link']
    msg = EmailMessage()
    msg['From'] = formataddr(('Bello Entertainment', sender))
    msg['To'] = row['recipient_email']
    msg['Subject'] = row['subject']
    msg['Date'] = formatdate(localtime=False)
    msg['Message-ID'] = f'<{row["contract_id"]}.{row["version"]}.{row["kind"]}@digital-signing.local>'
    msg.set_content(row['body'].replace(row['link'], link))
    if row['html_body']:
        msg.add_alternative(row['html_body'].replace(f'href="{html.escape(row["link"], quote=True)}"', f'href="{html.escape(link, quote=True)}"'), subtype='html')
    if row.get('attach_final_pdf'):
        if not final_pdf:
            raise ValueError('The fully executed PDF is missing.')
        msg.add_attachment(final_pdf, maintype='application', subtype='pdf',
                           filename=row['attachment_filename'])
    return msg


def install_mail(app, db, lock, settings):
    with db() as con:
        columns = {row[1] for row in con.execute('PRAGMA table_info(outbox)')}
        for column, kind in [('delivery_status', "TEXT NOT NULL DEFAULT 'local'"), ('delivery_error','TEXT'), ('sent_at','TEXT')]:
            if column not in columns:
                con.execute(f'ALTER TABLE outbox ADD COLUMN {column} {kind}')
        # A process stopped during SMTP may already have handed the email to Gmail.
        con.execute("UPDATE outbox SET delivery_status='unknown', delivery_error='The app stopped during sending. Check Gmail Sent before retrying.' WHERE delivery_status='sending'")

    def deliver(message_id, retry=False):
        with lock:
            with db() as con:
                row = con.execute('SELECT * FROM outbox WHERE id=?', (message_id,)).fetchone()
                if not row:
                    raise HTTPException(404, 'Email not found.')
                row = dict(row)
                if row['delivery_status']=='sent' or (not retry and row['delivery_status']!='pending'):
                    return row
                if settings.email_mode != 'gmail':
                    raise HTTPException(409, 'Enable Gmail delivery first.')
                invite = con.execute('SELECT * FROM invitations WHERE contract_id=? AND version=?', (row['contract_id'],row['version'])).fetchone()
                latest = con.execute('SELECT MAX(version) FROM renders WHERE contract_id=?', (row['contract_id'],)).fetchone()[0]
                if row['kind'] in {'completed', 'completed_vendor'}:
                    contract = con.execute('SELECT status FROM contracts WHERE id=?', (row['contract_id'],)).fetchone()
                    inactive = latest != row['version'] or not contract or contract['status'] != 'completed'
                    reason = 'This agreement version is no longer current.'
                elif row['kind'] == 'vendor_alert':
                    contract = con.execute('SELECT status FROM contracts WHERE id=?', (row['contract_id'],)).fetchone()
                    inactive = latest != row['version'] or not contract or contract['status'] != 'awaiting_vendor'
                    reason = 'This agreement no longer needs a vendor signature.'
                else:
                    expiry = invite['expires_at'] if invite else None
                    inactive = latest != row['version'] or not expiry or datetime.fromisoformat(expiry) <= datetime.now(timezone.utc)
                    reason = 'This link expired or was replaced. Create a new revision.'
                if inactive:
                    con.execute("UPDATE outbox SET delivery_status='failed', delivery_error=? WHERE id=?", (reason,message_id))
                    return dict(con.execute('SELECT * FROM outbox WHERE id=?', (message_id,)).fetchone())
                final_pdf = None
                if row['attach_final_pdf']:
                    saved = con.execute('SELECT submissions.final_pdf, contracts.name FROM submissions JOIN contracts ON contracts.id=submissions.contract_id WHERE submissions.contract_id=? AND submissions.version=?', (row['contract_id'],row['version'])).fetchone()
                    if not saved or not saved['final_pdf']:
                        con.execute("UPDATE outbox SET delivery_status='failed',delivery_error='The fully executed PDF is unavailable.' WHERE id=?", (message_id,))
                        return dict(con.execute('SELECT * FROM outbox WHERE id=?',(message_id,)).fetchone())
                    final_pdf = bytes(saved['final_pdf'])
                    row['attachment_filename'] = saved['name'] + '.pdf'
                con.execute("UPDATE outbox SET delivery_status='sending', delivery_error=NULL WHERE id=?", (message_id,))
            smtp = None
            attempted = False
            status, error, sent_at = 'failed', None, None
            try:
                auth = credentials(settings)
                if row['kind'] in {'vendor_alert','completed_vendor'} and not row['recipient_email']:
                    row['recipient_email'] = settings.vendor_email or auth['email']
                    with db() as con:
                        con.execute('UPDATE outbox SET recipient_email=? WHERE id=?', (row['recipient_email'],message_id))
                msg = build_message(row, settings, auth['email'], final_pdf)
                smtp = smtplib.SMTP('smtp.gmail.com', 587, timeout=20)
                smtp.ehlo()
                smtp.starttls(context=ssl.create_default_context())
                smtp.ehlo()
                smtp.login(auth['email'], auth['app_password'])
                attempted = True
                refused = smtp.send_message(msg)
                if refused:
                    raise smtplib.SMTPRecipientsRefused(refused)
                status, sent_at = 'sent', datetime.now(timezone.utc).isoformat()
            except smtplib.SMTPAuthenticationError:
                error = 'Gmail rejected the credentials. Check the Gmail address and app password.'
            except (smtplib.SMTPRecipientsRefused, smtplib.SMTPDataError, smtplib.SMTPSenderRefused):
                error = 'Gmail rejected the email. Check the recipient and Gmail sending limits before retrying.'
            except Exception:
                if attempted:
                    status = 'unknown'
                    error = 'Delivery could not be confirmed. Check Gmail Sent before retrying to avoid a duplicate.'
                else:
                    error = 'Could not connect to Gmail. Check the local Gmail setup and internet connection.'
            finally:
                if smtp:
                    try: smtp.close()
                    except Exception: pass
            with db() as con:
                con.execute('UPDATE outbox SET delivery_status=?,delivery_error=?,sent_at=? WHERE id=?', (status,error,sent_at,message_id))
                return dict(con.execute('SELECT * FROM outbox WHERE id=?', (message_id,)).fetchone())

    def deliver_contract(contract_id, version, kind='completed'):
        with db() as con:
            row = con.execute("SELECT id FROM outbox WHERE contract_id=? AND version=? AND kind=?", (contract_id,version,kind)).fetchone()
        return deliver(row['id']) if row else None

    def vendor_recipient():
        recipient = settings.vendor_email
        if not recipient:
            try: recipient = credentials(settings)['email']
            except (OSError, ValueError, KeyError): recipient = ''
        return recipient

    def queue_vendor_alert(con, snapshot, client):
        recipient = vendor_recipient()
        contract_id, version = snapshot['contract_id'], snapshot['version']
        link = f'/countersign/{contract_id}?version={version}'
        text = f"Hello,\n\n{client['signer_name']} has signed {snapshot['name']} (version {version}).\n\nPlease review the client's completed answers and add your signature:\n\n{link}\n\nThank you,\nBello Entertainment"
        formatted = f"<p>Hello,</p><p>{html.escape(client['signer_name'])} has signed <strong>{html.escape(snapshot['name'])}</strong> (version {version}).</p><p>Please review the client's completed answers and add your signature.</p>{agreement_link(link, 'Review & countersign')}<p>Thank you,<br>Bello Entertainment</p>"
        con.execute('INSERT INTO outbox (contract_id,version,kind,recipient_email,subject,body,link,created_at,html_body,delivery_status) VALUES (?,?,?,?,?,?,?,?,?,?)',
                    (contract_id,version,'vendor_alert',recipient,f"Ready for your signature: {snapshot['name']}",text,link,client['signed_at'],formatted,'pending' if settings.email_mode=='gmail' else 'local'))

    app.state.queue_vendor_alert = queue_vendor_alert
    app.state.deliver_vendor_alert = lambda contract_id, version: deliver_contract(contract_id, version, 'vendor_alert')
    app.state.deliver_email = deliver
    def deliver_completion(contract_id, version):
        results = [deliver_contract(contract_id,version,kind) for kind in ('completed','completed_vendor')]
        results = [row for row in results if row]
        return next((row for row in results if row['delivery_status'] not in {'sent','local'}), results[0] if results else None)

    app.state.vendor_recipient = vendor_recipient
    app.state.deliver_completion = deliver_completion

    @app.get('/api/email-delivery')
    def configuration():
        return {'mode':settings.email_mode, 'configured':(settings.data_dir/'gmail-credentials.json').exists(), 'base_url':settings.email_base_url}

    @app.post('/api/outbox/{message_id}/send')
    def retry_email(message_id: int):
        return deliver(message_id, retry=True)
