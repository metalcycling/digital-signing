import json
import sqlite3
from datetime import datetime, timedelta

from test_contracts import HEADERS, client
from test_delivery import invitation, RECIPIENT
from test_workflow import prepared, vendor_payload


def test_corrections_preserve_document_and_answers_but_require_fresh_signing(tmp_path, monkeypatch):
    app, contract, base, payload = prepared(tmp_path)
    with sqlite3.connect(tmp_path/'contracts-demo.sqlite3') as con:
        snapshot = json.loads(con.execute('SELECT snapshot FROM renders').fetchone()[0])
        snapshot['fields'] += [
            {'key':'client.signing_date','type':'signing_date','role':'client','label':'Signed on','required':True,'options':[]},
            {'key':'client.initials','type':'initials','role':'client','label':'Initials','required':True,'options':[]},
        ]
        con.execute('UPDATE renders SET snapshot=?', (json.dumps(snapshot),))
    payload['values'].update({'client.signing_date':'1900-01-01','client.initials':'JS'})
    monkeypatch.setattr('digital_signing.signing_rules.signing_day',lambda *args:'2026-11-23')
    email, private = invitation(app,base)
    state = app.post(private+'/1/submit',json=payload,headers=HEADERS).json()
    original_pdf = app.get(state['client_pdf_url']).content
    note = 'Please correct the event address.\nUse <Suite 5> & confirm the start time.'
    response = app.post(base+'/1/request-corrections',json={'note':note},headers=HEADERS)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['version'] == 2
    email2 = result['email']
    assert email2['recipient_email'] == RECIPIENT['recipient_email']
    assert email2['subject'].startswith('Corrections requested for ')
    assert note in email2['body']
    for content in (email2['body'], email2['html_body']):
        assert 'Hello Jane Smith' not in content
        assert 'Please review and sign the agreement for' not in content
        assert 'Thank you!' not in content
        assert 'This private link expires in' in content
        assert email2['link'] in content
    assert '<strong>30 days</strong>' in email2['html_body']
    assert '&lt;Suite 5&gt; &amp;' in email2['html_body']
    assert '<Suite 5>' not in email2['html_body']
    assert app.get(email['link']).status_code == 410
    assert app.post(private+'/1/submit',json=payload,headers=HEADERS).status_code == 410
    assert app.post(base+'/1/countersign',json=vendor_payload(state),headers=HEADERS).status_code == 409
    new_base = '/api/sign/'+email2['link'].split('/')[-1]+'/2'
    new_snapshot = app.get(new_base).json()
    assert new_snapshot['correction_note'] == note
    assert new_snapshot['source_sha256'] == snapshot['source_sha256']
    assert new_snapshot['fields'] == snapshot['fields']
    excluded = {f['key'] for f in snapshot['fields'] if f['type'] in {'signature','initials','signing_date'}}
    assert new_snapshot['prefill_values'] == {k:v for k,v in payload['values'].items() if k not in excluded}
    for old,new in zip(snapshot['pages'],new_snapshot['pages']):
        assert app.get(old['image_url']).content == app.get(new['image_url']).content
    assert app.get(new_base+'/workflow').json()['client'] is None
    assert app.get('/api/workspace').json()['contracts'][0]['status'] == 'sent'
    assert app.get('/api/notifications').json()[0]['kind'] == 'corrections_requested'
    assert app.post(new_base+'/submit',json={'values':new_snapshot['prefill_values'],'consent':True},headers=HEADERS).status_code == 422
    monkeypatch.setattr('digital_signing.signing_rules.signing_day',lambda *args:'2026-11-24')
    corrected = {**payload,'values':{**payload['values']}}
    text_key = next(f['key'] for f in snapshot['fields'] if f['role']=='client' and f['type']=='text')
    corrected['values'][text_key] = 'Corrected answer'
    submitted = app.post(new_base+'/submit',json=corrected,headers=HEADERS)
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()['client']['values']['client.signing_date'] == '2026-11-24'
    assert submitted.json()['client']['values'][text_key] == 'Corrected answer'
    assert client(tmp_path).get(base+'/2/workflow').json()['state'] == 'awaiting_vendor'
    final = app.post(base+'/2/countersign',json=vendor_payload(submitted.json()),headers=HEADERS)
    assert final.status_code == 200, final.text
    assert app.get(final.json()['final_pdf_url']).content.startswith(b'%PDF')
    assert app.get(base+'/1/workflow').json()['client'] == state['client']
    assert app.get(f'/api/contracts/{contract}/submissions/1/client.pdf').content == original_pdf
    with sqlite3.connect(tmp_path/'contracts-demo.sqlite3') as con:
        created,expiry = con.execute('SELECT created_at,expires_at FROM invitations WHERE version=2').fetchone()
        assert datetime.fromisoformat(expiry)-datetime.fromisoformat(created) == timedelta(days=30)


def test_corrections_validate_state_and_prevent_duplicate_revisions(tmp_path):
    app, _, base, payload = prepared(tmp_path)
    endpoint = base+'/1/request-corrections'
    assert app.post(endpoint,json={'note':'Fix address'},headers=HEADERS).status_code == 409
    invitation(app,base)
    app.post(base+'/1/submit',json=payload,headers=HEADERS)
    assert app.post(endpoint,json={'note':'Fix address'}).status_code == 403
    for note in ['', '  ', 'x'*5001]:
        assert app.post(endpoint,json={'note':note},headers=HEADERS).status_code == 422
    assert app.post(endpoint,json={'note':'Fix address'},headers=HEADERS).status_code == 200
    assert app.post(endpoint,json={'note':'Fix address'},headers=HEADERS).status_code == 409
    assert len(app.get(base).json()) == 2
    assert len([m for m in app.get('/api/outbox').json() if m['version']==2]) == 1
    assert app.post(base+'/2/request-corrections',json={'note':'Fix address'},headers=HEADERS).status_code == 409
    state = app.post(base+'/2/submit',json=payload,headers=HEADERS).json()
    app.post(base+'/2/countersign',json=vendor_payload(state),headers=HEADERS)
    assert app.post(base+'/2/request-corrections',json={'note':'Fix address'},headers=HEADERS).status_code == 409


def test_failed_correction_preparation_rolls_back_revision(tmp_path, monkeypatch):
    app, _, base, payload = prepared(tmp_path)
    email,_ = invitation(app,base)
    app.post(base+'/1/submit',json=payload,headers=HEADERS)
    def fail(*args):
        raise RuntimeError('Simulated token generation failure')
    monkeypatch.setattr('digital_signing.delivery.secrets.token_urlsafe',fail)
    import pytest
    with pytest.raises(RuntimeError):
        app.post(base+'/1/request-corrections',json={'note':'Fix address'},headers=HEADERS)
    assert len(app.get(base).json()) == 1
    assert app.get(email['link']).status_code == 200
    assert app.get('/api/workspace').json()['contracts'][0]['status'] == 'awaiting_vendor'
    with sqlite3.connect(tmp_path/'contracts-demo.sqlite3') as con:
        assert con.execute('SELECT COUNT(*) FROM render_assets WHERE version=2').fetchone()[0] == 0


def test_correction_email_failure_keeps_revision_and_supports_outbox_retry(tmp_path, monkeypatch):
    import smtplib
    from test_mail_transport import gmail_client
    _,_,base,payload = prepared(tmp_path)
    app,smtp,_ = gmail_client(tmp_path,monkeypatch)
    invitation(app,base)
    app.post(base+'/1/submit',json=payload,headers=HEADERS)
    smtp.login.side_effect = smtplib.SMTPAuthenticationError(535,b'test')
    result = app.post(base+'/1/request-corrections',json={'note':'Correct the address'},headers=HEADERS).json()
    assert result['email']['delivery_status'] == 'failed'
    assert app.get(result['email']['link']).status_code == 200
    smtp.login.side_effect = None
    sent = app.post(f"/api/outbox/{result['email']['id']}/send",headers=HEADERS).json()
    assert sent['delivery_status'] == 'sent'
    assert sent['link'] == result['email']['link']
    mail = smtp.send_message.call_args.args[0]
    assert mail['To'] == RECIPIENT['recipient_email']
    assert 'Correct the address' in mail.get_body(preferencelist=('html',)).get_content()
