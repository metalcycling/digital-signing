import json
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path

import pdfplumber
import pytest

from test_contracts import client, HEADERS
from test_signatures import signature


def prepared(tmp_path):
    app = client(tmp_path)
    contract = app.post('/api/contracts', json={'name':'Full signing workflow','template_id':'demo-harp'}, headers=HEADERS).json()
    base = f'/api/contracts/{contract["id"]}/renders'
    assert app.post(base, headers=HEADERS).status_code == 201
    snapshot = app.get(base+'/1').json()
    answers = {}
    for f in snapshot['fields']:
        if f['role'] != 'client': continue
        answers[f['key']] = {'text':'Jane Smith','email':'jane@example.com','phone':'+1 954 555 0100',
            'select':'Wedding','date':'2027-06-12','time':'14:30','number':'50','textarea':'A long note\nSecond line',
            'checkbox':True,'signature':signature('typed')}[f['type']]
    payload = {'values':answers,'signer_name':'Jane Smith','consent':True}
    return app, contract['id'], base, payload


def vendor_payload(state):
    return {'signer_name':'Vendor Name','consent':True,
            'values':{f['key']:signature('drawn') for f in state['vendor_fields']}}


def test_full_workflow_survives_restart_and_keeps_client_pdf_immutable(tmp_path):
    app, contract, base, payload = prepared(tmp_path)
    result = app.post(base+'/1/submit', json=payload, headers=HEADERS)
    assert result.status_code == 200, result.text
    submitted = result.json()
    assert submitted['state'] == 'awaiting_vendor'
    assert submitted['client']['values'] == payload['values']
    initial_pdf = app.get(submitted['client_pdf_url']).content
    assert initial_pdf.startswith(b'%PDF')
    with pdfplumber.open(BytesIO(initial_pdf)) as pdf:
        text = '\n'.join(p.extract_text() or '' for p in pdf.pages)
        assert 'Jane Smith' in text and 'Second line' in text
        assert '06-12-2027' in text
        assert '2027-06-12' not in text
        assert 'Awaiting vendor signature' in text
    restarted = client(tmp_path)
    assert restarted.get(base+'/1/workflow').json()['client'] == submitted['client']
    assert restarted.get('/api/workspace').json()['contracts'][0]['status'] == 'awaiting_vendor'
    assert restarted.get('/api/notifications').json()[0]['kind'] == 'client_submitted'
    signed = restarted.post(base+'/1/countersign', json=vendor_payload(submitted), headers=HEADERS)
    assert signed.status_code == 200, signed.text
    final = signed.json()
    assert final['state'] == 'completed'
    assert app.get(submitted['client_pdf_url']).content == initial_pdf
    final_pdf = app.get(final['final_pdf_url']).content
    assert final_pdf != initial_pdf
    with pdfplumber.open(BytesIO(final_pdf)) as pdf:
        text = '\n'.join(p.extract_text() or '' for p in pdf.pages)
        assert 'Fully executed' in text and 'Vendor Name' in text
    assert app.get('/api/workspace').json()['contracts'][0]['status'] == 'completed'
    vendor_page = app.get('/countersign/'+contract)
    assert vendor_page.status_code == 200
    assert '/static/preview.js' in vendor_page.text
    assert 'id="document"' in vendor_page.text


def test_validation_consent_and_ownership_are_enforced(tmp_path):
    app, _, base, payload = prepared(tmp_path)
    assert app.post(base+'/1/submit', json={**payload,'consent':False}, headers=HEADERS).status_code == 422
    assert app.post(base+'/1/submit', json={**payload,'consent':'true'}, headers=HEADERS).status_code == 422
    assert app.post(base+'/1/submit', json={**payload,'values':{}}, headers=HEADERS).status_code == 422
    assert app.post(base+'/1/submit', json={**payload,'values':{**payload['values'],'vendor.signature':signature()}}, headers=HEADERS).status_code == 422
    assert app.post(base+'/1/countersign', json={'signer_name':'Vendor','consent':True,'values':{}}, headers=HEADERS).status_code == 409
    assert app.get(base+'/1/workflow').json()['state'] == 'editable'
    assert app.get('/api/notifications').json() == []


def test_retry_is_idempotent_and_conflicting_writes_are_rejected(tmp_path):
    app, _, base, payload = prepared(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _:app.post(base+'/1/submit',json=payload,headers=HEADERS),range(2)))
    assert [r.status_code for r in results] == [200,200]
    assert results[0].json() == results[1].json()
    assert len(app.get('/api/notifications').json()) == 1
    assert app.post(base+'/1/submit',json={**payload,'signer_name':'Someone else'},headers=HEADERS).status_code == 409
    vendor = vendor_payload(results[0].json())
    first = app.post(base+'/1/countersign',json=vendor,headers=HEADERS)
    retry = app.post(base+'/1/countersign',json=vendor,headers=HEADERS)
    assert first.json() == retry.json()
    assert len(app.get('/api/notifications').json()) == 2
    assert app.post(base+'/1/countersign',json={**vendor,'signer_name':'Other vendor'},headers=HEADERS).status_code == 409


def test_revisions_preserve_signed_versions_and_reject_stale_submission(tmp_path):
    app, contract, base, payload = prepared(tmp_path)
    submitted=app.post(base+'/1/submit',json=payload,headers=HEADERS).json()
    old_pdf=app.get(submitted['client_pdf_url']).content
    assert app.post(base,headers=HEADERS).status_code == 409
    revision=app.post(f'/api/contracts/{contract}/revisions',headers=HEADERS)
    assert revision.status_code == 201
    assert revision.json()['version'] == 2
    assert app.get(base+'/1/workflow').json()['superseded']
    assert app.post(base+'/1/countersign',json=vendor_payload(submitted),headers=HEADERS).status_code == 409
    assert app.get(submitted['client_pdf_url']).content == old_pdf
    assert app.get(base+'/2/workflow').json()['state'] == 'editable'
    assert app.post(f'/api/contracts/{contract}/revisions',headers=HEADERS).status_code == 409
    assert app.post(base,headers=HEADERS).status_code == 201
    assert app.post(base+'/2/submit',json=payload,headers=HEADERS).status_code == 409
    assert app.get(base+'/2/workflow').json()['state'] == 'editable'


def test_failed_pdf_generation_does_not_record_a_submission(tmp_path, monkeypatch):
    app, _, base, payload = prepared(tmp_path)
    def fail(*args, **kwargs): raise ValueError('Test PDF generation failure')
    monkeypatch.setattr('digital_signing.workflow.create_completed_pdf', fail)
    assert app.post(base+'/1/submit',json=payload,headers=HEADERS).status_code == 422
    assert app.get(base+'/1/workflow').json()['state'] == 'editable'
    assert app.get('/api/notifications').json() == []


def test_fallback_signature_field_cannot_overwrite_client_field():
    from digital_signing.workflow import vendor_fields
    snapshot={'fields':[{'key':'vendor.completion_signature','role':'client','type':'text'}]}
    assert vendor_fields(snapshot)[0]['key'] != 'vendor.completion_signature'


def test_missing_vendor_tag_uses_attached_completion_signature(tmp_path, monkeypatch):
    import importlib
    app_module = importlib.import_module('digital_signing.app')
    monkeypatch.setattr(app_module, 'DEMO_HTML', app_module.DEMO_HTML.replace('<p>Vendor signature: {{vendor.signature|signature}}</p>', ''))
    app, _, base, payload = prepared(tmp_path)
    submitted = app.post(base+'/1/submit', json=payload, headers=HEADERS).json()
    assert submitted['vendor_fields'][0]['key'] == 'vendor.completion_signature'
    result = app.post(base+'/1/countersign', json=vendor_payload(submitted), headers=HEADERS)
    assert result.status_code == 200
    with pdfplumber.open(BytesIO(app.get(result.json()['final_pdf_url']).content)) as doc:
        assert 'Vendor signature' in '\n'.join(p.extract_text() or '' for p in doc.pages)


def test_long_answers_are_preserved_in_pdf_record(tmp_path):
    app, _, base, payload = prepared(tmp_path)
    payload['values']['event.notes'] = 'Z' * 5000
    response = app.post(base+'/1/submit', json=payload, headers=HEADERS)
    assert response.status_code == 200
    content = app.get(response.json()['client_pdf_url']).content
    with pdfplumber.open(BytesIO(content)) as doc:
        text = '\n'.join(p.extract_text() or '' for p in doc.pages)
        assert 'See answer record' in text
        assert text.count('Z') == 5000
