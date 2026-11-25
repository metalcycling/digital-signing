from io import BytesIO
import json

import pdfplumber
import pytest

from digital_signing.calculations import calculate_values
from digital_signing.rendering import RenderError, render_html, validate_values
from test_contracts import HEADERS
from test_delivery import invitation
from test_workflow import prepared, vendor_payload

TAGS = '''<p>{{client.day|text|calc=weekday(client.date)}}</p>
<p>{{client.hours|number|calc=duration_hours(client.start,client.end)}}</p>
<p>{{client.date|date}}</p><p>{{client.start|time}}</p><p>{{client.end|time}}</p>'''


@pytest.mark.parametrize('day,weekday,start,end,hours', [
    ('2026-11-30','Monday','19:00','21:30','2.5'),
    ('2024-02-29','Thursday','23:45','00:00','0.25'),
    ('2026-12-31','Thursday','00:00','23:45','23.75'),
    ('2026-11-01','Sunday','01:00','02:00','1'),
    ('0001-01-01','Monday','00:00:00','12:00:00','12'),
])
def test_calculations_replace_forged_results_and_preserve_source_answers(day, weekday, start, end, hours):
    fields = render_html(TAGS)['fields']
    values = {'client.date':day,'client.start':start,'client.end':end,'client.day':'Forged','client.hours':'999'}
    result, errors = calculate_values(fields, values)
    assert not errors and not validate_values(fields, values)
    assert result['client.day'] == weekday
    assert result['client.hours'] == hours
    assert values['client.hours'] == '999'
    assert all(field['locked'] for field in fields if field.get('calc'))


@pytest.mark.parametrize('values,keys', [
    ({}, {'client.date','client.start','client.end'}),
    ({'client.date':'2027-02-29','client.start':'19:00','client.end':'19:00'}, {'client.date','client.end'}),
    ({'client.date':'0000-01-01','client.start':'24:00','client.end':'14:31'}, {'client.date','client.start','client.end'}),
    ({'client.date':{},'client.start':True,'client.end':'12:00:01'}, {'client.date','client.start','client.end'}),
])
def test_missing_invalid_and_equal_inputs_report_source_fields(values, keys):
    fields = render_html(TAGS)['fields']
    result, errors = calculate_values(fields, values)
    assert set(errors) == keys
    assert result['client.day'] == result['client.hours'] == ''
    assert set(validate_values(fields, values)) == keys


@pytest.mark.parametrize('tag', [
    '{{result|text|calc=eval(client.date)}}',
    '{{result|text|calc=weekday(client.missing)}}',
    '{{result|text|calc=weekday(client.start)}}',
    '{{result|number|calc=duration_hours(client.start,client.start)}}',
    '{{result|number|calc=duration_hours(client.start)}}',
    '{{result|number|calc=duration_hours(client.start,client.end,client.date)}}',
    '{{result|text|calc=weekday(result)}}',
    '{{result|number|calc=weekday(client.date)}}',
    '{{result|text|calc=weekday(client.date)|default=Monday}}',
    '{{result|text|calc=weekday(client.date)|locked=false}}',
    '{{vendor.result|text|calc=weekday(client.date)}}',
    '{{result|text|calc=weekday(client.signing_date)}}',
    '{{result|text|calc=weekday(client.date)|calc=weekday(client.date)}}',
])
def test_bad_calculation_definitions_block_render(tag):
    with pytest.raises(RenderError):
        render_html(TAGS + '<p>{{client.signing_date|signing_date}}</p><p>'+tag+'</p>')


def test_optional_calculation_can_remain_blank_and_required_result_requires_optional_source():
    fields = render_html('<p>{{d|date|optional}}</p><p>{{w|text|optional|calc=weekday(d)}}</p>')['fields']
    assert not validate_values(fields, {})
    fields[1]['required'] = True
    assert 'd' in validate_values(fields, {})


def test_live_submission_corrections_and_final_pdf_use_server_calculations(tmp_path, monkeypatch):
    import digital_signing.app as module
    extra = '<p>{{event.end|time}}</p><p>{{event.weekday|text|calc=weekday(event.date)}}</p><p>{{event.duration|number|calc=duration_hours(event.start,event.end)}} hours</p>'
    monkeypatch.setattr(module, 'DEMO_HTML', module.DEMO_HTML + extra)
    app, contract, base, payload = prepared(tmp_path)
    payload['values'].update({'event.date':'2026-11-30','event.start':'23:00','event.end':'01:30','event.weekday':'Wrong','event.duration':'999'})
    _, private = invitation(app, base)
    assert app.post(private+'/1/validate',json=payload['values'],headers=HEADERS).json()['valid']
    submitted = app.post(private+'/1/submit',json=payload,headers=HEADERS)
    assert submitted.status_code == 200, submitted.text
    saved = submitted.json()['client']['values']
    assert saved['event.weekday'] == 'Monday' and saved['event.duration'] == '2.5'
    with pdfplumber.open(BytesIO(app.get(submitted.json()['client_pdf_url']).content)) as pdf:
        text = '\n'.join(page.extract_text() or '' for page in pdf.pages)
        assert 'Monday' in text and '2.5' in text
        assert 'End time is on the following day.' in text
        assert '999' not in text and 'Wrong' not in text
    # Changed forged results do not affect retry identity.
    payload['values'].update({'event.weekday':'Other forgery','event.duration':'1000'})
    assert app.post(private+'/1/submit',json=payload,headers=HEADERS).status_code == 200
    revision = app.post(base+'/1/request-corrections',json={'note':'Please update the date and ending time.'},headers=HEADERS).json()
    corrected = '/api/sign/'+revision['email']['link'].split('/')[-1]+'/2'
    prefill = app.get(corrected).json()['prefill_values']
    assert 'event.weekday' not in prefill and 'event.duration' not in prefill
    assert prefill['event.start'] == '23:00'
    payload['values'].update({'event.date':'2026-12-01','event.end':'02:00'})
    second = app.post(corrected+'/submit',json=payload,headers=HEADERS)
    assert second.status_code == 200, second.text
    assert second.json()['client']['values']['event.weekday'] == 'Tuesday'
    assert second.json()['client']['values']['event.duration'] == '3'
    signed = app.post(base+'/2/countersign',json=vendor_payload(second.json()),headers=HEADERS)
    assert signed.status_code == 200, signed.text
    with pdfplumber.open(BytesIO(app.get(signed.json()['final_pdf_url']).content)) as pdf:
        text = '\n'.join(page.extract_text() or '' for page in pdf.pages)
        assert 'Tuesday' in text and '1000' not in text and 'Other forgery' not in text
    assert app.get(base+'/1/workflow').json()['client']['values'] == saved


def test_invalid_calculation_cannot_be_submitted_even_if_forged_result_is_valid(tmp_path, monkeypatch):
    import digital_signing.app as module
    monkeypatch.setattr(module, 'DEMO_HTML', module.DEMO_HTML + '<p>{{event.end|time}}</p><p>{{event.hours|number|calc=duration_hours(event.start,event.end)}}</p>')
    app, _, base, payload = prepared(tmp_path)
    payload['values'].update({'event.start':'12:00','event.end':'12:00','event.hours':'2'})
    result = app.post(base+'/1/submit',json=payload,headers=HEADERS)
    assert result.status_code == 422
    assert 'event.end' in result.json()['detail']['errors']
    assert app.get(base+'/1/workflow').json()['state'] == 'editable'


def test_vendor_calculations_use_only_vendor_sources_and_are_saved(tmp_path, monkeypatch):
    import digital_signing.app as module
    monkeypatch.setattr(module, 'DEMO_HTML', module.DEMO_HTML + '<p>{{vendor.date|date}}</p><p>{{vendor.day|text|calc=weekday(vendor.date)}}</p>')
    app, _, base, payload = prepared(tmp_path)
    submitted = app.post(base+'/1/submit',json=payload,headers=HEADERS).json()
    vendor = vendor_payload(submitted)
    vendor['values'].update({'vendor.date':'2026-11-30','vendor.day':'Wrong'})
    signed = app.post(base+'/1/countersign',json=vendor,headers=HEADERS)
    assert signed.status_code == 200, signed.text
    assert signed.json()['vendor']['values']['vendor.day'] == 'Monday'
