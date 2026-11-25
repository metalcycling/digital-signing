import json
from io import BytesIO
from zipfile import ZipFile

import pytest
from bs4 import BeautifulSoup

from digital_signing.rendering import RenderError, render_html, unpack_export, validate_values
from test_contracts import client, HEADERS
from digital_signing.pdf_rendering import demo_pdf


def export(html):
    stream = BytesIO()
    with ZipFile(stream, 'w') as archive:
        archive.writestr('contract.html', html)
    return stream.getvalue()


def test_split_runs_tables_and_repeated_named_fields():
    result = render_html('<p>Name: <b>{{client.</b><i>name|text}}</i>!</p>'
                         '<table><tr><td>{{client.name|text}}</td><td>Date: {{event.date|date|label=Date}}</td></tr></table>'
                         '<p>Venue: {{event.venue}}; Address: {{event.address}}</p>')
    assert len(result['fields']) == 4
    assert result['occurrences'] == 5
    assert result['fields'][1]['label'] == 'Date'
    assert result['fields'][2]['key'] != result['fields'][3]['key']
    soup = BeautifulSoup(result['html'], 'html.parser')
    assert len(soup.select('[data-field-key="client.name"]')) == 2
    assert soup.table is not None
    assert 'Name: ' in soup.get_text() and '!' in soup.get_text()
    assert '{{' not in result['html'] and '{{event.date|date|label=Date}}' not in result['html']


@pytest.mark.parametrize('tag', [
    '{{event.date|bogus}}', '{{Bad Name|text}}', '{{event.type|select}}',
    '{{event.type|select|options=A;A}}', '{{client.name|text|surprise=yes}}',
    '{{client.name|text|optional|required}}', '{{client.name|text', '{{date|datte}}',
])
def test_bad_tags_block_render(tag):
    with pytest.raises(RenderError): render_html(f'<p>{tag}</p>')


def test_conflicting_repeated_fields_block_render():
    with pytest.raises(RenderError, match='conflicting'):
        render_html('<p>{{event.date|date}}</p><p>{{event.date|text}}</p>')


def test_export_sanitization_and_embedded_images():
    html = '<html><head><style>.bold {font-weight:700}</style></head><body><script>alert(1)</script>'
    html += '<p class="bold" onclick="alert(1)" style="background-image:url(https://evil);position:fixed">'
    html += '{{client.name|text}}</p><img src="images/logo.png" onerror="alert(1)">'
    html += '<img src="https://evil/pixel"><a href="javascript:alert(1)">Visible text</a></body></html>'
    stream = BytesIO()
    with ZipFile(stream, 'w') as archive:
        archive.writestr('contract.html', html)
        archive.writestr('images/logo.png', b'\x89PNG\r\n\x1a\nimage')
        archive.writestr('../outside', 'not extracted')
    source, images = unpack_export(stream.getvalue())
    result = render_html(source, images)
    soup = BeautifulSoup(result['html'], 'html.parser')
    assert soup.p['style'] == 'font-weight:700'
    assert soup.img['src'].startswith('data:image/png;base64,')
    assert not soup.find(['script', 'a', 'style'])
    assert not soup.select('[onclick],[onerror]')
    assert 'https://evil' not in result['html']
    assert '[Image unavailable]' in result['html']


def test_values_validate_types_options_and_roles():
    tags = [f'{{{{client.{kind}|{kind}}}}}' for kind in ['email','phone','date','time','number','checkbox']]
    tags += ['{{event.type|select|options=Wedding;Party}}', '{{vendor.signature|signature}}', '{{notes|text|optional}}']
    fields = render_html('<p>' + ' '.join(tags) + '</p>')['fields']
    valid = {'client.email':'a@b.com', 'client.phone':'+1 (954) 555-0100', 'client.date':'2027-02-28',
             'client.time':'14:30', 'client.number':'12.5', 'client.checkbox':True, 'event.type':'Wedding'}
    assert validate_values(fields, valid) == {}
    bad = {**valid, 'client.date':'2027-02-30', 'client.number':'NaN', 'event.type':'Unknown',
           'client.checkbox':False, 'vendor.signature':'not allowed'}
    assert set(validate_values(fields, bad)) == {'client.date','client.number','event.type','client.checkbox','vendor.signature'}


def test_render_versions_are_immutable_and_export_only_uses_contract_copy(tmp_path):
    calls = []
    class Drive:
        html = '<p>Hello {{client.name|text}}</p>'
        def __init__(self, settings): pass
        def templates(self): return [{'id':'template'}]
        def copy(self, *args): return {'id':'copy', 'webViewLink':'https://docs.google.com/document/d/copy/edit'}
        def export_contract(self, doc):
            calls.append(doc)
            return {'html': export(self.html), 'pdf': demo_pdf(self.html)}, {'modifiedTime':'2026-11-17T12:00:00Z', 'version':'42'}
    app = client(tmp_path, 'google', Drive)
    contract = app.post('/api/contracts', json={'name':'Test render', 'template_id':'template'}, headers=HEADERS).json()
    base = f'/api/contracts/{contract["id"]}/renders'
    assert app.post(base, headers=HEADERS).status_code == 201
    old = app.get(base + '/1').json()
    assert old['source_version'] == '42'
    Drive.html = '<p>Changed {{client.email|email}}</p>'
    assert app.post(base, headers=HEADERS).json()['version'] == 2
    assert app.get(base + '/1').json() == old
    assert app.get(base + '/2').json()['fields'][0]['key'] == 'client.email'
    assert len(app.get(base).json()) == 2
    assert calls == ['copy', 'copy']
    assert app.get(f'/preview/{contract["id"]}').status_code == 200
    validation = app.post(base + '/1/validate', json={'client.name':'Jane'}, headers=HEADERS).json()
    assert validation['valid']
    assert app.get(base + '/1').json() == old
    Drive.html = '<p>{{broken|unknown}}</p>'
    assert app.post(base, headers=HEADERS).status_code == 422
    assert len(app.get(base).json()) == 2
    assert app.get('/api/contracts/missing/renders').status_code == 404


def test_demo_renders_without_google_and_reports_no_fields(tmp_path):
    app = client(tmp_path)
    contract = app.post('/api/contracts', json={'name':'Demo render', 'template_id':'demo-harp'}, headers=HEADERS).json()
    result = app.post(f'/api/contracts/{contract["id"]}/renders', headers=HEADERS)
    assert result.status_code == 201
    assert result.json()['field_count'] > 5
    assert render_html('<p>Plain document</p>')['fields'] == []


def test_other_event_requires_description_and_does_not_relax_unrelated_selects():
    from digital_signing.rendering import field_definition, validate_values
    event = field_definition('{{client.type_of_event|select|options=Wedding;Corporate}}')
    assert not validate_values([event],{'client.type_of_event':'Other: Graduation party'})
    for invalid in ['Other','Other:','Other:   ','Graduation party','Other: '+'x'*151]:
        assert validate_values([event],{'client.type_of_event':invalid})
    other = field_definition('{{client.package|select|options=Basic;Premium}}')
    assert validate_values([other],{'client.package':'Other: Custom'})
    enabled = field_definition('{{client.package|select|options=Basic;Premium|other}}')
    assert not validate_values([enabled],{'client.package':'Other: Custom'})


def test_explicit_dropdown_other_setting_overrides_legacy_inference():
    from digital_signing.rendering import field_definition, allows_other
    closed = field_definition('{{client.event_type|select|label=Type of event|options=Wedding;Corporate|other=false}}')
    assert closed['allow_other'] is False
    assert not allows_other(closed)
    assert not validate_values([closed], {'client.event_type':'Wedding'})
    assert validate_values([closed], {'client.event_type':'Other: Birthday'})
    opened = field_definition('{{client.setting|select|options=Indoors;Outdoors|other=true}}')
    assert allows_other(opened)
    assert not validate_values([opened], {'client.setting':'Other: Covered terrace'})
    assert validate_values([opened], {'client.setting':'Other: '})
    assert validate_values([opened], {'client.setting':'Other'})
    literal = field_definition('{{client.setting|select|options=Indoors;Other|other=false}}')
    assert not allows_other(literal)
    assert not validate_values([literal], {'client.setting':'Other'})
    assert validate_values([literal], {'client.setting':'Other: Terrace'})


@pytest.mark.parametrize('setting',['other=','other=yes','other=false|other','other=true|other=false'])
def test_invalid_dropdown_other_settings_block_render(setting):
    with pytest.raises(RenderError):
        render_html('<p>{{client.setting|select|options=Indoors;Outdoors|'+setting+'}}</p>')


def test_malformed_label_explains_equals_syntax():
    with pytest.raises(RenderError, match='Use label=Your label'):
        render_html('<p>{{client.event_type|select|label|Wedding|options=Wedding;Corporate}}</p>')
    result = render_html('<p>{{client.event_type|select|label=Wedding|options=Wedding;Corporate|other=true}}</p>')
    assert result['fields'][0]['label'] == 'Wedding'


@pytest.mark.parametrize('tag,value', [
    ('{{client.name|text|default=Jane Smith}}','Jane Smith'),
    ('{{vendor.name|textarea|default=Bello Entertainment}}','Bello Entertainment'),
    ('{{client.phone|phone|default=203-942-8143}}','203-942-8143'),
    ('{{client.email|email|default=jane@example.com}}','jane@example.com'),
    ('{{client.rooms|number|default=0}}','0'),
    ('{{client.date|date|default=2027-06-12}}','2027-06-12'),
    ('{{client.time|time|default=14:30}}','14:30'),
    ('{{client.event_type|select|default=Wedding|options=Wedding;Corporate|other=false}}','Wedding'),
    ('{{client.setting|select|options=Indoors;Outdoors|other=true|default=Other: Terrace}}','Other: Terrace'),
    ('{{client.checked|checkbox|default=true}}',True),
    ('{{client.checked|checkbox|default=false}}',False),
])
def test_valid_defaults_are_typed_and_preserved_in_schema(tag,value):
    field = render_html('<p>'+tag+'</p>')['fields'][0]
    assert field['default'] == value
    assert type(field['default']) is type(value)


@pytest.mark.parametrize('tag', [
    '{{client.phone|phone|default=hello}}',
    '{{client.number|number|default=NaN}}',
    '{{vendor.email|email|default=invalid}}',
    '{{client.date|date|default=2027-02-30}}',
    '{{client.time|time|default=14:31}}',
    '{{client.choice|select|options=A;B|other=false|default=Other: C}}',
    '{{client.choice|select|options=A;B|default=C}}',
    '{{client.checked|checkbox|default=yes}}',
    '{{client.signature|signature|default=Jane}}',
    '{{client.initials|initials|default=JS}}',
    '{{client.signed|signing_date|default=2027-06-12}}',
    '{{vendor.singning_date|date|default=2027-06-12}}',
    '{{client.name|text|default=}}',
    '{{client.name|text|default=A|default=B}}',
])
def test_invalid_defaults_block_render(tag):
    with pytest.raises(RenderError):
        render_html('<p>'+tag+'</p>')


def test_default_is_not_a_submission_fallback_or_consent():
    field = render_html('<p>{{client.name|text|default=Jane}}</p>')['fields'][0]
    assert validate_values([field], {})
    assert validate_values([field], {'client.name':''})
    assert not validate_values([field], {'client.name':'Changed name'})
    checkbox = render_html('<p>{{client.checked|checkbox|default=false}}</p>')['fields'][0]
    assert validate_values([checkbox], {'client.checked':checkbox['default']})
    assert not validate_values([checkbox], {'client.checked':True})
    with pytest.raises(RenderError, match='conflicting'):
        render_html('<p>{{client.name|text|default=Jane}}</p><p>{{client.name|text|default=John}}</p>')


@pytest.mark.parametrize('tag', [
    '{{client.price|number|default=1000|locked=true}}',
    '{{client.provided_name|default=Jane|locked}}',
    '{{client.choice|select|options=A;B|default=A|locked=true}}',
    '{{vendor.checked|checkbox|optional|default=false|locked=true}}',
])
def test_locked_values_cannot_be_changed_or_omitted(tag):
    field = dict(render_html('<p>'+tag+'</p>')['fields'][0], role='client')
    assert field['locked']
    assert not validate_values([field], {field['key']:field['default']})
    for value in ['changed', '', True, {'value':field['default']}]:
        assert validate_values([field], {field['key']:value})
    assert validate_values([field], {})


@pytest.mark.parametrize('tag', [
    '{{client.price|number|locked=true}}',
    '{{client.checked|checkbox|default=false|locked=true}}',
    '{{client.signature|signature|locked=true}}',
    '{{client.initials|initials|locked=true}}',
    '{{client.signing_date|signing_date|locked=true}}',
    '{{client.name|default=Jane|locked=maybe}}',
    '{{client.name|default=Jane|locked=true|locked=false}}',
])
def test_invalid_locks_fail_at_render_time(tag):
    with pytest.raises(RenderError):
        render_html('<p>'+tag+'</p>')


@pytest.mark.parametrize('value', ['0', '0.25', '.5', '12', '1e3', '1e-3'])
def test_number_fields_accept_nonnegative_numbers(value):
    field = render_html('<p>{{client.amount|number}}</p>')['fields'][0]
    assert not validate_values([field], {'client.amount':value})


@pytest.mark.parametrize('value', ['-1', '-0.01', '-1e3', 'NaN', 'Infinity', 'abc', '1_000', '$100', '1.2.3'])
def test_number_fields_reject_negative_or_nonnumeric_values_and_defaults(value):
    field = render_html('<p>{{client.amount|number}}</p>')['fields'][0]
    assert validate_values([field], {'client.amount':value})['client.amount'] == 'Enter a number that is zero or greater.'
    with pytest.raises(RenderError):
        render_html('<p>{{client.amount|number|default='+value+'}}</p>')


def test_time_labels_use_event_wording_and_preserve_custom_labels():
    fields = render_html('<p>{{client.start_time|time}}</p><p>{{client.end_time|time}}</p><p>{{event.time|time|label=Client start time}}</p><p>{{vendor.end|time|label=Sound check}}</p>')['fields']
    assert [f['label'] for f in fields] == ['Event starting time', 'Event ending time', 'Event starting time', 'Sound check']
