import sqlite3

from test_contracts import HEADERS
from test_workflow import prepared, vendor_payload

RECIPIENT = {'recipient_name':'Jane Smith','recipient_email':'jane@example.com','reviewed':True}


def invitation(app, base):
    result = app.post(base+'/1/send',json=RECIPIENT,headers=HEADERS)
    assert result.status_code == 200, result.text
    row = result.json()
    return row, '/api/sign/'+row['link'].split('/')[-1]


def test_local_send_to_client_through_final_pdf(tmp_path):
    app, contract, base, payload = prepared(tmp_path)
    email, private = invitation(app,base)
    assert app.get(email['link']).status_code == 200
    assert app.get('/api/workspace').json()['contracts'][0]['status'] == 'sent'
    assert app.post(base,headers=HEADERS).status_code == 409
    assert app.get(private).json()[0]['version'] == 1
    snapshot = app.get(private+'/1').json()
    assert 'document_url' not in snapshot
    assert all(p['image_url'].startswith(private) for p in snapshot['pages'])
    assert app.get(snapshot['pages'][0]['image_url']).status_code == 200
    assert app.get(private+'/1').headers['cache-control'] == 'no-store'
    assert app.post(private+'/1/validate',json=payload['values'],headers=HEADERS).json()['valid']
    submitted = app.post(private+'/1/submit',json=payload,headers=HEADERS)
    assert submitted.status_code == 200, submitted.text
    state = submitted.json()
    assert state['client_pdf_url'].startswith(private)
    assert app.get(state['client_pdf_url']).content.startswith(b'%PDF')
    final = app.post(base+'/1/countersign',json=vendor_payload(state),headers=HEADERS)
    assert final.status_code == 200
    state = app.get(private+'/1/workflow').json()
    assert state['state'] == 'completed'
    assert app.get(state['final_pdf_url']).content.startswith(b'%PDF')
    emails = app.get('/api/outbox').json()
    assert [email['kind'] for email in emails] == ['completed','completed_vendor','vendor_alert','invitation']
    assert emails[0]['recipient_email'] == RECIPIENT['recipient_email']
    assert emails[0]['link'] != email['link']
    assert app.get(emails[0]['link']).status_code == 200


def test_send_requires_review_and_valid_recipient_and_is_idempotent(tmp_path):
    app, _, base, _ = prepared(tmp_path)
    for change in [{'reviewed':False},{'reviewed':'yes'},{'recipient_email':'bad'},{'recipient_name':' '}]:
        assert app.post(base+'/1/send',json={**RECIPIENT,**change},headers=HEADERS).status_code == 422
    assert app.post(base+'/1/send',json=RECIPIENT).status_code == 403
    row, _ = invitation(app,base)
    assert invitation(app,base)[0] == row
    assert len(app.get('/api/outbox').json()) == 1
    assert app.post(base+'/1/send',json={**RECIPIENT,'recipient_email':'other@example.com'},headers=HEADERS).status_code == 409


def test_links_are_scoped_expire_and_cannot_countersign(tmp_path):
    app, _, base, payload = prepared(tmp_path)
    email, private = invitation(app,base)
    assert app.get('/api/sign/not-a-token').status_code == 404
    assert app.get(private+'/2').status_code == 404
    assert app.post(private+'/1/countersign',json=payload,headers=HEADERS).status_code == 404
    assert app.post(private+'/1/submit',json={**payload,'values':{**payload['values'],'vendor.signature':'bad'}},headers=HEADERS).status_code == 422
    with sqlite3.connect(tmp_path/'contracts-demo.sqlite3') as con:
        con.execute("UPDATE invitations SET expires_at='2000-01-01T00:00:00+00:00'")
    assert app.get(email['link']).status_code == 410
    assert app.post(private+'/1/submit',json=payload,headers=HEADERS).status_code == 410


def test_revision_revokes_old_link_and_deletion_clears_delivery_records(tmp_path):
    app, contract, base, payload = prepared(tmp_path)
    email, private = invitation(app,base)
    assert app.post(f'/api/contracts/{contract}/revisions',headers=HEADERS).status_code == 201
    assert app.get(email['link']).status_code == 410
    assert app.post(private+'/1/submit',json=payload,headers=HEADERS).status_code == 410
    assert app.post(base+'/1/send',json=RECIPIENT,headers=HEADERS).status_code == 409
    second = app.post(base+'/2/send',json=RECIPIENT,headers=HEADERS)
    assert second.status_code == 200
    assert second.json()['link'] != email['link']
    assert app.request('DELETE','/api/contracts/'+contract,json={'confirmation':'Full signing workflow'},headers=HEADERS).status_code == 200
    assert app.get('/api/outbox').json() == []
    assert app.get(second.json()['link']).status_code == 404
    with sqlite3.connect(tmp_path/'contracts-demo.sqlite3') as con:
        assert con.execute('SELECT COUNT(*) FROM invitations').fetchone()[0] == 0


def test_custom_messages_defaults_and_fresh_completion_expiration(tmp_path):
    from datetime import datetime, timedelta
    from test_contracts import client
    app, contract, base, payload = prepared(tmp_path)
    options = app.get('/api/email-defaults').json()
    options.update(invitation_body='Dear {recipient_name},\nSign {contract_name}.\nBest, The team',
                   completion_body='Hello {recipient_name},\nHere is {contract_name}.\nThank you, The team',
                   signing_days=7, completion_days=60)
    assert app.put('/api/email-defaults',json=options,headers=HEADERS).status_code == 200
    assert client(tmp_path).get('/api/email-defaults').json() == options
    email, private = invitation(app, base)
    assert 'Dear Jane Smith,' in email['body']
    assert 'Sign Full signing workflow.' in email['body']
    assert 'Best, The team' in email['body']
    assert 'expires in 7 days' in email['body']
    submitted = app.post(private+'/1/submit',json=payload,headers=HEADERS).json()
    with sqlite3.connect(tmp_path/'contracts-demo.sqlite3') as con:
        created, expiry = con.execute('SELECT created_at, expires_at FROM invitations').fetchone()
        assert datetime.fromisoformat(expiry)-datetime.fromisoformat(created) == timedelta(days=7)
        con.execute("UPDATE invitations SET expires_at='2000-01-01T00:00:00+00:00'")
    # Changes to defaults must not silently change a previously prepared completion email.
    app.put('/api/email-defaults',json={**options,'completion_days':1,'completion_body':'Different future message'},headers=HEADERS)
    final = app.post(base+'/1/countersign',json=vendor_payload(submitted),headers=HEADERS).json()
    completion = app.get('/api/outbox').json()[0]
    assert 'Hello Jane Smith,' in completion['body']
    assert 'Thank you, The team' in completion['body']
    assert 'expires in 60 days' in completion['body']
    assert app.get(email['link']).status_code == 410
    assert app.get(completion['link']).status_code == 200
    final_base = '/api/sign/'+completion['link'].split('/')[-1]+'/1'
    state = app.get(final_base+'/workflow').json()
    assert app.get(state['final_pdf_url']).content.startswith(b'%PDF')
    assert app.post(final_base+'/submit',json=payload,headers=HEADERS).status_code == 403
    with sqlite3.connect(tmp_path/'contracts-demo.sqlite3') as con:
        expiry = con.execute('SELECT completion_expires_at FROM invitations').fetchone()[0]
        assert datetime.fromisoformat(expiry)-datetime.fromisoformat(final['vendor']['signed_at']) == timedelta(days=60)
    # Countersign retries must not regenerate the link or extend its lifetime.
    assert app.post(base+'/1/countersign',json=vendor_payload(submitted),headers=HEADERS).status_code == 200
    assert app.get('/api/outbox').json()[0] == completion
    with sqlite3.connect(tmp_path/'contracts-demo.sqlite3') as con:
        con.execute("UPDATE invitations SET completion_expires_at='2000-01-01T00:00:00+00:00'")
    assert app.get(completion['link']).status_code == 410


def test_email_options_validation_and_contract_overrides(tmp_path):
    app, _, base, _ = prepared(tmp_path)
    options = app.get('/api/email-defaults').json()
    for invalid in [0,366,1.5,True,'30']:
        assert app.put('/api/email-defaults',json={**options,'signing_days':invalid},headers=HEADERS).status_code == 422
        assert app.post(base+'/1/send',json={**RECIPIENT,'email_options':{**options,'completion_days':invalid}},headers=HEADERS).status_code == 422
    assert app.put('/api/email-defaults',json={**options,'completion_body':' '},headers=HEADERS).status_code == 422
    assert app.put('/api/email-defaults',json=options).status_code == 403
    override = {**options,'invitation_body':'Personal message','completion_days':90}
    result = app.post(base+'/1/send',json={**RECIPIENT,'email_options':override},headers=HEADERS)
    assert result.status_code == 200
    assert result.json()['body'].startswith('Personal message')
    assert app.get('/api/email-defaults').json() == options
    assert app.post(base+'/1/send',json={**RECIPIENT,'email_options':override},headers=HEADERS).json() == result.json()
    assert app.post(base+'/1/send',json={**RECIPIENT,'email_options':options},headers=HEADERS).status_code == 409


def test_formatted_email_bolds_contract_and_escapes_user_text():
    from digital_signing.email_messages import html_message
    result = html_message('Hi {recipient_name},\nSign {contract_name}. <script>alert(1)</script>',
                          '<img src=x>', 'Wedding & reception', '/sign/test', 30)
    assert '<strong>Wedding &amp; reception</strong>' in result
    assert '&lt;img src=x&gt;' in result
    assert '<script>' not in result
    assert '<a href="/sign/test">Open agreement</a>' in result


def test_invitation_subject_title_is_customizable_and_header_safe(tmp_path):
    app, _, base, _ = prepared(tmp_path)
    options = app.get('/api/email-defaults').json()
    assert options['invitation_title'] == '{contract_name}'
    for title in [' ', 'Hello\r\nBcc: other@example.com', 'x'*201]:
        assert app.post(base+'/1/send',json={**RECIPIENT,'email_options':{**options,'invitation_title':title}},headers=HEADERS).status_code == 422
    custom = {**options,'invitation_title':'Music for {recipient_name} — {contract_name}'}
    assert app.put('/api/email-defaults',json=custom,headers=HEADERS).status_code == 200
    email, _ = invitation(app,base)
    assert email['subject']=='Signature request for Music for Jane Smith — Full signing workflow'
