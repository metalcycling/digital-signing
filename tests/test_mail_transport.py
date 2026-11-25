import json
import smtplib
from unittest.mock import Mock

from fastapi.testclient import TestClient

from digital_signing.app import create_app
from digital_signing.config import Settings
from test_contracts import HEADERS
from test_delivery import RECIPIENT
from test_workflow import prepared, vendor_payload


def gmail_client(tmp_path, monkeypatch, vendor_email=""):
    smtp = Mock()
    smtp.send_message.return_value = {}
    factory = Mock(return_value=smtp)
    monkeypatch.setattr('digital_signing.mail_transport.smtplib.SMTP',factory)
    (tmp_path/'gmail-credentials.json').write_text(json.dumps({'email':'sender@example.com','app_password':'test-password'}))
    return TestClient(create_app(Settings(auth_mode="local", data_dir=tmp_path,mode='demo',email_mode='gmail',vendor_email=vendor_email))), smtp, factory


def test_gmail_invitation_and_completion_are_multipart_and_idempotent(tmp_path,monkeypatch):
    _, contract, base, payload = prepared(tmp_path)
    app, smtp, factory = gmail_client(tmp_path,monkeypatch)
    first = app.post(base+'/1/send',json=RECIPIENT,headers=HEADERS).json()
    assert first['delivery_status'] == 'sent'
    factory.assert_called_with('smtp.gmail.com',587,timeout=20)
    assert smtp.starttls.call_count == 1
    message = smtp.send_message.call_args.args[0]
    assert message['To'] == RECIPIENT['recipient_email']
    assert 'http://localhost:8000/sign/' in message.get_body(preferencelist=('plain',)).get_content()
    assert '<strong>Full signing workflow</strong>' in message.get_body(preferencelist=('html',)).get_content()
    assert 'href="http://localhost:8000/sign/' in message.get_body(preferencelist=('html',)).get_content()
    assert app.post(base+'/1/send',json=RECIPIENT,headers=HEADERS).json() == first
    assert smtp.send_message.call_count == 1
    submitted = app.post(base+'/1/submit',json=payload,headers=HEADERS).json()
    signed = app.post(base+'/1/countersign',json=vendor_payload(submitted),headers=HEADERS)
    assert signed.status_code == 200
    assert signed.json()['email_delivery']['status'] == 'sent'
    assert smtp.send_message.call_count == 4
    final_pdf = app.get(signed.json()['final_pdf_url']).content
    completion_messages = [call.args[0] for call in smtp.send_message.call_args_list[-2:]]
    assert {m['To'] for m in completion_messages} == {'jane@example.com','sender@example.com'}
    for m in completion_messages:
        assert m['Subject'].startswith('Fully executed:')
        for kind in ('plain', 'html'):
            body = m.get_body(preferencelist=(kind,)).get_content()
            assert 'http://localhost' not in body
            assert '/sign/' not in body and '/countersign/' not in body
            assert 'href=' not in body and 'expires' not in body
            assert 'attached for your records' in body
        attachments=list(m.iter_attachments())
        assert len(attachments)==1
        assert attachments[0].get_content_type()=='application/pdf'
        assert attachments[0].get_payload(decode=True)==final_pdf
        assert attachments[0].get_filename() == 'Full signing workflow.pdf'
    app.post(base+'/1/countersign',json=vendor_payload(submitted),headers=HEADERS)
    assert smtp.send_message.call_count == 4
    assert app.get('/api/email-delivery').json()['mode'] == 'gmail'


def test_auth_failure_is_saved_and_explicit_retry_succeeds(tmp_path,monkeypatch):
    _, _, base, _ = prepared(tmp_path)
    app, smtp, _ = gmail_client(tmp_path,monkeypatch)
    smtp.login.side_effect = smtplib.SMTPAuthenticationError(535,b'secret details')
    failed = app.post(base+'/1/send',json=RECIPIENT,headers=HEADERS).json()
    assert failed['delivery_status'] == 'failed'
    assert 'secret details' not in failed['delivery_error']
    assert app.get('/api/workspace').json()['contracts'][0]['status'] == 'sent'
    smtp.login.side_effect = None
    retried = app.post(f'/api/outbox/{failed["id"]}/send',headers=HEADERS).json()
    assert retried['delivery_status'] == 'sent'
    assert retried['link'] == failed['link']
    assert app.post(f'/api/outbox/{failed["id"]}/send',headers=HEADERS).json() == retried
    assert smtp.send_message.call_count == 1


def test_uncertain_send_is_never_automatically_retried(tmp_path,monkeypatch):
    _, _, base, _ = prepared(tmp_path)
    app, smtp, _ = gmail_client(tmp_path,monkeypatch)
    smtp.send_message.side_effect = TimeoutError()
    result = app.post(base+'/1/send',json=RECIPIENT,headers=HEADERS).json()
    assert result['delivery_status'] == 'unknown'
    assert app.post(base+'/1/send',json=RECIPIENT,headers=HEADERS).json() == result
    assert smtp.send_message.call_count == 1
    assert app.post(f'/api/outbox/{result["id"]}/send').status_code == 403


def test_old_local_emails_do_not_send_when_gmail_is_enabled(tmp_path,monkeypatch):
    app, _, base, _ = prepared(tmp_path)
    local = app.post(base+'/1/send',json=RECIPIENT,headers=HEADERS).json()
    assert local['delivery_status'] == 'local'
    gmail, smtp, _ = gmail_client(tmp_path,monkeypatch)
    gmail.post(base+'/1/send',json=RECIPIENT,headers=HEADERS)
    smtp.send_message.assert_not_called()


def test_vendor_alert_targets_sender_and_duplicate_client_submit_does_not_resend(tmp_path,monkeypatch):
    _, contract, base, payload = prepared(tmp_path)
    app, smtp, _ = gmail_client(tmp_path,monkeypatch)
    email = app.post(base+'/1/send',json=RECIPIENT,headers=HEADERS).json()
    private = '/api/sign/'+email['link'].split('/')[-1]+'/1/submit'
    first = app.post(private,json=payload,headers=HEADERS)
    assert first.status_code == 200
    message = smtp.send_message.call_args.args[0]
    assert message['To'] == 'sender@example.com'
    assert message['Subject'] == 'Ready for your signature: Full signing workflow'
    assert 'Jane Smith' in message.get_body(preferencelist=('plain',)).get_content()
    assert f'http://localhost:8000/countersign/{contract}?version=1' in message.get_body(preferencelist=('plain',)).get_content()
    assert 'Review &amp; countersign' in message.get_body(preferencelist=('html',)).get_content()
    assert smtp.send_message.call_count == 2
    assert app.post(private,json=payload,headers=HEADERS).json() == first.json()
    assert smtp.send_message.call_count == 2
    alerts = [m for m in app.get('/api/outbox').json() if m['kind']=='vendor_alert']
    assert len(alerts) == 1 and alerts[0]['delivery_status'] == 'sent'


def test_vendor_alert_failure_keeps_submission_and_can_retry_after_client_link_expires(tmp_path,monkeypatch):
    import sqlite3
    _, contract, base, payload = prepared(tmp_path)
    app, smtp, _ = gmail_client(tmp_path,monkeypatch,vendor_email='vendor@example.com')
    app.post(base+'/1/send',json=RECIPIENT,headers=HEADERS)
    smtp.login.side_effect = smtplib.SMTPAuthenticationError(535,b'private')
    submitted = app.post(base+'/1/submit',json=payload,headers=HEADERS)
    assert submitted.status_code == 200
    assert submitted.json()['state'] == 'awaiting_vendor'
    assert app.get(submitted.json()['client_pdf_url']).content.startswith(b'%PDF')
    alert = app.get('/api/outbox').json()[0]
    assert alert['kind'] == 'vendor_alert' and alert['delivery_status'] == 'failed'
    assert alert['recipient_email'] == 'vendor@example.com'
    with sqlite3.connect(tmp_path/'contracts-demo.sqlite3') as con:
        con.execute("UPDATE invitations SET expires_at='2000-01-01T00:00:00+00:00'")
    smtp.login.side_effect = None
    assert app.post(f'/api/outbox/{alert["id"]}/send',headers=HEADERS).json()['delivery_status'] == 'sent'
    assert smtp.send_message.call_args.args[0]['To'] == 'vendor@example.com'


def test_vendor_alert_for_superseded_submission_is_not_sent(tmp_path,monkeypatch):
    local, contract, base, payload = prepared(tmp_path)
    local.post(base+'/1/submit',json=payload,headers=HEADERS)
    alert = local.get('/api/outbox').json()[0]
    local.post(f'/api/contracts/{contract}/revisions',headers=HEADERS)
    app,smtp,_ = gmail_client(tmp_path,monkeypatch)
    result = app.post(f'/api/outbox/{alert["id"]}/send',headers=HEADERS).json()
    assert result['delivery_status'] == 'failed'
    smtp.send_message.assert_not_called()


def test_vendor_completion_retry_does_not_resend_client_copy(tmp_path,monkeypatch):
    _, _, base, payload = prepared(tmp_path)
    app,smtp,_=gmail_client(tmp_path,monkeypatch,vendor_email='vendor@example.com')
    app.post(base+'/1/send',json=RECIPIENT,headers=HEADERS)
    submitted=app.post(base+'/1/submit',json=payload,headers=HEADERS).json()
    def send(message):
        if message['To']=='vendor@example.com':
            raise smtplib.SMTPRecipientsRefused({'vendor@example.com':(550,b'rejected')})
        return {}
    smtp.send_message.side_effect=send
    signed=app.post(base+'/1/countersign',json=vendor_payload(submitted),headers=HEADERS)
    assert signed.status_code==200
    assert signed.json()['state']=='completed'
    assert signed.json()['email_delivery']['status']=='failed'
    outbox=app.get('/api/outbox').json()
    client_email=next(m for m in outbox if m['kind']=='completed')
    vendor_email=next(m for m in outbox if m['kind']=='completed_vendor')
    assert client_email['delivery_status']=='sent'
    assert vendor_email['delivery_status']=='failed'
    assert client_email['attach_final_pdf']==vendor_email['attach_final_pdf']==1
    count=smtp.send_message.call_count
    app.post(base+'/1/countersign',json=vendor_payload(submitted),headers=HEADERS)
    assert smtp.send_message.call_count==count
    smtp.send_message.side_effect=None
    assert app.post(f'/api/outbox/{vendor_email["id"]}/send',headers=HEADERS).json()['delivery_status']=='sent'
    assert smtp.send_message.call_count==count+1
    assert smtp.send_message.call_args.args[0]['To']=='vendor@example.com'
    attached=list(smtp.send_message.call_args.args[0].iter_attachments())[0]
    assert attached.get_payload(decode=True)==app.get(signed.json()['final_pdf_url']).content


def test_attachment_only_completion_can_retry_after_page_link_expires(tmp_path, monkeypatch):
    import sqlite3
    _, _, base, payload = prepared(tmp_path)
    app, smtp, _ = gmail_client(tmp_path, monkeypatch)
    app.post(base+'/1/send', json=RECIPIENT, headers=HEADERS)
    submitted = app.post(base+'/1/submit', json=payload, headers=HEADERS).json()
    smtp.login.side_effect = smtplib.SMTPAuthenticationError(535, b'private')
    result = app.post(base+'/1/countersign', json=vendor_payload(submitted), headers=HEADERS)
    assert result.status_code == 200
    with sqlite3.connect(tmp_path/'contracts-demo.sqlite3') as con:
        con.execute("UPDATE invitations SET completion_expires_at='2000-01-01T00:00:00+00:00'")
    emails = [row for row in app.get('/api/outbox').json() if row['kind'] in {'completed', 'completed_vendor'}]
    assert len(emails) == 2
    smtp.login.side_effect = None
    for email in emails:
        assert email['delivery_status'] == 'failed'
        retry = app.post(f'/api/outbox/{email["id"]}/send', headers=HEADERS)
        assert retry.json()['delivery_status'] == 'sent'
        message = smtp.send_message.call_args.args[0]
        assert len(list(message.iter_attachments())) == 1
        assert 'href=' not in message.get_body(preferencelist=('html',)).get_content()
