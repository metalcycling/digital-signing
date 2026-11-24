import base64
from io import BytesIO

import pytest
from PIL import Image, ImageDraw

from digital_signing.signatures import signature_error
from digital_signing.rendering import validate_values
from test_contracts import client, HEADERS


def image_url(blank=False, transparent=False, size=(300, 90)):
    image = Image.new('RGBA', size, (255, 255, 255, 0 if transparent else 255))
    if not blank:
        draw = ImageDraw.Draw(image)
        draw.line([(10, 65), (60, 15), (45, 65), (150, 30), (250, 55)], fill=(20, 50, 35, 255), width=4)
    output = BytesIO()
    image.save(output, format='PNG')
    return 'data:image/png;base64,' + base64.b64encode(output.getvalue()).decode()


def signature(method='drawn'):
    value = {'method': method, 'image': image_url(transparent=True)}
    if method == 'typed': value['typed_name'] = 'Jane Smith'
    return value


@pytest.mark.parametrize('method', ['typed', 'drawn', 'uploaded'])
def test_accepts_each_signature_method(method):
    assert signature_error(signature(method)) is None


@pytest.mark.parametrize('value', [
    '', 'Jane Smith', {}, {'method': []}, {'method': 'forged'},
    {'method': 'typed', 'typed_name': '', 'image': image_url()},
    {'method': 'typed', 'typed_name': 'x' * 101, 'image': image_url()},
    {'method': 'drawn', 'image': 'data:image/svg+xml,<svg></svg>'},
    {'method': 'drawn', 'image': 'data:image/png;base64,not-valid'},
    {'method': 'uploaded', 'image': 'data:image/png;base64,' + 'A' * 2_000_000},
    {'method': 'uploaded', 'image': image_url(), 'unexpected': 'value'},
    {'method': 'drawn', 'typed_name': 'Jane', 'image': image_url()},
])
def test_rejects_invalid_signature_payloads(value):
    assert signature_error(value)


@pytest.mark.parametrize('transparent', [False, True])
def test_rejects_blank_images(transparent):
    assert 'blank' in signature_error({'method':'uploaded', 'image':image_url(blank=True, transparent=transparent)})


def test_rejects_excessive_dimensions():
    assert signature_error({'method':'uploaded', 'image':image_url(size=(1601,90))})


def test_validation_handles_required_optional_and_vendor_signatures():
    fields = [{'key':'client.signature','type':'signature','required':True,'role':'client'},
              {'key':'client.optional','type':'signature','required':False,'role':'client'},
              {'key':'vendor.signature','type':'signature','required':True,'role':'vendor'}]
    assert 'client.signature' in validate_values(fields, {})
    assert validate_values(fields, {'client.signature':signature()}) == {}
    assert 'vendor.signature' in validate_values(fields, {'client.signature':signature(), 'vendor.signature':signature()})


def test_signature_image_can_be_checked_but_is_not_stored(tmp_path):
    app = client(tmp_path)
    contract = app.post('/api/contracts', json={'name':'Signature preview','template_id':'demo-harp'}, headers=HEADERS).json()
    base = f'/api/contracts/{contract["id"]}/renders'
    assert app.post(base, headers=HEADERS).status_code == 201
    snapshot = app.get(base+'/1').json()
    response = app.post(base+'/1/validate', json={'client.signature':signature('typed')}, headers=HEADERS)
    assert response.status_code == 200
    assert 'client.signature' not in response.json()['errors']
    assert app.get(base+'/1').json() == snapshot
    bad = app.post(base+'/1/validate', json={'client.signature':{'method':[]}}, headers=HEADERS)
    assert bad.status_code == 200
    assert 'client.signature' in bad.json()['errors']
    assert app.get('/static/signatures.js').status_code == 200
    font = app.get('/static/fonts/Allura-Regular.ttf')
    assert font.status_code == 200 and font.content[:4] == b'\x00\x01\x00\x00'
    assert 'blob:' in app.get('/').headers['content-security-policy']
